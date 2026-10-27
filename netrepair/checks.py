"""Reading the live network: link loss, protocol state, and config drift against the intent.

FRR's JSON output differs a little between versions, so the protocol parsers
look for the fields they need instead of assuming one exact layout.
"""

from __future__ import annotations

import ipaddress
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .device import CommandFailed, Device, Transport
from .intent import Intent

DEFAULTS = {"isis_metric": 10, "ospf_hello": 10, "ospf_dead": 40}


@dataclass
class Observation:
    t: float
    link_loss: dict[str, float] = field(default_factory=dict)  # core link -> probe loss 0..1
    link_up: dict[str, bool] = field(default_factory=dict)  # every link -> both ends operationally up
    bgp: dict[str, dict[str, str]] = field(default_factory=dict)  # router -> peer -> state
    ospf_full: dict[str, int] = field(default_factory=dict)  # router -> neighbors in Full
    drift: dict[str, dict[str, dict[str, tuple[Any, Any]]]] = field(default_factory=dict)
    # router -> iface -> param -> (intended, actual)
    errors: list[str] = field(default_factory=list)


# --- parsers -----------------------------------------------------------------------------

def bgp_peer_states(summary: dict[str, Any]) -> dict[str, str]:
    """`show bgp summary json` -> {peer address: state}."""
    af = summary.get("ipv4Unicast", summary)
    peers = af.get("peers", {}) if isinstance(af, dict) else {}
    return {addr: str(p.get("state", "unknown")) for addr, p in peers.items()}


def ospf_full_count(neighbors: dict[str, Any]) -> int:
    """`show ip ospf neighbor json` -> number of neighbors in Full state (any FRR layout)."""
    count = 0
    for entries in neighbors.get("neighbors", {}).values():
        for n in entries if isinstance(entries, list) else [entries]:
            state = " ".join(str(n.get(k, "")) for k in ("nbrState", "converged", "state"))
            count += "Full" in state
    return count


def interface_oper_up(show_interface: dict[str, Any], iface: str) -> bool:
    """`show interface IFACE json` -> is it operationally up?"""
    data = show_interface.get(iface, show_interface)
    status = str(data.get("operationalStatus", data.get("operStatus", ""))).lower()
    return status == "up"


def running_params(running_config: str) -> dict[str, dict[str, Any]]:
    """Interface parameters from `show running-config`, with FRR's defaults filled in."""
    out: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for raw in running_config.splitlines():
        line = raw.strip()
        if raw.startswith("interface "):
            current = out.setdefault(raw.split()[1], dict(DEFAULTS))
            continue
        if current is None:
            continue
        if line in ("exit", "!") or not raw.startswith(" "):
            current = None
            continue
        if m := re.fullmatch(r"isis metric (\d+)", line):
            current["isis_metric"] = int(m.group(1))
        elif m := re.fullmatch(r"ip ospf hello-interval (\d+)", line):
            current["ospf_hello"] = int(m.group(1))
        elif m := re.fullmatch(r"ip ospf dead-interval (\d+)", line):
            current["ospf_dead"] = int(m.group(1))
        elif m := re.fullmatch(r"ip ospf area (\S+)", line):
            current["ospf_area"] = str(ipaddress.ip_address(int(m.group(1)) if m.group(1).isdigit() else m.group(1)))
    return out


def intended_params(intent: Intent, router: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    t = intent.timers
    for lk in intent.links_of(router):
        iface = lk.end(router).iface
        if lk.proto == "isis":
            out[iface] = {"isis_metric": lk.metric}
        elif lk.proto == "ospf":
            out[iface] = {"ospf_hello": t["ospf_hello"], "ospf_dead": t["ospf_dead"],
                          "ospf_area": str(ipaddress.ip_address(lk.ospf_area))}
    return out


def drift(intent: Intent, router: str, running: str, ignore: set[str] = frozenset()) -> dict[str, dict[str, tuple]]:
    """{iface: {param: (intended, actual)}} for every parameter that differs."""
    actual = running_params(running)
    diffs: dict[str, dict[str, tuple]] = {}
    for iface, want in intended_params(intent, router).items():
        if iface in ignore:
            continue
        have = actual.get(iface, dict(DEFAULTS))
        d = {k: (v, have.get(k)) for k, v in want.items() if have.get(k) != v}
        if d:
            diffs[iface] = d
    return diffs


# --- collection --------------------------------------------------------------------------

def observe(intent: Intent, transport: Transport, t: float, drained_ifaces: dict[str, set[str]] | None = None,
            probes: int = 20) -> Observation:
    devs = {name: Device(name, transport) for name in intent.routers}
    obs = Observation(t)
    drained_ifaces = drained_ifaces or {}

    def link_loss(lk):  # noqa: ANN001, ANN202
        return lk.name, devs[lk.a.node].ping(lk.b.address, count=probes)

    def router_state(name):  # noqa: ANN001, ANN202
        d = devs[name]
        res: dict[str, Any] = {"name": name}
        try:
            res["running"] = d.vtysh("show running-config")
            if intent.routers[name].asn is not None:
                res["bgp"] = bgp_peer_states(d.show("show bgp summary"))
            if name in intent.ospf_routers():
                res["ospf"] = ospf_full_count(d.show("show ip ospf neighbor"))
            res["ifaces"] = {lk.end(name).iface: interface_oper_up(d.show(f"show interface {lk.end(name).iface}"),
                                                                   lk.end(name).iface)
                             for lk in intent.links_of(name)}
        except (CommandFailed, ValueError) as exc:
            res["error"] = str(exc)
        return res

    with ThreadPoolExecutor(max_workers=8) as pool:
        losses = list(pool.map(link_loss, intent.core_links()))
        states = list(pool.map(router_state, intent.routers))
    obs.link_loss = dict(losses)
    up: dict[str, bool] = {}
    for s in states:
        if "error" in s:
            obs.errors.append(f"{s['name']}: {s['error']}")
            continue
        if "bgp" in s:
            obs.bgp[s["name"]] = s["bgp"]
        if "ospf" in s:
            obs.ospf_full[s["name"]] = s["ospf"]
        d = drift(intent, s["name"], s["running"], drained_ifaces.get(s["name"], set()))
        if d:
            obs.drift[s["name"]] = d
        for iface, ok in s["ifaces"].items():
            up[f"{s['name']}:{iface}"] = ok
    for lk in intent.links:
        obs.link_up[lk.name] = up.get(str(lk.a), False) and up.get(str(lk.b), False)
    return obs
