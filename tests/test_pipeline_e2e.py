"""End-to-end mocked-lane pipeline test (ADR §12): discovered → scored →
policy-cleared → prepared → ci-green → draft-on-fork → approved →
upstream-open, with FakeSandboxRunner (C8 seam), FakeGitHubClient (C5 seam),
FakeLLMClient and FakeGitRunner. Asserts the audit chain verifies end-to-end
and the budget ledger recorded exactly the F-06 content creations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import FORK, FORK_OWNER, UPSTREAM, FakeGitHubClient, make_pull_ref
from outreach_agent.config import Config
from outreach_agent.discovery import discover
from outreach_agent.github_gateway import GitHubGateway
from outreach_agent.fix_generator import FakeFixGenerator
from outreach_agent.llm_gateway import FakeLLMClient, LLMGateway
from outreach_agent.persistence import Database
from outreach_agent.policy import preflight, recheck_policy
from outreach_agent.prep import FakeGitRunner, prepare_contribution
from outreach_agent.publisher import submit_for_approval, sync_approval
from outreach_agent.sandbox import FakeSandboxRunner
from outreach_agent.state_machine import ContributionStore, State

GOOD_DIFF = """diff --git a/src/lib.py b/src/lib.py
--- a/src/lib.py
+++ b/src/lib.py
@@ -1,2 +1,3 @@
 def parse(x):
-    return x.split(',')
+    if not x:
+        return []
"""


def _issue_item(repo: str, number: int) -> dict[str, Any]:
    return {
        "repository_url": f"https://api.github.com/repos/{repo}",
        "number": number,
        "title": "Crash when parsing empty input",
        "body": "parse('') raises IndexError",
        "html_url": f"https://github.com/{repo}/issues/{number}",
        "labels": [{"name": "good first issue"}],
        "comments": 4,
        "reactions": {"total_count": 3},
    }


def test_full_pipeline_discovered_to_upstream_open(
        gateway: GitHubGateway, fake_client: FakeGitHubClient,
        db: Database, config: Config, tmp_path: Path) -> None:
    # -- [1] discovery ---------------------------------------------------------
    fake_client.search_results = [_issue_item(UPSTREAM, 12)]
    candidates = discover(gateway, db, config)
    assert len(candidates) >= 1
    candidate = candidates[0]
    assert candidate.contribution_type == "bugfix-static-analysis"

    # -- [2] policy pre-flight -------------------------------------------------
    fake_client.repo_files[f"{UPSTREAM}/CONTRIBUTING.md"] = "PRs with tests welcome."
    verdict = preflight(gateway, db, config,
                        repo_full_name=candidate.repo_full_name,
                        candidate_id=candidate.candidate_id)
    assert verdict.verdict == "cleared"

    # -- state machine through prep ---------------------------------------------
    store = ContributionStore(db)
    cid = store.create(candidate_id=candidate.candidate_id,
                       repo_full_name=candidate.repo_full_name)
    store.transition(cid, State.SCORED, reason=f"score={candidate.score.total}")
    store.transition(cid, State.POLICY_CLEARED, reason="pre-flight cleared",
                     fields={"fork_full_name": FORK})

    # -- [3] prep: fix-generation (FixGenerator seam) + sandbox validation (C8
    # fake) — ADR-002: fix-gen mutates the clone; prep captures `git diff`.
    llm_client = FakeLLMClient(["Fix empty-input crash\nHandles ''."])
    llm = LLMGateway(llm_client, db, config)
    sandbox = FakeSandboxRunner()
    git = FakeGitRunner({"rev-parse": "abc123\n", "diff": GOOD_DIFF})
    prep_result = prepare_contribution(
        db=db, store=store, llm=llm, fix_generator=FakeFixGenerator(),
        sandbox=sandbox, git=git, config=config,
        contribution_id=cid,
        fork_clone_url=f"https://github.com/{FORK}.git",
        issue_title="Crash when parsing empty input",
        issue_body="parse('') raises IndexError",
        issue_number=candidate.issue_number,
        issue_url=candidate.issue_url,
        stack=candidate.stack,
        work_root=tmp_path / "work",
    )
    assert prep_result.state == State.CI_GREEN
    prepared = prep_result.prepared
    assert prepared is not None
    assert "AI-assistance disclosure" in prepared.pr_text.body_md  # NFR-6

    # -- [4] approval flow: intra-fork draft PR ---------------------------------
    draft_number = submit_for_approval(
        db=db, store=store, gateway=gateway, git=git, config=config,
        contribution_id=cid, prepared=prepared,
        fork_full_name=FORK, fork_default_branch="main",
        upstream_full_name=UPSTREAM, work_dir=None,
    )
    assert store.get_state(cid) == State.DRAFT_ON_FORK

    # human approves on github.com (timeline event by the fork owner)
    fake_client.timeline = [
        {"event": "labeled", "actor": {"login": FORK_OWNER},
         "label": {"name": config.label_approve}, "id": 4242},
    ]

    # -- [5] publisher: gate (incl. C-2 cross-check) → upstream PR → close draft -
    upstream_pr = make_pull_ref(
        number=991, state="open", draft=False,
        base_repo_full_name=UPSTREAM, head_repo_full_name=FORK,
    )
    fake_client.pull_response = make_pull_ref(state="open")  # draft re-read
    outcome_holder = []

    def _sync() -> None:
        outcome_holder.append(sync_approval(
            db=db, store=store, gateway=gateway, config=config,
            contribution_id=cid, fork_owner=FORK_OWNER, fork_full_name=FORK,
            draft_pr_number=draft_number, upstream_full_name=UPSTREAM,
            upstream_base_branch="main",
            prepared_title=prepared.pr_text.title,
            prepared_body=prepared.pr_text.body_md,
            head_branch=prepared.branch,
            policy_recheck=lambda: recheck_policy(
                gateway, db, config, repo_full_name=UPSTREAM,
                candidate_id=candidate.candidate_id),
        ))

    # the fake returns pull_response for every create; switch it to the
    # upstream shape right before publish
    fake_client.pull_response = upstream_pr
    _sync()
    outcome = outcome_holder[0]
    assert outcome.status == "published"
    assert outcome.upstream_pr_number == 991
    assert store.get_state(cid) == State.UPSTREAM_OPEN

    # -- cross-stack assertions ---------------------------------------------------
    # audit chain intact end-to-end (V4/FM12)
    db.verify_chains()

    # budget consumed exactly the F-06 content creations, in order
    kinds = [r["kind"] for r in db.conn.execute(
        "SELECT kind FROM rate_budget ORDER BY seq")]
    assert kinds == ["fork_draft_pr", "upstream_pr", "fork_draft_close"]

    # gate confirmation audited as the USER actor with the GitHub event id (AC-3)
    gate_row = db.conn.execute(
        "SELECT actor, outcome_json FROM audit_log"
        " WHERE endpoint='gate:pre-publish(F-05)' AND phase='confirmed'"
    ).fetchone()
    assert gate_row["actor"] == "user"
    assert "4242" in gate_row["outcome_json"]

    # LLM spend: under ADR-002 fix-generation no longer routes a text
    # completion through the gateway (the FixGenerator mutates the clone and
    # prep captures `git diff`), so only the pr-text call is ledgered here.
    spend = db.conn.execute(
        "SELECT COUNT(*) AS n, SUM(cost_usd) AS total FROM llm_spend").fetchone()
    assert spend["n"] == 1 and spend["total"] > 0

    # daily budget: a second upstream PR today is denied (AC-6)
    from outreach_agent.budget import BudgetTracker
    auth = BudgetTracker(db, config).authorize(
        "content_creation", kind="upstream_pr", endpoint="x")
    assert auth.granted is False
    assert "daily upstream-PR budget" in auth.reason
