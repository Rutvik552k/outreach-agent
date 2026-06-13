from __future__ import annotations

from outreach_agent.config import Config
from outreach_agent.persistence import Database, new_ulid, utc_now_iso
from outreach_agent.report import build_report, render_report
from outreach_agent.state_machine import ContributionStore, State


def test_report_surfaces_graph_credit_and_merge_rate(db: Database,
                                                     config: Config) -> None:
    store = ContributionStore(db)
    for outcome, graph in (("merged", "credited"), ("merged", "missing"),
                           ("closed", None), ("merged", None), ("closed", None)):
        cid = store.create(candidate_id=None, repo_full_name="acme/some-lib")
        with db.transaction():
            db.conn.execute(
                "INSERT INTO kpi_outcomes(outcome_id, contribution_id, outcome,"
                " counts_in_merge_rate, graph_credit, recorded_at)"
                " VALUES(?,?,?,1,NULL,?)",
                (new_ulid(), cid, outcome, utc_now_iso()),
            )
            if graph:
                db.conn.execute(
                    "INSERT INTO kpi_outcomes(outcome_id, contribution_id, outcome,"
                    " counts_in_merge_rate, graph_credit, recorded_at)"
                    " VALUES(?,?,?,0,?,?)",
                    (new_ulid(), cid, f"graph-{graph}", graph, utc_now_iso()),
                )
    report = build_report(db, config)
    assert report.merge_rate == 0.6  # 3 merged / 5 decided
    assert report.graph_credited == 1
    assert report.graph_missing == 1
    text = render_report(report, config)
    assert "merge rate: 60%" in text
    assert "graph-missing (partial failure): 1" in text


def test_report_counts_sandbox_unfit(db: Database, config: Config) -> None:
    store = ContributionStore(db)
    cid = store.create(candidate_id=None, repo_full_name="acme/some-lib")
    store.transition(cid, State.SCORED)
    store.transition(cid, State.POLICY_CLEARED)
    store.transition(cid, State.PREPARED)
    store.transition(cid, State.SANDBOX_UNFIT, reason="test suite needs network (F-10)")
    report = build_report(db, config)
    assert report.sandbox_unfit_count == 1
    assert "Sandbox-unfit count (F-10): 1" in render_report(report, config)
