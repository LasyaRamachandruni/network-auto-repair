"""Talking to routers.

The controller only uses `Transport`. Here it's `DockerExec`, which runs commands in
containerlab's containers with `docker exec`: `vtysh` for FRR, `ip`/`tc`/`ping`
for Linux. Against real hardware the same interface would sit on SSH (Netmiko),
NETCONF or gNMI; nothing above this file would change. Tests use `FakeTransport`.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Protocol


class Transport(Protocol):
    def sh(self, node: str, command: str, timeout: float = 20) -> str:
        """Run a command inside the router's container (FRR's vtysh lives there)."""
        ...

    def net(self, node: str, command: str, timeout: float = 20) -> str:
        """Run a host tool (ping, tc, ip) inside the node's network namespace."""
        ...


class CommandFailed(RuntimeError):
    pass


class DockerExec:
    """containerlab nodes: `docker exec` for vtysh, `ip netns exec` for host tools.

    containerlab names each node's network namespace after its container, so the
    host's own iputils ping and tc work inside it (the FRR image only has busybox).
    """

    def __init__(self, lab: str = "netrepair", sudo: bool = True):
        self.lab = lab
        self.sudo = ["sudo", "-n"] if sudo else []

    def container(self, node: str) -> str:
        return f"clab-{self.lab}-{node}"

    def sh(self, node: str, command: str, timeout: float = 20) -> str:
        return self._run(node, command, ["docker", "exec", self.container(node), "sh", "-c", command], timeout)

    def net(self, node: str, command: str, timeout: float = 20) -> str:
        return self._run(node, command, [*self.sudo, "ip", "netns", "exec", self.container(node), "sh", "-c", command],
                         timeout)

    @staticmethod
    def _run(node: str, command: str, argv: list[str], timeout: float) -> str:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            raise CommandFailed(f"{node}: {command!r} exited {r.returncode}: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout


class Device:
    """One router: FRR show commands as JSON, config changes, Linux commands."""

    def __init__(self, name: str, transport: Transport):
        self.name = name
        self.t = transport

    def vtysh(self, *commands: str) -> str:
        args = " ".join(f"-c {_quote(c)}" for c in commands)
        return self.t.sh(self.name, f"vtysh {args}")

    def show(self, command: str) -> Any:
        out = self.vtysh(f"{command} json")
        return json.loads(out) if out.strip() else {}

    def configure(self, *lines: str) -> None:
        """Enter config mode, apply lines, leave. Lines are FRR config syntax."""
        self.vtysh("configure terminal", *lines, "end")

    def ping(self, address: str, count: int = 20, interval: float = 0.05) -> float:
        """Fraction of `count` pings to `address` that got no reply."""
        try:
            out = self.t.net(self.name, f"ping -n -q -c {count} -i {interval} -W 1 {address}", timeout=count + 10)
        except CommandFailed as exc:  # ping exits 1 when replies are missing
            out = str(exc)
        return parse_ping_loss(out)


def parse_ping_loss(output: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)% packet loss", output)
    if not m:
        return 1.0  # no summary line: treat as total loss
    return float(m.group(1)) / 100


def _quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


class FakeTransport:
    """Scripted responses for tests: {(node, command substring): output or callable}."""

    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], Any] = {}
        self.calls: list[tuple[str, str]] = []

    def on(self, node: str, contains: str, output: Any) -> None:
        self.responses[(node, contains)] = output

    def net(self, node: str, command: str, timeout: float = 20) -> str:
        return self.sh(node, command, timeout)

    def sh(self, node: str, command: str, timeout: float = 20) -> str:
        self.calls.append((node, command))
        # longest matching pattern wins, so specific scripts override general ones
        matches = [(k, v) for k, v in self.responses.items() if k[0] == node and k[1] in command]
        if not matches:
            return ""
        _, out = max(matches, key=lambda kv: len(kv[0][1]))
        out = out(command) if callable(out) else out
        return json.dumps(out) if isinstance(out, (dict, list)) else str(out)
