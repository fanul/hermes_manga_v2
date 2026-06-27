"""Orchestrator — process_title() + main() composition.

This is the only module that knows the full lifecycle. It composes:
  state, spreadsheet, suwayomi, library, scan, search, kavita
into the per-title processing pipeline and tick loop.

Key responsibilities:
- Kavita skip check (cache + manual list)
- Per-title: search → fetch → chapter probe → enqueue
- Internal retry (5 attempts) for chapter indexing lag
- Live tick budget enforcement
- Summary emission

Each `process_title` call is a single, well-defined attempt. The caller
handles retries and recovery.
"""

import json
import time
from datetime import datetime

from .config import (
    TICK_BUDGET_SECONDS, ENQUEUE_BUDGET_SECONDS, MAX_INTERNAL_RETRIES, RETRY_DELAY, SUMMARY_FILE,
    MAX_ENQUEUES_PER_TICK, _log_global,
)
from .state import load_state, save_state, save_progress
from .spreadsheet import load_titles
from .suwayomi import (
    fetch_manga, update_manga_in_library, get_chapter_ids,
    enqueue_chapters_bulk, start_downloader, get_queue_info,
)
from .library import retry_error_chapters
from .queue_ops import restart_suwayomi_container, clear_download_queue
from .scan import scan_and_mark_done
from .search import find_best_match, find_fuzzy_match
from .kavita import (
    get_kavita_titles, is_in_kavita, is_in_manual_kavita_skip,
)


# ============================================================================
# Per-title pipeline
# ============================================================================

def process_title(title, log):
    """Process a single title: search → fetch → enqueue.
    
    Returns: (status, manga_id, num_chapters, score)
        status in {"done", "downloading", "not_found", "download_pending", "skipped", "error"}
    """
    log(f"   🔍 Searching Suwayomi for: '{title}'")
    match, score = find_best_match(title, log)
    if not match or score < 40:
        return "not_found", None, 0, 0

    manga_dict, source_id, matched_variant, ch_count = match
    manga_id = manga_dict["id"]
    log(f"   🎯 Best match: '{manga_dict['title']}' (score={score:.0f}, ch={ch_count})")

    # Step 1: ensure manga is in library and update metadata
    update_manga_in_library(manga_id, True)

    # Step 2: probe chapters (waits for source indexing)
    chapter_title, chapter_ids = get_chapter_ids(manga_id)
    if not chapter_ids:
        # Source still indexing — return download_pending for caller to retry
        return "download_pending", manga_id, 0, score

    # Step 3: enqueue all chapters
    enqueue_chapters_bulk(chapter_ids)

    # Step 4: start downloader
    start_downloader()

    # Step 5: post-enqueue verify
    time.sleep(2)
    post_queue = get_queue_info()
    if post_queue is not None and post_queue["queue_size"] == 0 and post_queue["active"] == 0:
        log(f"   ⚠️  Queue empty after enqueue — all chapters already downloaded")
        return "done", manga_id, len(chapter_ids), score

    return "downloading", manga_id, len(chapter_ids), score


# ============================================================================
# Per-title outer loop (kavita + retries + fuzzy fallback)
# ============================================================================

def _process_with_retry(title, log, kavita_titles):
    """Outer loop: kavita check → 5 attempts → fuzzy fallback.
    
    Mutates state and returns nothing (state has all the result data).
    Returns True if progress was made (callers use this to break infinite loop).
    """
    # Kavita skip check (API)
    if kavita_titles and is_in_kavita(title, kavita_titles):
        log(f"   📚 Search trail: kavita ✅ → kavita_skip")
        log(f"   ⏭️  Outcome: marked kavita_skip → advance to next")
        return "kavita_skip", None, 0, 0

    # Kavita manual list check
    if is_in_manual_kavita_skip(title):
        log(f"   📚 Search trail: kavita (manual) → kavita_skip")
        return "kavita_skip", None, 0, 0

    # Try up to MAX_INTERNAL_RETRIES times
    for attempt in range(MAX_INTERNAL_RETRIES):
        status, manga_id, num_chapters, score = process_title(title, log)

        if status == "not_found":
            # Fuzzy fallback
            log(f"   🔄 Not found in standard search, trying fuzzy...")
            fuzzy_match, fuzzy_score, fuzzy_ch_count = find_fuzzy_match(title, log)
            if fuzzy_match and fuzzy_score >= 40:
                fuzzy_id = fuzzy_match[0]["id"]
                log(f"   ✅ Fuzzy match: '{fuzzy_match[0]['title']}' (score={fuzzy_score:.0f}, ch={fuzzy_ch_count})")
                update_manga_in_library(fuzzy_id, True)
                time.sleep(2)
                chapter_title, chapter_ids = get_chapter_ids(fuzzy_id)
                if not chapter_ids:
                    return "not_found", fuzzy_id, 0, fuzzy_score
                enqueue_chapters_bulk(chapter_ids)
                start_downloader()
                time.sleep(2)
                post_queue = get_queue_info()
                if post_queue is not None and post_queue["queue_size"] == 0 and post_queue["active"] == 0:
                    return "done", fuzzy_id, len(chapter_ids), fuzzy_score
                return "downloading", fuzzy_id, len(chapter_ids), fuzzy_score
            return "not_found", None, 0, 0

        if status == "download_pending":
            if attempt < MAX_INTERNAL_RETRIES - 1:
                log(f"   🔄 Chapter belum ter-index (attempt {attempt+1}/{MAX_INTERNAL_RETRIES}), retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
                continue
            # Last attempt: still 0 chapters, try fuzzy
            log(f"   ❌ After {MAX_INTERNAL_RETRIES} attempts still 0 chapters → fuzzy fallback")
            fuzzy_match, fuzzy_score, fuzzy_ch_count = find_fuzzy_match(title, log)
            if fuzzy_match and fuzzy_score >= 80:
                fuzzy_id = fuzzy_match[0]["id"]
                log(f"   ✅ Fuzzy high-score match: {fuzzy_match[0]['title']}")
                update_manga_in_library(fuzzy_id, True)
                time.sleep(2)
                chapter_title, chapter_ids = get_chapter_ids(fuzzy_id)
                if not chapter_ids:
                    return "not_found", fuzzy_id, 0, fuzzy_score
                enqueue_chapters_bulk(chapter_ids)
                start_downloader()
                time.sleep(2)
                post_queue = get_queue_info()
                if post_queue is not None and post_queue["queue_size"] == 0 and post_queue["active"] == 0:
                    return "done", fuzzy_id, len(chapter_ids), fuzzy_score
                return "downloading", fuzzy_id, len(chapter_ids), fuzzy_score
            return "not_found", manga_id, 0, score

        # success path
        return status, manga_id, num_chapters, score

    return "not_found", None, 0, 0


# ============================================================================
# Main tick
# ============================================================================

def main():
    """Single tick of the manga auto-search pipeline."""
    # Reload state
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

    # Load Kavita titles
    _log_global("🔎 Fetching Kavita library...")
    kavita_titles = get_kavita_titles()
    _log_global(f"   ✅ Got {len(kavita_titles)} Kavita titles")
    if not kavita_titles:
        _log_global("   ⚠️  Kavita API unreachable — falling back to manual skip list only")

    # Tick budget
    tick_start = time.time()
    log_lines = []
    def log(msg):
        ts = datetime.now().strftime('%H:%M:%S')
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        log_lines.append(line)

    def budget_ok():
        """Total wall-time budget (cron wrapper safety net)."""
        return (time.time() - tick_start) < TICK_BUDGET_SECONDS

    # Adaptive enqueue budget: tracks only network-I/O time (search/enqueue calls).
    # Bookkeeping (done/not_found/kavita_skip) is instant and does NOT consume budget.
    # Every time we successfully enqueue (status="downloading"), reset the budget
    # so the tick continues searching for more titles to enqueue.
    enqueue_budget_remaining = ENQUEUE_BUDGET_SECONDS
    last_enqueue_wall = time.time()
    enqueue_attempts_this_tick = 0
    enqueue_successes_this_tick = 0

    def save_and_advance(title, status, manga_id=None, num_chapters=0, score=0, reason=""):
        state["status"][title] = status
        if manga_id is not None:
            state["manga_ids"][title] = manga_id
        state["results"][title] = {
            "manga_id": manga_id, "status": status, "num_chapters": num_chapters,
            "score": score, "reason": reason, "found_at": datetime.now().isoformat(),
        }
        state["scores"][title] = max(state["scores"].get(title, 0), score or 0)
        state["current_index"] += 1
        save_state(state)
        save_progress("\n".join(log_lines))

    # Recovery: detect deadlocked queue and recover
    q = get_queue_info()
    if q and q["all_error"] and q["queue_size"] > 0:
        log(f"⚠️  Queue has {q['queue_size']} items all in ERROR state — recovering...")
        if restart_suwayomi_container():
            log("✅ Suwayomi container restarted, clearing queue...")
            if clear_download_queue():
                log("✅ Queue cleared, re-enqueuing tracked manga...")
                tracked_ids = list(state.get("manga_ids", {}).values())
                enqueued, checked, skipped, skip_msg = retry_error_chapters(tracked_ids, state)
                if enqueued > 0:
                    log(f"✅ {enqueued} undownloaded chapters re-enqueued (from {checked} manga)")
                if skip_msg:
                    log(skip_msg)

    # Scan: mark stale "downloading" entries as done if all chapters downloaded
    scan_and_mark_done(state, log)

    # Main loop
    tick_number = state.get("tick_count", 0) + 1
    state["tick_count"] = tick_number
    log(f"\n🐛 TICK #{tick_number} @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} — resume at #{state['current_index']+1}/{len(titles)}")

    while budget_ok() and state["current_index"] < len(titles):
        # Early exit: < 3 minutes total remaining (hard safety net)
        total_remaining = int(TICK_BUDGET_SECONDS - (time.time() - tick_start))
        if total_remaining < 180:
            log(f"\n⏰ Only {total_remaining}s total remaining in tick budget, exiting to let next tick resume")
            break

        title = titles[state["current_index"]]
        log(f"\n--- #{state['current_index']+1}/{len(titles)}: '{title}' ---")
        log(f"   ⏱️  Total remaining: {total_remaining}s | Enqueue budget: {enqueue_budget_remaining:.0f}s | Enqueued: {enqueue_successes_this_tick}")

        # Skip titles that already have a terminal or in-progress status (instant bookkeeping — no budget cost)
        existing = state["status"].get(title)
        if existing in ("done", "kavita_skip", "not_found", "downloading", "download_pending"):
            if existing in ("downloading", "download_pending"):
                log(f"   ⏭️  Status '{existing}' — title still in progress, skipping (will retry next tick)")
            else:
                log(f"   ⏭️  Status already '{existing}', advancing")
            state["current_index"] += 1
            save_state(state)
            continue

        # Before processing, check if we have enqueue budget left
        if enqueue_budget_remaining <= 0:
            log(f"\n⏰ Enqueue budget exhausted ({ENQUEUE_BUDGET_SECONDS}s of network-I/O consumed this batch)")
            if enqueue_successes_this_tick == 0:
                log(f"   💡 No titles enqueued this batch — giving next tick a fresh start")
                save_and_advance(title, "not_found", reason="enqueue_budget_exhausted_no_success")
                log(f"   ✅ Outcome: '{title}' → not_found (budget guard)")
            else:
                log(f"   📊 Batch complete: {enqueue_successes_this_tick} titles enqueued this batch")
            break

        # Process with retry+fuzzy
        t0 = time.time()
        status, manga_id, num_chapters, score = _process_with_retry(title, log, kavita_titles)
        t1 = time.time()
        elapsed = t1 - t0

        # Track network-I/O time (only for titles that actually need processing)
        enqueue_budget_remaining -= elapsed
        enqueue_attempts_this_tick += 1

        save_and_advance(title, status, manga_id, num_chapters, score,
                         reason="kavita_skip" if status == "kavita_skip" else "auto_search")
        log(f"   ✅ Outcome: '{title}' → {status} ({num_chapters} ch) [{elapsed:.1f}s]")

        # Count successful enqueues. Stop the tick after MAX_ENQUEUES_PER_TICK
        # to avoid overloading Suwayomi with too many parallel chapter downloads.
        if status == "downloading":
            enqueue_successes_this_tick += 1
            if enqueue_successes_this_tick >= MAX_ENQUEUES_PER_TICK:
                log(f"\n🛑 Hit MAX_ENQUEUES_PER_TICK={MAX_ENQUEUES_PER_TICK} — stopping tick to avoid Suwayomi overload")
                log(f"   📚 Already downloading: {sum(1 for s in state['status'].values() if s == 'downloading')} titles")
                log(f"   ⏭️  Next tick will resume at #{state['current_index']+1}")
                break
        elif status == "done" or status == "kavita_skip":
            # Terminal status — instant bookkeeping, does NOT count as enqueue
            log(f"   ⏭️  Terminal status (no new enqueue) — continuing search")

        # Periodic summary
        pending = sum(1 for s in state["status"].values() if s == "pending")
        downloading = sum(1 for s in state["status"].values() if s == "downloading")
        done = sum(1 for s in state["status"].values() if s == "done")
        not_found = sum(1 for s in state["status"].values() if s == "not_found")
        kavita_skip = sum(1 for s in state["status"].values() if s == "kavita_skip")
        elapsed_total = int(time.time() - tick_start)
        log(f"   📊 pending={pending} downloading={downloading} done={done} "
            f"not_found={not_found} kavita_skip={kavita_skip}  ⏱️  {elapsed_total}s")

    # Final summary
    if not budget_ok():
        log(f"\n⏰ Tick budget exhausted ({TICK_BUDGET_SECONDS}s), stopping")
    elif state["current_index"] >= len(titles):
        log(f"\n✅ All {len(titles)} titles processed")
    else:
        log(f"\n💤 Tick exited cleanly")

    summary = {
        "total": len(titles),
        "processed": state["current_index"],
        "pending": sum(1 for s in state["status"].values() if s == "pending"),
        "downloading": sum(1 for s in state["status"].values() if s == "downloading"),
        "done": sum(1 for s in state["status"].values() if s == "done"),
        "not_found": sum(1 for s in state["status"].values() if s == "not_found"),
        "kavita_skip": sum(1 for s in state["status"].values() if s == "kavita_skip"),
        "last_update": datetime.now().isoformat(),
    }
    with open(SUMMARY_FILE, 'w') as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()