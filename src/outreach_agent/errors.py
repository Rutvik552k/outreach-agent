"""Typed error model. RFC-7807-inspired record per ADR-001 §11."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProblemDetail:
    type: str
    title: str
    detail: str
    retriable: bool
    source_component: str


class OutreachError(Exception):
    """Base for all agent errors. Carries an RFC-7807-inspired problem record."""

    type_uri: str = "urn:outreach-agent:error:generic"
    title: str = "Outreach agent error"
    retriable: bool = False
    source_component: str = "core"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.problem = ProblemDetail(
            type=self.type_uri,
            title=self.title,
            detail=detail,
            retriable=self.retriable,
            source_component=self.source_component,
        )


class SyncRootError(OutreachError):
    type_uri = "urn:outreach-agent:error:sync-root"
    title = "Database path is inside a cloud-sync root (F-07)"
    source_component = "config"


class ChainIntegrityError(OutreachError):
    type_uri = "urn:outreach-agent:error:chain-integrity"
    title = "Hash chain verification failed (FM12/V4)"
    source_component = "persistence"


class IllegalTransitionError(OutreachError):
    type_uri = "urn:outreach-agent:error:illegal-transition"
    title = "Illegal contribution state transition"
    source_component = "state_machine"


class BudgetDeniedError(OutreachError):
    type_uri = "urn:outreach-agent:error:budget-denied"
    title = "Mutation denied by rate-budget tracker (C7)"
    retriable = True
    source_component = "budget"


class GlobalPauseError(OutreachError):
    type_uri = "urn:outreach-agent:error:global-pause"
    title = "Agent is globally paused"
    source_component = "budget"


class IntraForkInvariantError(OutreachError):
    type_uri = "urn:outreach-agent:error:intra-fork-invariant"
    title = "Fork draft PR is not intra-fork (F-03)"
    source_component = "github_gateway"


class ApprovalVerificationError(OutreachError):
    type_uri = "urn:outreach-agent:error:approval-verification"
    title = "Approval signal failed actor-binding verification (V2)"
    source_component = "approval"


class PrePublishGateError(OutreachError):
    type_uri = "urn:outreach-agent:error:pre-publish-gate"
    title = "Atomic pre-publish gate failed (F-05)"
    source_component = "approval"


class SandboxUnavailableError(OutreachError):
    type_uri = "urn:outreach-agent:error:sandbox-unavailable"
    title = "Execution sandbox unavailable — refusing bare-host execution (C8/V1)"
    source_component = "sandbox"


class WorkflowFileTouchError(OutreachError):
    type_uri = "urn:outreach-agent:error:workflow-file-touch"
    title = "Diff touches .github/workflows/** (V3/FM11)"
    source_component = "diff_checks"


class DiffInvariantError(OutreachError):
    type_uri = "urn:outreach-agent:error:diff-invariant"
    title = "Diff violates a C3 construction invariant"
    source_component = "diff_checks"


class GitHubMutationError(OutreachError):
    type_uri = "urn:outreach-agent:error:github-mutation"
    title = "GitHub mutation failed"
    retriable = True
    source_component = "github_gateway"


class StructuralIncapabilityError(OutreachError):
    type_uri = "urn:outreach-agent:error:structural-incapability"
    title = "Mutation refused: outside the gateway's closed capability set (C4 v2.1/C-1)"
    source_component = "github_gateway"


class SecretLeakError(OutreachError):
    type_uri = "urn:outreach-agent:error:secret-leak"
    title = "Outbound prompt matched the secret deny-regex (NFR-6) — fail-closed"
    source_component = "llm_gateway"


class LlmBudgetError(OutreachError):
    type_uri = "urn:outreach-agent:error:llm-budget"
    title = "Monthly LLM spend cap reached (F-13) — hard stop"
    source_component = "llm_gateway"


class LlmUnavailableError(OutreachError):
    type_uri = "urn:outreach-agent:error:llm-unavailable"
    title = "Claude API unavailable after retries (FM9)"
    retriable = True
    source_component = "llm_gateway"


class LlmBackendError(OutreachError):
    """The configured LLM backend cannot be constructed (NFR-7).

    Raised by the backend factory: unknown ``llm_backend`` value, or
    backend=claude-code with no ``claude`` CLI on PATH. Non-retriable —
    fixing it requires operator action (install the CLI / fix config),
    and ``detail`` always names that action.
    """

    type_uri = "urn:outreach-agent:error:llm-backend"
    title = "LLM backend misconfigured or unavailable (NFR-7)"
    source_component = "llm_gateway"


class LlmCliError(OutreachError):
    """A Claude Code CLI invocation failed (NFR-7).

    Non-zero exit or malformed output: non-retriable by default, because
    the observed failure modes (bad model, auth/subscription problems)
    do not heal on retry. Timeouts raise LlmUnavailableError instead
    (retriable=True) — slow/overloaded service is the retriable cause.
    """

    type_uri = "urn:outreach-agent:error:llm-cli"
    title = "Claude Code CLI invocation failed (NFR-7)"
    source_component = "llm_gateway"


class GitOperationError(OutreachError):
    type_uri = "urn:outreach-agent:error:git-operation"
    title = "Local git operation failed"
    source_component = "prep"


class OAuthError(OutreachError):
    type_uri = "urn:outreach-agent:error:oauth"
    title = "OAuth login flow failed (V6 hardening enforced)"
    source_component = "oauth"


class CredentialError(OutreachError):
    """A required credential is missing from the keyring store (DEF-001).

    Must be an OutreachError subtype so the missing-credential path flows
    through ``main()``'s sanitized one-line handler instead of escaping as a
    bare ``LookupError`` traceback. ``detail`` carries per-credential
    remediation text (DEF-002) — never "re-run the command that just failed".
    """

    type_uri = "urn:outreach-agent:error:credential-missing"
    title = "Required credential missing (NFR-3)"
    source_component = "tokens"
