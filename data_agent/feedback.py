"""Online feedback, bucketed by provenance tier — the +1 layer's online half.

After release, accuracy is watched per ANSWER SOURCE: answers built on a
verified query, on a registered metric, or on SQL the model wrote. Which tier
a turn used is already on every audit line (`provenance`); this module
turns a user's verdict on an answer into one more audit line that carries
that tier, so the decay that matters — "the free-SQL bucket started failing"
— shows up as a ratio on a dashboard instead of an anecdote.

    verdict      up | down
    correction   optional free text: what the right answer / definition is.
                 UNTRUSTED user content: stored (truncated) as data and only
                 ever surfaced to a human reviewer (scripts/harvest_corrections.py
                 turns down-votes with corrections into review drafts — never
                 into automatic changes to an asset).

The turn's bucket is the most deterministic tier any executed statement of
that turn used (verified > metric > free_sql), so a turn that started from a
verified query and then explored with free SQL is attributed to the verified
query it rested on; the full per-statement mix rides along for the reviewer.
A turn with no executed statement (a refusal, a plain-text answer) is
bucketed `none`. EMF metrics `Feedback` / `FeedbackNegative` (dimension
`Provenance`) give the down-rate per bucket without any PutMetricData call.
"""

from __future__ import annotations

import hashlib
import time

from data_agent import audit

VERDICTS = ("up", "down")
TIERS = ("verified", "metric", "free_sql")  # most → least deterministic
CORRECTION_MAX = 1000


class FeedbackError(ValueError):
    """A feedback payload that does not fit (caller error, refused)."""


def bucket(mix: list[str]) -> str:
    for tier in TIERS:
        if tier in mix:
            return tier
    return "none"


def record(
    payload: dict,
    *,
    principal: str,
    user_sub: str | None,
) -> dict:
    """Validate a `{"verdict", "correction"?, "turn_id"?}` payload and emit
    its audit line. Returns the summary the caller is shown."""
    if not isinstance(payload, dict):
        raise FeedbackError("feedback must be an object {verdict, correction?, turn_id?}")
    verdict = payload.get("verdict")
    if verdict not in VERDICTS:
        raise FeedbackError(f"verdict must be one of {VERDICTS}")
    correction = payload.get("correction")
    if correction is not None and not isinstance(correction, str):
        raise FeedbackError("correction must be text")
    correction = (correction or "").strip()[:CORRECTION_MAX] or None
    turn_id, turn = audit.turn(payload.get("turn_id") or None)
    mix = list((turn or {}).get("provenance") or [])
    tier = bucket(mix)
    question = (turn or {}).get("question") or ""
    rec = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": audit.NAMESPACE,
                    "Dimensions": [["Provenance"]],
                    "Metrics": [
                        {"Name": "Feedback", "Unit": "Count"},
                        {"Name": "FeedbackNegative", "Unit": "Count"},
                    ],
                }
            ],
        },
        "Provenance": tier,
        "Feedback": 1,
        "FeedbackNegative": 1 if verdict == "down" else 0,
        "audit": {
            "tool": "feedback",
            "stage": "feedback",
            "principal": principal,
            "agent_identity": audit.AGENT_IDENTITY,
            "user_sub": user_sub,
            "verdict": verdict,
            "provenance": tier,
            "provenance_mix": mix,
            "turn_id": turn_id,
            # the question the verdict is about — the same data class the
            # audit already keeps for SQL; the reviewer needs it to act
            "question": question,
            "question_sha256": hashlib.sha256(question.encode()).hexdigest()[:16],
            "correction": correction,
            "matched_turn": turn is not None,
        },
    }
    audit.emit(rec)
    return {
        "verdict": verdict,
        "provenance": tier,
        "turn_id": turn_id,
        "matched_turn": turn is not None,
        "correction_recorded": correction is not None,
    }
