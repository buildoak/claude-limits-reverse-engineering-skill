# claude-limits-reverse-engineering-skill
Reverse-engineering Claude Code usage limits from local JSONL ground truth.

## The problem
Claude Max at `$200/month` gives you throughput, then hides the meter. The dashboard shows percentages, throttling can arrive without warning, and Anthropic does not publish the formula that maps token activity to dashboard usage. That makes capacity planning guesswork when you are deciding whether to keep a long session alive, switch models, or stop before the wall.

The common community assumption has been simple: treat `cache_read = 0` and focus on input, output, and cache creation. That assumption is wrong. It misses the token class that dominates real Claude Code traffic, so it understates usage until the dashboard proves otherwise.

## The discovery
Current best fit is Formula E. `CU = input * 1 + output * 5 + cache_creation * 1 + cache_read * 0.58`. Weekly limit is about `374,000,000 CU`. The active registry entry in `reference/models/registry.json` was fit on `11` calibration points with `0.57%` MAE and `1.14%` max error. Dashboard percentages are integers, which injects about `+/-0.5%` rounding noise into every calibration point.

The important result is not the formula shape. It is what the weights say about cache reads. They cost about `58%` of fresh input, which matches server-side KV cache memory bandwidth, not free reuse. Formula A, the earlier model with `cache_read = 0`, fit worse because it ignored the dominant token type. Across the current calibration set, that means ignoring roughly `70%` of all tokens.

Calibration point 11 makes the failure mode obvious. Token composition was `cache_read 70%`, `cache_creation 30%`, `output 0.4%`, `input 0.04%`. CU contribution after weighting was `cache_read * 0.58 = 56%`, `cache_creation = 41%`, `output * 5 = 2.7%`. The meter is mostly cache activity, not fresh prompt text.

## Install
Clone the repo, enter the root, then install the package editable. The package target in `pyproject.toml` exposes a `token-track` CLI and requires Python `>=3.9`. Use a virtualenv if you want isolation.

```bash
cd claude-limits-reverse-engineering-skill
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Commands
`report` is the daily ledger. It aggregates usage by day, model, and project, then shows raw token counts, cost, message count, compute units, and estimated dashboard percent when calibration data exists.

```bash
token-track report --days 7
```

`session` groups raw JSONL entries into conversation sessions. Use it to inspect duration, context size, active status, model mix, and cost. Add `--active` to focus on sessions that touched disk within the last 30 minutes.

```bash
token-track session --active --days 1
```

`burn` compares the current usage window against a four-week baseline. It is the fastest way to spot that this week is materially hotter than normal before the weekly pool is gone.

```bash
token-track burn --days 7
```

`calibrate` records a dashboard reading into the local calibration store. Read the percent from the dashboard or screenshot, choose the pool, and save the point with an exact timestamp.

```bash
token-track calibrate --pool weekly-all --percent 52 --timestamp 2026-03-25T12:22:00+00:00 --days 7
```

`context` shows context growth from the input side of each turn: `input + cache_read + cache_creation`. That makes context bloat visible before the model context window becomes the real bottleneck.

```bash
token-track context --days 1
```

## How it works
The tool reads `~/.claude/projects/**/*.jsonl` directly. No API calls. Those files are the closest thing to ground truth because they reflect what Claude Code actually wrote after each request, including real `cache_read` hits. Cache TTL is `5 minutes`, and the JSONL log reflects the server truth of whether a hit happened.

The workflow is deliberately simple. Take a dashboard screenshot, note the integer percent, then run `token-track calibrate`. The point is stored in `~/.config/token-track/calibrations.json` together with a per-model token snapshot and derived compute units for that moment. From there, formula refits are driven by accumulated calibration data rather than by anecdote.

The model registry lives at `reference/models/registry.json`. It tracks formula versions, active status, fit date range, parameter values, and fit quality metrics such as MAE and max error. Right now the active entry is `formula-e-v1`.

There are separate meters to model. Weekly usage is split into two independent pools: `weekly-all` for all models and `weekly-sonnet` for the Sonnet-only bucket. The CLI also supports a `session` pool for the `5-hour` rolling session meter. Current operational assumptions are a weekly reset at Friday `05:00 UTC` and session usage measured on a rolling `5-hour` window.

## Current limitations
The current model is good enough to reason about weekly usage and compare candidate formulas. It is not full observability.

> **Note --** Anthropic exposes a dashboard endpoint at `/api/organizations/{orgId}/usage`, but Cloudflare blocks practical programmatic access. Calibration is manual for now.

> **Note --** The per-model multiplier is still unresolved. About `95%` of the current data is Opus, so Formula E nails token-type weighting first and leaves model-family multipliers at `1.0`.

> **Note --** The `weekly-sonnet` pool can drift from local JSONL totals because Anthropic includes Sonnet web usage that never appears in Claude Code session logs.

## Contributing
The highest-value contribution right now is new calibration data across the full percentage range. Range matters more than density. Another point at `55%` does little. A clean point below `20%` or above `80%` improves formula discrimination much faster.

Submit a calibration point with the dashboard screenshot, the observed pool, the exact UTC timestamp, and the CLI output that was saved locally. `weekly-all` and `weekly-sonnet` are both useful. Include the reset assumption if it differed from Friday `05:00 UTC`.

```bash
token-track calibrate --pool weekly-all --percent 52 --timestamp 2026-03-25T12:22:00+00:00 --days 7 --json
```

Open an issue or pull request with that JSON payload plus the screenshot. Redact account identifiers if needed. The point is to preserve a trustworthy `(timestamp, pool, percent, token snapshot)` record so the next refit has better range and less folklore.
