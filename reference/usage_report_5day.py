#!/usr/bin/env python3
"""5-day usage report for March 20-24, 2026.
Runs the Claude Monitor analysis API and produces a structured daily breakdown.
"""

import sys
sys.path.insert(0, '/Users/otonashi/thinking/building/Claude-Code-Usage-Monitor/src')

import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from claude_monitor.data.analysis import analyze_usage

# March 20 00:00 UTC to March 25 00:00 UTC = 5 full days
# Calculate hours_back from now to March 20 00:00 UTC
now = datetime.now(timezone.utc)
target_start = datetime(2026, 3, 20, 0, 0, 0, tzinfo=timezone.utc)
hours_back = int((now - target_start).total_seconds() / 3600) + 1  # +1 buffer

print(f"[CONFIG] Now: {now.isoformat()}")
print(f"[CONFIG] Target start: {target_start.isoformat()}")
print(f"[CONFIG] Hours back: {hours_back}")
print(f"[CONFIG] Loading data...\n")

result = analyze_usage(hours_back=hours_back, use_cache=False, quick_start=False)

blocks = result.get("blocks", [])
metadata = result.get("metadata", {})

print(f"[METADATA] Entries processed: {metadata.get('entries_processed', 0)}")
print(f"[METADATA] Blocks created: {metadata.get('blocks_created', 0)}")
print(f"[METADATA] Limits detected: {metadata.get('limits_detected', 0)}")
print(f"[METADATA] Load time: {metadata.get('load_time_seconds', 0):.1f}s")
print(f"[METADATA] Transform time: {metadata.get('transform_time_seconds', 0):.3f}s")
print()

# Group blocks by day (UTC)
daily = defaultdict(lambda: {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_creation_tokens": 0,
    "cache_read_tokens": 0,
    "cost_usd": 0.0,
    "blocks": 0,
    "entries": 0,
    "models": defaultdict(lambda: {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "cost": 0.0, "entries": 0}),
    "peak_block_id": None,
    "peak_block_tokens": 0,
    "peak_block_cost": 0.0,
    "limit_messages": [],
})

# Grand totals
grand = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_creation_tokens": 0,
    "cache_read_tokens": 0,
    "cost_usd": 0.0,
    "entries": 0,
    "blocks": 0,
    "models": defaultdict(lambda: {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "cost": 0.0, "entries": 0}),
}

target_end = datetime(2026, 3, 25, 0, 0, 0, tzinfo=timezone.utc)

for block in blocks:
    if block.get("isGap"):
        continue

    start_str = block.get("startTime", "")
    try:
        block_start = datetime.fromisoformat(start_str)
    except (ValueError, TypeError):
        continue

    # Filter to our 5-day window
    if block_start < target_start or block_start >= target_end:
        continue

    day_key = block_start.strftime("%Y-%m-%d")
    tc = block.get("tokenCounts", {})
    inp = tc.get("inputTokens", 0)
    out = tc.get("outputTokens", 0)
    cc = tc.get("cacheCreationInputTokens", 0)
    cr = tc.get("cacheReadInputTokens", 0)
    cost = block.get("costUSD", 0.0)
    total_tok = block.get("totalTokens", 0)
    entries_count = block.get("entries_count", 0)

    d = daily[day_key]
    d["input_tokens"] += inp
    d["output_tokens"] += out
    d["cache_creation_tokens"] += cc
    d["cache_read_tokens"] += cr
    d["cost_usd"] += cost
    d["blocks"] += 1
    d["entries"] += entries_count

    # Track peak block
    if total_tok > d["peak_block_tokens"]:
        d["peak_block_tokens"] = total_tok
        d["peak_block_cost"] = cost
        d["peak_block_id"] = block.get("id", "?")

    # Model breakdown
    for model, stats in block.get("perModelStats", {}).items():
        m = d["models"][model]
        m["input"] += stats.get("input_tokens", 0)
        m["output"] += stats.get("output_tokens", 0)
        m["cache_create"] += stats.get("cache_creation_tokens", 0)
        m["cache_read"] += stats.get("cache_read_tokens", 0)
        m["cost"] += stats.get("cost_usd", 0.0)
        m["entries"] += stats.get("entries_count", 0)

        gm = grand["models"][model]
        gm["input"] += stats.get("input_tokens", 0)
        gm["output"] += stats.get("output_tokens", 0)
        gm["cache_create"] += stats.get("cache_creation_tokens", 0)
        gm["cache_read"] += stats.get("cache_read_tokens", 0)
        gm["cost"] += stats.get("cost_usd", 0.0)
        gm["entries"] += stats.get("entries_count", 0)

    # Limit messages
    if block.get("limitMessages"):
        for lm in block["limitMessages"]:
            d["limit_messages"].append({
                "block_id": block.get("id"),
                "type": lm.get("type"),
                "timestamp": lm.get("timestamp"),
                "content": lm.get("content", "")[:200],
            })

    grand["input_tokens"] += inp
    grand["output_tokens"] += out
    grand["cache_creation_tokens"] += cc
    grand["cache_read_tokens"] += cr
    grand["cost_usd"] += cost
    grand["entries"] += entries_count
    grand["blocks"] += 1


# ---- REPORT ----
print("=" * 80)
print("  CLAUDE CODE USAGE REPORT: March 20-24, 2026 (5 days)")
print("=" * 80)

for day in sorted(daily.keys()):
    d = daily[day]
    print(f"\n{'─' * 80}")
    print(f"  {day}")
    print(f"{'─' * 80}")
    print(f"  Blocks: {d['blocks']}    API calls: {d['entries']}")
    print(f"  Input tokens:          {d['input_tokens']:>14,}")
    print(f"  Output tokens:         {d['output_tokens']:>14,}")
    print(f"  Cache creation tokens: {d['cache_creation_tokens']:>14,}")
    print(f"  Cache read tokens:     {d['cache_read_tokens']:>14,}")
    total_tok = d['input_tokens'] + d['output_tokens']
    print(f"  Total (in+out):        {total_tok:>14,}")
    print(f"  Cost (USD):            ${d['cost_usd']:>13.2f}")
    print()
    print(f"  Model Breakdown:")
    for model in sorted(d["models"].keys()):
        m = d["models"][model]
        mtotal = m["input"] + m["output"]
        print(f"    {model}:")
        print(f"      in={m['input']:,}  out={m['output']:,}  cc={m['cache_create']:,}  cr={m['cache_read']:,}  cost=${m['cost']:.2f}  calls={m['entries']}")
    print()
    if d["peak_block_id"]:
        print(f"  Peak block: {d['peak_block_id']}")
        print(f"    Tokens: {d['peak_block_tokens']:,}  Cost: ${d['peak_block_cost']:.2f}")

    if d["limit_messages"]:
        print(f"\n  *** LIMIT EVENTS: {len(d['limit_messages'])} ***")
        for lm in d["limit_messages"]:
            print(f"    [{lm['type']}] {lm['timestamp']}")
            print(f"      {lm['content'][:150]}")


print(f"\n{'=' * 80}")
print(f"  GRAND TOTALS (March 20-24)")
print(f"{'=' * 80}")
print(f"  Sessions/blocks: {grand['blocks']}")
print(f"  API calls:       {grand['entries']}")
print(f"  Input tokens:          {grand['input_tokens']:>14,}")
print(f"  Output tokens:         {grand['output_tokens']:>14,}")
print(f"  Cache creation tokens: {grand['cache_creation_tokens']:>14,}")
print(f"  Cache read tokens:     {grand['cache_read_tokens']:>14,}")
gtotal = grand['input_tokens'] + grand['output_tokens']
print(f"  Total (in+out):        {gtotal:>14,}")
print(f"  TOTAL COST (USD):      ${grand['cost_usd']:>13.2f}")

print(f"\n  Model Breakdown (Grand):")
for model in sorted(grand["models"].keys()):
    m = grand["models"][model]
    pct = (m["cost"] / grand["cost_usd"] * 100) if grand["cost_usd"] > 0 else 0
    print(f"    {model}:")
    print(f"      in={m['input']:,}  out={m['output']:,}  cc={m['cache_create']:,}  cr={m['cache_read']:,}")
    print(f"      cost=${m['cost']:.2f} ({pct:.1f}%)  calls={m['entries']}")


# High-token blocks (approaching limits)
print(f"\n{'=' * 80}")
print(f"  HIGH-TOKEN BLOCKS (>500K total tokens)")
print(f"{'=' * 80}")

high_token_blocks = []
for block in blocks:
    if block.get("isGap"):
        continue
    start_str = block.get("startTime", "")
    try:
        block_start = datetime.fromisoformat(start_str)
    except (ValueError, TypeError):
        continue
    if block_start < target_start or block_start >= target_end:
        continue
    total_tok = block.get("totalTokens", 0)
    if total_tok > 500_000:
        high_token_blocks.append(block)

high_token_blocks.sort(key=lambda b: b.get("totalTokens", 0), reverse=True)

if not high_token_blocks:
    print("  (none)")
else:
    for b in high_token_blocks[:20]:
        tc = b.get("tokenCounts", {})
        print(f"  {b['id']}  tokens={b['totalTokens']:,}  cost=${b['costUSD']:.2f}  calls={b['entries_count']}  models={b['models']}")
        print(f"    in={tc.get('inputTokens',0):,}  out={tc.get('outputTokens',0):,}  cc={tc.get('cacheCreationInputTokens',0):,}  cr={tc.get('cacheReadInputTokens',0):,}")
        if b.get("limitMessages"):
            print(f"    *** HAS LIMIT MESSAGES: {len(b['limitMessages'])} ***")

# Per-day cost ranking
print(f"\n{'=' * 80}")
print(f"  DAILY COST RANKING")
print(f"{'=' * 80}")
for day in sorted(daily.keys(), key=lambda d: daily[d]["cost_usd"], reverse=True):
    d = daily[day]
    bar_len = int(d["cost_usd"] / max(daily[dk]["cost_usd"] for dk in daily) * 40) if daily else 0
    bar = "#" * bar_len
    print(f"  {day}  ${d['cost_usd']:>8.2f}  {bar}")

print(f"\n  Average daily cost: ${grand['cost_usd']/max(len(daily),1):.2f}")
print(f"  Projected weekly:   ${grand['cost_usd']/max(len(daily),1)*7:.2f}")
