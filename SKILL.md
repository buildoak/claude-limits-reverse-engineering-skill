---
name: token-track
description: >
  Claude Code token usage tracking and limit reverse-engineering.
  Daily/project/model breakdown, burn rate, context gauge, limit calibration.
  Active model: Formula A (CU = Σ_model[mult × (inp×1.0 + out×5.0 + cc×1.25 + cr×0.1)], limit 527.6M CU/week).
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

## CRITICAL: Streaming Dedup

**Every JSONL token count MUST filter on `stop_reason != null` before summing.**

Each API call produces 2–5 streaming JSONL entries with identical `usage` fields. Only the final entry (the one with a non-null `stop_reason`) is authoritative. Summing all entries causes ~1.86× overcounting. This was the root cause of all previous formula failures — inflated token counts forced the optimizer to fit artificially low weights (e.g., cr=0.58, limit=374M) that were artifacts of the bug, not physics.

```python
# Correct: filter streaming duplicates before aggregating
entries = [e for e in session_entries if e.get("stop_reason") is not None]
```

Failure to do this will make any calibration useless.

## Active Compute Model

**Formula A** (API-pricing-anchored, active 2026-03-26, 20 calibration points, MAE 1.54%, max error 3.66%, R² 0.977, spanning 52%→94% usage, limit refitted to 527.6M CU after 2026-03-27 corrections)

```
CU = Σ_model [ model_mult × (input×1.0 + output×5.0 + cache_creation×1.25 + cache_read×0.1) ]
Weekly limit: 527,627,120 CU
```

**Model multipliers** (derived from Anthropic API pricing ratios):

| Model | Multiplier | Derivation |
|-------|-----------|------------|
| claude-opus-4-6 | 1.667 | 5/3 vs Sonnet (API: $75/$15 input) |
| claude-sonnet-4-6 | 1.000 | baseline |
| claude-haiku-4-5 | 0.333 | 1/3 vs Sonnet (API: $1/$3 input) |

**Token type weights** (derived from API pricing ratios, normalized to input=1.0):

| Type | Weight | Derivation |
|------|--------|------------|
| input | 1.0 | baseline |
| output | 5.0 | API: $25/$5 output/input ratio (Opus), $15/$3 (Sonnet) |
| cache_creation | 1.25 | API: 5-min TTL write price ($3.75/$3 for Sonnet) |
| cache_read | 0.1 | API: cache read price ($0.30/$3 for Sonnet) |

**Key insight:** All formula parameters except the weekly limit are derived directly from Anthropic's published API pricing ratios. One free parameter (limit = 527.6M CU, LS-fitted 2026-03-27), everything else is physics. No empirical curve-fitting of token weights needed.

Model registry: `reference/models/registry.json`

## Formula History

| Formula | Period | Status | Notes |
|---------|--------|--------|-------|
| Formula A (original) | 2026-03-24–25 | superseded | cache_read=0; ignored dominant token type |
| Formula E | 2026-03-25 | retired | cr=0.58, limit=374M. Streaming dedup bug inflated token counts; weights were artifacts of overcounting, not real physics |
| Formula A (v2, current) | 2026-03-26– | **active** | API-pricing-anchored, 1 free parameter, R²=0.94 |

Formula E's cr=0.58 was not real — it was the optimizer compensating for ~1.86× overcounted cache reads. Once streaming dedup was fixed, the true weight (0.1, matching API pricing) emerged.

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

**JSONL parsing requirement:** Always filter entries to `stop_reason != null` before summing token fields. See [Streaming Dedup](#critical-streaming-dedup) above.

## Calibration Store

Location: `~/.config/token-track/calibrations.json`

Each calibration point records: pool, dashboard %, tokens consumed, per-model token snapshot, compute units, timestamp, and reset time. The tool uses median of inferred limits across all points for a pool.

**Timezone hygiene — CRITICAL:**
- All timestamps MUST be UTC. Store only UTC in the JSON.
- BKK = UTC+7, Dubai = UTC+4. Do NOT mix these offsets when converting from local time.
- When the user provides a timestamp with a city annotation (e.g., "09:25 BKK"), convert using the correct city offset before storing.
- On 2026-03-27, 4 calibration points were corrected: 08:31/09:25/19:03/20:30 on 2026-03-25 had been logged using UTC+4 (Dubai) instead of UTC+7 (Bangkok). Correct UTC times are 05:31/06:25/16:03/17:30. This shifted the limit estimate from 599.2M to 527.6M CU.

## Reference

Source code, documentation, model registry, and tests live under `reference/`:

- `reference/src/claude_monitor/` -- Python package source
- `reference/src/tests/` -- Test suite
- `reference/models/registry.json` -- Model version history with fit quality metrics
- `reference/doc/` -- Screenshots and assets
- `reference/README.md` -- Upstream documentation

## Codex Token Tracking

Codex usage is tracked separately via a companion script.

**Script:** `.private/codex_stats.py`
**Data source:** `~/.codex/sessions/**/*.jsonl`

Reads Codex session files to report token consumption for Codex Spark and other Codex engine variants. Useful for tracking parallel spend when running agent-mux workflows that dispatch to Codex alongside Claude.

Usage: run directly -- `python3 .private/codex_stats.py` -- or check its `--help` for options.

## Rate Limit Event (API-native data source)

Every Anthropic API response includes a `rate_limit_event` in the SSE stream with live utilization data.

**Schema:** `{status, rateLimitType, utilization, resetsAt, isUsingOverage}`
- `utilization` = 0.0-1.0 float (dashboard %). Only present above a threshold -- absent at low usage.
- Pools: `five_hour`, `seven_day`, `seven_day_opus`, `seven_day_sonnet`

**Persistence reality (corrected):**
- `rate_limit_event` is NOT persisted to JSONL files -- neither subagent nor interactive session logs contain it.
- The Claude Code SDK explicitly suppresses it before yielding to consumers: `return { type: "ignored" }`.
- Earlier notes claiming "SDK subagent JSONLs persist rate_limit_events" were WRONG.

**How the data surfaces (zero cost):**

1. `/usage` interactive command -- displays live session %, weekly %, sonnet % mid-session. Interactive-only, cannot be scripted.

2. Statusline stdin -- on every turn in interactive/TG sessions, the statusline command receives the full state payload via stdin. This includes:
   - `rate_limits.five_hour.used_percentage`
   - `rate_limits.seven_day.used_percentage`
   - `rate_limits.five_hour.resets_at`
   - `rate_limits.seven_day.resets_at`

**Statusline logger (recommended monitoring path):**

Patch `~/.claude/statusline.sh` (or whatever `CLAUDE_STATUSLINE_CMD` points to) to append rate limit data on every render:

```bash
input=$(cat)
# ... existing statusline logic ...
# Append rate limit data to log
echo "$input" | jq -c '{ts: now|todate, five_h: .rate_limits.five_hour.used_percentage, seven_d: .rate_limits.seven_day.used_percentage, resets_5h: .rate_limits.five_hour.resets_at, resets_7d: .rate_limits.seven_day.resets_at} | select(.seven_d != null)' >> ~/.claude/rate-limit-log.jsonl 2>/dev/null
```

- `select(.seven_d != null)` filters renders where rate_limits isn't populated yet.
- Fires on every turn -- continuous timeseries at zero API cost.
- Limitation: interactive sessions only. Headless subagent calls do not trigger the statusline.
- Threshold: server only includes utilization percentages above certain levels (approximately 75%/50%/25% for 7-day depending on time conditions, 90% for 5-hour). Low usage = absent field, not zero.

**Fallback:** `claude -p "hi"` forces a fresh event at negligible cost (surfaces in statusline stdin if running interactively).

## Off-Peak Promotions

Anthropic runs periodic promotions that modify metering. These affect calibration validity.

**March 13–28, 2026 promotion:**
- 5-hour session cap doubled during off-peak (weekdays outside 12:00–18:00 UTC + all weekends)
- Mechanism: bonus zone above normal cap. Tokens within normal cap count toward weekly at full rate. Tokens in bonus zone (above normal cap) exempt from weekly.
- NOT a flat 0.5× discount -- weekly meter grows at the same rate during off-peak as during peak
- Weekly calibration unaffected IF session usage stays below normal cap during off-peak periods
- Session% calibration invalid during promotion (denominator is 2X, not X)
- Post-promotion: re-validate formula with 3–5 fresh calibration points after March 28

Contrast: Holiday 2025 promotion doubled weekly caps directly -- different mechanism entirely.

## Coordinator Usage

For quick status checks, dispatch a subagent to run `token-track report --days 1 --json` and parse the result.
For context monitoring of TG bot sessions, use `token-track session --active --json`.
For anomaly alerts, `token-track burn --json` returns `anomaly: true/false` with ratio.
