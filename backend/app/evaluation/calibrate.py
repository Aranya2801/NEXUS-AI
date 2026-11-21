"""Calibrate the abstention gate (`abstain_confidence_threshold`) on the DEV split only, then report on the untouched TEST split.

Procedure (tuning rule in docs/research/adaptive-retrieval-protocol.md): collect end-to-end runs with the gate disabled, take the
retrieval confidence score of every run, label answerable vs unanswerable, choose the threshold that maximises balanced accuracy
on dev (ties -> middle of the optimal set), and evaluate that fixed threshold on test.

The result is specific to the backend that produced it (embedder, reranker, FTS); `environment` is stored with it."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.evaluation import harness
from app.evaluation import metrics as M


def _bal_acc(rows, t):
    ans = [r for r in rows if r["qclass"] != "unanswerable"]
    un = [r for r in rows if r["qclass"] == "unanswerable"]
    tpr = sum(r["confidence"].get("score", 0) >= t for r in ans) / len(ans) if ans else float("nan")    # answerable kept
    tnr = sum(r["confidence"].get("score", 0) < t for r in un) / len(un) if un else float("nan")        # unanswerable abstained
    return tpr, tnr, (tpr + tnr) / 2


def choose(rows) -> tuple[float, list[float]]:
    scores = sorted({round(r["confidence"].get("score", 0), 4) for r in rows})
    cands = [0.0] + [(a + b) / 2 for a, b in zip(scores, scores[1:])] + [scores[-1] + 1e-3]
    best = max(_bal_acc(rows, t)[2] for t in cands)
    opt = [t for t in cands if abs(_bal_acc(rows, t)[2] - best) < 1e-9]
    return round(opt[len(opt) // 2], 4), opt


async def main_async(out: Path) -> dict:
    out = Path(out).resolve()
    await harness.run(out, repeats=1, systems=["S5"], abstain_threshold=0.0)
    gen = [r for r in json.loads((out / "per_query.json").read_text())["generation"] if "error" not in r]
    dev = [r for r in gen if r["split"] == "dev"]
    test = [r for r in gen if r["split"] == "test"]
    thr, interval = choose(dev)
    d, t = _bal_acc(dev, thr), _bal_acc(test, thr)
    base_t = _bal_acc(test, 0.0)
    cal = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "gate": "abstain_confidence_threshold",
        "chosen_threshold": thr, "optimal_set_size": len(interval),
        "dev": {"n_answerable": sum(r["qclass"] != "unanswerable" for r in dev), "n_unanswerable": sum(r["qclass"] == "unanswerable" for r in dev),
                "answerable_kept": round(d[0], 4), "unanswerable_abstained": round(d[1], 4), "balanced_accuracy": round(d[2], 4)},
        "test": {"n_answerable": sum(r["qclass"] != "unanswerable" for r in test), "n_unanswerable": sum(r["qclass"] == "unanswerable" for r in test),
                 "answerable_kept": round(t[0], 4), "unanswerable_abstained": round(t[1], 4), "balanced_accuracy": round(t[2], 4)},
        "test_without_gate": {"answerable_kept": round(base_t[0], 4), "unanswerable_abstained": round(base_t[1], 4), "balanced_accuracy": round(base_t[2], 4)},
        "auroc_dev": M.auroc([r["confidence"].get("score", 0) for r in dev], [int(r["qclass"] != "unanswerable") for r in dev]),
        "auroc_test": M.auroc([r["confidence"].get("score", 0) for r in test], [int(r["qclass"] != "unanswerable") for r in test]),
        "environment": json.loads((out / "latest.json").read_text())["environment"],
        "caveat": "Calibrated on mock components over a synthetic corpus; recalibrate for any other embedder/reranker/LLM. The unanswerable set "
                  "is small, so the test-split estimate has wide uncertainty.",
    }
    (out / "calibration.json").write_text(json.dumps(cal, indent=2))
    return cal


def main(argv: list[str] | None = None) -> int:
    import asyncio
    out = Path(argv[0] if argv else "../experiments/results")
    json.dump(asyncio.run(main_async(out)), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
