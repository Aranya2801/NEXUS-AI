from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_principal
from app.auth.principal import Principal
from app.auth.service import audit
from app.core.exceptions import NotFound
from app.database.models import AgentRun, Feedback

router = APIRouter(prefix="/feedback", tags=["feedback"])


class FeedbackIn(BaseModel):
    run_id: str = Field(max_length=64)
    rating: Literal[-1, 1]
    comment: str = Field(default="", max_length=1000)
    reasons: list[Literal["wrong_answer", "missing_information", "bad_citation", "outdated", "too_long", "unclear", "off_topic", "other"]] = Field(default_factory=list, max_length=4)
    allow_review: bool = False        # opt-in: lets org admins read this run's question + answer to improve the system (never implied)


@router.post("")
async def give_feedback(body: FeedbackIn, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    run = await db.get(AgentRun, body.run_id)
    if run is None or run.org_id != p.org_id or run.user_id != p.user_id:  # only the person who got the answer can rate it
        raise NotFound("Run not found")
    fb = (await db.execute(select(Feedback).where(Feedback.run_id == run.id, Feedback.user_id == p.user_id))).scalar_one_or_none()
    if fb is None:
        fb = Feedback(org_id=p.org_id, user_id=p.user_id, run_id=run.id, rating=body.rating, comment=body.comment, reasons=sorted(set(body.reasons)),
                      allow_review=body.allow_review)
        db.add(fb)
    else:
        fb.rating, fb.comment, fb.reasons, fb.allow_review = body.rating, body.comment, sorted(set(body.reasons)), body.allow_review
    await db.flush()
    await audit(db, "feedback.give", org_id=p.org_id, user_id=p.user_id, resource=run.id, rating=body.rating, reasons=sorted(set(body.reasons)), allow_review=body.allow_review)
    return {"run_id": run.id, "rating": fb.rating, "comment": fb.comment, "reasons": fb.reasons, "allow_review": fb.allow_review}


@router.get("/{run_id}")
async def my_feedback(run_id: str, p: Principal = Depends(get_principal), db: AsyncSession = Depends(get_db)):
    fb = (await db.execute(select(Feedback).where(Feedback.run_id == run_id, Feedback.user_id == p.user_id, Feedback.org_id == p.org_id))).scalar_one_or_none()
    return {"run_id": run_id, "rating": fb.rating if fb else None, "comment": fb.comment if fb else "", "reasons": fb.reasons if fb else [],
            "allow_review": fb.allow_review if fb else False}
