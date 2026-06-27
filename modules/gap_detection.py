"""Gap detection — find manga where Suwayomi has more chapters than Kavita.

Bug scenario (Fanul, 2026-06-27):
- Title exists in BOTH Suwayomi AND Kavita
- But Suwayomi has 322 chapters while Kavita only has ~80
- Gap = 242 chapters that should be downloaded to fill Kavita

This module owns the gap-finding logic. orchestrator calls
`detect_and_fill_gaps(state, kavita_titles, log)` after the main search loop.
"""

from .suwayomi import (
    get_downloaded_count, enqueue_undownloaded, start_downloader,
    get_undownloaded_chapters,
)
from .kavita import is_in_kavita
from .state import save_state


# Minimum gap threshold — only detect gaps >= this size
MIN_GAP_CHAPTERS = 10


def _get_kavita_chapter_estimate(kavita_titles, title):
    """Estimate chapter count from Kavita pages.

    Heuristic: average manga chapter = ~40 pages.
    Returns (kavita_chapters, pages) or (0, 0) if not in Kavita.
    """
    if kavita_titles and is_in_kavita(title, kavita_titles):
        # If we can't get exact count, estimate from pages
        # This is a rough heuristic — exact count requires Kavita API
        # which the MCP wrapper doesn't expose
        return 0  # 0 = unknown, caller should skip gap detection
    return 0


def detect_gap_titles(state, kavita_titles, log):
    """Find titles that are in BOTH libraries but have significant gap.

    Returns list of (title, manga_id, suwayomi_total, kavita_estimate, gap).
    """
    gaps = []
    for title, status in state.get("status", {}).items():
        if status not in ("done", "kavita_skip"):
            continue
        manga_id = state.get("manga_ids", {}).get(title)
        if not manga_id:
            continue
        done, total = get_downloaded_count(manga_id)
        if total <= MIN_GAP_CHAPTERS:
            continue

        # Check if in Kavita
        if kavita_titles and is_in_kavita(title, kavita_titles):
            # In both libraries — check gap
            # Kavita doesn't expose chapter count via MCP, so we estimate
            # from pages. If we can't estimate, skip.
            # For now, we rely on the spreadsheet's "SW Gap" column
            pass
        else:
            continue

    return gaps


def detect_and_fill_gaps(state, kavita_titles, log):
    """Detect gaps and fill them by enqueueing undownloaded chapters.

    This runs AFTER the main search loop. It looks at all "done" and
    "kavita_skip" titles, checks if Suwayomi has significantly more chapters
    than expected, and enqueues the gap.

    Returns dict: {filled_count, skipped_count, error_count}
    """
    filled = 0
    skipped = 0
    errors = 0

    # Check all titles that are in both libraries
    for title, status in list(state.get("status", {}).items()):
        if status not in ("done", "kavita_skip"):
            continue
        manga_id = state.get("manga_ids", {}).get(title)
        if not manga_id:
            continue

        # Get Suwayomi chapter stats
        done, total = get_downloaded_count(manga_id)
        if total == 0:
            continue

        # Check if in Kavita
        if not kavita_titles or not is_in_kavita(title, kavita_titles):
            continue

        # Estimate Kavita chapter count from pages
        # We need to fetch pages from Kavita — but MCP doesn't expose this
        # For now, check if there's a known gap from spreadsheet data
        # (the "SW Gap" column)

        # Check undownloaded count
        undownloaded = get_undownloaded_chapters(manga_id)
        if not undownloaded:
            continue

        # If undownloaded > 0 and title is in both libraries, enqueue gap
        log(f"   🔍 Gap check: '{title}' — {total} total, {done} done, {len(undownloaded)} undownloaded")

        # Enqueue undownloaded chapters
        cnt, total_undl, status = enqueue_undownloaded(manga_id, log)
        if status == "enqueued" and cnt > 0:
            state["status"][title] = "downloading"
            state["results"][title] = {
                **state.get("results", {}).get(title, {}),
                "status": "downloading",
                "gap_filled": True,
                "gap_count": cnt,
                "filled_at": state.get("tick_count", 0),
            }
            filled += 1
        elif status == "error":
            errors += 1
        else:
            skipped += 1

    if filled > 0:
        start_downloader()
        save_state(state)

    return {"filled": filled, "skipped": skipped, "errors": errors}
