"""Serves *measured* evaluation output produced by `python -m app.evaluation`. The API never computes or fabricates numbers:
if no run has been written yet it answers 404 and the UI shows an honest empty state."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_principal
from app.auth.principal import Principal
from app.core.exceptions import NotFound

router = APIRouter(prefix="/evaluation", tags=["evaluation"])


def _dir(request: Request) -> Path:
    s = request.app.state.settings
    return Path(s.evaluation_results_dir) if s.evaluation_results_dir else Path(__file__).resolve().parents[3] / "experiments" / "results"


def _load(request: Request, name: str) -> dict:
    p = _dir(request) / name
    if not p.is_file():
        raise NotFound("No evaluation results yet. Run `python -m app.evaluation` (see docs/evaluation.md).")
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise NotFound(f"Evaluation results unreadable: {type(e).__name__}") from e


@router.get("/results")
async def results(request: Request, _: Principal = Depends(get_principal)):
    return _load(request, "latest.json")


@router.get("/calibration")
async def calibration(request: Request, _: Principal = Depends(get_principal)):
    return _load(request, "calibration.json")
