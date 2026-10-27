"""Offline tests: config rendering, parsers, drift detection, guardrails and controller decisions.

The live behaviour (real FRR routers in containerlab) is tested by the lab workflow.
"""

from pathlib import Path

import pytest

from netrepair import checks
from netrepair.checks import Observation
from netrepair.controller import Controller, connected_without
from netrepair.device import Device, FakeTransport, parse_ping_loss
from netrepair.experiment import analyse_probe
from netrepair.intent import load
from netrepair.render import frr_conf, topology

INTENT = load()


def healthy(t=0.0, **over):
    obs = Observation(t)
    obs.link_loss = {lk.name: 0.0 for lk in INTENT.core_links()}
    obs.link_up = {lk.name: True for lk in INTENT.links}
    obs.bgp = {"ce1": {"10.1.1.0": "Established", "10.1.2.0": "Established"}}
    for k, v in over.items():
        getattr(obs, k).update(v)
    return obs


def ctl(**policy):
    c = Controller(INTENT, FakeTransport())
    c.policy = {**INTENT.policy, **policy}
    return c


# --- intent and rendering -------------------------------------------------------------------

def test_intent_links_and_roles():
    assert {lk.name for lk in INTENT.core_links()} == {"p1-p2", "p2-p3", "p1-p3"}
    assert INTENT.link("ce1-p1").role == "primary" and INTENT.link("p2-ce1").role == "backup"
    assert INTENT.ospf_routers() == {"ce1", "a1"} and INTENT.isis_routers() == {"p1", "p2", "p3"}


def test_core_router_config_has_isis_and_ibgp_mesh():
    conf = frr_conf(INTENT, INTENT.routers["p1"])
    assert "router isis core" in conf and "net 49.0001.0000.0000.0001.00" in conf
    assert "metric-style wide" in conf  # needed for drain metrics above 63
    for peer in ("10.255.0.2", "10.255.0.3"):
        assert f"neighbor {peer} remote-as 65000" in conf and f"neighbor {peer} next-hop-self" in conf
    assert "neighbor 10.1.1.1 remote-as 65001" in conf  # eBGP to ce1


def test_dual_homed_site_prefers_primary_both_ways():
    conf = frr_conf(INTENT, INTENT.routers["ce1"])
    assert "neighbor 10.1.1.0 route-map PRIMARY-IN in" in conf  # p1: local-pref 200
    assert "neighbor 10.1.2.0 route-map BACKUP-IN in" in conf  # p2: local-pref 100
    assert "set as-path prepend 65001 65001" in conf  # return traffic avoids the backup
    assert "ip prefix-list OWN seq 10 permit 192.168.1.0/24" in conf
    assert "redistribute bgp route-map BGP-TO-OSPF" in conf  # the other site's routes, into OSPF


def test_a1_is_a_multi_area_abr():
    conf = frr_conf(INTENT, INTENT.routers["a1"])
    assert "ip ospf area 0" in conf and "ip ospf area 1" in conf and "ip ospf passive" in conf
    assert "router bgp" not in conf


def test_topology_wires_every_link_and_host():
    topo = topology(INTENT, Path("/tmp/build"))
    links = topo["topology"]["links"]
    assert len(links) == len(INTENT.links) + len(INTENT.hosts)
    assert {"endpoints": ["a1:eth2", "h1:eth1"]} in links


# --- parsers ----------------------------------------------------------------------------------

def test_parse_ping_loss():
    assert parse_ping_loss("20 packets transmitted, 16 received, 20% packet loss, time 1001ms") == 0.2
    assert parse_ping_loss("garbage") == 1.0


def test_bgp_and_ospf_parsers_accept_frr_layouts():
    summary = {"ipv4Unicast": {"peers": {"10.1.1.0": {"state": "Established"}, "10.1.2.0": {"state": "Active"}}}}
    assert checks.bgp_peer_states(summary) == {"10.1.1.0": "Established", "10.1.2.0": "Active"}
    old = {"neighbors": {"10.255.1.2": [{"nbrState": "Full/-"}]}}
    new = {"neighbors": {"10.255.1.2": [{"converged": "Full", "role": "DROther"}], "1.1.1.1": [{"nbrState": "Init/-"}]}}
    assert checks.ospf_full_count(old) == 1 and checks.ospf_full_count(new) == 1
    assert checks.interface_oper_up({"eth1": {"operationalStatus": "up"}}, "eth1")
    assert not checks.interface_oper_up({"eth1": {"operationalStatus": "down"}}, "eth1")


RUNNING_A1 = """frr version 9.1
hostname a1
!
interface eth1
 ip address 10.10.0.1/31
 ip ospf area 0
 ip ospf dead-interval 4
 ip ospf hello-interval 5
 ip ospf network point-to-point
exit
!
"""


def test_drift_finds_the_changed_parameter_only():
    d = checks.drift(INTENT, "a1", RUNNING_A1)
    assert d == {"eth1": {"ospf_hello": (1, 5)}}


def test_default_metric_is_not_drift_but_a_drain_is():
    running = "interface eth1\n ip router isis core\nexit\ninterface eth2\n isis metric 1000\nexit\n"
    assert checks.drift(INTENT, "p1", running) == {"eth2": {"isis_metric": (10, 1000)}}
    assert checks.drift(INTENT, "p1", running, ignore={"eth2"}) == {}  # a drained link is intentional


def test_probe_analysis_counts_losses_after_the_fault():
    lines = [f"[{100 + (s - 1) * 0.05:.6f}] 64 bytes from 192.168.2.10: icmp_seq={s} ttl=61 time=0.1 ms"
             for s in range(1, 101) if not 41 <= s <= 60]
    r = analyse_probe("\n".join(lines), fault_at=101.9)
    assert r["lost"] == 20 and r["recovered_s"] == pytest.approx(1.1, abs=0.06)


# --- guardrails and decisions ----------------------------------------------------------------

def test_connectivity_guardrail():
    assert connected_without(INTENT, {"p1-p3"})  # the triangle survives one link
    assert not connected_without(INTENT, {"p1-p3", "p1-p2"})  # p1 would be cut off


def test_gray_failure_is_drained_after_consecutive_lossy_rounds():
    c = ctl()
    assert c.decide(healthy(1, link_loss={"p1-p3": 0.2})) == []  # one round isn't enough
    acts = c.decide(healthy(2, link_loss={"p1-p3": 0.25}))
    assert [(a.kind, a.target) for a in acts] == [("drain", "p1-p3")]


def test_a_single_lossy_round_resets():
    c = ctl()
    c.decide(healthy(1, link_loss={"p1-p3": 0.2}))
    c.decide(healthy(2))
    assert c.decide(healthy(3, link_loss={"p1-p3": 0.2})) == []


def test_never_drains_a_second_link_or_partitions_the_core():
    c = ctl(max_drained_links=1)
    c.drained["p1-p2"] = 0
    c.decide(healthy(1, link_loss={"p1-p3": 0.5}))
    acts = c.decide(healthy(2, link_loss={"p1-p3": 0.5}))
    assert [a.kind for a in acts] == ["escalate"] and "already 1 drained" in acts[0].reason
    c2 = ctl(max_drained_links=2)
    c2.drained["p1-p2"] = 0
    c2.decide(healthy(1, link_loss={"p1-p3": 0.5}))
    acts = c2.decide(healthy(2, link_loss={"p1-p3": 0.5}))
    assert acts[0].kind == "escalate" and "partition" in acts[0].reason
    assert c2.decide(healthy(3, link_loss={"p1-p3": 0.5})) == []  # escalated once, not every round


def test_drain_changes_both_ends_and_undrain_restores_the_intended_metric():
    t = FakeTransport()
    c = Controller(INTENT, t)
    c.decide(healthy(1, link_loss={"p1-p3": 0.3}))
    for a in c.decide(healthy(2, link_loss={"p1-p3": 0.3})):
        c.act(a)
    cmds = [cmd for _node, cmd in t.calls]
    assert any("interface eth2" in x and "isis metric 1000" in x for x in cmds)
    assert {node for node, cmd in t.calls if "isis metric 1000" in cmd} == {"p1", "p3"}
    assert "p1-p3" in c.drained
    c.undrain("p1-p3")
    assert any("isis metric 10'" in cmd for _n, cmd in t.calls) and not c.drained


def test_drift_is_restored_from_intent():
    t = FakeTransport()
    c = Controller(INTENT, t)
    acts = c.decide(healthy(1, drift={"a1": {"eth1": {"ospf_hello": (1, 5)}}}))
    assert [(a.kind, a.target) for a in acts] == [("restore", "a1:eth1")]
    c.act(acts[0])
    sent = " ".join(cmd for node, cmd in t.calls if node == "a1")
    assert "ip ospf hello-interval 1" in sent and "ip ospf dead-interval 4" in sent
    assert c.decide(healthy(2, drift={"a1": {"eth1": {"ospf_hello": (1, 5)}}})) == []  # cooldown


def test_hard_failures_get_one_ticket_each():
    c = ctl()
    down = healthy(1, link_up={"p1-p3": False, "p1-ce1": False},
                   bgp={"ce1": {"10.1.1.0": "Active", "10.1.2.0": "Established"}})
    acts = {a.target: a for a in c.decide(down)}
    assert acts["p1-p3"].kind == "ticket" and "rerouted" in acts["p1-p3"].reason
    assert "failed over, 1 session(s) still up" in acts["p1-ce1"].reason
    assert c.decide(down) == []  # not again while still down
    c.decide(healthy(2))  # recovered
    assert len(c.decide(down)) == 2  # a new outage gets new tickets


def test_dry_run_logs_but_changes_nothing(tmp_path):
    t = FakeTransport()
    c = Controller(INTENT, t, log_path=tmp_path / "log.jsonl", dry_run=True)
    c.decide(healthy(1, link_loss={"p1-p3": 0.3}))
    for a in c.decide(healthy(2, link_loss={"p1-p3": 0.3})):
        c.act(a)
    assert t.calls == [] and '"dry_run": true' in (tmp_path / "log.jsonl").read_text()


def test_device_quotes_vtysh_commands():
    t = FakeTransport()
    Device("p1", t).configure("interface eth2", "isis metric 1000")
    assert t.calls == [("p1", "vtysh -c 'configure terminal' -c 'interface eth2' -c 'isis metric 1000' -c 'end'")]


def test_each_router_runs_only_its_protocols():
    from netrepair.render import daemons

    on = lambda r: {ln.split("=")[0] for ln in daemons(INTENT, INTENT.routers[r]).splitlines()  # noqa: E731
                    if ln.endswith("=yes") and not ln.startswith("vtysh")}
    assert on("p1") == {"zebra", "staticd", "isisd", "bgpd"}
    assert on("a1") == {"zebra", "staticd", "ospfd"}
    assert on("ce2") == {"zebra", "staticd", "bgpd"}
