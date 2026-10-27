"""The repair controller: observe the network, decide, act, and log every decision.

Routing protocols already fix the failures they can see: a link that goes down
drops its IS-IS adjacency and traffic reroutes in about a second. The controller
handles what they can't see, and checks the rest:

| Incident | Who repairs it | Controller action |
|---|---|---|
| gray failure: a core link is up but drops packets | nobody, without automation | drain it: raise its IS-IS metric on both ends, so traffic moves to other links |
| config drift: an interface differs from the intent | nobody, without automation | re-apply the intended interface settings |
| core link down | IS-IS reroutes | confirm another path exists, open a ticket |
| BGP session down | BGP fails over to the backup uplink | confirm the site still has a session, open a ticket |

Guardrails before any drain: at most `max_drained_links` drained at once, the
rest of the core must stay connected without the link, and a link isn't touched
again within `cooldown_seconds`. If a guardrail blocks the repair, the controller
escalates to a person instead of acting. Dry-run mode logs decisions without
changing anything.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .checks import Observation, observe
from .device import Device, Transport
from .intent import Intent, Link
from .render import interface_block


@dataclass
class Action:
    kind: str  # drain | undrain | restore | ticket | escalate
    target: str  # link name or router:iface
    reason: str
    t: float = 0.0
    done: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


def connected_without(intent: Intent, removed: set[str]) -> bool:
    """Are all core routers still connected if the `removed` core links are gone?"""
    nodes = sorted(intent.isis_routers())
    edges = [lk for lk in intent.core_links() if lk.name not in removed]
    seen, todo = {nodes[0]}, [nodes[0]]
    while todo:
        n = todo.pop()
        for lk in edges:
            if n in (lk.a.node, lk.b.node):
                m = lk.other(n).node
                if m not in seen:
                    seen.add(m)
                    todo.append(m)
    return seen == set(nodes)


class Controller:
    def __init__(self, intent: Intent, transport: Transport, log_path: str | Path | None = None,
                 dry_run: bool = False, clock: Callable[[], float] = time.time,
                 observe_fn: Callable[..., Observation] = observe):
        self.intent = intent
        self.t = transport
        self.dry_run = dry_run
        self.clock = clock
        self.observe_fn = observe_fn
        self.log_path = Path(log_path) if log_path else None
        self.policy = intent.policy
        self.drained: dict[str, float] = {}  # link -> when it was drained
        self.strikes: dict[str, int] = {}  # link -> consecutive lossy rounds
        self.last_touched: dict[str, float] = {}
        self.open_tickets: set[str] = set()  # incidents already reported, so we don't repeat them
        self.actions: list[Action] = []
        self.first_seen: dict[str, float] = {}  # incident key -> first observation time

    # --- the loop --------------------------------------------------------------------------

    def step(self) -> tuple[Observation, list[Action]]:
        drained_ifaces: dict[str, set[str]] = {}
        for name in self.drained:
            lk = self.intent.link(name)
            for e in (lk.a, lk.b):
                drained_ifaces.setdefault(e.node, set()).add(e.iface)
        obs = self.observe_fn(self.intent, self.t, self.clock(), drained_ifaces)
        actions = self.decide(obs)
        for a in actions:
            self.act(a)
        return obs, actions

    def decide(self, obs: Observation) -> list[Action]:
        now = obs.t
        acts: list[Action] = []
        p = self.policy
        down_links = {name for name, up in obs.link_up.items() if not up}

        # 1. Gray failures on core links: up, but losing probes.
        for lk in self.intent.core_links():
            loss = obs.link_loss.get(lk.name, 0.0)
            if lk.name in down_links or lk.name in self.drained:
                self.strikes.pop(lk.name, None)
                continue
            if loss > p["gray_loss_threshold"]:
                self.strikes[lk.name] = self.strikes.get(lk.name, 0) + 1
                self.first_seen.setdefault(f"gray:{lk.name}", now)
            else:
                self.strikes.pop(lk.name, None)
                continue
            if self.strikes[lk.name] < p["gray_consecutive"]:
                continue
            blocked = self._drain_blocked(lk, down_links, now)
            if blocked:
                acts += self._once(Action("escalate", lk.name, f"{loss:.0%} loss but not draining: {blocked}", now))
            else:
                acts.append(Action("drain", lk.name, f"{loss:.0%} probe loss for {self.strikes[lk.name]} rounds "
                                                     "while the link is up", now, detail={"loss": loss}))

        # 2. Config drift: re-apply the intended interface settings.
        for router, ifaces in obs.drift.items():
            for iface, diffs in ifaces.items():
                key = f"{router}:{iface}"
                self.first_seen.setdefault(f"drift:{key}", now)
                if self._cooling(key, now):
                    continue
                desc = ", ".join(f"{k} {have} (intended {want})" for k, (want, have) in diffs.items())
                acts.append(Action("restore", key, f"config drift: {desc}", now))

        # 3. Hard failures the protocols handle: confirm, then ticket.
        for name in down_links:
            self.first_seen.setdefault(f"down:{name}", now)
            lk = self.intent.link(name)
            if lk.proto == "isis":
                ok = connected_without(self.intent, down_links | set(self.drained))
                acts += self._once(Action("ticket", name, "core link down; IS-IS rerouted" if ok
                                          else "core link down and the core is partitioned", now))
            elif lk.proto == "ebgp":
                ce = lk.b.node if lk.a.node in self.intent.isis_routers() else lk.a.node
                sessions = obs.bgp.get(ce, {})
                up = [peer for peer, state in sessions.items() if state == "Established"]
                acts += self._once(Action("ticket", name, f"uplink down; {ce} failed over, "
                                          f"{len(up)} session(s) still up" if up else f"{ce} has no BGP session", now))
            else:
                acts += self._once(Action("ticket", name, "link down", now))
        for key in list(self.open_tickets):  # clear tickets for links that recovered
            if key.startswith("ticket:") and key.split(":", 1)[1] not in down_links:
                self.open_tickets.discard(key)
        return acts

    def _drain_blocked(self, lk: Link, down_links: set[str], now: float) -> str | None:
        if len(self.drained) >= self.policy["max_drained_links"]:
            return f"already {len(self.drained)} drained link(s) (limit {self.policy['max_drained_links']})"
        if not connected_without(self.intent, {lk.name} | set(self.drained) | down_links):
            return "draining it would partition the core"
        if self._cooling(lk.name, now):
            return f"touched less than {self.policy['cooldown_seconds']} s ago"
        return None

    def _cooling(self, key: str, now: float) -> bool:
        return now - self.last_touched.get(key, -1e9) < self.policy["cooldown_seconds"]

    def _once(self, a: Action) -> list[Action]:
        key = f"{a.kind}:{a.target}"
        if key in self.open_tickets:
            return []
        self.open_tickets.add(key)
        return [a]

    # --- acting --------------------------------------------------------------------------------

    def act(self, a: Action) -> None:
        if not self.dry_run:
            if a.kind == "drain":
                self._set_link_metric(self.intent.link(a.target), int(self.policy["drain_metric"]))
                self.drained[a.target] = a.t
                self.strikes.pop(a.target, None)
            elif a.kind == "undrain":
                lk = self.intent.link(a.target)
                self._set_link_metric(lk, lk.metric)
                self.drained.pop(a.target, None)
            elif a.kind == "restore":
                router, iface = a.target.split(":")
                lk = next(x for x in self.intent.links_of(router) if x.end(router).iface == iface)
                block = interface_block(self.intent, self.intent.routers[router], lk)
                Device(router, self.t).configure(*[ln.strip() for ln in block if ln.strip() not in ("exit", "!")])
            a.done = a.kind in ("drain", "undrain", "restore")
        self.last_touched[a.target] = a.t
        a.detail["completed_at"] = self.clock()
        self.actions.append(a)
        self._log(a)

    def undrain(self, link: str) -> Action:
        """An operator puts a repaired link back into service."""
        a = Action("undrain", link, "operator: link repaired", self.clock())
        self.act(a)
        return a

    def _set_link_metric(self, lk: Link, metric: int) -> None:
        for e in (lk.a, lk.b):  # both directions, or traffic would still use it one way
            Device(e.node, self.t).configure(f"interface {e.iface}", f"isis metric {metric}")

    def _log(self, a: Action) -> None:
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as f:
                f.write(json.dumps({**asdict(a), "dry_run": self.dry_run}) + "\n")

    def run(self, seconds: float, until: Callable[["Controller"], bool] | None = None, every: float = 0.0) -> None:
        end = self.clock() + seconds
        while self.clock() < end:
            self.step()
            if until and until(self):
                return
            if every:
                time.sleep(every)
