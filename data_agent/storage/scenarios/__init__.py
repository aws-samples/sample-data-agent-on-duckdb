"""Scenario packages. Each one is a `Scenario` built from its own env vars.

Adding a data domain = adding a module here that exposes `SCENARIO` and
listing it in `storage.registry.SCENARIOS`. Nothing else changes: the engine
mounts its views, the cost gate and the data map read its partitioned
sources, the prompt gets its doc, governance gets its default policies.
"""
