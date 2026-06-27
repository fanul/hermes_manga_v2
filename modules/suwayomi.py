"""Suwayomi GraphQL client — manga, chapter, and queue operations.

Single point of contact with the Suwayomi server. All higher-level modules
(orchestrator, library, queue_ops) import from here.
"""

import json
import time
import urllib.request

from .config import SUWAYOMI_DIRECT, MANGAFIRE_SOURCES, _log_global


# ============================================================================
# Low-level GraphQL transport
# ============================================================================

def graphql_query(query, timeout=30):
    """POST a GraphQL query to Suwayomi. Returns parsed JSON dict.

    GraphQL requires the body to be a JSON envelope: {"query": "..."}.
    Sending the raw query string returns HTTP 400.
    """
    try:
        req = urllib.request.Request(
            SUWAYOMI_DIRECT,
            data=json.dumps({"query": query}).encode('utf-8'),
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=timeout).read()
        return json.loads(resp)
    except Exception as e:
        _log_global(f"[graphql_query] error: {e}")
        return {"errors": [{"message": str(e)}]}


# ============================================================================
# Search / manga operations
# ============================================================================

def search_manga(query, source_id):
    """Search manga in a source. Returns list of (id, title)."""
    q = f'''mutation {{
        fetchSourceManga(input: {{
            source: "{source_id}",
            query: {json.dumps(query)},
            page: 1,
            type: SEARCH,
            filters: []
        }}) {{
            hasNextPage
            mangas {{ id title }}
        }}
    }}'''
    result = graphql_query(q, timeout=20)
    if "errors" in result:
        return []
    return result.get("data", {}).get("fetchSourceManga", {}).get("mangas", [])


def fetch_manga(manga_id):
    """Import manga from source to local DB."""
    q = f'''mutation {{
        fetchManga(input: {{id: {manga_id}, clientMutationId: "auto-search"}}) {{
            clientMutationId
        }}
    }}'''
    return graphql_query(q, timeout=60)


def update_manga_in_library(manga_id, in_library=True):
    """Set manga in/out of library."""
    q = f'''mutation {{
        updateManga(input: {{
            id: {manga_id},
            patch: {{inLibrary: {str(in_library).lower()}}},
            clientMutationId: "auto-search"
        }}) {{
            clientMutationId
        }}
    }}'''
    return graphql_query(q, timeout=30)


# ============================================================================
# Chapter probes
# ============================================================================

def get_chapter_count(manga_id):
    """Quick chapter count probe for confidence boosting."""
    q = f'''{{
        manga(id: {manga_id}) {{
            id
            chapters {{ totalCount nodes {{ id }} }}
        }}
    }}'''
    try:
        r = graphql_query(q, timeout=10)
        if r and "data" in r and r["data"].get("manga"):
            ch = r["data"]["manga"].get("chapters", {})
            return ch.get("totalCount", 0) or len(ch.get("nodes", []))
    except Exception:
        pass
    return 0


def get_chapter_ids(manga_id):
    """Get all chapter IDs for a manga. Returns (title, [chapter_ids]) or (None, None)."""
    q = f'''query {{
        manga(id: {manga_id}) {{
            id
            title
            chapters {{
                nodes {{ id chapterNumber isDownloaded }}
            }}
        }}
    }}'''
    try:
        result = graphql_query(q, timeout=30)
        if "errors" in result:
            return None, None
        manga = result.get("data", {}).get("manga")
        if not manga:
            return None, None
        return manga["title"], [ch["id"] for ch in manga["chapters"]["nodes"]]
    except Exception:
        return None, None


def get_undownloaded_chapters(manga_id):
    """Get chapter IDs for a manga that are NOT yet downloaded. Optimized payload."""
    q = f'''query {{
        manga(id: {manga_id}) {{
            id
            chapters {{
                nodes {{
                    id
                    isDownloaded
                }}
            }}
        }}
    }}'''
    result = graphql_query(q, timeout=15)
    if not result or "errors" in result or "data" not in result:
        return []
    chapters = result["data"].get("manga", {}).get("chapters", {}).get("nodes") or []
    return [c["id"] for c in chapters if not c.get("isDownloaded", False)]


# ============================================================================
# Download queue
# ============================================================================

def enqueue_undownloaded(manga_id, log):
    """Enqueue ALL undownloaded chapters for a manga in a single bulk call.

    Resources-light: only sends chapters that haven't been downloaded yet.
    Returns (enqueued_count, total_count, status):
        status in {"enqueued", "none_pending", "error"}
    """
    try:
        undownloaded = get_undownloaded_chapters(manga_id)
        if not undownloaded:
            return 0, 0, "none_pending"

        # Bulk enqueue — split into batches of 200 to avoid GraphQL payload limits
        BATCH_SIZE = 200
        total_enqueued = 0
        for i in range(0, len(undownloaded), BATCH_SIZE):
            batch = undownloaded[i:i + BATCH_SIZE]
            q = f'''mutation {{
                enqueueChapterDownloads(input: {{
                    ids: {json.dumps(batch)},
                    clientMutationId: "auto-search"
                }}) {{
                    clientMutationId
                }}
            }}'''
            result = graphql_query(q, timeout=60)
            if "errors" in result:
                log(f"   ⚠️  Enqueue batch failed: {result['errors']}")
                return total_enqueued, len(undownloaded), "error"
            total_enqueued += len(batch)

        return total_enqueued, len(undownloaded), "enqueued"
    except Exception as e:
        log(f"   ⚠️  enqueue_undownloaded error: {e}")
        return 0, 0, "error"


def get_downloaded_count(manga_id):
    """Return (downloaded_count, total_count) for a manga."""
    q = '{ manga(id: %d) { chapters { totalCount nodes { id isDownloaded } } } }' % int(manga_id)
    data = graphql_query(q, timeout=10)
    if not data or "errors" in data:
        return 0, 0
    m = data.get("data", {}).get("manga") or {}
    nodes = m.get("chapters", {}).get("nodes", [])
    total = len(nodes)
    done = sum(1 for n in nodes if n.get("isDownloaded"))
    return done, total


def start_downloader():
    """Start the downloader. Skips if queue empty (would hang 30s+)."""
    q_info = get_queue_info()
    if not q_info or q_info["queue_size"] == 0:
        return {"skipped": "empty_queue", "queue_size": q_info["queue_size"] if q_info else -1}
    q = '''mutation {
        startDownloader(input: {clientMutationId: "auto-search"}) {
            clientMutationId
        }
    }'''
    try:
        return graphql_query(q, timeout=10)
    except Exception as e:
        return {"errors": [{"message": str(e)}]}


def get_queue_info():
    """Single GraphQL call to get full queue state.
    
    Returns dict: {queue_size, active, error_count, all_error, total}.
    Returns None on query failure.
    """
    q = '''query {
        downloadStatus {
            queue {
                state
            }
        }
    }'''
    result = graphql_query(q, timeout=10)
    if not result or "errors" in result or "data" not in result:
        return None
    ds = result["data"].get("downloadStatus") or {}
    queue = ds.get("queue") or []
    total = len(queue)
    active = sum(1 for item in queue if item.get("state") == "DOWNLOADING")
    error_count = sum(1 for item in queue if item.get("state") == "ERROR")
    return {
        "queue_size": total,
        "active": active,
        "error_count": error_count,
        "all_error": error_count == total and total > 0,
        "total": total,
    }


# ============================================================================
# Library (all manga in user library)
# ============================================================================

def get_library_manga_ids():
    """Get all manga IDs in Suwayomi library (inLibrary=True)."""
    q = '''query {
        mangas {
            nodes {
                id
                inLibrary
            }
        }
    }'''
    result = graphql_query(q, timeout=30)
    if not result or "errors" in result or "data" not in result:
        return []
    nodes = result["data"].get("mangas", {}).get("nodes") or []
    return [n["id"] for n in nodes if n.get("inLibrary")]


def get_manga_download_progress(manga_id):
    """Return (downloaded_count, total_count) for a manga. None on error."""
    q = '{ manga(id: %d) { title chapters { totalCount nodes { id isDownloaded } } } }' % int(manga_id)
    data = graphql_query(q, timeout=10)
    if not data or "errors" in data:
        return None
    m = data.get("data", {}).get("manga") or {}
    nodes = m.get("chapters", {}).get("nodes", [])
    total = len(nodes)
    done = sum(1 for n in nodes if n.get("isDownloaded"))
    return done, total