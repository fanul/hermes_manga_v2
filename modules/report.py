"""Report — formatted tick summary table.

User-approved format (Fanul, 2026-06-27):
- Status counts as markdown TABLE (not bullet list) — header | Status | Count | Δ |
- Emoji-prefix per row: ✅ done, 📥 downloading, ⏳ download_pending, ❌ not_found, 📚 kavita_skip
- New since last tick section (delta vs previous summary)
- Notes section (queue state, next-tick resume point)
- Column widths aligned, separator matches header

This module is the ONLY place that knows the report shape. Call `emit_summary_table()`
from orchestrator at end of tick. To change the report format, edit this file only.
"""

import json
import os
from datetime import datetime


def emit_summary_table(state, titles, log, summary_file, max_enqueues_per_tick):
    """Emit the formatted tick summary table at end of tick.

    Args:
        state:           current state dict (from load_state)
        titles:          list of all titles (from spreadsheet)
        log:             callable(string) — appends to log buffer + prints
        summary_file:    path to last-tick summary JSON (for delta calc)
        max_enqueues_per_tick: constant value for the Notes section
    """
    statuses = state.get("status", {})
    counts = {
        "done":             sum(1 for s in statuses.values() if s == "done"),
        "downloading":      sum(1 for s in statuses.values() if s == "downloading"),
        "download_pending": sum(1 for s in statuses.values() if s == "download_pending"),
        "not_found":        sum(1 for s in statuses.values() if s == "not_found"),
        "kavita_skip":      sum(1 for s in statuses.values() if s == "kavita_skip"),
        "pending":          sum(1 for s in statuses.values() if s == "pending"),
    }
    total = len(titles)
    current_index = state.get("current_index", 0)
    tick_number = state.get("tick_count", 0)

    # Load previous tick for delta calculation
    prev_counts = _load_prev_counts(summary_file)

    # ── Header ─────────────────────────────────────────────────────────────
    log("\n" + "=" * 78)
    log(f"📊 MANGA AUTO-SEARCH TICK #{tick_number} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 78)

    # ── Status counts table (markdown, fixed-width) ────────────────────────
    log("")
    log("| Status              | Count | Δ vs prev |")
    log("|---------------------|-------|-----------|")
    log(f"| ✅ done              | {counts['done']:5d} | {_delta(counts['done'],   prev_counts.get('done'),   '✅'):9s} |")
    log(f"| 📥 downloading       | {counts['downloading']:5d} | {_delta(counts['downloading'], prev_counts.get('downloading'), '📥'):9s} |")
    log(f"| ⏳ download_pending  | {counts['download_pending']:5d} | (n/a)        |")
    log(f"| ❌ not_found         | {counts['not_found']:5d} | {_delta(counts['not_found'], prev_counts.get('not_found'), '❌'):9s} |")
    log(f"| 📚 kavita_skip       | {counts['kavita_skip']:5d} | {_delta(counts['kavita_skip'], prev_counts.get('kavita_skip'), '📚'):9s} |")
    log(f"| ⏳ pending           | {counts['pending']:5d} | (n/a)        |")
    log(f"| **total**           | **{total}** |             |")
    log("")
    pct = (current_index * 100 // total) if total else 0
    log(f"📍 Index: {current_index}/{total} ({pct}%)")

    # ── New since last tick ────────────────────────────────────────────────
    new_done = counts["done"] - prev_counts.get("done", 0)
    new_nf   = counts["not_found"] - prev_counts.get("not_found", 0)
    new_dl   = counts["downloading"] - prev_counts.get("downloading", 0)
    new_ks   = counts["kavita_skip"] - prev_counts.get("kavita_skip", 0)
    log("")
    log("🆕 New since last tick:")
    log(f"   ✅ done: +{new_done} | ❌ not_found: +{new_nf} | "
        f"📥 downloading: {new_dl:+d} | 📚 kavita_skip: +{new_ks}")

    # ── Notes ──────────────────────────────────────────────────────────────
    log("")
    log("📝 Notes:")
    log(f"   • MAX_ENQUEUES_PER_TICK = {max_enqueues_per_tick} (Suwayomi overload protection)")
    log(f"   • Next tick resumes at #{current_index + 1}")
    log(f"   • Already-downloading titles: {counts['downloading']} (preserved, not re-enqueued)")
    log("=" * 78)


# ============================================================================
# Helpers
# ============================================================================

def _delta(curr, prev, emoji):
    """Format delta string for table cell. Returns '—' if no prev, '+N ✅'/'−N ✅' otherwise."""
    if prev is None:
        return "—"
    diff = curr - prev
    if diff > 0:
        return f"+{diff} {emoji}"
    elif diff < 0:
        return f"−{abs(diff)} {emoji}"
    return "—"


def _load_prev_counts(summary_file):
    """Load previous tick's counts from summary JSON. Returns {} on any failure."""
    try:
        if os.path.exists(summary_file):
            with open(summary_file) as f:
                prev = json.load(f)
            return {
                k: prev.get(k, 0)
                for k in ("done", "downloading", "not_found", "kavita_skip")
            }
    except Exception:
        pass
    return {}