"""Semantic layer — what a business number means, in code.

The layer answers one question the other layers do not: given a name like
"inbound ETH" or "unblended spend", which rows, which conversion, which unit.
Governance decides who may see a row; the semantic layer decides what a figure
is. Both are policy, both are enforced by rewriting the statement before the
engine sees it, and both are deterministic code rather than model judgement.

Two modules:
  metrics.py — the registry: one frozen definition per number
  compile.py — the compiler and the `metric(...)` SQL surface it is called
               through, plus the role-filtered prompt catalogue

It plugs into the pipeline as a stage at index 1 (after the read-only gate,
before governance). Dependencies point downward only: this package reads the
Storage registry to know which sources are partitioned, and nothing else.
"""

from __future__ import annotations

from .compile import (
    CALL_NAME,
    MetricNotCovered,
    catalog_doc,
    compile_metric,
    expand,
    source_footer,
    visible_metrics,
)
from .metrics import METRICS, TIME_WINDOWS

__all__ = [
    "CALL_NAME",
    "METRICS",
    "TIME_WINDOWS",
    "MetricNotCovered",
    "catalog_doc",
    "compile_metric",
    "expand",
    "source_footer",
    "visible_metrics",
]
