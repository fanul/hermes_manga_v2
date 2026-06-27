"""Scan-and-mark-done — catches manga that finished downloading between ticks.

When state says "downloading" but all chapters are actually downloaded,
mark it done so it doesn't pile up in the active list.

Also re-checks Kavita for titles that were previously skipped due to Kavita
API being down — if Kavita is now reachable, mark those as kavita_skip.
"""

from datetime import datetime

from .config import ctx as _ssl_ctx
from .kavita import is_in_kavita
from .suwayomi import get_undownloaded_chapters
from .state import save_state


def scan_and_mark_done(state, log):
    """Scan manga with status=downloading/download_pending.
    
    Two passes:
    1. Re-check Kavita — if title exists in Kavita, mark kavita_skip
    2. Check download completion — if all chapters downloaded, mark done
    
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

    # Try to fetch Kavita library (best-effort, don't crash if unreachable)
    kavita_titles = set()
    kavita_available = False
    try:
        import json, urllib.request
        from .config import KAVITA_MCP
        url = f"{KAVITA_MCP}/series/all?limit=2000"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, context=_ssl_ctx, timeout=30).read())
        series_list = resp.get("series", [])
        for s in series_list:
            for name_field in ("name", "originalName", "localizedName", "sortName", "folderPath", "lowestFolderPath"):
                n = s.get(name_field)
                if n:
                    if "/" in n:
                        n = n.rsplit("/", 1)[-1]
                    n = n.replace("_", " ").strip()
                    import re
                    norm_n = re.sub(r'[\(\[\【《〔].*?[】)》〕\)\]]', '', n.lower()).strip()
                    norm_n = re.sub(r'\s+', ' ', norm_n).strip()
                    if norm_n:
                        kavita_titles.add(norm_n)
        kavita_available = bool(kavita_titles)
        log(f"   ℹ️  Kavita library fetched: {len(kavita_titles)} titles (available={kavita_available})")
    except Exception as e:
        log(f"   ⚠️  Kavita unavailable during scan: {e}")

    kavita_skipped = 0
    marked_done = 0
    still_pending = 0

    for title, manga_id in to_check:
        # Pass 1: re-check Kavita
        if kavita_available and is_in_kavita(title, kavita_titles):
            state["status"][title] = "kavita_skip"
            kavita_skipped += 1
            log(f"   🛡️  {title[:50]}: marked kavita_skip (found in library)")
            continue

        # Pass 2: check download completion
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

    if kavita_skipped or marked_done:
        log(f"   📊 Scan result: {kavita_skipped} kavita_skip, {marked_done} marked done, {still_pending} still pending")
        save_state(state)

    return state