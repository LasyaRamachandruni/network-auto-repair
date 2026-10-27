"""Break the network on purpose, let the controller respond, and measure.

    python -m netrepair experiment --runs 3      # writes results/experiment.jsonl

For each scenario and run:
  1. wait until the network is healthy (end-to-end ping clean, BGP up, no drift);
  2. start a probe on h1: 20 pings per second to h2 (site A to site B) with timestamps;
  3. inject the fault and run the controller until it has dealt with it;
  4. measure from the controller's log and the probe:
       detect   = fault -> the controller first saw the problem
       act      = fault -> the controller's repair was applied (or its ticket filed)
       recovered = fault -> the last lost probe packet (traffic fully back after that)
       lost     = probe packets lost after the fault
  5. undo the fault and put drained links back.

Scenarios:
  gray_core_link   p1-p3 keeps running but drops 20% of packets (tc netem). IS-IS
                   keeps the adjacency, so only the controller can fix it: drain.
  core_link_down   p1-p3 goes down. IS-IS reroutes by itself; the controller files a ticket.
  primary_uplink_down   site A's primary uplink (p1-ce1) goes down. BGP fails over to
                   the backup (p2) by itself; the controller files a ticket.
  ospf_drift       someone sets a wrong OSPF hello interval on a1's uplink. The
                   adjacency to ce1 would drop and cut site A off; the controller
                   re-applies the intended setting.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .checks import observe
from .controller import Controller
from .device import CommandFailed, Device, DockerExec, Transport
from .intent import Intent, load

PROBE_INTERVAL = 0.05
RESULTS = Path(__file__).resolve().parent.parent / "results"


@dataclass
class Scenario:
    name: str
    inject: Callable[[Transport], None]
    undo: Callable[[Transport], None]
    done: Callable[[Controller], bool]  # the controller has dealt with it
    incident: str  # key prefix in Controller.first_seen


def _netem(t: Transport, ends: list[tuple[str, str]], loss: int) -> None:
    for node, iface in ends:
        t.net(node, f"tc qdisc replace dev {iface} root netem loss {loss}%")


def _clear_netem(t: Transport, ends: list[tuple[str, str]]) -> None:
    for node, iface in ends:
        try:
            t.net(node, f"tc qdisc del dev {iface} root")
        except CommandFailed:
            pass


def _link(t: Transport, node: str, iface: str, state: str) -> None:
    t.net(node, f"ip link set {iface} {state}")


def scenarios(intent: Intent) -> list[Scenario]:
    core = intent.link("p1-p3")
    ends = [(core.a.node, core.a.iface), (core.b.node, core.b.iface)]
    up = intent.link("p1-ce1")
    a1_up = next(lk for lk in intent.links_of("a1") if lk.proto == "ospf")
    a1_if = a1_up.end("a1").iface

    def has(kind: str, target: str) -> Callable[[Controller], bool]:
        return lambda c: any(a.kind == kind and a.target == target for a in c.actions)

    return [
        Scenario("gray_core_link", lambda t: _netem(t, ends, 20), lambda t: _clear_netem(t, ends),
                 has("drain", core.name), f"gray:{core.name}"),
        Scenario("core_link_down", lambda t: _link(t, core.a.node, core.a.iface, "down"),
                 lambda t: _link(t, core.a.node, core.a.iface, "up"), has("ticket", core.name), f"down:{core.name}"),
        Scenario("primary_uplink_down", lambda t: _link(t, up.b.node, up.b.iface, "down"),
                 lambda t: _link(t, up.b.node, up.b.iface, "up"), has("ticket", up.name), f"down:{up.name}"),
        Scenario("ospf_drift",
                 lambda t: Device("a1", t).configure(f"interface {a1_if}", "ip ospf hello-interval 5"),
                 lambda t: None, has("restore", f"a1:{a1_if}"), f"drift:a1:{a1_if}"),
    ]


# --- end-to-end probe ----------------------------------------------------------------------

def start_probe(t: Transport, target: str = "192.168.2.10") -> None:
    t.sh("h1", "pkill ping; rm -f /tmp/probe.log; true")
    t.sh("h1", f"nohup ping -n -D -i {PROBE_INTERVAL} -W 1 {target} > /tmp/probe.log 2>&1 &")


def stop_probe(t: Transport) -> str:
    t.sh("h1", "pkill -INT ping; sleep 0.5; true")
    return t.sh("h1", "cat /tmp/probe.log")


def analyse_probe(log: str, fault_at: float) -> dict[str, float | int | None]:
    """Lost packets after the fault and when the last one was sent, from `ping -D` output.

    Uses ping's own "N packets transmitted" summary for the last sequence number,
    so losses at the very end (traffic never came back) are counted too.
    """
    received: dict[int, float] = {}
    for m in re.finditer(r"\[(\d+\.\d+)\] .*?icmp_seq=(\d+)", log):
        received[int(m.group(2))] = float(m.group(1))
    if not received:
        return {"sent": 0, "lost": None, "recovered_s": None}
    first_seq = min(received)
    summary = re.search(r"(\d+) packets transmitted", log)
    last_seq = int(summary.group(1)) if summary else max(received)
    t0 = received[first_seq] - (first_seq - 1) * PROBE_INTERVAL  # when seq 1 was sent (approximately)
    send_time = lambda seq: t0 + (seq - 1) * PROBE_INTERVAL  # noqa: E731
    lost = [s for s in range(first_seq, last_seq + 1) if s not in received and send_time(s) >= fault_at]
    recovered = not lost or max(received) > max(lost)
    sent_after = sum(1 for s in range(first_seq, last_seq + 1) if send_time(s) >= fault_at)
    return {"sent": sent_after, "lost": len(lost),
            "recovered_s": (round(send_time(max(lost)) + PROBE_INTERVAL - fault_at, 2) if lost else 0.0)
            if recovered else None}  # None: traffic had not come back when the probe stopped


def path(t: Transport, intent: Intent) -> dict[str, str]:
    """Where site-to-site traffic goes right now: traceroute from h1, and each core
    router's kernel forwarding decision (`ip route get`) in both directions."""
    out: dict[str, str] = {}
    try:  # netshoot has traceroute; run it inside the h1 container
        tr = t.sh("h1", "traceroute -n -q 1 -w 1 -m 8 192.168.2.10", timeout=20)
        out["traceroute"] = " ".join(re.findall(r"^\s*\d+\s+(\S+)", tr, re.M))
    except CommandFailed as exc:
        out["traceroute"] = f"failed: {exc}"[:200]
    for node in sorted(intent.isis_routers()):
        for dest in ("192.168.2.10", "192.168.1.10", *(intent.routers[r].loopback for r in sorted(intent.isis_routers())
                                                         if r != node)):
            try:
                out[f"{node}->{dest}"] = " ".join(t.net(node, f"ip route get {dest}").split()[:5])
            except CommandFailed as exc:
                out[f"{node}->{dest}"] = f"failed: {exc}"[:160]
    return out


def normal_path(t: Transport) -> bool:
    """Both directions back on the direct p1-p3 link (so a run doesn't start mid-reconvergence)."""
    try:
        fwd = t.net("p1", "ip route get 192.168.2.10")
        back = t.net("p3", "ip route get 192.168.1.10")
    except CommandFailed:
        return False
    return "10.0.13.1" in fwd and "10.0.13.0" in back


def wait_healthy(intent: Intent, t: Transport, timeout: float = 120) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        obs = observe(intent, t, time.time())
        bgp_down = [f"{r}->{p}" for r, peers in obs.bgp.items() for p, s in peers.items() if s != "Established"]
        e2e = Device("h1", t).ping("192.168.2.10", count=10)
        lossy = [k for k, v in obs.link_loss.items() if v > 0]
        problems = bgp_down + [f"drift {k}" for k in obs.drift] + [f"loss {k}" for k in lossy] + obs.errors
        if e2e == 0 and not problems and normal_path(t):
            return
        last = f"e2e loss {e2e:.0%}; " + "; ".join(problems)
        time.sleep(2)
    raise TimeoutError(f"network not healthy after {timeout:.0f}s: {last}")


def run_once(intent: Intent, t: Transport, sc: Scenario, log_path: Path) -> dict:
    wait_healthy(intent, t)
    ctl = Controller(intent, t, log_path=log_path)
    start_probe(t)
    time.sleep(2)
    before = path(t, intent)
    fault_at = time.time()
    sc.inject(t)
    # Snapshot the path 1.5 s in (protocols have reacted, repair usually not yet),
    # on a side thread so it doesn't delay the controller.
    during: dict[str, str] = {}
    snap = threading.Thread(target=lambda: (time.sleep(1.5), during.update(path(t, intent))))
    snap.start()
    ctl.run(seconds=60, until=sc.done)
    snap.join()
    after = path(t, intent)
    time.sleep(5)  # let traffic settle so the probe shows full recovery
    probe = analyse_probe(stop_probe(t), fault_at)
    acted = next((a for a in ctl.actions if sc.done(_Single(a))), None)
    row = {
        "scenario": sc.name,
        "detect_s": round(ctl.first_seen[sc.incident] - fault_at, 2) if sc.incident in ctl.first_seen else None,
        "act_s": round(acted.detail["completed_at"] - fault_at, 2) if acted else None,
        "action": f"{acted.kind}: {acted.reason}" if acted else None,
        **probe,
        "path_before": before,
        "path_during": during,
        "path_after_action": after,
    }
    sc.undo(t)
    for link in list(ctl.drained):
        ctl.undrain(link)
    return row


class _Single:
    """Lets a scenario's done() check one action."""

    def __init__(self, action):  # noqa: ANN001
        self.actions = [action]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only", action="append", help="scenario name (repeatable)")
    args = ap.parse_args(argv)
    intent = load()
    t = DockerExec()
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / "experiment.jsonl"
    for sc in scenarios(intent):
        if args.only and sc.name not in args.only:
            continue
        for i in range(args.runs):
            row = {"run": i + 1, **run_once(intent, t, sc, RESULTS / "actions.jsonl")}
            print(json.dumps(row), flush=True)
            with out.open("a") as f:
                f.write(json.dumps(row) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
