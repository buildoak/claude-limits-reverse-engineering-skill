"""Analytics engine for token and session usage data."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

from claude_monitor.core.calculations import (
    BurnRateCalculator as BlockBurnRateCalculator,
)
from claude_monitor.core.models import (
    BurnRate,
    BurnRateInfo,
    CalibrationPoint,
    CostMode,
    DayTokens,
    ProjectTokens,
    SessionBlock,
    SessionInfo,
    TokenCounts,
    TokenSnapshot,
    UsageEntry,
    UsageProjection,
    normalize_model_name,
)
from claude_monitor.core.pricing import PricingCalculator
from claude_monitor.data.analyzer import SessionAnalyzer
from claude_monitor.data.reader import load_usage_entries

_ACTIVE_SESSION_WINDOW = timedelta(minutes=30)
_CALIBRATION_POOLS = {"session", "weekly-all", "weekly-sonnet"}


def _utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    """Normalize datetimes to timezone-aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _new_token_counts() -> TokenCounts:
    """Create an empty token counter."""
    return TokenCounts()


def _add_entry_to_counts(counts: TokenCounts, entry: UsageEntry) -> None:
    """Accumulate usage entry tokens into a counter."""
    counts.input_tokens += entry.input_tokens
    counts.output_tokens += entry.output_tokens
    counts.cache_creation_tokens += entry.cache_creation_tokens
    counts.cache_read_tokens += entry.cache_read_tokens


def _resolve_entry_mapping(
    entry: UsageEntry, mapping: dict[str, str] | None
) -> str | None:
    """Resolve an entry against a request/message keyed mapping."""
    if not mapping:
        return None

    for key in (entry.request_id, entry.message_id):
        if key and key in mapping:
            return mapping[key]
    return None


def _entry_cost(entry: UsageEntry, pricing_calculator: PricingCalculator) -> float:
    """Return an entry cost, calculating it when cached cost is absent."""
    if entry.cost_usd > 0:
        return round(float(entry.cost_usd), 6)

    return pricing_calculator.calculate_cost(
        model=entry.model,
        input_tokens=entry.input_tokens,
        output_tokens=entry.output_tokens,
        cache_creation_tokens=entry.cache_creation_tokens,
        cache_read_tokens=entry.cache_read_tokens,
    )


def _total_cost(
    entries: list[UsageEntry], pricing_calculator: PricingCalculator
) -> float:
    """Sum entry costs with stable rounding."""
    return round(sum(_entry_cost(entry, pricing_calculator) for entry in entries), 6)


def _total_tokens(entries: list[UsageEntry]) -> int:
    """Return the total token volume for a list of entries."""
    return sum(
        entry.input_tokens
        + entry.output_tokens
        + entry.cache_creation_tokens
        + entry.cache_read_tokens
        for entry in entries
    )


def _isoformat_or_none(value: datetime | None) -> str | None:
    """Serialize a UTC datetime if present."""
    if value is None:
        return None
    return _ensure_utc(value).isoformat()


def _classify_model_family(model: str) -> str:
    """Map a model string to its family: opus, sonnet, haiku, or other."""
    lower = model.lower()
    if "opus" in lower:
        return "opus"
    if "sonnet" in lower:
        return "sonnet"
    if "haiku" in lower:
        return "haiku"
    return "other"


class ComputeModel:
    """Map raw tokens to estimated compute units using calibrated weights.

    Active formula: **Formula E** (fitted 2026-03-25, 11 data points, MAE 0.57%).
    CU = input*1 + output*5 + cache_creation*1 + cache_read*0.58
    Weekly limit: 374,000,000 CU.

    cache_read weight 0.58 reflects ~58% of fresh-input cost (KV-cache
    memory-bandwidth cost on the server side).  Stable across the
    0.45-0.65 sensitivity range.

    Supersedes Formula A (cache_read=0, limit=178M) which ignored the
    dominant token type entirely.
    """

    # Token-type weights — Formula E (2026-03-25)
    TOKEN_WEIGHTS: dict[str, float] = {
        "input": 1.0,
        "output": 5.0,
        "cache_creation": 1.0,
        "cache_read": 0.58,
    }

    # Weekly compute-unit budget (Formula E calibration)
    WEEKLY_LIMIT: int = 374_000_000

    # Per-model multiplier (1.0 = no distinction)
    MODEL_WEIGHTS: dict[str, float] = {
        "opus": 1.0,
        "sonnet": 1.0,
        "haiku": 1.0,
        "other": 1.0,
    }

    def __init__(
        self,
        token_weights: dict[str, float] | None = None,
        model_weights: dict[str, float] | None = None,
    ) -> None:
        if token_weights is not None:
            self.TOKEN_WEIGHTS = token_weights
        if model_weights is not None:
            self.MODEL_WEIGHTS = model_weights

    def compute_units_for_entry(self, entry: UsageEntry) -> float:
        """Compute weighted units for a single usage entry."""
        family = _classify_model_family(entry.model)
        model_mult = self.MODEL_WEIGHTS.get(family, 1.0)
        return model_mult * (
            entry.input_tokens * self.TOKEN_WEIGHTS["input"]
            + entry.output_tokens * self.TOKEN_WEIGHTS["output"]
            + entry.cache_creation_tokens * self.TOKEN_WEIGHTS["cache_creation"]
            + entry.cache_read_tokens * self.TOKEN_WEIGHTS["cache_read"]
        )

    def compute_units(self, entries: list[UsageEntry]) -> float:
        """Sum weighted compute units across all entries."""
        return sum(self.compute_units_for_entry(e) for e in entries)

    def compute_units_from_snapshot(
        self, snapshot: dict[str, TokenSnapshot]
    ) -> float:
        """Compute units from a per-model token snapshot dict."""
        total = 0.0
        for family, snap in snapshot.items():
            model_mult = self.MODEL_WEIGHTS.get(family, 1.0)
            total += model_mult * (
                snap.input_tokens * self.TOKEN_WEIGHTS["input"]
                + snap.output_tokens * self.TOKEN_WEIGHTS["output"]
                + snap.cache_creation_tokens * self.TOKEN_WEIGHTS["cache_creation"]
                + snap.cache_read_tokens * self.TOKEN_WEIGHTS["cache_read"]
            )
        return total

    def estimate_percent(
        self,
        entries: list[UsageEntry],
        pool: str,
        calibration_store: "CalibrationStore",
    ) -> float | None:
        """Estimate % of budget consumed using calibrated compute-unit limit."""
        limit = calibration_store.estimate_compute_limit(pool)
        if limit is None or limit <= 0:
            return None
        return (self.compute_units(entries) / limit) * 100.0

    @staticmethod
    def build_snapshot(entries: list[UsageEntry]) -> dict[str, TokenSnapshot]:
        """Build a per-model-family token snapshot from usage entries."""
        buckets: dict[str, TokenSnapshot] = {}
        for entry in entries:
            family = _classify_model_family(entry.model)
            snap = buckets.setdefault(family, TokenSnapshot())
            snap.input_tokens += entry.input_tokens
            snap.output_tokens += entry.output_tokens
            snap.cache_creation_tokens += entry.cache_creation_tokens
            snap.cache_read_tokens += entry.cache_read_tokens
        return buckets


class TokenLedger:
    """Aggregate usage entries across common reporting dimensions."""

    def __init__(self, pricing_calculator: PricingCalculator | None = None) -> None:
        self.pricing_calculator = pricing_calculator or PricingCalculator()

    def aggregate_by_day(
        self,
        entries: list[UsageEntry],
        project_map: dict[str, str] | None = None,
    ) -> list[DayTokens]:
        """Group entries by UTC day with model and project breakdowns."""
        grouped: dict[date, _DayAccumulator] = {}

        for entry in sorted(entries, key=lambda item: _ensure_utc(item.timestamp)):
            day_key = _ensure_utc(entry.timestamp).date()
            accumulator = grouped.setdefault(day_key, _DayAccumulator())
            model_name = normalize_model_name(entry.model) or "unknown"
            project_name = _resolve_entry_mapping(entry, project_map) or "unknown"

            _add_entry_to_counts(accumulator.tokens, entry)
            _add_entry_to_counts(
                accumulator.by_model.setdefault(model_name, _new_token_counts()), entry
            )
            _add_entry_to_counts(
                accumulator.by_project.setdefault(project_name, _new_token_counts()),
                entry,
            )
            accumulator.message_count += 1
            accumulator.cost_usd = round(
                accumulator.cost_usd + _entry_cost(entry, self.pricing_calculator), 6
            )

        return [
            DayTokens(
                date=day_key,
                tokens=accumulator.tokens,
                by_model=accumulator.by_model,
                by_project=accumulator.by_project,
                message_count=accumulator.message_count,
                cost_usd=accumulator.cost_usd,
            )
            for day_key, accumulator in sorted(grouped.items())
        ]

    def aggregate_by_model(self, entries: list[UsageEntry]) -> dict[str, TokenCounts]:
        """Group entries by normalized model name."""
        grouped: dict[str, TokenCounts] = {}

        for entry in entries:
            model_name = normalize_model_name(entry.model) or "unknown"
            _add_entry_to_counts(
                grouped.setdefault(model_name, _new_token_counts()), entry
            )

        return dict(sorted(grouped.items()))

    def aggregate_by_project(
        self,
        entries: list[UsageEntry],
        project_map: dict[str, str] | None = None,
    ) -> dict[str, ProjectTokens]:
        """Group entries by project name."""
        grouped: dict[str, ProjectTokens] = {}
        project_sessions: dict[str, set[str]] = defaultdict(set)

        for entry in entries:
            project_name = _resolve_entry_mapping(entry, project_map) or "unknown"
            project = grouped.setdefault(
                project_name,
                ProjectTokens(
                    name=project_name,
                    tokens=_new_token_counts(),
                    message_count=0,
                    cost_usd=0.0,
                    sessions=[],
                ),
            )

            _add_entry_to_counts(project.tokens, entry)
            project.message_count += 1
            project.cost_usd = round(
                project.cost_usd + _entry_cost(entry, self.pricing_calculator), 6
            )

            identifier = entry.request_id or entry.message_id
            if identifier:
                project_sessions[project_name].add(identifier)

        for project_name, sessions in project_sessions.items():
            grouped[project_name].sessions = sorted(sessions)

        return dict(sorted(grouped.items()))

    def aggregate_by_session(
        self,
        entries: list[UsageEntry],
        session_map: dict[str, str] | None = None,
    ) -> dict[str, SessionInfo]:
        """Group entries by session id using best-effort mapping fallback."""
        grouped_entries: dict[str, list[UsageEntry]] = defaultdict(list)

        for entry in entries:
            session_id = (
                _resolve_entry_mapping(entry, session_map)
                or entry.request_id
                or entry.message_id
                or "unknown-session"
            )
            grouped_entries[session_id].append(entry)

        sessions: dict[str, SessionInfo] = {}
        context_gauge = ContextGauge()
        now = _utc_now()

        for session_id, session_entries in grouped_entries.items():
            ordered_entries = sorted(
                session_entries, key=lambda item: _ensure_utc(item.timestamp)
            )
            start_time = _ensure_utc(ordered_entries[0].timestamp)
            end_time = _ensure_utc(ordered_entries[-1].timestamp)
            tokens = _new_token_counts()
            models: set[str] = set()

            for entry in ordered_entries:
                _add_entry_to_counts(tokens, entry)
                model_name = normalize_model_name(entry.model)
                if model_name:
                    models.add(model_name)

            sessions[session_id] = SessionInfo(
                session_id=session_id,
                project="unknown",
                models=sorted(models),
                start_time=start_time,
                end_time=end_time,
                tokens=tokens,
                message_count=len(ordered_entries),
                cost_usd=_total_cost(ordered_entries, self.pricing_calculator),
                is_active=end_time >= (now - _ACTIVE_SESSION_WINDOW),
                is_subagent=False,
                context_size=context_gauge.get_context_size(ordered_entries),
                entries=ordered_entries,
            )

        return dict(sorted(sessions.items()))


class BurnRateCalculator:
    """Calculate token velocity from usage entries and session blocks."""

    def __init__(
        self,
        pricing_calculator: PricingCalculator | None = None,
        block_calculator: BlockBurnRateCalculator | None = None,
    ) -> None:
        self.pricing_calculator = pricing_calculator or PricingCalculator()
        self.block_calculator = block_calculator or BlockBurnRateCalculator()

    def current_rate(
        self, entries: list[UsageEntry], window_hours: float = 1.0
    ) -> BurnRateInfo:
        """Calculate the current burn rate over the most recent window."""
        if not entries or window_hours <= 0:
            return self._empty_burn_rate()

        ordered_entries = sorted(entries, key=lambda item: _ensure_utc(item.timestamp))
        window_end = _ensure_utc(ordered_entries[-1].timestamp)
        window_start = window_end - timedelta(hours=window_hours)
        window_entries = [
            entry
            for entry in ordered_entries
            if _ensure_utc(entry.timestamp) >= window_start
        ]

        total_tokens = _total_tokens(window_entries)
        total_cost = _total_cost(window_entries, self.pricing_calculator)
        tokens_per_hour = total_tokens / window_hours
        cost_per_hour = total_cost / window_hours

        return BurnRateInfo(
            tokens_per_hour=tokens_per_hour,
            tokens_per_day=tokens_per_hour * 24,
            cost_per_hour=cost_per_hour,
            cost_per_day=cost_per_hour * 24,
            baseline_tokens_per_day=None,
            anomaly=False,
            anomaly_ratio=None,
        )

    def baseline_rate(
        self, entries: list[UsageEntry], weeks: int = 4
    ) -> BurnRateInfo:
        """Calculate the average daily burn rate over a trailing multi-week window."""
        if not entries or weeks <= 0:
            return self._empty_burn_rate()

        ordered_entries = sorted(entries, key=lambda item: _ensure_utc(item.timestamp))
        window_end = _ensure_utc(ordered_entries[-1].timestamp)
        window_days = weeks * 7
        window_start = window_end - timedelta(days=window_days)
        window_entries = [
            entry
            for entry in ordered_entries
            if _ensure_utc(entry.timestamp) >= window_start
        ]

        total_tokens = _total_tokens(window_entries)
        total_cost = _total_cost(window_entries, self.pricing_calculator)
        tokens_per_day = total_tokens / window_days
        cost_per_day = total_cost / window_days

        return BurnRateInfo(
            tokens_per_hour=tokens_per_day / 24,
            tokens_per_day=tokens_per_day,
            cost_per_hour=cost_per_day / 24,
            cost_per_day=cost_per_day,
            baseline_tokens_per_day=tokens_per_day,
            anomaly=False,
            anomaly_ratio=None,
        )

    def is_anomalous(
        self,
        current: BurnRateInfo,
        baseline: BurnRateInfo,
        threshold: float = 2.0,
    ) -> bool:
        """Return True when current burn materially exceeds baseline."""
        baseline_tokens_per_day = (
            baseline.baseline_tokens_per_day
            if baseline.baseline_tokens_per_day is not None
            else baseline.tokens_per_day
        )
        if threshold <= 0 or baseline_tokens_per_day <= 0:
            return False
        return current.tokens_per_day > (threshold * baseline_tokens_per_day)

    def calculate_burn_rate(self, block: SessionBlock) -> BurnRate | None:
        """Compatibility wrapper for session-block burn rate calculations."""
        return self.block_calculator.calculate_burn_rate(block)

    def project_block_usage(self, block: SessionBlock) -> UsageProjection | None:
        """Compatibility wrapper for session-block usage projection."""
        return self.block_calculator.project_block_usage(block)

    @staticmethod
    def _empty_burn_rate() -> BurnRateInfo:
        """Return a zero-valued burn rate object."""
        return BurnRateInfo(
            tokens_per_hour=0.0,
            tokens_per_day=0.0,
            cost_per_hour=0.0,
            cost_per_day=0.0,
            baseline_tokens_per_day=None,
            anomaly=False,
            anomaly_ratio=None,
        )


class ContextGauge:
    """Track context size growth within a session.

    Context size is approximated by summing all input-side tokens for each
    assistant response: input_tokens + cache_read_tokens + cache_creation_tokens.
    This represents the total tokens the model "saw" for that turn, which is
    the closest proxy for the context window size at that point.
    """

    @staticmethod
    def _context_tokens(entry: UsageEntry) -> int:
        """Approximate context window size from an entry's input-side tokens."""
        return entry.input_tokens + entry.cache_read_tokens + entry.cache_creation_tokens

    def get_context_size(self, session_entries: list[UsageEntry]) -> int:
        """Return the latest context-window approximation for a session."""
        if not session_entries:
            return 0

        ordered_entries = sorted(
            session_entries, key=lambda entry: _ensure_utc(entry.timestamp)
        )
        return self._context_tokens(ordered_entries[-1])

    def get_context_progression(
        self, session_entries: list[UsageEntry]
    ) -> list[tuple[datetime, int]]:
        """Return timestamped context growth points in UTC."""
        return [
            (_ensure_utc(entry.timestamp), self._context_tokens(entry))
            for entry in sorted(
                session_entries, key=lambda item: _ensure_utc(item.timestamp)
            )
        ]


class CalibrationStore:
    """Persist and query calibration points used to estimate token limits."""

    def __init__(self, config_dir: Path | None = None) -> None:
        self.config_dir = (
            config_dir.expanduser()
            if config_dir is not None
            else Path.home() / ".config" / "token-track"
        )
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.config_dir / "calibrations.json"

    def save_calibration(
        self,
        percent: float,
        pool: str,
        tokens_consumed: int,
        reset_time: datetime | None = None,
        timestamp: datetime | None = None,
        token_snapshot: dict[str, TokenSnapshot] | None = None,
        compute_units: float | None = None,
    ) -> CalibrationPoint:
        """Save a calibration point to disk."""
        self._validate_pool(pool)
        if percent <= 0:
            raise ValueError("percent must be greater than 0")
        if tokens_consumed < 0:
            raise ValueError("tokens_consumed must be non-negative")

        point = CalibrationPoint(
            pool=pool,
            percent=float(percent),
            tokens_consumed=int(tokens_consumed),
            timestamp=_ensure_utc(timestamp) if timestamp is not None else _utc_now(),
            reset_time=_ensure_utc(reset_time) if reset_time is not None else None,
            token_snapshot=token_snapshot,
            compute_units=compute_units,
        )

        payload = self._load_payload()
        payload.append(self._serialize_point(point))
        self.file_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return point

    def load_calibrations(self) -> dict[str, list[CalibrationPoint]]:
        """Load calibration history grouped by pool."""
        grouped: dict[str, list[CalibrationPoint]] = {
            pool: [] for pool in sorted(_CALIBRATION_POOLS)
        }

        for item in self._load_payload():
            point = self._deserialize_point(item)
            if point is None:
                continue
            grouped.setdefault(point.pool, []).append(point)

        for points in grouped.values():
            points.sort(key=lambda point: point.timestamp)

        return grouped

    def estimate_limit(self, pool: str) -> int | None:
        """Estimate a token limit from saved calibration points."""
        self._validate_pool(pool)
        points = self.load_calibrations().get(pool, [])
        estimates = [
            point.tokens_consumed / (point.percent / 100.0)
            for point in points
            if point.percent > 0
        ]
        if not estimates:
            return None
        return int(round(median(estimates)))

    def estimate_compute_limit(self, pool: str) -> float | None:
        """Estimate a compute-unit limit from calibration points that have CU data."""
        self._validate_pool(pool)
        points = self.load_calibrations().get(pool, [])
        estimates = [
            point.compute_units / (point.percent / 100.0)
            for point in points
            if point.percent > 0 and point.compute_units is not None and point.compute_units > 0
        ]
        if not estimates:
            return None
        return median(estimates)

    def get_usage_percent(self, tokens_used: int, pool: str) -> float | None:
        """Estimate usage percent for a pool from the median inferred limit."""
        estimated_limit = self.estimate_limit(pool)
        if estimated_limit is None or estimated_limit <= 0:
            return None
        return (tokens_used / estimated_limit) * 100.0

    def get_compute_usage_percent(self, compute_units: float, pool: str) -> float | None:
        """Estimate usage percent from compute units and calibrated limit."""
        limit = self.estimate_compute_limit(pool)
        if limit is None or limit <= 0:
            return None
        return (compute_units / limit) * 100.0

    def _load_payload(self) -> list[dict[str, Any]]:
        """Read the raw calibration payload from disk."""
        if not self.file_path.exists():
            return []

        try:
            data = json.loads(self.file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    @staticmethod
    def _serialize_point(point: CalibrationPoint) -> dict[str, Any]:
        """Convert a calibration point to JSON-safe data."""
        data: dict[str, Any] = {
            "pool": point.pool,
            "percent": point.percent,
            "tokens_consumed": point.tokens_consumed,
            "timestamp": point.timestamp.isoformat(),
            "reset_time": _isoformat_or_none(point.reset_time),
        }
        if point.token_snapshot is not None:
            data["token_snapshot"] = {
                family: {
                    "input_tokens": snap.input_tokens,
                    "output_tokens": snap.output_tokens,
                    "cache_creation_tokens": snap.cache_creation_tokens,
                    "cache_read_tokens": snap.cache_read_tokens,
                }
                for family, snap in point.token_snapshot.items()
            }
        if point.compute_units is not None:
            data["compute_units"] = point.compute_units
        return data

    def _deserialize_point(self, data: dict[str, Any]) -> CalibrationPoint | None:
        """Convert stored JSON data into a calibration point."""
        pool = data.get("pool")
        if pool not in _CALIBRATION_POOLS:
            return None

        timestamp = self._parse_datetime(data.get("timestamp"))
        if timestamp is None:
            return None

        try:
            percent = float(data["percent"])
            tokens_consumed = int(data["tokens_consumed"])
        except (KeyError, TypeError, ValueError):
            return None

        token_snapshot: dict[str, TokenSnapshot] | None = None
        raw_snapshot = data.get("token_snapshot")
        if isinstance(raw_snapshot, dict):
            token_snapshot = {}
            for family, snap_data in raw_snapshot.items():
                if isinstance(snap_data, dict):
                    token_snapshot[family] = TokenSnapshot(
                        input_tokens=int(snap_data.get("input_tokens", 0)),
                        output_tokens=int(snap_data.get("output_tokens", 0)),
                        cache_creation_tokens=int(snap_data.get("cache_creation_tokens", 0)),
                        cache_read_tokens=int(snap_data.get("cache_read_tokens", 0)),
                    )

        raw_cu = data.get("compute_units")
        compute_units = float(raw_cu) if raw_cu is not None else None

        return CalibrationPoint(
            pool=pool,
            percent=percent,
            tokens_consumed=tokens_consumed,
            timestamp=timestamp,
            reset_time=self._parse_datetime(data.get("reset_time")),
            token_snapshot=token_snapshot,
            compute_units=compute_units,
        )

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        """Parse an ISO datetime and normalize it to UTC."""
        if not value or not isinstance(value, str):
            return None

        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

        return _ensure_utc(parsed)

    @staticmethod
    def _validate_pool(pool: str) -> None:
        """Validate a calibration pool name."""
        if pool not in _CALIBRATION_POOLS:
            raise ValueError(
                f"pool must be one of {', '.join(sorted(_CALIBRATION_POOLS))}"
            )


def analyze_usage(
    hours_back: int | None = None,
    quick_start: bool = False,
    use_cache: bool = True,
    data_path: str | None = None,
) -> dict[str, Any]:
    """Load usage data, build session blocks, and return CLI-friendly results."""
    effective_hours = 24 if quick_start and hours_back is None else hours_back
    entries, raw_entries = load_usage_entries(
        data_path=data_path,
        hours_back=effective_hours,
        mode=CostMode.AUTO if use_cache else CostMode.AUTO,
        include_raw=True,
    )

    analyzer = SessionAnalyzer()
    blocks = analyzer.transform_to_blocks(entries)

    limits = analyzer.detect_limits(raw_entries) if raw_entries else []
    for block in blocks:
        block.limit_messages = []

    for limit in limits:
        for block in blocks:
            if _is_limit_in_block_timerange(limit, block):
                block.limit_messages.append(limit)

    calculator = BurnRateCalculator()
    _process_burn_rates(blocks, calculator)

    metadata = {
        "hours_analyzed": effective_hours,
        "quick_start": quick_start,
        "limits_detected": len(limits),
    }
    return _create_result(blocks, entries, metadata)


def _process_burn_rates(
    blocks: list[SessionBlock], calculator: BurnRateCalculator
) -> None:
    """Attach burn-rate snapshots and projections to active blocks."""
    for block in blocks:
        block.burn_rate_snapshot = None
        block.projection_data = None

        if not block.is_active:
            continue

        burn_rate = calculator.calculate_burn_rate(block)
        block.burn_rate_snapshot = burn_rate
        if burn_rate is None:
            continue

        projection = calculator.project_block_usage(block)
        if projection is None:
            continue

        block.projection_data = {
            "totalTokens": projection.projected_total_tokens,
            "totalCost": projection.projected_total_cost,
            "remainingMinutes": projection.remaining_minutes,
        }


def _create_result(
    blocks: list[SessionBlock], entries: list[UsageEntry], metadata: dict[str, Any]
) -> dict[str, Any]:
    """Create the standard serialized analysis result."""
    return {
        "blocks": _convert_blocks_to_dict_format(blocks),
        "metadata": metadata,
        "entries_count": len(entries),
        "total_tokens": sum(block.total_tokens for block in blocks),
        "total_cost": round(sum(block.cost_usd for block in blocks), 6),
    }


def _is_limit_in_block_timerange(
    limit_info: dict[str, Any], block: SessionBlock
) -> bool:
    """Return True when a limit event occurred during the block window."""
    timestamp = limit_info.get("timestamp")
    if not isinstance(timestamp, datetime):
        return False

    limit_time = _ensure_utc(timestamp)
    return block.start_time <= limit_time <= block.end_time


def _format_limit_info(limit_info: dict[str, Any]) -> dict[str, Any]:
    """Serialize a limit event for CLI/API output."""
    timestamp = limit_info.get("timestamp")
    reset_time = limit_info.get("reset_time")

    return {
        "type": limit_info.get("type"),
        "timestamp": _isoformat_or_none(timestamp)
        if isinstance(timestamp, datetime)
        else None,
        "content": limit_info.get("content"),
        "reset_time": _isoformat_or_none(reset_time)
        if isinstance(reset_time, datetime)
        else None,
    }


def _format_block_entries(entries: list[UsageEntry]) -> list[dict[str, Any]]:
    """Serialize block entries for display."""
    return [
        {
            "timestamp": _ensure_utc(entry.timestamp).isoformat(),
            "inputTokens": entry.input_tokens,
            "outputTokens": entry.output_tokens,
            "cacheCreationTokens": entry.cache_creation_tokens,
            "cacheReadInputTokens": entry.cache_read_tokens,
            "costUSD": entry.cost_usd,
            "model": entry.model,
            "messageId": entry.message_id,
            "requestId": entry.request_id,
        }
        for entry in sorted(entries, key=lambda item: _ensure_utc(item.timestamp))
    ]


def _create_base_block_dict(block: SessionBlock) -> dict[str, Any]:
    """Create the common serialized shape for a session block."""
    visible_total_tokens = (
        block.token_counts.input_tokens + block.token_counts.output_tokens
    )

    return {
        "id": block.id,
        "isActive": block.is_active,
        "isGap": block.is_gap,
        "startTime": _ensure_utc(block.start_time).isoformat(),
        "endTime": _ensure_utc(block.end_time).isoformat(),
        "actualEndTime": _isoformat_or_none(block.actual_end_time),
        "tokenCounts": {
            "inputTokens": block.token_counts.input_tokens,
            "outputTokens": block.token_counts.output_tokens,
            "cacheCreationTokens": block.token_counts.cache_creation_tokens,
            "cacheReadInputTokens": block.token_counts.cache_read_tokens,
        },
        "totalTokens": visible_total_tokens,
        "costUSD": block.cost_usd,
        "models": block.models,
        "perModelStats": block.per_model_stats,
        "sentMessagesCount": block.sent_messages_count,
        "durationMinutes": block.duration_minutes,
        "entries": _format_block_entries(block.entries),
        "entries_count": len(block.entries),
    }


def _add_optional_block_data(block: SessionBlock, block_dict: dict[str, Any]) -> None:
    """Attach optional block fields when available."""
    burn_rate = getattr(block, "burn_rate_snapshot", None)
    if burn_rate is not None:
        block_dict["burnRate"] = {
            "tokensPerMinute": burn_rate.tokens_per_minute,
            "costPerHour": burn_rate.cost_per_hour,
        }

    projection = getattr(block, "projection_data", None)
    if projection is not None:
        block_dict["projection"] = projection

    limit_messages = getattr(block, "limit_messages", None)
    if limit_messages:
        block_dict["limitMessages"] = [
            _format_limit_info(limit)
            if any(isinstance(limit.get(key), datetime) for key in ("timestamp", "reset_time"))
            else dict(limit)
            for limit in limit_messages
            if isinstance(limit, dict)
        ]


def _convert_blocks_to_dict_format(blocks: list[SessionBlock]) -> list[dict[str, Any]]:
    """Serialize session blocks for consumer-facing output."""
    serialized_blocks: list[dict[str, Any]] = []

    for block in blocks:
        block_dict = _create_base_block_dict(block)
        _add_optional_block_data(block, block_dict)
        serialized_blocks.append(block_dict)

    return serialized_blocks


@dataclass
class _DayAccumulator:
    """Internal daily aggregation state."""

    tokens: TokenCounts = field(default_factory=TokenCounts)
    by_model: dict[str, TokenCounts] = field(default_factory=dict)
    by_project: dict[str, TokenCounts] = field(default_factory=dict)
    message_count: int = 0
    cost_usd: float = 0.0


__all__ = [
    "BurnRateCalculator",
    "CalibrationStore",
    "ComputeModel",
    "ContextGauge",
    "TokenLedger",
    "analyze_usage",
]
