"""Offline lockfile-conformance check (H-2 / sign-off C-5, local half).

WHAT THIS VERIFIES (offline, against the running venv):
1. Every package pinned in requirements.lock is installed at EXACTLY the
   locked version (catches drift and ad-hoc upgrades).
2. Every installed distribution is accounted for by the lock (catches
   unpinned ingress — a package that entered the venv outside the lock),
   modulo the explicit exemptions below.
3. Every locked package carries at least one --hash entry (the lock stays
   hash-bearing, so a hash-verified install remains possible).

WHAT THIS CANNOT VERIFY — DOCUMENTED GAP, STATED HONESTLY:
pip offers no offline way to re-verify the sha256 of an ALREADY-INSTALLED
package against the lockfile: the locked hashes are over the downloaded
wheel/sdist archives, and installation unpacks them (the per-file RECORD
hashes are not comparable to the archive hashes). True hash verification
happens only AT INSTALL TIME via `pip install --require-hashes`, which is
enforced in CI (.github/workflows/ci.yml) and in the README's fresh-setup
instructions. This script closes the conformance half locally; the
cryptographic half requires a fresh `--require-hashes` install.

Exit code 0 = conformant; 1 = any mismatch (fail-closed for check.ps1).
"""

from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCKFILE = REPO_ROOT / "requirements.lock"

# Distributions legitimately present but never lockfile-pinned:
# - the project itself (installed `-e . --no-deps`)
# - the installer toolchain that ships with the venv
_EXEMPT = {"outreach-agent", "pip", "setuptools", "wheel"}

_PIN_RE = re.compile(r"^([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.+!\-]+)\s*\\?\s*$")
_HASH_RE = re.compile(r"^\s*--hash=sha256:[0-9a-f]{64}\s*\\?\s*$")


def _canon(name: str) -> str:
    """PEP 503 normalized name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(path: Path) -> dict[str, tuple[str, int]]:
    """{canonical name: (locked version, hash count)}."""
    pins: dict[str, tuple[str, int]] = {}
    current: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _PIN_RE.match(line)
        if m:
            current = _canon(m.group(1))
            pins[current] = (m.group(2), 0)
            continue
        if _HASH_RE.match(line) and current:
            version, n = pins[current]
            pins[current] = (version, n + 1)
    return pins


def main() -> int:
    if not LOCKFILE.exists():
        print(f"FAIL: lockfile not found at {LOCKFILE}")
        return 1

    pins = parse_lock(LOCKFILE)
    installed = {
        _canon(dist.metadata["Name"]): dist.version
        for dist in metadata.distributions()
        if dist.metadata["Name"]
    }

    problems: list[str] = []

    for name, (version, hash_count) in sorted(pins.items()):
        if hash_count == 0:
            problems.append(f"lock entry {name}=={version} carries no --hash (C-5)")
        if name not in installed:
            problems.append(f"locked {name}=={version} is NOT installed")
        elif installed[name] != version:
            problems.append(
                f"version drift: {name} locked=={version} installed=={installed[name]}"
            )

    for name, version in sorted(installed.items()):
        if name not in pins and name not in _EXEMPT:
            problems.append(
                f"unpinned ingress: {name}=={version} installed but absent "
                "from requirements.lock"
            )

    if problems:
        print(f"FAIL: venv does not conform to requirements.lock "
              f"({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        print("\nRemediation: rebuild the venv per README "
              "(pip install --require-hashes -r requirements.lock).")
        return 1

    print(f"OK: {len(pins)} locked packages all installed at locked versions, "
          f"all hash-bearing; no unpinned ingress "
          f"({len(installed)} distributions checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
