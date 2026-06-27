"""Scan-and-mark-done — catches manga that finished downloading between ticks.

When state says "downloading" but all chapters are actually downloaded,
mark it done so it doesn't pile up in the active list.

This fixes the case where manga finished downloading but state was never
updated because post-enqueue verify only triggers when global queue is empty.
"""

from datetime import datetime

from .suwayomi import get_undownloaded_chapters
from .state import save_state


def scan_and_mark_done(state, log):
    """Scan manga with status=downloading/download_pending. Mark done if all chs downloaded.
    
    Args:
        state: state dict (mutated in place)
        log: log function (line, ...)
    
    Returns:
        state (same reference)
    """
    to_check = [
        (title, state["manga_ids"][title])
        for title, status in state["status"].items()
        if status in ("downloading", "download_pending") and title in state["manga_ids"]
    ]

    if not to_check:
        return state

    log(f"🔎 Scanning {len(to_check)} manga for completion...")
    marked_done = 0
    still_pending = 0
    for title, manga_id in to_check:
        undownloaded = get_undownloaded_chapters(manga_id)
        if not undownloaded:
            ch_count = state["results"].get(title, {}).get("num_chapters", 0)
            state["status"][title] = "done"
            state["results"][title] = {
                "manga_id": manga_id,
                "status": "done",
                "num_chapters": ch_count,
                "reason": "scan_all_chapters_downloaded",
                "found_at": datetime.now().isoformat(),
            }
            log(f"   ✅ {title[:50]}: marked done (all {ch_count} chapters downloaded)")
            marked_done += 1
        else:
            still_pending += 1

    if marked_done:
        log(f"   📊 Scan result: {marked_done} marked done, {still_pending} still pending")
        save_state(state)

    return state