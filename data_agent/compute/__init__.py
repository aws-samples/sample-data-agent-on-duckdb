"""Compute layer: the in-process DuckDB engine and its read-only gate.

`engine` owns the one connection per agent instance (lazily created, in
memory, dies with the session), mounts the Storage layer's views and legs, and
serializes access with a single lock (invariant 6). `gate` decides which
statement shapes may reach the engine at all (invariant 1).
"""
