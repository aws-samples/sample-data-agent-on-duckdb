"""Metric definitions as customer-owned Git assets.

The single source of truth for a scenario's registered KPIs is
`semantic/<scenario>.yaml` at the repository root (open format, in the
customer's Git, changed through review + the asset regression gate), not
Python code. This module loads one scenario's file into the internal
representation (`model.Metric` — the dataclass stays, as the validated
in-process form) at cold start.

Fail-closed, exactly like a malformed governance policy: a missing file, an
unparseable document, an unknown field or a definition that fails
`model.validate` raises `MetricError` at import — a bad asset never serves a
question. `SEMANTIC_ASSETS_DIR` overrides the directory (tests, split repos).

A leaf module like `model`: imports nothing from the rest of the package
besides `model`, so scenarios can call it without an import cycle.
"""

from __future__ import annotations

import os
from dataclasses import fields
from pathlib import Path

import yaml

from .model import Metric, MetricError, validate

_DEFAULT_DIR = Path(__file__).resolve().parents[2] / "semantic"
_FIELDS = {f.name for f in fields(Metric)}


def assets_dir() -> Path:
    return Path(os.environ.get("SEMANTIC_ASSETS_DIR") or _DEFAULT_DIR)


def load_scenario_metrics(scenario: str) -> dict[str, Metric]:
    """`semantic/<scenario>.yaml` → validated metrics, keyed by name."""
    path = assets_dir() / f"{scenario}.yaml"
    where = f"semantic asset {path.name}"
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise MetricError(
            f"{where}: not found under {path.parent} — the metric "
            "definitions are Git assets and ship with the deployment"
        ) from e
    except yaml.YAMLError as e:
        raise MetricError(f"{where}: does not parse ({e})") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("metrics"), list):
        raise MetricError(f"{where}: must be a mapping with a `metrics` list")
    out: dict[str, Metric] = {}
    for item in doc["metrics"]:
        if not isinstance(item, dict):
            raise MetricError(f"{where}: every metrics entry must be a mapping")
        unknown = set(item) - _FIELDS
        if unknown:
            raise MetricError(
                f"{where}: metric {item.get('name', '?')!r}: unknown fields {sorted(unknown)}"
            )
        # YAML lists → the dataclass's tuple fields; everything else as-is.
        # Unknown/missing required fields surface as TypeError → MetricError.
        for key in ("default_dimensions", "filters", "fixed_filters"):
            if key in item:
                if not isinstance(item[key], list):
                    raise MetricError(
                        f"{where}: metric {item.get('name', '?')!r}: {key} must be a list"
                    )
                item[key] = tuple(item[key])
        try:
            m = Metric(**item)
        except TypeError as e:
            raise MetricError(f"{where}: metric {item.get('name', '?')!r}: {e}") from e
        validate(m)  # fail-stop on a malformed definition, like a policy
        if m.name in out:
            raise MetricError(f"{where}: metric {m.name!r} is defined twice")
        out[m.name] = m
    return out
