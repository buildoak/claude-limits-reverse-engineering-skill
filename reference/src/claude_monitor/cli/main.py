"""Command-line entry point for token-track."""

from __future__ import annotations

import argparse
from typing import Sequence

from rich.console import Console

from claude_monitor import __version__

DEFAULT_DATA_PATH = "~/.claude/projects"
CALIBRATION_POOLS = ("session", "weekly-all", "weekly-sonnet")


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach shared CLI flags to a parser."""
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Time window to analyze in days. Default: 7.",
    )
    parser.add_argument(
        "--data-path",
        default=DEFAULT_DATA_PATH,
        help="Claude usage data directory. Default: ~/.claude/projects.",
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the top-level argument parser."""
    common = argparse.ArgumentParser(add_help=False)
    _add_common_arguments(common)

    parser = argparse.ArgumentParser(
        prog="token-track",
        description="Inspect Claude Code token usage from local JSONL data.",
        parents=[common],
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "report",
        parents=[common],
        help="Show token usage report by day, model, and project.",
        description="Show token usage report by day, model, and project.",
    )

    session_parser = subparsers.add_parser(
        "session",
        parents=[common],
        help="Show session breakdowns.",
        description="Show session breakdowns.",
    )
    session_parser.add_argument(
        "--active",
        action="store_true",
        help="Only include sessions active within the last 30 minutes.",
    )

    subparsers.add_parser(
        "burn",
        parents=[common],
        help="Show current burn rate versus baseline.",
        description="Show current burn rate versus baseline.",
    )

    calibrate_parser = subparsers.add_parser(
        "calibrate",
        parents=[common],
        help="Save a calibration point for pool limit estimation.",
        description="Save a calibration point for pool limit estimation.",
    )
    calibrate_parser.add_argument(
        "--percent",
        type=float,
        required=True,
        help="Observed usage percent for the selected pool.",
    )
    calibrate_parser.add_argument(
        "--pool",
        choices=CALIBRATION_POOLS,
        required=True,
        help="Pool to calibrate.",
    )
    calibrate_parser.add_argument(
        "--tokens",
        type=int,
        help="Observed tokens consumed. If omitted, uses recent usage in --days.",
    )
    calibrate_parser.add_argument(
        "--timestamp",
        type=str,
        help="ISO timestamp of the reading, e.g. '2026-03-24T18:20+07:00'. "
        "If omitted, uses current time.",
    )

    context_parser = subparsers.add_parser(
        "context",
        parents=[common],
        help="Show context progression for a session or all active sessions.",
        description="Show context progression for a session or all active sessions.",
    )
    context_parser.add_argument(
        "session_id",
        nargs="?",
        help="Session identifier. If omitted, all active sessions are shown.",
    )

    return parser


def _dispatch(args: argparse.Namespace) -> int:
    """Dispatch to the selected command handler."""
    if args.command == "report":
        from .commands import cmd_report

        return cmd_report(args)
    if args.command == "session":
        from .commands import cmd_session

        return cmd_session(args)
    if args.command == "burn":
        from .commands import cmd_burn

        return cmd_burn(args)
    if args.command == "calibrate":
        from .commands import cmd_calibrate

        return cmd_calibrate(args)
    if args.command == "context":
        from .commands import cmd_context

        return cmd_context(args)
    raise ValueError(f"Unknown command: {args.command}")


def _print_error(message: str, as_json: bool) -> None:
    """Emit an error message in the requested output format."""
    if as_json:
        from .formatters import print_json

        print_json({"error": message})
        return

    Console(stderr=True).print(f"Error: {message}", style="bold red")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments and execute the requested command."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.days <= 0:
        parser.error("--days must be greater than 0")
    if hasattr(args, "tokens") and args.tokens is not None and args.tokens < 0:
        parser.error("--tokens must be non-negative")
    if hasattr(args, "percent") and args.percent is not None:
        if args.percent <= 0 or args.percent > 100:
            parser.error("--percent must be between 0 and 100")

    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        _print_error("Interrupted.", getattr(args, "json", False))
        return 130
    except (FileNotFoundError, ValueError) as exc:
        _print_error(str(exc), getattr(args, "json", False))
        return 1
    except Exception as exc:  # pragma: no cover - safety net for CLI use
        _print_error(str(exc), getattr(args, "json", False))
        return 1
