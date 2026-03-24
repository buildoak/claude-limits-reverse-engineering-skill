"""Session parsing and compatibility block analysis for Claude Monitor."""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from claude_monitor.core.data_processors import (
    DataConverter,
    TimestampProcessor,
    TokenExtractor,
)
from claude_monitor.core.models import (
    SessionBlock,
    SessionInfo,
    TokenCounts,
    UsageEntry,
    normalize_model_name,
)
from claude_monitor.core.pricing import PricingCalculator
from claude_monitor.data.reader import extract_project_name
from claude_monitor.error_handling import report_file_error
from claude_monitor.utils.time_utils import TimezoneHandler, get_system_timezone

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(timezone.utc)


def _add_entry_to_counts(counts: TokenCounts, entry: UsageEntry) -> None:
    """Accumulate a usage entry into token counts."""
    counts.input_tokens += entry.input_tokens
    counts.output_tokens += entry.output_tokens
    counts.cache_creation_tokens += entry.cache_creation_tokens
    counts.cache_read_tokens += entry.cache_read_tokens


def _context_size(entries: List[UsageEntry]) -> int:
    """Approximate context window size from the latest entry's input-side tokens.

    Context size is the total tokens the model "saw" for the last turn:
    input_tokens + cache_read_tokens + cache_creation_tokens.
    """
    if not entries:
        return 0
    ordered_entries = sorted(entries, key=lambda entry: entry.timestamp)
    last = ordered_entries[-1]
    return last.input_tokens + last.cache_read_tokens + last.cache_creation_tokens


class SessionParser:
    """Parse Claude conversation files into session summaries."""

    def __init__(
        self,
        pricing_calculator: Optional[PricingCalculator] = None,
        timezone_handler: Optional[TimezoneHandler] = None,
    ) -> None:
        self.pricing_calculator = pricing_calculator or PricingCalculator()
        self.timezone_handler = timezone_handler or TimezoneHandler(
            get_system_timezone()
        )
        self.timestamp_processor = TimestampProcessor(self.timezone_handler)

    def parse_sessions(self, data_path: Optional[Union[str, Path]]) -> List[SessionInfo]:
        """Parse all conversation files under the Claude projects directory."""
        root = Path(data_path if data_path else "~/.claude/projects").expanduser()
        if not root.exists():
            logger.warning("Data path does not exist: %s", root)
            return []

        sessions: List[SessionInfo] = []
        for file_path in sorted(root.rglob("*.jsonl")):
            session = self._parse_conversation_file(file_path)
            if session is not None:
                sessions.append(session)

        sessions.sort(key=lambda session: session.start_time)
        return sessions

    def get_active_sessions(
        self, sessions: List[SessionInfo], threshold_minutes: int = 30
    ) -> List[SessionInfo]:
        """Return sessions whose latest activity is within the threshold."""
        cutoff = _utc_now() - timedelta(minutes=threshold_minutes)
        active_sessions: List[SessionInfo] = []

        for session in sessions:
            session.is_active = session.end_time >= cutoff
            if session.is_active:
                active_sessions.append(session)

        return active_sessions

    def _parse_conversation_file(self, file_path: Path) -> Optional[SessionInfo]:
        """Parse a single conversation JSONL file into SessionInfo."""
        timestamps: List[datetime] = []
        usage_entries: List[UsageEntry] = []
        models: Set[str] = set()
        session_id: Optional[str] = None

        try:
            with open(file_path, encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue

                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    timestamp = self._extract_timestamp(payload)
                    if timestamp is not None:
                        timestamps.append(timestamp)

                    candidate_session_id = payload.get("sessionId") or payload.get(
                        "session_id"
                    )
                    if isinstance(candidate_session_id, str) and candidate_session_id:
                        session_id = candidate_session_id

                    model_name = DataConverter.extract_model_name(payload, default="")
                    if model_name:
                        models.add(normalize_model_name(model_name))

                    usage_entry = self._create_usage_entry(payload)
                    if usage_entry is not None:
                        usage_entries.append(usage_entry)
                        if usage_entry.model:
                            models.add(normalize_model_name(usage_entry.model))

        except Exception as exc:
            logger.warning("Failed to parse session file %s: %s", file_path, exc)
            report_file_error(
                exception=exc,
                file_path=file_path,
                operation="parse_session",
                additional_context={"file_exists": file_path.exists()},
            )
            return None

        if not timestamps and not usage_entries:
            return None

        usage_entries.sort(key=lambda entry: entry.timestamp)
        if not timestamps:
            timestamps = [entry.timestamp for entry in usage_entries]

        start_time = min(timestamps)
        end_time = max(timestamps)
        token_counts = TokenCounts()
        for entry in usage_entries:
            _add_entry_to_counts(token_counts, entry)

        ordered_models = sorted(model for model in models if model)
        project_name = extract_project_name(file_path)
        return SessionInfo(
            session_id=session_id or file_path.stem,
            project=project_name,
            models=ordered_models,
            start_time=start_time,
            end_time=end_time,
            tokens=token_counts,
            message_count=len(usage_entries),
            cost_usd=round(sum(entry.cost_usd for entry in usage_entries), 6),
            is_active=end_time >= (_utc_now() - timedelta(minutes=30)),
            is_subagent="subagent" in file_path.parts,
            context_size=_context_size(usage_entries),
            entries=usage_entries,
        )

    def _extract_timestamp(self, payload: Dict[str, Any]) -> Optional[datetime]:
        """Extract a UTC timestamp from a raw JSONL payload."""
        timestamp_value = payload.get("timestamp")
        timestamp = self.timestamp_processor.parse_timestamp(timestamp_value)
        if timestamp is None:
            return None
        return self.timezone_handler.ensure_utc(timestamp)

    def _create_usage_entry(self, payload: Dict[str, Any]) -> Optional[UsageEntry]:
        """Create a UsageEntry from a raw JSONL payload when usage exists."""
        timestamp = self._extract_timestamp(payload)
        if timestamp is None:
            return None

        token_data = TokenExtractor.extract_tokens(payload)
        if token_data["total_tokens"] <= 0:
            return None

        model = DataConverter.extract_model_name(
            payload, default="claude-sonnet-4-6"
        )
        entry_data: Dict[str, Any] = {
            "model": model,
            "input_tokens": token_data["input_tokens"],
            "output_tokens": token_data["output_tokens"],
            "cache_creation_tokens": token_data["cache_creation_tokens"],
            "cache_read_tokens": token_data["cache_read_tokens"],
            "cost_usd": payload.get("costUSD")
            or payload.get("cost")
            or payload.get("cost_usd"),
        }
        cost_value = entry_data["cost_usd"]
        if cost_value is None:
            cost_value = self.pricing_calculator.calculate_cost(
                model=model,
                input_tokens=token_data["input_tokens"],
                output_tokens=token_data["output_tokens"],
                cache_creation_tokens=token_data["cache_creation_tokens"],
                cache_read_tokens=token_data["cache_read_tokens"],
            )

        message = payload.get("message", {})
        return UsageEntry(
            timestamp=timestamp,
            input_tokens=token_data["input_tokens"],
            output_tokens=token_data["output_tokens"],
            cache_creation_tokens=token_data["cache_creation_tokens"],
            cache_read_tokens=token_data["cache_read_tokens"],
            cost_usd=float(cost_value),
            model=model,
            message_id=payload.get("message_id") or message.get("id") or "",
            request_id=payload.get("request_id") or payload.get("requestId") or "",
        )


class SessionAnalyzer:
    """Compatibility wrapper that still produces session blocks and limit events."""

    def __init__(self, session_duration_hours: int = 5):
        self.session_duration_hours = session_duration_hours
        self.session_duration = timedelta(hours=session_duration_hours)
        self.timezone_handler = TimezoneHandler(get_system_timezone())
        self.timestamp_processor = TimestampProcessor(self.timezone_handler)

    def transform_to_blocks(self, entries: List[UsageEntry]) -> List[SessionBlock]:
        """Create 5-hour blocks from usage entries."""
        if not entries:
            return []

        blocks: List[SessionBlock] = []
        current_block: Optional[SessionBlock] = None

        for entry in sorted(entries, key=lambda item: item.timestamp):
            if current_block is None or self._should_create_new_block(
                current_block, entry
            ):
                if current_block is not None:
                    self._finalize_block(current_block)
                    blocks.append(current_block)

                    gap_block = self._check_for_gap(current_block, entry)
                    if gap_block is not None:
                        blocks.append(gap_block)

                current_block = self._create_new_block(entry)

            self._add_entry_to_block(current_block, entry)

        if current_block is not None:
            self._finalize_block(current_block)
            blocks.append(current_block)

        self._mark_active_blocks(blocks)
        return blocks

    def detect_limits(self, raw_entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Detect limit or capacity messages in raw JSONL entries."""
        limits: List[Dict[str, Any]] = []
        for raw_entry in raw_entries:
            limit_info = self._detect_single_limit(raw_entry)
            if limit_info is not None:
                limits.append(limit_info)
        return limits

    def _should_create_new_block(self, block: SessionBlock, entry: UsageEntry) -> bool:
        if entry.timestamp >= block.end_time:
            return True
        return bool(
            block.entries
            and (entry.timestamp - block.entries[-1].timestamp) >= self.session_duration
        )

    def _round_to_hour(self, timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )

    def _create_new_block(self, entry: UsageEntry) -> SessionBlock:
        start_time = self._round_to_hour(entry.timestamp)
        return SessionBlock(
            id=start_time.isoformat(),
            start_time=start_time,
            end_time=start_time + self.session_duration,
            token_counts=TokenCounts(),
            entries=[],
            cost_usd=0.0,
        )

    def _add_entry_to_block(self, block: SessionBlock, entry: UsageEntry) -> None:
        block.entries.append(entry)
        _add_entry_to_counts(block.token_counts, entry)
        block.cost_usd = round(block.cost_usd + (entry.cost_usd or 0.0), 6)
        block.sent_messages_count += 1

        model = normalize_model_name(entry.model or "claude-sonnet-4-6")
        if model not in block.models:
            block.models.append(model)

        if model not in block.per_model_stats:
            block.per_model_stats[model] = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "cost_usd": 0.0,
                "entries_count": 0,
            }

        model_stats = block.per_model_stats[model]
        model_stats["input_tokens"] += entry.input_tokens
        model_stats["output_tokens"] += entry.output_tokens
        model_stats["cache_creation_tokens"] += entry.cache_creation_tokens
        model_stats["cache_read_tokens"] += entry.cache_read_tokens
        model_stats["cost_usd"] = round(
            model_stats["cost_usd"] + (entry.cost_usd or 0.0), 6
        )
        model_stats["entries_count"] += 1

    def _finalize_block(self, block: SessionBlock) -> None:
        if block.entries:
            block.actual_end_time = block.entries[-1].timestamp
            block.sent_messages_count = len(block.entries)

    def _check_for_gap(
        self, last_block: SessionBlock, next_entry: UsageEntry
    ) -> Optional[SessionBlock]:
        if last_block.actual_end_time is None:
            return None

        if (next_entry.timestamp - last_block.actual_end_time) < self.session_duration:
            return None

        return SessionBlock(
            id=f"gap-{last_block.actual_end_time.isoformat()}",
            start_time=last_block.actual_end_time,
            end_time=next_entry.timestamp,
            actual_end_time=None,
            is_gap=True,
            entries=[],
            token_counts=TokenCounts(),
            cost_usd=0.0,
            models=[],
        )

    def _mark_active_blocks(self, blocks: List[SessionBlock]) -> None:
        now = _utc_now()
        for block in blocks:
            if not block.is_gap and block.end_time > now:
                block.is_active = True

    def _detect_single_limit(
        self, raw_data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        timestamp = self._extract_raw_timestamp(raw_data)
        if timestamp is None:
            return None

        messages = self._extract_text_messages(raw_data)
        if not messages:
            return None

        for message in messages:
            message_lower = message.lower()
            if not any(
                phrase in message_lower
                for phrase in ("limit", "capacity", "rate", "try again")
            ):
                continue

            limit_type = "opus_limit" if self._is_opus_limit(message_lower) else "system_limit"
            reset_time, wait_minutes = self._extract_wait_time(message, timestamp)
            parsed_reset = self._parse_reset_timestamp(message)
            limit_info: Dict[str, Any] = {
                "type": limit_type,
                "timestamp": timestamp,
                "content": message,
                "message": message,
                "reset_time": parsed_reset or reset_time,
                "wait_minutes": wait_minutes,
                "raw_data": raw_data,
                "block_context": self._extract_block_context(raw_data),
            }
            return limit_info

        return None

    def _extract_raw_timestamp(self, raw_data: Dict[str, Any]) -> Optional[datetime]:
        timestamp = self.timestamp_processor.parse_timestamp(raw_data.get("timestamp"))
        if timestamp is None:
            return None
        return self.timezone_handler.ensure_utc(timestamp)

    def _extract_text_messages(self, raw_data: Dict[str, Any]) -> List[str]:
        messages: List[str] = []
        for candidate in (
            raw_data.get("content"),
            raw_data.get("message", {}).get("content"),
        ):
            messages.extend(self._flatten_text(candidate))
        return messages

    def _flatten_text(self, content: Any) -> List[str]:
        if isinstance(content, str):
            return [content]
        if isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str):
                return [text]
            nested_content = content.get("content")
            return self._flatten_text(nested_content)
        if isinstance(content, list):
            flattened: List[str] = []
            for item in content:
                flattened.extend(self._flatten_text(item))
            return flattened
        return []

    def _extract_block_context(
        self, raw_data: Dict[str, Any], message: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        context: Dict[str, Any] = {
            "message_id": raw_data.get("messageId") or raw_data.get("message_id"),
            "request_id": raw_data.get("requestId") or raw_data.get("request_id"),
            "session_id": raw_data.get("sessionId") or raw_data.get("session_id"),
            "version": raw_data.get("version"),
            "model": raw_data.get("model"),
        }

        if message:
            context["message_id"] = message.get("id") or context["message_id"]
            context["model"] = message.get("model") or context["model"]
            context["usage"] = message.get("usage", {})
            context["stop_reason"] = message.get("stop_reason")

        return context

    def _is_opus_limit(self, content_lower: str) -> bool:
        if "opus" not in content_lower:
            return False
        return any(
            phrase in content_lower
            for phrase in ("rate limit", "limit exceeded", "limit reached", "daily limit")
        )

    def _extract_wait_time(
        self, content: str, timestamp: datetime
    ) -> Tuple[Optional[datetime], Optional[int]]:
        match = re.search(r"wait\s+(\d+)\s+minutes?", content.lower())
        if match is None:
            return None, None

        wait_minutes = int(match.group(1))
        return timestamp + timedelta(minutes=wait_minutes), wait_minutes

    def _parse_reset_timestamp(self, text: str) -> Optional[datetime]:
        epoch_match = re.search(r"limit reached\|(\d+)", text)
        if epoch_match is not None:
            try:
                return datetime.fromtimestamp(
                    int(epoch_match.group(1)), tz=timezone.utc
                )
            except (OSError, ValueError):
                return None

        iso_match = re.search(
            r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", text
        )
        if iso_match is not None:
            parsed = self.timestamp_processor.parse_timestamp(iso_match.group(1))
            if parsed is not None:
                return self.timezone_handler.ensure_utc(parsed)

        return None
