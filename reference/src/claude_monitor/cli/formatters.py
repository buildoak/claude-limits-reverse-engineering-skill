"""Formatting helpers for token-track CLI output."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from rich.table import Table

from claude_monitor.core.models import BurnRateInfo, DayTokens, SessionInfo, TokenCounts
from claude_monitor.utils.time_utils import TimezoneHandler, get_system_timezone


def format_tokens(n: int) -> str:
    """Format token counts with compact human-readable suffixes."""
    abs_value = abs(n)
    if abs_value >= 1_000_000:
        return _format_compact(n / 1_000_000, "M")
    if abs_value >= 1_000:
        decimals = 0 if abs_value >= 100_000 else 1
        return _format_compact(n / 1_000, "K", decimals=decimals)
    return f"{n:,}"


def format_cost(usd: float) -> str:
    """Format a USD amount without the currency symbol."""
    return f"{usd:.2f}"


def format_duration(minutes: float) -> str:
    """Format minutes as a short duration string."""
    rounded = max(int(round(minutes)), 0)
    hours, mins = divmod(rounded, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def print_json(data: Any) -> None:
    """Write a JSON document to stdout."""
    print(json.dumps(data, indent=2, default=_json_default))


def create_report_table(
    days: list[DayTokens],
    totals: TokenCounts | None = None,
    total_cost: float | None = None,
    estimated_percents: dict[date, float | None] | None = None,
) -> Table:
    """Create the main daily report table."""
    table = Table(title="Daily Usage", show_footer=totals is not None)
    table.add_column("Date", style="cyan")
    table.add_column(
        "Input",
        justify="right",
        footer=format_tokens(totals.input_tokens) if totals else "",
    )
    table.add_column(
        "Output",
        justify="right",
        footer=format_tokens(totals.output_tokens) if totals else "",
    )
    table.add_column(
        "Cache Create",
        justify="right",
        footer=format_tokens(totals.cache_creation_tokens) if totals else "",
    )
    table.add_column(
        "Cache Read",
        justify="right",
        footer=format_tokens(totals.cache_read_tokens) if totals else "",
    )
    table.add_column(
        "Total",
        justify="right",
        footer=format_tokens(totals.total_tokens) if totals else "",
    )
    table.add_column(
        "Cost",
        justify="right",
        footer=f"${format_cost(total_cost)}" if total_cost is not None else "",
    )
    table.add_column("Models")

    estimated_percents = estimated_percents or {}
    for day in days:
        models = sorted(day.by_model)
        model_summary = ", ".join(models[:3])
        if len(models) > 3:
            model_summary = f"{model_summary}, +{len(models) - 3}"

        total_label = format_tokens(day.tokens.total_tokens)
        percent = estimated_percents.get(day.date)
        if percent is not None:
            total_label = f"{total_label} ({percent:.1f}%)"

        table.add_row(
            day.date.isoformat(),
            format_tokens(day.tokens.input_tokens),
            format_tokens(day.tokens.output_tokens),
            format_tokens(day.tokens.cache_creation_tokens),
            format_tokens(day.tokens.cache_read_tokens),
            total_label,
            f"${format_cost(day.cost_usd)}",
            model_summary or "-",
        )

    return table


def create_session_table(
    sessions: list[SessionInfo],
    timezone_handler: TimezoneHandler | None = None,
) -> Table:
    """Create the session summary table."""
    tz_handler = timezone_handler or TimezoneHandler(get_system_timezone())
    table = Table(title="Sessions")
    table.add_column("Session ID", style="cyan")
    table.add_column("Project")
    table.add_column("Model(s)")
    table.add_column("Start")
    table.add_column("End")
    table.add_column("Duration", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Context", justify="right")
    table.add_column("Status")

    for session in sessions:
        table.add_row(
            session.session_id,
            session.project or "unknown",
            ", ".join(session.models) or "unknown",
            _format_local_datetime(session.start_time, tz_handler),
            _format_local_datetime(session.end_time, tz_handler),
            format_duration(
                (session.end_time - session.start_time).total_seconds() / 60
            ),
            format_tokens(session.tokens.total_tokens),
            format_tokens(session.context_size),
            _session_status(session),
        )

    return table


def create_burn_table(current: BurnRateInfo, baseline: BurnRateInfo) -> Table:
    """Create the burn rate comparison table."""
    table = Table(title="Burn Rate")
    table.add_column("Metric", style="cyan")
    table.add_column("Current", justify="right")
    table.add_column("Baseline", justify="right")

    baseline_tokens_day = (
        baseline.baseline_tokens_per_day
        if baseline.baseline_tokens_per_day is not None
        else baseline.tokens_per_day
    )
    ratio = current.anomaly_ratio

    table.add_row("Tokens / hour", format_tokens(int(round(current.tokens_per_hour))), "-")
    table.add_row(
        "Tokens / day",
        format_tokens(int(round(current.tokens_per_day))),
        format_tokens(int(round(baseline_tokens_day))),
    )
    table.add_row("Cost / hour", f"${format_cost(current.cost_per_hour)}", "-")
    table.add_row(
        "Cost / day",
        f"${format_cost(current.cost_per_day)}",
        f"${format_cost(baseline.cost_per_day)}",
    )
    table.add_row("Ratio", f"{ratio:.2f}x" if ratio is not None else "-", "-")
    table.add_row("Anomaly", "yes" if current.anomaly else "no", "-")
    return table


def _format_compact(value: float, suffix: str, decimals: int = 1) -> str:
    """Format a compact numeric value."""
    text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return f"{text}{suffix}"


def _json_default(value: Any) -> Any:
    """Serialize common non-JSON-native values."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _format_local_datetime(
    value: datetime,
    timezone_handler: TimezoneHandler,
) -> str:
    """Render a datetime in local time without seconds."""
    return timezone_handler.to_timezone(value).strftime("%Y-%m-%d %H:%M")


def _session_status(session: SessionInfo) -> str:
    """Return a compact session status label."""
    if session.is_active and session.is_subagent:
        return "active/subagent"
    if session.is_active:
        return "active"
    if session.is_subagent:
        return "ended/subagent"
    return "ended"
