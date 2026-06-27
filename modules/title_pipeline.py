"""Title pipeline — process one title from search to enqueue.

This is the core per-title workflow. orchestrator calls this for each title.
Each title enqueues ALL its undownloaded chapters at once (not 1 per tick).

Pipeline:
  kavita_check → search → library_add → enqueue_undownloaded → start_downloader

Returns: (status, manga_id, num_chapters, score)
  status in {
    "done",           — all chapters already downloaded
    "downloading",    — chapters enqueued, downloader started
    "not_found",      — no match in Suwayomi
    "download_pending", — chapter indexing still in progress (retry next tick)
    "kavita_skip",    — already in user's Kavita library
    "skip",           — other skip reasons (spinoff, etc.)
  }
"""

from .config import MANGAFIRE_SOURCES
from .search import find_best_match, find_fuzzy_match
from .suwayomi import (
    fetch_manga, update_manga_in_library, get_undownloaded_chapters,
    enqueue_undownloaded, start_downloader, get_queue_info,
    get_chapter_count,
)
from .kavita import is_in_kavita, is_in_manual_kavita_skip
from .state import save_state


def process_title(title, kavita_titles, log):
    """Process a single title: search → enqueue all undownloaded chapters.

    This is the main per-title function. It handles the full pipeline:
    1. Kavita skip check
    2. Suwayomi search (exact + fuzzy)
    3. Add to library
    4. Enqueue ALL undownloaded chapters (bulk)
    5. Start downloader

    Returns: (status, manga_id, num_chapters, score)
    """
    # Step 0: Kavita skip check
    if kavita_titles and is_in_kavita(title, kavita_titles):
        log(f"   📚 Kavita check: found → kavita_skip")
        # Still need to find manga_id for gap detection later
        match, score = find_best_match(title, log)
        manga_id = None
        if match and score >= 40:
            manga_id = match[0]["id"]
        return "kavita_skip", manga_id, 0, score

    if is_in_manual_kavita_skip(title):
        log(f"   📚 Kavita manual check: found → kavita_skip")
        return "kavita_skip", None, 0, 0

    # Step 1: Search Suwayomi
    log(f"   🔍 Searching Suwayomi for: '{title}'")
    match, score = find_best_match(title, log)
    if not match or score < 40:
        log(f"   ❌ No match found (score={score:.0f} < 40)")
        return "not_found", None, 0, 0

    manga_dict, source_id, matched_variant, ch_count = match
    manga_id = manga_dict["id"]
    log(f"   🎯 Best match: '{manga_dict['title']}' (score={score:.0f}, ch={ch_count})")

    # Step 2: Add to library
    update_manga_in_library(manga_id, True)
    log(f"   📖 Added to library: {manga_id}")

    # Step 3: Enqueue ALL undownloaded chapters (bulk)
    log(f"   📥 Enqueueing all undownloaded chapters...")
    cnt, total_undl, status = enqueue_undownloaded(manga_id, log)
    if status == "error":
        log(f"   ⚠️  Enqueue failed, marking download_pending")
        return "download_pending", manga_id, total_undl, score

    if status == "none_pending":
        log(f"   ✅ No undownloaded chapters — marking done")
        return "done", manga_id, total_undl, score

    log(f"   ✅ Enqueued {cnt}/{total_undl} undownloaded chapters")

    # Step 4: Start downloader (only if queue not empty)
    start_downloader()

    # Step 5: Post-enqueue verify
    post_queue = get_queue_info()
    if post_queue and post_queue["queue_size"] == 0 and post_queue["active"] == 0:
        log(f"   ⚠️  Queue empty after enqueue — all chapters already downloaded")
        return "done", manga_id, cnt, score

    return "downloading", manga_id, cnt, score


def process_title_with_retry(title, kavita_titles, log, max_retries=3):
    """Outer loop: process_title with retry + fuzzy fallback.

    If the title is not found on first try, tries fuzzy matching.
    If still 0 chapters after retries, marks as not_found.

    Returns: (status, manga_id, num_chapters, score)
    """
    # Try standard search first
    status, manga_id, num_chapters, score = process_title(title, kavita_titles, log)

    if status == "not_found":
        # Fuzzy fallback
        log(f"   🔄 Standard search failed, trying fuzzy...")
        fuzzy_match, fuzzy_score, fuzzy_ch_count = find_fuzzy_match(title, log)
        if fuzzy_match and fuzzy_score >= 40:
            fuzzy_manga, fuzzy_source, fuzzy_variant, fuzzy_ch_count = fuzzy_match
            fuzzy_id = fuzzy_manga["id"]
            log(f"   ✅ Fuzzy match: '{fuzzy_manga['title']}' (score={fuzzy_score:.0f}, ch={fuzzy_ch_count})")

            # Add to library
            update_manga_in_library(fuzzy_id, True)

            # Enqueue undownloaded
            cnt, total_undl, status = enqueue_undownloaded(fuzzy_id, log)
            if status == "error":
                return "download_pending", fuzzy_id, 0, fuzzy_score
            if status == "none_pending":
                return "done", fuzzy_id, 0, fuzzy_score

            log(f"   ✅ Fuzzy enqueued {cnt}/{total_undl} undownloaded")
            start_downloader()
            return "downloading", fuzzy_id, cnt, fuzzy_score

        return "not_found", None, 0, 0

    if status == "download_pending":
        # Chapter indexing still in progress — will retry on next tick
        log(f"   ⏳ Chapter indexing in progress, will retry next tick")
        return "download_pending", manga_id, 0, score

    # Success path
    return status, manga_id, num_chapters, score
