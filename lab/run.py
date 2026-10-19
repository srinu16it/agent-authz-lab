"""Run cases x configs x repetitions for one or more models; append one JSON line per run.

  .venv/bin/python -m lab.run --models openai:gpt-5.4-mini ollama:qwen3:30b --reps 10
  .venv/bin/python -m lab.run --models openai:gpt-5.4-mini --reps 1 --out results/smoke

Re-running with the same --out resumes: finished (model, case, config, rep) rows are skipped.
"""
from __future__ import annotations

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .agent import run_case
from .cases import CASE_BY_ID, CASES, legit_success
from .gateway import CONFIGS
from .llm import OllamaChat, OpenAIChat


def make_llm(spec: str):
    provider, model = spec.split(":", 1)
    return OpenAIChat(model) if provider == "openai" else OllamaChat(model)


def one(spec: str, case, config: str, rep: int) -> dict:
    llm = make_llm(spec)
    r = run_case(llm, case, config)
    calls = [c.__dict__ for c in r["calls"]]
    effects = [dict(e) for e in r["db"].execute("SELECT action, detail FROM effects")]
    return {
        "model": spec, "case": case.id, "config": config, "rep": rep, "legit": case.legit,
        "success": legit_success(case, r["calls"], r["final"], r["db"]) if case.legit else None,
        "unauth_requested": sum(not c["oracle_allowed"] for c in calls),
        "unauth_executed": sum((not c["oracle_allowed"]) and c["executed"] for c in calls),
        "calls": calls, "effects": effects, "final": r["final"] or "", "error": r["error"],
        "turns": r["turns"], "wall_s": round(r["wall_s"], 3), "llm_s": round(r["llm_s"], 3),
        "tokens": r["tokens"],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--cases", nargs="*", default=[c.id for c in CASES])
    ap.add_argument("--configs", nargs="*", default=list(CONFIGS))
    ap.add_argument("--out", default="results/run")
    ap.add_argument("--workers", type=int, default=6, help="parallel runs for hosted models; local models run 1 at a time")
    a = ap.parse_args(argv)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    path = out / "runs.jsonl"
    done = set()
    if path.exists():
        for line in path.read_text().splitlines():
            d = json.loads(line)
            if not d.get("error"):
                done.add((d["model"], d["case"], d["config"], d["rep"]))
    lock = threading.Lock()

    def job(spec, cid, config, rep):
        row = one(spec, CASE_BY_ID[cid], config, rep)
        with lock, path.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
        flag = "ERR" if row["error"] else ("ok" if row["legit"] and row["success"] else
                                           f"req={row['unauth_requested']} exe={row['unauth_executed']}")
        print(f"{spec:28} {cid} {config} rep{rep}: {flag}", flush=True)

    for spec in a.models:
        todo = [(spec, c, cfg, r) for r in range(a.reps) for c in a.cases for cfg in a.configs
                if (spec, c, cfg, r) not in done]
        workers = a.workers if spec.startswith("openai:") else 1
        print(f"== {spec}: {len(todo)} runs, {workers} at a time", flush=True)
        with ThreadPoolExecutor(workers) as ex:
            list(ex.map(lambda t: job(*t), todo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
