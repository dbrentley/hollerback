#!/usr/bin/env python3
"""Read-only auto-allow while this session owes a peer an answer.

The failure mode this prevents: a peer question wakes an idle session, the
session tries to Read a file to answer it, hits a permission modal, and hangs
forever because nobody is at that keyboard. From the outside that is
indistinguishable from "still thinking".

Policy (deliberately narrow):
  * only Read / Grep / Glob -- no writes, no Bash, no network
  * only while a question is actually open (entries expire)
  * otherwise stay silent and let normal permissions apply

It never denies anything. A hook that could deny would be able to break the
user's own work in their own session; this one can only skip a prompt for
tools that cannot change anything.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin"))

ALLOWED = {"Read", "Grep", "Glob", "NotebookRead"}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return 0  # fail open: never block the session on our account

    tool = payload.get("tool_name", "")
    if tool not in ALLOWED:
        return 0

    try:
        from _common import has_open_question  # noqa: PLC0415

        if not has_open_question():
            return 0
    except Exception:  # noqa: BLE001
        return 0

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": (
                        "hollerback: answering a peer session's question; "
                        "read-only tools are auto-allowed so the session does "
                        "not stall on a prompt with nobody at the keyboard."
                    ),
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
