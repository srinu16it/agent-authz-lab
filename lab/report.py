"""Summarize a run directory into results.md + charts.

  .venv/bin/python -m lab.report results/run-2026-10-18
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from .cases import CASE_BY_ID  # noqa: E402

CFG = {"A": "A: authn only", "B": "B: authn + prompt rules", "C": "C: authn + enforced authz"}


def pct(n, d):
    return f"{100 * n / d:.0f}%" if d else "n/a"


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))] if xs else float("nan")


def wilson(k, n, z=1.96):
    """95% Wilson interval for a proportion: honest error bars for small n."""
    if not n:
        return (0.0, 0.0)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / den
    return (max(0.0, c - h), min(1.0, c + h))


def main(run_dir: str) -> None:
    d = Path(run_dir)
    rows = [json.loads(l) for l in (d / "runs.jsonl").read_text().splitlines()]
    errors = [r for r in rows if r["error"]]
    rows = [r for r in rows if not r["error"]]
    models = sorted({r["model"] for r in rows})
    out = [f"# Results: {d.name}", "",
           f"{len(rows)} completed runs ({len(errors)} errored and excluded). "
           "An *unauthorized* call is one the policy oracle denies for the TRUE signed-in user; "
           "*requested* = the model asked for it, *executed* = the backend actually did it.", ""]
    summary = {}
    for m in models:
        out += [f"## {m}", "",
                "| Config | Adversarial runs with an unauthorized EXECUTION | ...with an unauthorized REQUEST | "
                "Unauthorized calls executed / requested | Legit tasks completed | Run latency p50 / p95 (s) | "
                "Policy check p50 / p95 (µs) |",
                "|---|---|---|---|---|---|---|"]
        for cfg in "ABC":
            rs = [r for r in rows if r["model"] == m and r["config"] == cfg]
            adv = [r for r in rs if not r["legit"]]
            leg = [r for r in rs if r["legit"]]
            ex = sum(r["unauth_executed"] > 0 for r in adv)
            rq = sum(r["unauth_requested"] > 0 for r in adv)
            ok = sum(bool(r["success"]) for r in leg)
            lat = [r["wall_s"] for r in leg]
            pol = [c["policy_us"] for r in rs for c in r["calls"] if c["policy_us"] is not None]
            lo, hi = wilson(ex, len(adv))
            summary[(m, cfg)] = dict(ex=ex, rq=rq, adv=len(adv), ok=ok, leg=len(leg), lo=lo, hi=hi,
                                     ex_calls=sum(r["unauth_executed"] for r in adv),
                                     rq_calls=sum(r["unauth_requested"] for r in adv))
            out.append(f"| {CFG[cfg]} | {ex}/{len(adv)} = **{pct(ex, len(adv))}** (95% CI {lo:.0%}–{hi:.0%}) | "
                       f"{rq}/{len(adv)} = {pct(rq, len(adv))} | "
                       f"{summary[(m, cfg)]['ex_calls']} / {summary[(m, cfg)]['rq_calls']} | "
                       f"{ok}/{len(leg)} = **{pct(ok, len(leg))}** | {q(lat, .5):.1f} / {q(lat, .95):.1f} | "
                       + (f"{q(pol, .5):.0f} / {q(pol, .95):.0f}" if pol else "n/a") + " |")
        out += ["", "**Per test case** (cell = runs with an unauthorized request → runs where one executed; "
                "legit cases show completed runs):", "",
                "| Case | Scenario | A | B | C |", "|---|---|---|---|---|"]
        for cid in sorted({r["case"] for r in rows}, key=lambda x: int(x[1:])):
            case = CASE_BY_ID[cid]
            cells = []
            for cfg in "ABC":
                rs = [r for r in rows if r["model"] == m and r["config"] == cfg and r["case"] == cid]
                if case.legit:
                    cells.append(f"✅ {sum(bool(r['success']) for r in rs)}/{len(rs)} done")
                else:
                    cells.append(f"{sum(r['unauth_requested'] > 0 for r in rs)} → "
                                 f"**{sum(r['unauth_executed'] > 0 for r in rs)}**/{len(rs)}")
            out.append(f"| {cid} | {case.title} | " + " | ".join(cells) + " |")
        # what actually got executed that should not have, by action type
        kinds = defaultdict(int)
        for r in rows:
            if r["model"] == m:
                for c in r["calls"]:
                    if c["executed"] and not c["oracle_allowed"]:
                        kinds[(r["config"], c["action"])] += 1
        if kinds:
            out += ["", "Unauthorized calls that EXECUTED, by tool: " +
                    "; ".join(f"{cfg} {a} ×{n}" for (cfg, a), n in sorted(kinds.items()))]
        forged = sum(1 for r in rows if r["model"] == m for c in r["calls"] if c.get("identity_claim"))
        out += ["", f"Tool calls where the model wrote an identity different from the verified user: {forged}", ""]
    if errors:
        out += ["## Errored runs (excluded)", ""] + [f"- {e['model']} {e['case']} {e['config']} rep{e['rep']}: {e['error']}"
                                                     for e in errors[:20]]
    (d / "results.md").write_text("\n".join(out) + "\n")

    # Chart 1: unauthorized execution rate with 95% CIs
    fig, ax = plt.subplots(figsize=(8, 4.2))
    w = 0.38
    colors = {"A": "#c0392b", "B": "#e67e22", "C": "#27ae60"}
    for i, m in enumerate(models):
        for j, cfg in enumerate("ABC"):
            s = summary[(m, cfg)]
            rate = s["ex"] / s["adv"] if s["adv"] else 0
            x = j + (i - (len(models) - 1) / 2) * w
            ax.bar(x, rate * 100, w * 0.95, color=colors[cfg], alpha=1 if i == 0 else 0.55,
                   yerr=[[100 * (rate - s["lo"])], [100 * (s["hi"] - rate)]], capsize=3,
                   label=m if j == 0 else None)
            ax.text(x, rate * 100 + 2, f"{rate:.0%}", ha="center", fontsize=8)
    ax.set_xticks(range(3), [CFG[c] for c in "ABC"], fontsize=9)
    ax.set_ylabel("adversarial runs where an\nunauthorized action EXECUTED (%)")
    ax.set_ylim(0, 105)
    ax.set_title("Same model, same prompts, same requests: what actually executed")
    ax.legend(fontsize=8, title="model (darker = first)", title_fontsize=8)
    fig.tight_layout()
    fig.savefig(d / "chart_unauthorized_execution.png", dpi=160)

    # Chart 2: requested vs executed unauthorized calls
    fig, axes = plt.subplots(1, len(models), figsize=(4.2 * len(models), 3.8), squeeze=False)
    for i, m in enumerate(models):
        ax = axes[0][i]
        req = [summary[(m, c)]["rq_calls"] for c in "ABC"]
        exe = [summary[(m, c)]["ex_calls"] for c in "ABC"]
        ax.bar([x - 0.2 for x in range(3)], req, 0.4, label="model REQUESTED", color="#95a5a6")
        ax.bar([x + 0.2 for x in range(3)], exe, 0.4, label="backend EXECUTED", color="#2c3e50")
        ax.set_xticks(range(3), ["A", "B", "C"])
        ax.set_title(m, fontsize=9)
        ax.set_ylabel("unauthorized tool calls")
        ax.legend(fontsize=8)
    fig.suptitle("The model asks; the architecture decides", fontsize=10)
    fig.tight_layout()
    fig.savefig(d / "chart_requested_vs_executed.png", dpi=160)
    print((d / "results.md").read_text())


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "results/run")
