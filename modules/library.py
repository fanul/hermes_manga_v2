"""Library scan — find and re-enqueue undownloaded chapters.

Used after `clear_download_queue` to "putar balik" — re-enqueue everything
that's still missing. Includes skip-cache to avoid re-checking manga that
had 0 undownloaded on last pass.
"""

from .suwayomi import (
    get_library_manga_ids,
    get_undownloaded_chapters,
    enqueue_chapters_bulk,
    start_downloader,
)


def retry_error_chapters(manga_ids=None, state=None):
    """After clearing queue, re-enqueue undownloaded chapters.
    
    Args:
        manga_ids: list of manga IDs to re-check. None = fetch all library manga.
        state: state dict (for skip-cache persistence).
    
    Returns:
        (total_enqueued, total_checked, skipped, log_msg)
        Returns (-1, -1, 0, "") on failure.
    """
    if manga_ids is None:
        manga_ids = get_library_manga_ids()
        if not manga_ids:
            return -1, -1, 0, ""

    # Skip-cache: manga_id -> last undownloaded count
    skip_cache = {}
    if state and "retry_skip" in state:
        skip_cache = state["retry_skip"]

    total_enqueued = 0
    total_checked = 0
    skipped = 0
    BATCH_SIZE = 50  # avoid huge payloads

    for manga_id in manga_ids:
        # Early-skip if previously had 0 undownloaded
        if manga_id in skip_cache and skip_cache[manga_id] == 0:
            skipped += 1
            continue

        chapters = get_undownloaded_chapters(manga_id)
        if chapters:
            for i in range(0, len(chapters), BATCH_SIZE):
                batch = chapters[i:i + BATCH_SIZE]
                enqueue_chapters_bulk(batch)
                total_enqueued += len(batch)
        total_checked += 1
        skip_cache[manga_id] = len(chapters)

        if total_checked % 5 == 0:
            print(f"   [retry] Checked {total_checked}/{len(manga_ids)}, enqueued {total_enqueued}", flush=True)

    # Persist skip cache
    if state is not None:
        state["retry_skip"] = skip_cache

    # Start downloader if anything was enqueued
    if total_enqueued > 0:
        result = start_downloader()
        if isinstance(result, dict) and result.get("skipped"):
            print(f"   ℹ️  Queue empty after retry enqueue, nothing to start")
    else:
        print(f"   ℹ️  No chapters enqueued, skipping start_downloader")

    log_msg = f"   ℹ️  Skipped {skipped} manga (already 0 undownloaded)" if skipped else ""
    return total_enqueued, total_checked, skipped, log_msg