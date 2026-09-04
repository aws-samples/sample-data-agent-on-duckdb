"""Scheduling layer: the agent-platform side of the session.

AgentCore Runtime owns session and resource management (one session = one
microVM = one engine). This package is what the platform calls into:
`runtime` resolves the caller's identity claims, streams the run as typed
envelopes and reports session counters; `cli` is the local one-shot entry.
"""
