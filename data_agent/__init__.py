"""Data agent on in-process DuckDB — the five-layer architecture as packages.

    storage/      where data lives and how a name resolves (S3 legs, scenarios)
    governance/   authorization only: principal → role → policy, SQL rewrite
    context/      what the model sees and what the engine is asked
    compute/      the in-process engine and its read-only gate
    scheduling/   the agent-platform side: entrypoint, identity, transport, CLI
    pipeline.py   one statement through the layers, as ordered stages
    audit.py      one EMF audit line per statement (cross-cutting)

Dependencies point downward only: scheduling → pipeline → context, governance
→ compute → storage. `agent.py` at the repository root is the composition root.
"""
