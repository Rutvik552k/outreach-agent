# outreach-agent

Local-first GitHub outreach agent (ADR-001 v2.2). See `docs/adr/` for the
architecture and `docs/requirements.md` for scope.

## Install — from the hash-pinned lockfile only (C-5)

The agent's own supply chain is lockfile-pinned with hash verification
(ADR §3, sign-off condition C-5). Install **only** from `requirements.lock`;
never `pip install` loose ranges into the agent venv.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
.venv\Scripts\python.exe -m pip install --no-deps -e .
```

CI must use the same `--require-hashes` install. Regenerate the lockfile only
via:

```powershell
uv pip compile pyproject.toml --extra dev --generate-hashes -o requirements.lock
```

and review the diff before committing (typosquat / provenance check).

The editable install (`pip install --no-deps -e .` above) is what provides the
`outreach-agent` console script — without it neither `outreach-agent` nor
`python -m outreach_agent` will resolve. After install, both invocations are
equivalent (`__main__.py` delegates to `cli:main`):

```powershell
outreach-agent status
.venv\Scripts\python.exe -m outreach_agent status
```

## Host prerequisites

- Windows 11, Python 3.12+
- Docker Desktop (WSL2 backend) — mandatory for contribution prep (C8);
  the agent refuses bare-host execution of repo code.
- `git` on PATH.

## Commands

```
outreach-agent auth login     # OAuth App authorization-code + PKCE (S256)
outreach-agent discover       # search + score + policy pre-flight candidates
outreach-agent prepare        # prep next cleared candidate (sandbox-gated)
outreach-agent status         # contribution states + budget snapshot
outreach-agent approve-sync   # poll fork-draft approvals; publish approved
outreach-agent report         # PR outcomes, merge rate, graph credit, spend
outreach-agent resume         # clear a global pause (manual un-pause, §8)
```

## Pre-push gate (H-2)

`scripts\check.ps1` is the mandatory local gate before every push — it
mirrors the CI workflow (`.github/workflows/ci.yml`):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\check.ps1
```

1. **Lockfile conformance** (`scripts\verify_lock.py`): every package in the
   venv matches its `requirements.lock` pin exactly, the lock is fully
   hash-bearing, and nothing entered the venv outside the lock.
   *Honest limitation:* pip cannot re-verify archive hashes of
   already-installed packages offline — cryptographic hash verification
   happens at install time only, which is why fresh setups MUST use
   `pip install --require-hashes -r requirements.lock` (above) and CI
   installs that way on every run.
2. **Full default pytest lane.**
3. **The C-1 structural-incapability scanner by explicit path** — also its
   own named job in CI, so it cannot be deselected silently.

## Test lanes (ADR §12)

- Mocked CI lane (default): `python -m pytest` — all GitHub/LLM/sandbox via
  fakes at the C5/C8/LLM seams. Never calls real services.
- Live-smoke lane: manual, off-CI, real token, fixture repos only.
