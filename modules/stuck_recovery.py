"""Stuck-title recovery — detect when title-A in queue is blocking title-B.

Bug scenario (Fanul, 2026-06-27):
- Tick processes title B, finds undownloaded chapters
- Queue has title A stuck (all error / deadlocked)
- Title B's enqueue looks fine, but the downloader is hung on title A
- Result: title B marked "download_pending" forever, queue stays full of errors

Recovery logic:
1. Detect all_error queue → Suwayomi deadlocked
2. Restart container, clear queue, mark stuck titles as "download_error"
3. Retry title B in same tick — it will now go through cleanly
4. If title B gets stuck 3 ticks in a row, force-enqueue + flag title A "download_error"

This module owns the retry-counter and stuck-detection. orchestrator just calls
`recover_stuck_queue(state, log)` at start of each tick.
"""

from .queue_ops import restart_suwayomi_container, clear_download_queue
from .suwayomi import (
    get_queue_info, get_downloaded_count,
    enqueue_undownloaded, start_downloader,
)
from .state import save_state


# Stuck threshold — if a title is in "downloading" state for this many ticks
# AND has 0 progress (undownloaded count unchanged), it's stuck.
MAX_STUCK_TICKS = 3

# Threshold for "queue is genuinely empty" — we only force-enqueue when
# the global Suwayomi queue is small enough that adding 1 more title is safe.
FORCE_ENQUEUE_QUEUE_LIMIT = 5


def _stale_stuck_titles(state, log):
    """Find titles marked 'downloading' or 'download_pending' whose chapter
    count hasn't changed in MAX_STUCK_TICKS ticks.

    Returns list of (title, manga_id, undownloaded_count).
    """
    tick_count = state.get("tick_count", 0)
    stuck = []
    for title, status in state.get("status", {}).items():
        if status not in ("downloading", "download_pending"):
            continue
        manga_id = state.get("manga_ids", {}).get(title)
        if not manga_id:
            continue
        result = state.get("results", {}).get(title, {})
        first_seen_tick = result.get("first_seen_tick")
        first_undownloaded = result.get("first_undownloaded_count")
        if first_seen_tick is None:
            # First time we see this title in this state — record baseline
            done, total = get_downloaded_count(manga_id)
            undownloaded = total - done
            result["first_seen_tick"] = tick_count
            result["first_undownloaded_count"] = undownloaded
            state["results"][title] = result
            continue
        # Check if stuck: undownloaded unchanged for MAX_STUCK_TICKS
        done, total = get_downloaded_count(manga_id)
        undownloaded = total - done
        ticks_elapsed = tick_count - first_seen_tick
        if undownloaded == first_undownloaded and ticks_elapsed >= MAX_STUCK_TICKS:
            log(f"   ⚠️  STUCK: '{title}' — {undownloaded} undownloaded for {ticks_elapsed} ticks")
            stuck.append((title, manga_id, undownloaded))
    return stuck


def recover_stuck_queue(state, log):
    """Run at start of tick. Detect deadlocked queue, restart if needed.

    Returns True if queue was reset (caller should re-fetch queue state).
    Returns False if queue is healthy.
    """
    # 1. Detect all_error queue (cookies expired, source blocked, etc.)
    q = get_queue_info()
    if q and q.get("all_error"):
        log(f"⚠️  Queue has {q['queue_size']} items all in ERROR state — restarting Suwayomi")
        if restart_suwayomi_container():
            log("   ✅ Suwayomi container restarted")
            if clear_download_queue():
                log("   ✅ Queue cleared")
                # Mark any titles currently "downloading" with progress=0 as error
                _mark_stuck_as_error(state, reason="queue_all_error_restart", log=log)
                return True
        else:
            log("   ❌ Container restart failed")
    return False


def _mark_stuck_as_error(state, reason, log):
    """Find downloading/download_pending titles with 0 progress and mark error."""
    tick_count = state.get("tick_count", 0)
    for title, status in list(state.get("status", {}).items()):
        if status not in ("downloading", "download_pending"):
            continue
        manga_id = state.get("manga_ids", {}).get(title)
        if not manga_id:
            continue
        result = state.get("results", {}).get(title, {})
        first_seen = result.get("first_seen_tick", tick_count)
        first_undl = result.get("first_undownloaded_count", -1)
        done, total = get_downloaded_count(manga_id)
        undl = total - done
        if undl == first_undl and (tick_count - first_seen) >= MAX_STUCK_TICKS:
            log(f"   🚨 Marking '{title}' as download_error (stuck {tick_count - first_seen} ticks)")
            state["status"][title] = "download_error"
            state["results"][title] = {
                **result,
                "status": "download_error",
                "reason": reason,
                "marked_error_tick": tick_count,
            }
    save_state(state)


def force_reenqueue_if_queue_empty(state, log):
    """For 'download_error' titles: if global queue is empty, force-enqueue them.

    Called mid-tick after main loop. Each download_error title gets one
    fresh attempt with cleared stuck-counter.
    """
    q = get_queue_info()
    if not q or q["queue_size"] > FORCE_ENQUEUE_QUEUE_LIMIT:
        return 0

    reenqueued = 0
    for title in list(state.get("status", {}).keys()):
        if state["status"][title] != "download_error":
            continue
        manga_id = state["manga_ids"].get(title)
        if not manga_id:
            continue
        log(f"   🔄 Force-retry '{title}' (was download_error)")
        cnt, total, status = enqueue_undownloaded(manga_id, log)
        if status == "enqueued":
            state["status"][title] = "downloading"
            state["results"][title] = {
                **state["results"].get(title, {}),
                "status": "downloading",
                "force_retry_tick": state.get("tick_count", 0),
                "enqueued_count": cnt,
            }
            reenqueued += 1
        elif status == "none_pending":
            state["status"][title] = "done"
            state["results"][title] = {
                **state["results"].get(title, {}),
                "status": "done",
                "reason": "force_retry_no_undownloaded",
            }
    if reenqueued:
        start_downloader()
        save_state(state)
        log(f"   ✅ Force-reenqueued {reenqueued} download_error titles")
    return reenqueued
