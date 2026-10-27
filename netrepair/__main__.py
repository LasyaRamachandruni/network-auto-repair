"""netrepair: an intent-driven network repair controller for a containerlab FRR lab.

    python -m netrepair render            # intent.yaml -> lab/build/ (FRR configs + topology)
    python -m netrepair deploy            # render, then start the lab with containerlab
    python -m netrepair status            # one observation: link loss, BGP, OSPF, drift
    python -m netrepair watch [--dry-run] # run the controller until Ctrl-C
    python -m netrepair experiment        # fault-injection experiment (see experiment.py)
    python -m netrepair destroy
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict

from . import experiment
from .checks import observe
from .controller import Controller
from .device import DockerExec
from .intent import load
from .render import BUILD, LAB_NAME, render


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="netrepair", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["render", "deploy", "status", "watch", "experiment", "destroy"])
    ap.add_argument("--dry-run", action="store_true", help="watch: log decisions, change nothing")
    ap.add_argument("rest", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    intent = load()

    if args.command == "render":
        print(render(intent))
    elif args.command == "deploy":
        topo = render(intent)
        subprocess.run(["sudo", "containerlab", "deploy", "-t", str(topo), "--reconfigure"], check=True)
    elif args.command == "destroy":
        subprocess.run(["sudo", "containerlab", "destroy", "-t", str(BUILD / f"{LAB_NAME}.clab.yml"), "--cleanup"],
                       check=True)
    elif args.command == "status":
        print(json.dumps(asdict(observe(intent, DockerExec(), time.time())), indent=2, default=str))
    elif args.command == "watch":
        ctl = Controller(intent, DockerExec(), log_path="results/actions.jsonl", dry_run=args.dry_run)
        try:
            while True:
                obs, actions = ctl.step()
                lossy = {k: f"{v:.0%}" for k, v in obs.link_loss.items() if v}
                print(time.strftime("%H:%M:%S"), "loss", lossy or "none", "drift", list(obs.drift) or "none",
                      "| actions:", [f"{a.kind} {a.target}" for a in actions] or "-", flush=True)
        except KeyboardInterrupt:
            return 0
    elif args.command == "experiment":
        return experiment.main(args.rest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
