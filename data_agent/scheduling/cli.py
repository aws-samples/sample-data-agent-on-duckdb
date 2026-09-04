"""Local one-shot mode: `python agent.py --as <tenant>:<role> "question"`."""

from __future__ import annotations

import os
import sys

from data_agent import governance
from data_agent.context import prompt

USAGE = 'usage: agent.py --as <tenant>:<role> "question"'


def cli_role(argv: list[str]) -> tuple[str, list[str]]:
    """Pull `--as <tenant>:<role>` out of the local CLI args.

    Local runs have no IdP, so the principal must be stated explicitly: the
    default role is the deny-all floor and would reject every query (that is
    the point — a missing identity is never the admin identity). AGENT_ROLE
    works too, for shells that would rather export it.
    """
    args = list(argv)
    role = os.environ.get("AGENT_ROLE", "")
    if "--as" in args:
        i = args.index("--as")
        if i + 1 >= len(args):
            sys.exit(USAGE)
        role = args[i + 1]
        del args[i : i + 2]
    if not role:
        sys.exit(
            "no identity: pass --as <tenant>:<role> (or set AGENT_ROLE). Known roles: "
            + ", ".join(sorted(k for k in governance.POLICIES if k != governance.DENY_ALL_ROLE))
        )
    if role not in governance.POLICIES:
        sys.exit(f"unknown role {role!r}; known roles: {sorted(governance.POLICIES)}")
    return role, args


def main(argv: list[str], agent) -> None:
    role, rest = cli_role(argv)
    governance.set_active_role(role)
    if not rest:
        sys.exit(USAGE)
    print(f"[principal: {governance.active_role()}]")
    prompt.ensure(agent)
    print(agent(" ".join(rest)))
