"""Turn the intent into FRR configs and a containerlab topology.

    python -m netrepair render       # writes lab/build/

Generated per router: frr.conf (interfaces, IS-IS / OSPF / BGP, prefix lists and
route maps), plus the shared daemons file. The topology file wires the links
and the two hosts.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .intent import Intent, Link, Router

BUILD = Path(__file__).resolve().parent.parent / "lab" / "build"
LAB_NAME = "netrepair"

DAEMONS = """\
zebra=yes
bgpd=yes
ospfd=yes
isisd=yes
staticd=yes
vtysh_enable=yes
zebra_options="  -A 127.0.0.1 -s 90000000"
bgpd_options="   -A 127.0.0.1"
ospfd_options="  -A 127.0.0.1"
isisd_options="  -A 127.0.0.1"
staticd_options="-A 127.0.0.1"
"""


def interface_block(intent: Intent, r: Router, lk: Link) -> list[str]:
    t = intent.timers
    end = lk.end(r.name)
    lines = [f"interface {end.iface}", f" ip address {end.address}/{lk.prefixlen}"]
    if lk.proto == "isis":
        lines += [" ip router isis core", " isis network point-to-point", " isis circuit-type level-2-only",
                  f" isis hello-interval {t['isis_hello']}", f" isis hello-multiplier {t['isis_multiplier']}",
                  f" isis metric {lk.metric}"]
    elif lk.proto == "ospf":
        lines += [f" ip ospf area {lk.ospf_area}", " ip ospf network point-to-point",
                  f" ip ospf hello-interval {t['ospf_hello']}", f" ip ospf dead-interval {t['ospf_dead']}"]
    return lines + ["exit", "!"]


def bgp_neighbors(intent: Intent, r: Router) -> list[tuple[str, int, str]]:
    """(address, remote AS, role) for every BGP neighbor of r."""
    out: list[tuple[str, int, str]] = []
    for lk in intent.links_of(r.name):
        if lk.proto == "ebgp":
            peer = intent.routers[lk.other(r.name).node]
            assert peer.asn is not None
            out.append((lk.other(r.name).address, peer.asn, lk.role))
    if r.asn is not None:  # iBGP full mesh between loopbacks of the same AS
        for other in intent.routers.values():
            if other.name != r.name and other.asn == r.asn:
                out.append((other.loopback, other.asn, "ibgp"))
    return out


def frr_conf(intent: Intent, r: Router) -> str:
    t = intent.timers
    is_core = r.name in intent.isis_routers()
    is_ospf = r.name in intent.ospf_routers()
    lines = ["frr defaults traditional", f"hostname {r.name}", "log stdout informational",
             "service integrated-vtysh-config", "!", "interface lo", f" ip address {r.loopback}/32"]
    if is_core:
        lines += [" ip router isis core", " isis passive"]
    if is_ospf:
        lines += [" ip ospf area 0"]
    lines += ["exit", "!"]
    for lk in intent.links_of(r.name):
        lines += interface_block(intent, r, lk)
    if r.lan:
        lines += [f"interface {r.lan['iface']}", f" ip address {r.lan['address']}"]
        if "ospf_area" in r.lan:
            lines += [f" ip ospf area {r.lan['ospf_area']}", " ip ospf passive"]
        lines += ["exit", "!"]

    if is_core:
        assert r.isis_net
        lines += ["router isis core", f" net {r.isis_net}", " is-type level-2-only", " metric-style wide",
                  " log-adjacency-changes", "exit", "!"]
    if is_ospf:
        lines += ["router ospf", f" ospf router-id {r.loopback}"]
        if r.ospf_default_originate:
            lines += [" default-information originate always"]
        lines += ["exit", "!"]

    if r.asn is not None:
        neighbors = bgp_neighbors(intent, r)
        customer = not is_core  # a customer edge (ce1, ce2) applies the site policy
        if customer:
            lines += [f"ip prefix-list OWN seq {10 * (i + 1)} permit {n}" for i, n in enumerate(r.bgp_networks)]
            lines += ["ip prefix-list FROM-PROVIDER seq 10 permit 192.168.0.0/16 le 24", "!",
                      "route-map PRIMARY-IN permit 10", " match ip address prefix-list FROM-PROVIDER",
                      " set local-preference 200", "exit", "!",
                      "route-map BACKUP-IN permit 10", " match ip address prefix-list FROM-PROVIDER",
                      " set local-preference 100", "exit", "!",
                      "route-map PRIMARY-OUT permit 10", " match ip address prefix-list OWN", "exit", "!",
                      "route-map BACKUP-OUT permit 10", " match ip address prefix-list OWN",
                      f" set as-path prepend {r.asn} {r.asn}", "exit", "!"]
        else:
            lines += ["route-map ALLOW permit 10", "exit", "!"]
        lines += [f"router bgp {r.asn}", f" bgp router-id {r.loopback}", " bgp log-neighbor-changes",
                  f" timers bgp {t['bgp_keepalive']} {t['bgp_hold']}"]
        for addr, asn, role in neighbors:
            lines += [f" neighbor {addr} remote-as {asn}"]
            if role == "ibgp":
                lines += [f" neighbor {addr} update-source lo"]
        lines += [" !", " address-family ipv4 unicast"]
        lines += [f"  network {n}" for n in r.bgp_networks]
        for addr, _asn, role in neighbors:
            if role == "ibgp":
                lines += [f"  neighbor {addr} next-hop-self"]
            elif customer:
                kind = "BACKUP" if role == "backup" else "PRIMARY"
                lines += [f"  neighbor {addr} route-map {kind}-IN in", f"  neighbor {addr} route-map {kind}-OUT out"]
            else:
                lines += [f"  neighbor {addr} route-map ALLOW in", f"  neighbor {addr} route-map ALLOW out"]
        lines += [" exit-address-family", "exit", "!"]
    return "\n".join(lines) + "\nend\n"


def topology(intent: Intent, build: Path) -> dict:
    nodes: dict[str, dict] = {}
    for name in intent.routers:
        nodes[name] = {"kind": "linux", "image": intent.image,
                       "binds": [f"{build / name / 'frr.conf'}:/etc/frr/frr.conf",
                                 f"{build / 'daemons'}:/etc/frr/daemons",
                                 f"{build / 'vtysh.conf'}:/etc/frr/vtysh.conf"],
                       "sysctls": {"net.ipv4.ip_forward": 1}}
    links = [{"endpoints": [str(lk.a), str(lk.b)]} for lk in intent.links]
    for host, spec in intent.hosts.items():
        nodes[host] = {"kind": "linux", "image": intent.host_image,
                       "exec": [f"ip addr add {spec['address']} dev eth1",
                                f"ip route replace default via {spec['gateway']}"]}
        links.append({"endpoints": [spec["router"], f"{host}:eth1"]})
    return {"name": LAB_NAME, "topology": {"nodes": nodes, "links": links}}


def render(intent: Intent, build: Path = BUILD) -> Path:
    build.mkdir(parents=True, exist_ok=True)
    (build / "daemons").write_text(DAEMONS)
    (build / "vtysh.conf").write_text("service integrated-vtysh-config\n")
    for r in intent.routers.values():
        (build / r.name).mkdir(exist_ok=True)
        (build / r.name / "frr.conf").write_text(frr_conf(intent, r))
    topo = build / f"{LAB_NAME}.clab.yml"
    topo.write_text(yaml.safe_dump(topology(intent, build), sort_keys=False))
    return topo
