# RouteMedic

An intent-driven repair controller for a routed network. It runs against a lab of real [FRRouting](https://frrouting.org) routers in containerlab, and does two jobs:
- it fixes the failures routing protocols can't see;
- it checks the failures they handle themselves.

Every claim below comes from a fault-injection run that the lab workflow repeats on every change.

```
            ┌──── p1 ────┐                    provider core, AS 65000:
    ce1 ────┤     │      ├──── p3 ──── ce2      IS-IS level 2 + iBGP full mesh
  (AS 65001)└──── p2 ────┘          (AS 65002)
     │                                │
     a1   OSPF: area 0 to ce1,        h2
     │          area 1 on the LAN
     h1
```

| Part | Setup |
|---|---|
| **Provider core** | p1, p2 and p3 run IS-IS (level 2, wide metrics, point-to-point, 1 s hellos) and an iBGP full mesh between loopbacks, with next-hop-self. |
| **Site A: dual-homed** | ce1 has eBGP to p1 (primary) and p2 (backup). Inbound, the primary gets local-pref 200 and the backup 100. Outbound, the path to the backup gets the AS prepended twice, so return traffic prefers the primary too. Prefix lists control what is accepted and announced. Behind ce1, a1 is the area 0/1 ABR (multi-area OSPF). ce1 redistributes the provider routes into OSPF through a route-map. |
| **Site B: single-homed** | ce2 has eBGP to p3. |
| **Source of truth** | [`lab/intent.yaml`](lab/intent.yaml) holds the intent. `netrepair render` generates every router's `frr.conf` and the containerlab topology from it. |

## What the controller does

Each round, the controller collects the network's state:
- probe loss on every core link;
- interface state;
- BGP and OSPF neighbors;
- every router's running config, compared with the intent.

Then it decides what to do:

| Incident | Who fixes it | Controller |
|---|---|---|
| **Gray failure**: a core link stays up but drops packets | nobody: IS-IS hellos still get through, so the adjacency stays up | **drains** the link by raising its IS-IS metric on both ends, so traffic moves to the other path |
| **Config drift**: an interface no longer matches the intent | nobody | **re-applies** the intended interface settings |
| Core link down | IS-IS reroutes | confirms the core is still connected and files a ticket |
| BGP uplink down | BGP fails over to the backup | confirms the site still has a session and files a ticket |

**Guardrails:**
- it never has more than 1 core link drained at a time;
- it never drains a link if that would partition the core;
- it waits 30 s before touching the same link or interface again;
- it escalates to a person when a guardrail blocks a repair;
- it has a dry-run mode, and every decision goes to a JSON-lines audit log.

The controller talks to routers through a small `Transport` interface: `vtysh` via `docker exec`, and `ping`/`tc` inside each node's network namespace. Against real hardware, that interface would sit on SSH (Netmiko), NETCONF or gNMI.

## Results

GitHub Actions run `40474191173`: 4-core runner, FRR 9.1, 3 runs per fault. A probe on h1 sends 20 pings per second to h2, from site A to site B, and every time is measured from the moment the fault is injected.

| Fault | Detected | Controller | Traffic fully back | Probe packets lost |
|---|---|---|---|---|
| Gray failure: p1–p3 drops 20% of packets each way (tc netem) | 0.02 s | **drained** the link at 4.4 s | **5.2 s** | 28–34 |
| Core link p1–p3 down | 0.01 s | ticket at 2.1 s | **0.16 s** (IS-IS rerouted) | 3 |
| Site A's primary uplink down | 0.01 s | ticket at 2.1 s | **0.21 s** (BGP failed over to p2) | 4 |
| OSPF hello interval changed on a1 (1 s → 5 s) | 0.08 s | **restored** at 2.2 s | **no outage** | 0 |

**Without the controller, a gray failure never ends.** IS-IS keeps the adjacency, so about a third of round trips through the link keep failing.

**The drain takes 4.4 s** because the controller waits for 2 consecutive lossy probe rounds (each round probes all three links for about a second, then reads every router), so one noisy round doesn't move traffic. Traffic is back 0.8 s after the drain.

**The drifted hello interval was fixed before it caused an outage.** It's caught in the config comparison 0.08 s after the change, long before the 4 s OSPF dead interval runs out, so the adjacency never drops.

Raw rows: [`results/`](results/). Reproduce with the lab workflow, or locally with `make lab experiment`.

## Bugs the lab found

1. **Traffic left through the management network.** At first, h1 couldn't reach h2 at all.
   - In containerlab every node already has a default route via its management interface. That route beat the OSPF default that ce1 advertised, so a1 sent site B's traffic out the management port.
   - Fix: ce1 now redistributes the BGP routes it accepts from the provider into OSPF as specific routes.
2. **Routers taking up to 30 s to react.** With FRR's default IS-IS timers, traffic took 8–11 s to move after a drain. After a core link failure, it didn't come back within the run at all: 287 of 287 probe packets were lost.
   - Recording each core router's forwarding decision (`ip route get`) during the fault showed that p1 and p3 had lost their BGP routes to each other, and were sending site traffic out the management interface.
   - FRR rate-limits link-state updates (`lsp-gen-interval`, up to 30 s by default). Setting it and `spf-interval` to 1 s brought recovery after a failure down to 0.16 s, and after a drain to 0.8 s.
3. **Losses at the end of a probe weren't counted.** The probe analysis ignored losses after the last reply, so "never recovered" looked like "no loss". It now uses ping's own transmitted count.
4. **Startup timeouts.** Starting every FRR daemon on every router at once made FRR's watchfrr time out on the 4-core runner. Each router now runs only the daemons it needs, and startups are staggered.

## Run it

You need Linux with Docker and [containerlab](https://containerlab.dev). On a Mac, use a Linux VM, for example OrbStack. Python 3.10+.

```bash
pip install -e ".[dev]"
make test                      # offline tests: rendering, parsers, drift, guardrails, decisions
make lab                       # render intent -> configs, deploy with containerlab
make status                    # one observation: link loss, BGP, OSPF, drift
make watch                     # run the controller (make watch DRY_RUN=1 to only log decisions)
make experiment                # the four faults x 3 runs -> results/experiment.jsonl
make destroy
```

## Layout

```
lab/intent.yaml          the network's intended state: routers, links, timers, controller policy
netrepair/intent.py      loads and checks the intent
netrepair/render.py      intent -> FRR configs (IS-IS, OSPF, BGP, prefix lists, route maps) + topology
netrepair/device.py      Transport (docker exec / network namespace), FRR JSON and config commands
netrepair/checks.py      observations: probe loss, protocol state, running config vs intent
netrepair/controller.py  decisions, guardrails, repairs, audit log
netrepair/experiment.py  fault injection and measurement
tests/                   offline tests with a fake transport
```

## Limits

- **A lab, not a production network:** 6 routers on one machine, connected by virtual ethernet links.
- **Gray failures come from `tc netem`**, not real optics or faulty line cards.
- **Detection is active probing over each core link.** Interface error counters and streaming telemetry would be the next signals to add.
- **Drained links stay drained** until an operator runs `undrain`. Automatic undrain after a healthy period is deliberately not implemented.
