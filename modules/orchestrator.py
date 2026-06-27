"""Orchestrator — slim entry point. Composes all modules into a tick.

This module is intentionally thin. All logic lives in specialized modules:
  - title_pipeline: per-title search → enqueue
  - gap_detection: fill chapter gaps between Suwayomi and Kavita
  - stuck_recovery: detect deadlocked queues, restart Suwayomi
  - scan: mark "downloading" as "done" when complete
  - report: format summary table
  - state: load/save state
  - kavita: Kavita library check

Tick flow (single tick):
  1. Load state + spreadsheet
  2. Fetch Kavita titles
  3. Recover stuck queue (restart if deadlocked)
  4. Scan-and-mark-done (mark completed titles)
  5. Process titles (search → enqueue all undownloaded)
     - Each title enqueues ALL its undownloaded chapters in one bulk call
     - Only pauses if global queue size exceeds MAX_QUEUE_SIZE
  6. Gap detection (fill chapter gaps for titles in both libraries)
  7. Force-reenqueue download_error titles (if queue is small)
  8. Emit summary report
"""

import json
import time
from datetime import datetime

from .config import (
    TICK_BUDGET_SECONDS, ENQUEUE_BUDGET_SECONDS, SUMMARY_FILE,
    MAX_QUEUE_SIZE, _log_global,
)
from .state import load_state, save_state, save_progress
from .spreadsheet import load_titles
from .suwayomi import get_queue_info, start_downloader
from .kavita import get_kavita_titles
from .scan import scan_and_mark_done
from .report import emit_summary_table
from .title_pipeline import process_title_with_retry
from .gap_detection import detect_and_fill_gaps
from .stuck_recovery import recover_stuck_queue, force_reenqueue_if_queue_empty


# ============================================================================
# State helper
# ============================================================================

def _save_and_advance(state, title, status, manga_id, num_chapters, score, reason):
    """Persist state for processed title and advance index."""
    state["status"][title] = status
    if manga_id is not None:
        state["manga_ids"][title] = manga_id
    state["results"][title] = {
        "manga_id": manga_id,
        "status": status,
        "num_chapters": num_chapters,
        "score": score,
        "reason": reason,
        "found_at": datetime.now().isoformat(),
        "first_seen_tick": state.get("tick_count", 0),
    }
    state["scores"][title] = max(state["scores"].get(title, 0), score or 0)
    state["current_index"] += 1
    save_state(state)


# ============================================================================
# Main tick
# ============================================================================

def main():
    """Single tick of the manga auto-search pipeline."""
    # ── 1. Load state ───────────────────────────────────────────────────────
    state = load_state()
    titles = state.get("titles", [])

    # First run: load from spreadsheet
    if not titles:
        titles = load_titles()
        state["titles"] = titles
        state["current_index"] = 0
        if not titles:
            _log_global("❌ No titles found in spreadsheet, exiting")
            return
        _log_global(f"📋 Loaded {len(titles)} titles from spreadsheet")

    # Initialize summary slots
    for k in ("status", "manga_ids", "results", "scores"):
        if k not in state:
            state[k] = {}

    # ── 2. Fetch Kavita library ─────────────────────────────────────────────
    _log_global("🔎 Fetching Kavita library...")
    kavita_titles = get_kavita_titles()
    _log_global(f"   ✅ Got {len(kavita_titles)} Kavita titles")
    if not kavita_titles:
        _log_global("   ⚠️  Kavita API unreachable — falling back to manual skip list only")

    # ── 3. Tick budget + logger ────────────────────────────────────────────
    tick_start = time.time()
    log_lines = []

    def log(msg):
        ts = datetime.now().strftime('%H:%M:%S')
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        log_lines.append(line)

    def budget_ok():
        return (time.time() - tick_start) < TICK_BUDGET_SECONDS

    # ── 4. Recover stuck queue ─────────────────────────────────────────────
    log("🔧 Checking for stuck queue...")
    if recover_stuck_queue(state, log):
        save_progress("\n".join(log_lines))

    # ── 5. Scan-and-mark-done ──────────────────────────────────────────────
    scan_and_mark_done(state, log)

    # ── 6. Tick header ─────────────────────────────────────────────────────
    tick_number = state.get("tick_count", 0) + 1
    state["tick_count"] = tick_number
    log(f"\n🐛 TICK #{tick_number} @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} — resume at #{state['current_index']+1}/{len(titles)}")

    # ── 7. Main loop: process titles ───────────────────────────────────────
    enqueue_budget_remaining = ENQUEUE_BUDGET_SECONDS
    enqueue_successes_this_tick = 0

    while budget_ok() and state["current_index"] < len(titles):
        # Early exit: < 3 minutes remaining
        total_remaining = int(TICK_BUDGET_SECONDS - (time.time() - tick_start))
        if total_remaining < 180:
            log(f"\n⏰ Only {total_remaining}s remaining in tick budget, exiting")
            break

        title = titles[state["current_index"]]
        log(f"\n--- #{state['current_index']+1}/{len(titles)}: '{title}' ---")

        # Skip titles with terminal or in-progress status
        existing = state["status"].get(title)
        if existing in ("done", "kavita_skip", "not_found", "downloading", "download_pending", "download_error"):
            if existing in ("downloading", "download_pending"):
                log(f"   ⏭️  Status '{existing}' — title still in progress, skipping (will retry next tick)")
            elif existing == "download_error":
                log(f"   ⏭️  Status 'download_error' — will force-retry if queue empty (later in tick)")
            else:
                log(f"   ⏭️  Status already '{existing}', advancing")
            state["current_index"] += 1
            save_state(state)
            continue

        # Queue size guard — if queue too big, pause
        q = get_queue_info()
        if q and q["queue_size"] >= MAX_QUEUE_SIZE:
            log(f"\n🛑 Queue size {q['queue_size']} >= MAX_QUEUE_SIZE={MAX_QUEUE_SIZE} — pausing enqueue")
            log(f"   📚 Already downloading: {sum(1 for s in state['status'].values() if s == 'downloading')} titles")
            log(f"   ⏭️  Next tick will resume at #{state['current_index']+1}")
            break

        # Enqueue budget check
        if enqueue_budget_remaining <= 0:
            log(f"\n⏰ Enqueue budget exhausted ({ENQUEUE_BUDGET_SECONDS}s of network-I/O)")
            break

        # Process the title
        t0 = time.time()
        status, manga_id, num_chapters, score = process_title_with_retry(title, kavita_titles, log)
        t1 = time.time()
        elapsed = t1 - t0
        enqueue_budget_remaining -= elapsed

        # Persist result
        reason = "kavita_skip" if status == "kavita_skip" else "auto_search"
        _save_and_advance(state, title, status, manga_id, num_chapters, score, reason)
        log(f"   ✅ Outcome: '{title}' → {status} ({num_chapters} ch) [{elapsed:.1f}s]")
        save_progress("\n".join(log_lines))

        # Reset budget on success
        if status == "downloading":
            enqueue_successes_this_tick += 1
            enqueue_budget_remaining = ENQUEUE_BUDGET_SECONDS
        elif status == "done" or status == "kavita_skip":
            log(f"   ⏭️  Terminal status (no new enqueue) — continuing search")

        # Periodic summary
        pending = sum(1 for s in state["status"].values() if s == "pending")
        downloading = sum(1 for s in state["status"].values() if s == "downloading")
        done = sum(1 for s in state["status"].values() if s == "done")
        not_found = sum(1 for s in state["status"].values() if s == "not_found")
        kavita_skip = sum(1 for s in state["status"].values() if s == "kavita_skip")
        download_error = sum(1 for s in state["status"].values() if s == "download_error")
        elapsed_total = int(time.time() - tick_start)
        log(f"   📊 pending={pending} downloading={downloading} done={done} "
            f"not_found={not_found} kavita_skip={kavita_skip} error={download_error}  ⏱️  {elapsed_total}s")

    # ── 8. Gap detection: fill chapter gaps between Suwayomi and Kavita ────
    log("\n🔍 Gap detection pass...")
    gap_result = detect_and_fill_gaps(state, kavita_titles, log)
    if gap_result["filled"]:
        log(f"   ✅ Filled {gap_result['filled']} gaps")
    if gap_result["errors"]:
        log(f"   ⚠️  {gap_result['errors']} gap-fill errors")

    # ── 9. Force-reenqueue download_error titles (if queue is small) ───────
    log("\n🔄 Force-retry pass for download_error titles...")
    reenqueued = force_reenqueue_if_queue_empty(state, log)
    if reenqueued:
        log(f"   ✅ Force-reenqueued {reenqueued} titles")

    # ── 10. Final summary ──────────────────────────────────────────────────
    if not budget_ok():
        log(f"\n⏰ Tick budget exhausted ({TICK_BUDGET_SECONDS}s), stopping")
    elif state["current_index"] >= len(titles):
        log(f"\n✅ All {len(titles)} titles processed")
    else:
        log(f"\n💤 Tick exited cleanly")

    # ── 11. Emit formatted report ──────────────────────────────────────────
    emit_summary_table(state, titles, log, SUMMARY_FILE, MAX_QUEUE_SIZE)

    summary = {
        "total": len(titles),
        "processed": state["current_index"],
        "pending": sum(1 for s in state["status"].values() if s == "pending"),
        "downloading": sum(1 for s in state["status"].values() if s == "downloading"),
        "done": sum(1 for s in state["status"].values() if s == "done"),
        "not_found": sum(1 for s in state["status"].values() if s == "not_found"),
        "kavita_skip": sum(1 for s in state["status"].values() if s == "kavita_skip"),
        "download_error": sum(1 for s in state["status"].values() if s == "download_error"),
        "last_update": datetime.now().isoformat(),
    }
    with open(SUMMARY_FILE, 'w') as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
