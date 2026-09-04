"""Measured-pressure history compression with engine-pointer stubs.

Why this exists: every model turn re-reads the whole conversation. Tool
results that were useful three turns ago are still being paid for, and near
the context limit they crowd out the reasoning. The conventional fixes —
drop the oldest 30 % by position, or cut each result to its first/last 200
characters — both destroy information silently; production data agents have
been measured losing a 34-message investigation's key result at turn 3 that way.

What we do instead, and why it only works because the engine is in-process:

* **Measure, don't guess.** Strands computes ``projected_input_tokens`` for
  the next model call from the last Bedrock ``usage`` report. We compress
  only when that crosses ``COMPRESSION_THRESHOLD`` × the model's context
  window (default 60 %), never on the first turn, never below
  ``MIN_MESSAGES_FOR_STUB`` messages.
* **Stub, don't truncate.** A ``run_sql`` result that was materialized
  is replaced by one line — the SQL head, the row count and the temp
  table ``_r_<n>`` that still holds every row. Nothing is lost: the data
  lives in the engine, not in the prompt. Inline (small) results are only
  stubbed under a real overflow, and the stub keeps the SQL so they can be
  re-run.
* **Superseded first, then oldest.** If the same SQL was executed again
  later, the earlier copy is pure redundancy and goes first.
* **Reclaim to target, then stop.** Proactive compression removes
  (projected − budget) × 4 bytes/token and no more; the current turn's last
  two messages are never touched.

Reactive overflow (the provider rejected the call) stubs everything eligible
and then falls back to the sliding window. Strands' own "truncate tool
results to 200+200 chars" is disabled — it violates invariant 9.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from strands.agent.conversation_manager import SlidingWindowConversationManager

logger = logging.getLogger(__name__)

COMPRESSION_THRESHOLD = float(os.environ.get("COMPRESSION_THRESHOLD", "0.6"))
MIN_MESSAGES_FOR_STUB = int(os.environ.get("MIN_MESSAGES_FOR_STUB", "6"))
MAX_CONTEXT_MESSAGES = int(os.environ.get("MAX_CONTEXT_MESSAGES", "150"))
BYTES_PER_TOKEN = 4
PROTECT_LAST = 2  # the current turn (assistant toolUse + user toolResult) is never stubbed
MIN_STUB_GAIN = 200  # don't bother stubbing results that are already tiny
STUB_PREFIX = "[Compressed]"
SQL_HEAD = 120

_REF_RE = re.compile(r'"ref":\s*"(_r_\d+)"')
_ROWS_RE = re.compile(r'"rows":\s*(\d+)')


def _text_of(block: dict) -> str:
    return "".join(c.get("text", "") for c in block.get("toolResult", {}).get("content", []))


def _norm_sql(sql: str) -> str:
    return " ".join(sql.lower().split())


class ResultStubConversationManager(SlidingWindowConversationManager):
    """Sliding window + measured-pressure stubbing of materialized results."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            window_size=MAX_CONTEXT_MESSAGES,
            should_truncate_results=False,  # invariant 9: never cut a result to its head/tail
            proactive_compression={"compression_threshold": COMPRESSION_THRESHOLD},
            **kwargs,
        )
        self._projected: int | None = None
        self.stubbed = 0  # session counter (surfaced in the result envelope)
        self.reclaimed_bytes = 0

    # Strands base hook: capture the measurement, then let it decide
    def _on_before_model_call_threshold(self, event) -> None:
        self._projected = getattr(event, "projected_input_tokens", None)
        super()._on_before_model_call_threshold(event)

    # ---------------------------------------------------------------- core
    def reduce_context(self, agent, e: Exception | None = None, **kwargs: Any) -> None:
        messages = agent.messages
        if len(messages) >= MIN_MESSAGES_FOR_STUB:
            target = None
            if e is None:  # proactive: reclaim only what the measurement says we're over
                limit = getattr(getattr(agent, "model", None), "context_window_limit", None)
                if self._projected is not None and limit:
                    over = self._projected - COMPRESSION_THRESHOLD * limit
                    target = max(int(over * BYTES_PER_TOKEN), 1)
            reclaimed = self.stub_results(
                messages, target_bytes=target, include_inline=e is not None
            )
            if reclaimed > 0:
                logger.info(
                    "compression: stubbed results, reclaimed=%s bytes, projected=%s, reactive=%s",
                    reclaimed,
                    self._projected,
                    e is not None,
                )
                return
        super().reduce_context(agent, e, **kwargs)  # nothing left to stub → window trim

    def stub_results(
        self, messages: list, target_bytes: int | None = None, include_inline: bool = False
    ) -> int:
        """Replace eligible run_sql tool results with one-line stubs, in order:
        superseded first, then oldest. Returns bytes reclaimed."""
        candidates = self._candidates(messages, include_inline)
        reclaimed = 0
        for cand in candidates:
            if target_bytes is not None and reclaimed >= target_bytes:
                break
            block = cand["block"]
            stub = self._stub_text(cand)
            before = len(cand["text"])
            block["toolResult"]["content"] = [{"text": stub}]
            reclaimed += max(before - len(stub), 0)
            self.stubbed += 1
        self.reclaimed_bytes += reclaimed
        return reclaimed

    # ------------------------------------------------------------ helpers
    def _candidates(self, messages: list, include_inline: bool) -> list[dict]:
        uses: dict[str, dict] = {}  # toolUseId → {"sql", "index"}
        for i, m in enumerate(messages):
            for block in m.get("content", []) or []:
                tu = block.get("toolUse")
                if tu and tu.get("name") == "run_sql":
                    sql = str((tu.get("input") or {}).get("sql", ""))
                    uses[tu["toolUseId"]] = {"sql": sql, "index": i}
        # a SQL text is superseded if the same statement was issued again later
        last_index_by_sql: dict[str, int] = {}
        for u in uses.values():
            key = _norm_sql(u["sql"])
            last_index_by_sql[key] = max(last_index_by_sql.get(key, -1), u["index"])
        out = []
        cutoff = len(messages) - PROTECT_LAST
        for i, m in enumerate(messages[:cutoff]):
            if m.get("role") != "user":
                continue
            for block in m.get("content", []) or []:
                tr = block.get("toolResult")
                if not tr or tr.get("toolUseId") not in uses:
                    continue
                text = _text_of(block)
                if text.startswith(STUB_PREFIX) or len(text) < MIN_STUB_GAIN:
                    continue
                ref = _REF_RE.search(text)
                if ref is None and not include_inline:
                    continue
                use = uses[tr["toolUseId"]]
                rows = _ROWS_RE.search(text)
                out.append(
                    {
                        "block": block,
                        "text": text,
                        "index": i,
                        "sql": use["sql"],
                        "ref": ref.group(1) if ref else None,
                        "rows": int(rows.group(1)) if rows else None,
                        "superseded": last_index_by_sql.get(_norm_sql(use["sql"]), -1)
                        > use["index"],
                    }
                )
        out.sort(key=lambda c: (not c["superseded"], c["index"]))
        return out

    @staticmethod
    def _stub_text(cand: dict) -> str:
        head = " ".join(cand["sql"].split())[:SQL_HEAD]
        rows = f"{cand['rows']} rows" if cand["rows"] is not None else "result"
        if cand["ref"]:
            return (
                f"{STUB_PREFIX} run_sql: {head} → {rows}, kept whole in temp table "
                f"{cand['ref']} — query it with run_sql."
            )
        return (
            f"{STUB_PREFIX} run_sql: {head} → {rows} "
            "(inline result removed; re-run the SQL if needed)."
        )

    # session state round-trip (Strands session managers)
    def get_state(self) -> dict[str, Any]:
        state = super().get_state()
        state["stubbed"] = self.stubbed
        return state

    def restore_from_session(self, state: dict[str, Any]):
        out = super().restore_from_session(state)
        self.stubbed = int(state.get("stubbed", 0))
        return out
