"""hollerback broker package.

Bump __version__ (and plugin.json to match) whenever the wire behaviour the
plugin depends on changes. install.sh compares the two and says so when they
differ; leaving it alone after a behaviour change makes that check report a
match against a broker that predates the change.
"""

import os
import sys

__version__ = "0.5.2"

_LEGACY_PREFIX = "AGENTSHARE_"
_PREFIX = "HOLLERBACK_"


def _adopt_legacy_env() -> list[str]:
    """Honour pre-rename AGENTSHARE_* variables, and say so.

    The project was renamed from agentshare. Renaming the variables without a
    fallback fails SILENTLY, which is the worst possible shape for this bug: a
    stale ~/.config/hollerback/broker.env still says AGENTSHARE_BIND=<private
    ip>, the renamed code reads HOLLERBACK_BIND, every old name is ignored, and
    the broker comes up "active" bound to loopback where no peer can reach it.
    No error, no warning -- just built-in defaults and an unreachable service.

    This lives in the package __init__ rather than app.py because store.py reads
    HOLLERBACK_DB at import time and app.py imports store before reading its own
    config; __init__ is the only place guaranteed to run before both.
    """
    adopted = []
    for old, value in sorted(os.environ.items()):
        if not old.startswith(_LEGACY_PREFIX) or not value:
            continue
        new = _PREFIX + old[len(_LEGACY_PREFIX) :]
        if not os.environ.get(new):
            os.environ[new] = value
            adopted.append(f"{old} -> {new}")
    return adopted


_ADOPTED_LEGACY_ENV = _adopt_legacy_env()

if _ADOPTED_LEGACY_ENV:
    print(
        "[hollerback] WARNING: honouring deprecated AGENTSHARE_* environment "
        "variables (the project was renamed):\n  "
        + "\n  ".join(_ADOPTED_LEGACY_ENV)
        + "\n[hollerback] Rename them -- this fallback will not last forever:\n"
        "    sed -i 's/^AGENTSHARE_/HOLLERBACK_/' ~/.config/hollerback/broker.env\n"
        "    systemctl --user restart hollerback-broker",
        file=sys.stderr,
        flush=True,
    )
