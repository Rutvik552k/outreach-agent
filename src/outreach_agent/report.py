"""Weekly Reporter — component §2[9] (FR-6, minimal MVP scope per chain step:
PR outcomes, merge rate, graph-credit outcomes, sandbox-unfit counts, plus
LLM spend, manual graph-verify checklist items, pending review-response
drafts (§2[6]/AC-5) and profile-growth proposals (§2[7]/FR-5))."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config
from .persistence import Database


@dataclass(frozen=True)
class Report:
    states: dict[str, int]
    decided_outcomes: dict[str, int]
    merge_rate: float | None
    merge_rate_window: int
    graph_credited: int
    graph_missing: int
    manual_graph_checks: int
    sandbox_unfit_count: int
    llm_spend_month_usd: float
    global_pause: str | None
    pending_review_drafts: int
    review_replies_posted: int
    profile_actions: dict[str, int]


def build_report(db: Database, config: Config) -> Report:
    states = {
        row["state"]: row["n"]
        for row in db.conn.execute(
            "SELECT state, COUNT(*) AS n FROM contributions GROUP BY state")
    }
    decided = {
        row["outcome"]: row["n"]
        for row in db.conn.execute(
            "SELECT outcome, COUNT(*) AS n FROM kpi_outcomes GROUP BY outcome")
    }
    window_rows = db.conn.execute(
        "SELECT outcome FROM kpi_outcomes WHERE counts_in_merge_rate=1"
        " ORDER BY recorded_at DESC LIMIT ?",
        (config.merge_rate_window,),
    ).fetchall()
    merge_rate = None
    if len(window_rows) >= config.merge_rate_min_outcomes:
        merge_rate = sum(1 for r in window_rows if r["outcome"] == "merged") / len(window_rows)
    manual = db.conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log"
        " WHERE endpoint='graph-verify:manual-check'"
    ).fetchone()["n"]
    sandbox_unfit = db.conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE endpoint='state:transition'"
        " AND outcome_json LIKE '%sandbox-unfit%'"
    ).fetchone()["n"]
    month_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    spend = db.conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM llm_spend WHERE ts >= ?",
        (month_start.isoformat(timespec="microseconds"),),
    ).fetchone()["total"]
    pending_drafts = db.conn.execute(
        "SELECT COUNT(*) AS n FROM review_threads"
        " WHERE response_state='pending' AND draft_response IS NOT NULL"
    ).fetchone()["n"]
    replies_posted = db.conn.execute(
        "SELECT COUNT(*) AS n FROM review_threads WHERE response_state='posted'"
    ).fetchone()["n"]
    profile_actions = {
        row["kind"]: row["n"]
        for row in db.conn.execute(
            "SELECT kind, COUNT(*) AS n FROM profile_actions"
            " WHERE state='proposed' GROUP BY kind")
    }
    return Report(
        states=states,
        decided_outcomes=decided,
        merge_rate=merge_rate,
        merge_rate_window=len(window_rows),
        graph_credited=decided.get("graph-credited", 0),
        graph_missing=decided.get("graph-missing", 0),
        manual_graph_checks=int(manual),
        sandbox_unfit_count=int(sandbox_unfit),
        llm_spend_month_usd=float(spend),
        global_pause=db.global_pause_reason(),
        pending_review_drafts=int(pending_drafts),
        review_replies_posted=int(replies_posted),
        profile_actions=profile_actions,
    )


def render_report(report: Report, config: Config) -> str:
    lines = ["# Outreach agent report", ""]
    if report.global_pause:
        lines.append(f"!! GLOBAL PAUSE ACTIVE: {report.global_pause}")
        lines.append("")
    lines.append("## Contribution states")
    for state, n in sorted(report.states.items()):
        lines.append(f"- {state}: {n}")
    lines.append("")
    lines.append("## Decided PR outcomes")
    for outcome, n in sorted(report.decided_outcomes.items()):
        lines.append(f"- {outcome}: {n}")
    if report.merge_rate is not None:
        lines.append(
            f"- merge rate: {report.merge_rate:.0%} over trailing "
            f"{report.merge_rate_window} (auto-pause < "
            f"{config.merge_rate_pause_threshold:.0%}, §8)")
    else:
        lines.append(
            f"- merge rate: n/a (< {config.merge_rate_min_outcomes} decided outcomes)")
    lines.append("")
    lines.append("## Visibility KPI (F-01: credited vs merged-uncredited)")
    lines.append(f"- graph-credited: {report.graph_credited}")
    lines.append(f"- graph-missing (partial failure): {report.graph_missing}")
    lines.append(f"- manual graph checks pending: {report.manual_graph_checks}")
    lines.append("")
    lines.append("## Review monitor (§2[6], AC-5)")
    lines.append(
        f"- pending review-response drafts (need /approve-reply): "
        f"{report.pending_review_drafts}")
    lines.append(f"- review replies posted: {report.review_replies_posted}")
    lines.append("")
    lines.append("## Profile growth proposals (FR-5, AC-7)")
    if report.profile_actions:
        for kind, n in sorted(report.profile_actions.items()):
            lines.append(f"- {kind}: {n}")
    else:
        lines.append("- none yet — run `outreach-agent profile`")
    lines.append("")
    lines.append(f"## Sandbox-unfit count (F-10): {report.sandbox_unfit_count}")
    lines.append(
        f"## LLM spend this month: ${report.llm_spend_month_usd:.2f} "
        f"(cap ${config.llm_monthly_spend_cap_usd:.2f})")
    return "\n".join(lines)
