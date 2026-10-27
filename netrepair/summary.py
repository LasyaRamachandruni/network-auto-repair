"""Summarise results/experiment.jsonl: one row per scenario, medians over runs.

    python -m netrepair.summary          # prints a Markdown table, writes results/summary.json
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

RESULTS = Path(__file__).resolve().parent.parent / "results"
LABELS = {
    "gray_core_link": "Gray failure: core link up, dropping 20% each way",
    "core_link_down": "Core link down",
    "primary_uplink_down": "Site A's primary uplink down",
    "ospf_drift": "OSPF hello interval changed on a1",
}


def med(values):
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals), 2) if vals else None


def summarise(rows: list[dict]) -> list[dict]:
    out = []
    for name in dict.fromkeys(r["scenario"] for r in rows):
        rs = [r for r in rows if r["scenario"] == name]
        out.append({
            "scenario": name,
            "runs": len(rs),
            "detect_s": med(r["detect_s"] for r in rs),
            "act_s": med(r["act_s"] for r in rs),
            "recovered_s": med(r["recovered_s"] for r in rs),
            "not_recovered": sum(r["recovered_s"] is None for r in rs),
            "lost_median": med(r["lost"] for r in rs),
            "lost_range": [min(r["lost"] for r in rs if r["lost"] is not None),
                           max(r["lost"] for r in rs if r["lost"] is not None)],
            "action": rs[0]["action"].split(":")[0] if rs[0]["action"] else None,
        })
    return out


def table(summary: list[dict]) -> str:
    head = ("| Fault | Runs | Detected | Controller acted | Traffic fully back | Probe packets lost (of 20/s) |\n"
            "|---|---|---|---|---|---|")
    lines = [head]
    for s in summary:
        back = f"{s['recovered_s']} s" if s["recovered_s"] is not None else "not within the run"
        if s["not_recovered"] and s["recovered_s"] is not None:
            back += f" ({s['not_recovered']} run(s) didn't recover)"
        lo, hi = s["lost_range"]
        lines.append(f"| {LABELS.get(s['scenario'], s['scenario'])} | {s['runs']} | {s['detect_s']} s | "
                     f"{s['act_s']} s ({s['action']}) | {back} | {s['lost_median']} ({lo}–{hi}) |")
    return "\n".join(lines)


def main() -> int:
    rows = [json.loads(x) for x in (RESULTS / "experiment.jsonl").read_text().splitlines() if x.strip()]
    s = summarise(rows)
    (RESULTS / "summary.json").write_text(json.dumps(s, indent=2) + "\n")
    print(table(s))
    for r in rows:  # one compact line per run
        print(json.dumps({k: r[k] for k in ("scenario", "run", "detect_s", "act_s", "lost", "recovered_s")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
