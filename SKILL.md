---
name: token-track
description: >
  Claude Code token usage tracking and limit reverse-engineering.
  Daily/project/model breakdown, burn rate, context gauge, limit calibration.
  Active model: Formula E (CU = input*1 + output*5 + cache_creation*1 + cache_read*0.58, limit 374M CU/week).
triggers:
  - usage
  - tokens
  - token count
  - burn rate
  - context size
  - quota
  - limits
  - how much have we used
  - weekly usage
  - session usage
  - usage report
  - token budget
  - compute units
  - CU
  - calibration
---

# token-track

Token usage tracker and limit reverse-engineering tool for Claude Code. Reads JSONL session files directly -- ground truth, no estimation.

## Activation

```bash
cd /path/to/claude-limits-reverse-engineering-skill
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Then run any `token-track` command below.

## Active Compute Model

**Formula E** (fitted 2026-03-25, 11 data points, MAE 0.57%, max error 1.14%)

```
CU = input*1 + output*5 + cache_creation*1 + cache_read*0.58
Weekly limit: 374,000,000 CU
```

Key insight: cache reads are NOT free. Weight 0.58 means ~58% of fresh input cost (KV-cache memory bandwidth on server side). Stable across 0.45-0.65 sensitivity range.

Previous model (Formula A) used cache_read=0 and limit=178M -- ignored the dominant token type entirely.

Model registry: `reference/models/registry.json`

## Commands

### report -- Daily token breakdown
```bash
token-track report [--days N] [--json]
```
Shows per-day totals broken down by model and project. Default: 7 days.
Output: input, output, cache_creation, cache_read tokens + cost + message count + compute units + estimated %.

### session -- Per-session breakdown
```bash
token-track session [--active] [--json]
```
Lists sessions with duration, token counts, context size estimate.
`--active` filters to currently running sessions only.

### burn -- Burn rate vs baseline
```bash
token-track burn [--json]
```
Current tokens/day velocity vs 4-week historical baseline. Flags anomalies (>2x ratio).

### calibrate -- Set limit reference point
```bash
token-track calibrate --percent N --pool POOL [--timestamp ISO] [--tokens N]
```
Saves a calibration point from Anthropic dashboard. Pools: `session`, `weekly-all`, `weekly-sonnet`.
Automatically captures per-model token snapshot and computes CU at calibration time.
Subsequent `report` output shows estimated % alongside raw tokens.

### context -- Context gauge per session
```bash
token-track context [SESSION_ID] [--json]
```
Shows input_token progression per session -- how context grows over the conversation.
Useful for detecting sessions approaching context limits.

## Output Modes

All commands support `--json` for machine-readable output. Default is Rich tables for terminal.

## Data Source

Reads `~/.claude/projects/**/*.jsonl` -- the files Claude Code writes after each API call.
Project attribution derived from directory path. Subagent entries in `subagents/` subdirs tagged separately.

## Calibration Store

Location: `~/.config/token-track/calibrations.json`

Each calibration point records: pool, dashboard %, tokens consumed, per-model token snapshot, compute units, timestamp, and reset time. The tool uses median of inferred limits across all points for a pool.

## Reference

Source code, documentation, model registry, and tests live under `reference/`:

- `reference/src/claude_monitor/` -- Python package source
- `reference/src/tests/` -- Test suite
- `reference/models/registry.json` -- Model version history with fit quality metrics
- `reference/doc/` -- Screenshots and assets
- `reference/README.md` -- Upstream documentation

## Off-Peak Promotions

Anthropic runs periodic promotions that modify metering. These affect calibration validity.

**March 13–28, 2026 promotion:**
- 5-hour session cap doubled during off-peak (weekdays outside 12:00–18:00 UTC + all weekends)
- Mechanism: bonus zone above normal cap. Tokens within normal cap count toward weekly at full rate. Tokens in bonus zone (above normal cap) exempt from weekly.
- NOT a flat 0.5× discount — weekly meter grows at the same rate during off-peak as during peak
- Weekly calibration unaffected IF session usage stays below normal cap during off-peak periods
- Session% calibration invalid during promotion (denominator is 2X, not X)
- Post-promotion: re-validate formula with 3–5 fresh calibration points after March 28

Contrast: Holiday 2025 promotion doubled weekly caps directly — different mechanism entirely.

## Coordinator Usage

For quick status checks, dispatch a subagent to run `token-track report --days 1 --json` and parse the result.
For context monitoring of TG bot sessions, use `token-track session --active --json`.
For anomaly alerts, `token-track burn --json` returns `anomaly: true/false` with ratio.
