"""The intended network, loaded from lab/intent.yaml.

Everything else derives from this: the router configs (render.py), what "healthy"
means (checks.py) and what a repair restores (controller.py).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_INTENT = Path(__file__).resolve().parent.parent / "lab" / "intent.yaml"


@dataclass(frozen=True)
class End:
    node: str
    iface: str
    address: str  # without prefix length

    def __str__(self) -> str:
        return f"{self.node}:{self.iface}"


@dataclass(frozen=True)
class Link:
    a: End
    b: End
    prefixlen: int
    proto: str  # isis | ospf | ebgp
    metric: int = 10
    role: str = ""  # ebgp: primary | backup
    ospf_area: int = 0

    @property
    def name(self) -> str:
        return f"{self.a.node}-{self.b.node}"

    def end(self, node: str) -> End:
        return self.a if self.a.node == node else self.b

    def other(self, node: str) -> End:
        return self.b if self.a.node == node else self.a


@dataclass
class Router:
    name: str
    loopback: str
    asn: int | None = None
    isis_net: str | None = None
    lan: dict[str, Any] | None = None
    bgp_networks: list[str] = field(default_factory=list)
    ospf_redistribute_bgp: bool = False


@dataclass
class Intent:
    image: str
    host_image: str
    timers: dict[str, int]
    routers: dict[str, Router]
    links: list[Link]
    hosts: dict[str, dict[str, str]]
    policy: dict[str, float]

    def links_of(self, node: str) -> list[Link]:
        return [lk for lk in self.links if node in (lk.a.node, lk.b.node)]

    def core_links(self) -> list[Link]:
        return [lk for lk in self.links if lk.proto == "isis"]

    def link(self, name: str) -> Link:
        for lk in self.links:
            if lk.name == name or f"{lk.b.node}-{lk.a.node}" == name:
                return lk
        raise KeyError(f"no link {name}")

    def ospf_routers(self) -> set[str]:
        return {e.node for lk in self.links if lk.proto == "ospf" for e in (lk.a, lk.b)}

    def isis_routers(self) -> set[str]:
        return {e.node for lk in self.core_links() for e in (lk.a, lk.b)}


def load(path: str | Path = DEFAULT_INTENT) -> Intent:
    raw = yaml.safe_load(Path(path).read_text())
    links = []
    for item in raw["links"]:
        net = ipaddress.ip_network(item["net"])
        if net.prefixlen != 31:
            raise ValueError(f"{item['net']}: links must be /31")
        lo, hi = (str(ip) for ip in (net.network_address, net.network_address + 1))
        (an, ai), (bn, bi) = (item[k].split(":") for k in ("a", "b"))
        links.append(Link(End(an, ai, lo), End(bn, bi, hi), 31, item["proto"], item.get("metric", 10),
                          item.get("role", ""), item.get("ospf_area", 0)))
    routers = {name: Router(name=name, **spec) for name, spec in raw["routers"].items()}
    for lk in links:
        for e in (lk.a, lk.b):
            if e.node not in routers:
                raise ValueError(f"link {lk.name} names unknown router {e.node}")
    return Intent(raw["image"], raw["host_image"], raw["timers"], routers, links, raw["hosts"], raw["policy"])
