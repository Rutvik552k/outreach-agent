"""`python -m outreach_agent` entry point (DEF-005).

Delegates to the same `cli:main` the `outreach-agent` console script uses, so
the module-invocation surface and the installed script are behaviorally
identical.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
