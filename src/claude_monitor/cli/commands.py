"""Command implementations for token-track."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from typing import Any

from rich.console import Console
from rich.table import Table

from claude_monitor.cli.formatters import (
    create_burn_table,
    create_report_table,
    create_session_table,
    format_cost,
    format_tokens,
    print_json,
)
from claude_monitor.core.models import BurnRateInfo, TokenCounts
from claude_monitor.core.pricing import PricingCalculator
from claude_monitor.data.analysis import (
    BurnRateCalculator,
    CalibrationStore,
    ContextGauge,
    TokenLedger,
)
from claude_monitor.data.analyzer import SessionParser
from claude_monitor.data.reader import (
    load_usage_entries,
    load_usage_entries_with_metadata,
)
from claude_monitor.utils.time_utils import TimezoneHandler, get_system_timezone

CALIBRATION_POOL_ORDER = ("weekly-all", "weekly-sonnet", "session")


def cmd_report(args: argparse.Namespace) -> int:
    """Render the usage report command."""
    entries_with_metadata = load_usage_entries_with_metadata(
        data_path=args.data_path,
        hours_back=_days_to_hours(args.days),
    )
    if not entries_with_metadata:
        return _no_data(
            "No usage data found for the requested time window.",
            args.json,
        )

    entries = [entry for entry, _, _ in entries_with_metadata]
    project_map = _build_project_map(entries_with_metadata)
    ledger = TokenLedger()
    pricing = PricingCalculator()

    days = ledger.aggregate_by_day(entries, project_map=project_map)
    models = ledger.aggregate_by_model(entries)
    projects = ledger.aggregate_by_project(entries, project_map=project_map)
    totals = _sum_token_counts(day.tokens for day in days)
    total_cost = round(sum(day.cost_usd for day in days), 6)
    total_messages = sum(day.message_count for day in days)

    calibration_store = CalibrationStore()
    calibration_pool, estimated_limit = _select_calibration(calibration_store)
    estimated_percents = {
        day.date: _usage_percent(
            calibration_store,
            calibration_pool,
            day.tokens.total_tokens,
        )
        for day in days
    }

    model_payload = {
        model: {
            **_serialize_token_counts(counts),
            "cost_usd": pricing.calculate_cost(model=model, tokens=counts),
            "estimated_percent": _usage_percent(
                calibration_store,
                calibration_pool,
                counts.total_tokens,
            ),
        }
        for model, counts in models.items()
    }
    project_payload = {
        name: {
            **_serialize_token_counts(project.tokens),
            "message_count": project.message_count,
            "cost_usd": project.cost_usd,
            "sessions": project.sessions,
            "estimated_percent": _usage_percent(
                calibration_store,
                calibration_pool,
                project.tokens.total_tokens,
            ),
        }
        for name, project in projects.items()
    }
    totals_payload = {
        **_serialize_token_counts(totals),
        "cost_usd": total_cost,
        "message_count": total_messages,
        "estimated_percent": _usage_percent(
            calibration_store,
            calibration_pool,
            totals.total_tokens,
        ),
        "calibration_pool": calibration_pool,
        "estimated_limit_tokens": estimated_limit,
    }

    if args.json:
        print_json(
            {
                "days": [
                    {
                        "date": day.date.isoformat(),
                        **_serialize_token_counts(day.tokens),
                        "cost_usd": day.cost_usd,
                        "message_count": day.message_count,
                        "models": {
                            model: _serialize_token_counts(counts)
                            for model, counts in day.by_model.items()
                        },
                        "projects": {
                            name: _serialize_token_counts(counts)
                            for name, counts in day.by_project.items()
                        },
                        "estimated_percent": estimated_percents.get(day.date),
                    }
                    for day in days
                ],
                "models": model_payload,
                "projects": project_payload,
                "totals": totals_payload,
            }
        )
        return 0

    console = Console()
    console.print(
        create_report_table(
            days,
            totals=totals,
            total_cost=total_cost,
            estimated_percents=estimated_percents,
        )
    )
    if calibration_pool and estimated_limit is not None:
        console.print(
            "Estimated usage percent uses "
            f"'{calibration_pool}' median limit of {format_tokens(estimated_limit)} tokens."
        )
    console.print(_create_model_summary_table(model_payload))
    console.print(_create_project_summary_table(project_payload))
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    """Render the session breakdown command."""
    parser = SessionParser()
    sessions = parser.parse_sessions(args.data_path)
    sessions = _filter_sessions_by_days(sessions, args.days)
    if args.active:
        sessions = parser.get_active_sessions(sessions, threshold_minutes=30)

    if not sessions:
        return _no_data(
            "No sessions found for the requested filters.",
            args.json,
        )

    tz_handler = TimezoneHandler(get_system_timezone())

    if args.json:
        print_json(
            [
                {
                    "session_id": session.session_id,
                    "project": session.project,
                    "models": session.models,
                    "start": tz_handler.to_timezone(session.start_time).isoformat(),
                    "end": tz_handler.to_timezone(session.end_time).isoformat(),
                    "duration_minutes": round(
                        (session.end_time - session.start_time).total_seconds() / 60,
                        2,
                    ),
                    **_serialize_token_counts(session.tokens),
                    "message_count": session.message_count,
                    "cost_usd": session.cost_usd,
                    "context_size": session.context_size,
                    "status": _session_status(session),
                    "is_active": session.is_active,
                    "is_subagent": session.is_subagent,
                }
                for session in sessions
            ]
        )
        return 0

    Console().print(create_session_table(sessions, timezone_handler=tz_handler))
    return 0


def cmd_burn(args: argparse.Namespace) -> int:
    """Render the burn rate command."""
    days = max(args.days, 1)
    entries, _ = load_usage_entries(
        data_path=args.data_path,
        hours_back=max(days, 28) * 24,
    )
    if not entries:
        return _no_data(
            "No usage data found for burn-rate analysis.",
            args.json,
        )

    calculator = BurnRateCalculator()
    current = calculator.current_rate(entries, window_hours=days * 24)
    baseline = calculator.baseline_rate(entries, weeks=4)
    ratio = _compute_ratio(current, baseline)
    anomaly = ratio is not None and ratio > 2.0
    current = _augment_burn_rate(current, ratio, anomaly, baseline)

    if args.json:
        print_json(
            {
                "current": _serialize_burn_rate(current),
                "baseline": _serialize_burn_rate(baseline),
                "ratio": ratio,
                "anomaly": anomaly,
                "current_window_days": days,
            }
        )
        return 0

    Console().print(create_burn_table(current, baseline))
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Save a calibration point."""
    tokens_consumed = args.tokens
    if tokens_consumed is None:
        entries, _ = load_usage_entries(
            data_path=args.data_path,
            hours_back=_days_to_hours(args.days),
        )
        tokens_consumed = sum(
            entry.input_tokens
            + entry.output_tokens
            + entry.cache_creation_tokens
            + entry.cache_read_tokens
            for entry in entries
        )
        if tokens_consumed <= 0:
            return _no_data(
                "No recent usage data available to derive --tokens.",
                args.json,
            )

    store = CalibrationStore()
    point = store.save_calibration(
        percent=args.percent,
        pool=args.pool,
        tokens_consumed=tokens_consumed,
    )
    estimated_limit = store.estimate_limit(args.pool)
    payload = {
        "pool": point.pool,
        "percent": point.percent,
        "tokens_consumed": point.tokens_consumed,
        "timestamp": point.timestamp,
        "reset_time": point.reset_time,
        "estimated_limit_tokens": estimated_limit,
    }

    if args.json:
        print_json(payload)
        return 0

    console = Console()
    console.print(
        "Saved calibration: "
        f"pool={point.pool} percent={point.percent:.1f}% "
        f"tokens={format_tokens(point.tokens_consumed)} "
        f"time={_format_local_time(point.timestamp)}"
    )
    if estimated_limit is not None:
        console.print(f"Estimated limit: {format_tokens(estimated_limit)} tokens")
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    """Render context growth for one or more sessions."""
    parser = SessionParser()
    sessions = parser.parse_sessions(args.data_path)
    sessions = _filter_sessions_by_days(sessions, args.days)
    if not sessions:
        return _no_data(
            "No sessions found for context analysis.",
            args.json,
        )

    if args.session_id:
        selected = [session for session in sessions if session.session_id == args.session_id]
        if not selected:
            raise ValueError(f"Session '{args.session_id}' was not found.")
    else:
        selected = parser.get_active_sessions(sessions, threshold_minutes=30)
        if not selected:
            return _no_data(
                "No active sessions found for context analysis.",
                args.json,
            )

    gauge = ContextGauge()
    tz_handler = TimezoneHandler(get_system_timezone())
    payload = [_serialize_context_progression(session, gauge, tz_handler) for session in selected]

    if args.json:
        print_json(payload[0] if args.session_id else payload)
        return 0

    console = Console()
    for session_data in payload:
        console.print(
            f"Session {session_data['session_id']} "
            f"({session_data['project']}, {session_data['status']})"
        )
        console.print(_create_context_table(session_data["progression"]))
    return 0


def _augment_burn_rate(
    current: BurnRateInfo,
    ratio: float | None,
    anomaly: bool,
    baseline: BurnRateInfo,
) -> BurnRateInfo:
    """Attach ratio and anomaly details to a burn-rate object."""
    return BurnRateInfo(
        tokens_per_hour=current.tokens_per_hour,
        tokens_per_day=current.tokens_per_day,
        cost_per_hour=current.cost_per_hour,
        cost_per_day=current.cost_per_day,
        baseline_tokens_per_day=(
            baseline.baseline_tokens_per_day
            if baseline.baseline_tokens_per_day is not None
            else baseline.tokens_per_day
        ),
        anomaly=anomaly,
        anomaly_ratio=ratio,
    )


def _build_project_map(
    entries_with_metadata: list[tuple[Any, str, str]]
) -> dict[str, str]:
    """Build request/message id to project mapping."""
    project_map: dict[str, str] = {}
    for entry, _, project_name in entries_with_metadata:
        if entry.request_id:
            project_map[entry.request_id] = project_name
        if entry.message_id:
            project_map[entry.message_id] = project_name
    return project_map


def _compute_ratio(current: BurnRateInfo, baseline: BurnRateInfo) -> float | None:
    """Compute current-to-baseline ratio."""
    baseline_tokens_day = (
        baseline.baseline_tokens_per_day
        if baseline.baseline_tokens_per_day is not None
        else baseline.tokens_per_day
    )
    if baseline_tokens_day <= 0:
        return None
    return current.tokens_per_day / baseline_tokens_day


def _create_context_table(progression: list[dict[str, Any]]) -> Table:
    """Create a context progression table."""
    table = Table(show_header=True)
    table.add_column("Timestamp", style="cyan")
    table.add_column("Context Tokens", justify="right")
    table.add_column("Delta", justify="right")

    for point in progression:
        delta = point["delta"]
        table.add_row(
            point["timestamp"],
            format_tokens(point["context_tokens"]),
            "-" if delta is None else format_tokens(delta),
        )

    return table


def _create_model_summary_table(models: dict[str, dict[str, Any]]) -> Table:
    """Create the model summary table."""
    table = Table(title="Model Summary")
    table.add_column("Model", style="cyan")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Est. %", justify="right")

    for model, data in sorted(
        models.items(),
        key=lambda item: item[1]["total_tokens"],
        reverse=True,
    ):
        table.add_row(
            model,
            format_tokens(data["total_tokens"]),
            f"${format_cost(data['cost_usd'])}",
            _format_percent(data["estimated_percent"]),
        )

    return table


def _create_project_summary_table(projects: dict[str, dict[str, Any]]) -> Table:
    """Create the project summary table."""
    table = Table(title="Project Summary")
    table.add_column("Project", style="cyan")
    table.add_column("Tokens", justify="right")
    table.add_column("Messages", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Est. %", justify="right")

    for project, data in sorted(
        projects.items(),
        key=lambda item: item[1]["total_tokens"],
        reverse=True,
    ):
        table.add_row(
            project,
            format_tokens(data["total_tokens"]),
            str(data["message_count"]),
            f"${format_cost(data['cost_usd'])}",
            _format_percent(data["estimated_percent"]),
        )

    return table


def _days_to_hours(days: int) -> int:
    """Convert days to hours for reader APIs."""
    return max(days, 1) * 24


def _filter_sessions_by_days(sessions: list[Any], days: int) -> list[Any]:
    """Keep sessions with recent activity inside the requested window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(days, 1))
    return [session for session in sessions if session.end_time >= cutoff]


def _format_local_time(value: datetime) -> str:
    """Format a datetime in the system timezone."""
    tz_handler = TimezoneHandler(get_system_timezone())
    return tz_handler.to_timezone(value).strftime("%Y-%m-%d %H:%M")


def _format_percent(value: float | None) -> str:
    """Format an optional percentage."""
    if value is None:
        return "-"
    return f"{value:.1f}%"


def _no_data(message: str, as_json: bool) -> int:
    """Emit a friendly no-data message."""
    if as_json:
        print_json({"error": message})
    else:
        Console().print(message)
    return 1


def _select_calibration(
    store: CalibrationStore,
) -> tuple[str | None, int | None]:
    """Pick the most useful calibration pool for reporting."""
    calibrations = store.load_calibrations()
    for pool in CALIBRATION_POOL_ORDER:
        if calibrations.get(pool):
            return pool, store.estimate_limit(pool)
    return None, None


def _serialize_burn_rate(rate: BurnRateInfo) -> dict[str, Any]:
    """Convert a burn-rate object to JSON-safe data."""
    return {
        "tokens_per_hour": rate.tokens_per_hour,
        "tokens_per_day": rate.tokens_per_day,
        "cost_per_hour": rate.cost_per_hour,
        "cost_per_day": rate.cost_per_day,
        "baseline_tokens_per_day": rate.baseline_tokens_per_day,
        "anomaly": rate.anomaly,
        "anomaly_ratio": rate.anomaly_ratio,
    }


def _serialize_context_progression(
    session: Any,
    gauge: ContextGauge,
    timezone_handler: TimezoneHandler,
) -> dict[str, Any]:
    """Serialize context progression for a session."""
    progression = gauge.get_context_progression(session.entries)
    points: list[dict[str, Any]] = []
    previous: int | None = None

    for timestamp, context_tokens in progression:
        delta = None if previous is None else context_tokens - previous
        points.append(
            {
                "timestamp": timezone_handler.to_timezone(timestamp).isoformat(),
                "context_tokens": context_tokens,
                "delta": delta,
            }
        )
        previous = context_tokens

    return {
        "session_id": session.session_id,
        "project": session.project,
        "status": _session_status(session),
        "progression": points,
    }


def _serialize_token_counts(tokens: TokenCounts) -> dict[str, Any]:
    """Convert token counts into a plain dictionary."""
    return {
        "input_tokens": tokens.input_tokens,
        "output_tokens": tokens.output_tokens,
        "cache_creation_tokens": tokens.cache_creation_tokens,
        "cache_read_tokens": tokens.cache_read_tokens,
        "total_tokens": tokens.total_tokens,
    }


def _session_status(session: Any) -> str:
    """Return the status label shared by session outputs."""
    if session.is_active and session.is_subagent:
        return "active/subagent"
    if session.is_active:
        return "active"
    if session.is_subagent:
        return "ended/subagent"
    return "ended"


def _sum_token_counts(counts: Any) -> TokenCounts:
    """Sum a collection of TokenCounts objects."""
    total = TokenCounts()
    for item in counts:
        total.input_tokens += item.input_tokens
        total.output_tokens += item.output_tokens
        total.cache_creation_tokens += item.cache_creation_tokens
        total.cache_read_tokens += item.cache_read_tokens
    return total


def _usage_percent(
    store: CalibrationStore,
    pool: str | None,
    tokens_used: int,
) -> float | None:
    """Estimate usage percent when calibration data is available."""
    if pool is None:
        return None
    return store.get_usage_percent(tokens_used, pool)
