"""Shared constants and logging helpers.

Constants live here so every module imports the same source-of-truth.
`ctx` (SSL context for self-signed certs) is created once at import time.
"""

import ssl
from datetime import datetime

# ============================================================================
# Endpoint constants
# ============================================================================

SUWAYOMI_DIRECT = "http://1.2.3.33:4567/api/graphql"
KAVITA_MCP = "http://1.2.3.131:8765"

SOURCE_DOC = "/config/.hermes_2/cache/documents/doc_1232e379b2b6_to_download_suwayomi.xlsx"

STATE_DIR = "/config/.hermes_2/state"
STATE_FILE = f"{STATE_DIR}/manga_search_state.json"
PROGRESS_FILE = f"{STATE_DIR}/manga_search_progress.txt"
SUMMARY_FILE = f"{STATE_DIR}/manga_search_summary.json"

# ============================================================================
# Domain constants
# ============================================================================

# Suwayomi sources to search (English only — ES/FR/JA produce false positives)
MANGAFIRE_SOURCES = [
    "6084907896154116083",  # MangaFire (EN)
]

# Manual skip list — titles confirmed already in user's Kavita library.
# Used as fallback when Kavita API is unreachable.
MANUAL_KAVITA_SKIP = {
    "bleach",
    "inuyasha",
    "karate shoukoushi kurogane",
    "karate shoukoushi",
    "kenketsu karate shoukoushi",
    "kenketsu karate shoukoushi kurogane",
}

# Words that don't help identify a manga (stop words)
STOP_WORDS = {
    "the", "a", "an", "of", "to", "and", "or", "in", "on", "at", "for",
    "is", "are", "was", "were", "be", "been", "no", "vol", "chapter",
    "ch", "chap", "part", "arc", "season", "story", "tales", "gaiden",
    "official", "edition", "complete", "extra", "side",
    "kindaichi",  # too generic in JP
}

# Spin-off markers — penalize these in title scoring
SPINOFF_MARKERS = [
    " gaiden", "side story", "boku doumei", "houkago", "iruma mafia",
    " spin-off", "spinoff", "official anthology", "fan book", "fanbook",
    "official comic", "comic anthology", "special edition", " kouhen",
    "if story", "another world", "doumei no game", "doumei", "if episode",
    "another episode", "omake", "bonus", "specials", "official guidebook",
    "festa", "anniversary", "comicalize", "koushiki", "gaiden",
    " artbook",
]

# ============================================================================
# Tick / retry tuning
# ============================================================================

TICK_BUDGET_SECONDS = 20 * 60     # 20 min — hard stop (cron wrapper safety net)
ENQUEUE_BUDGET_SECONDS = 5 * 60   # 5 min of network-I/O per enqueue batch
                                  # Reset every successful enqueue (status="downloading")
                                  # Bookkeeping (done/not_found/kavita_skip) does NOT consume budget
MAX_QUEUE_SIZE = 100              # max Suwayomi queue size before we stop enqueuing
                                  # Allows many titles/tick as long as queue stays below threshold
                                  # Each title enqueues ALL its chapters at once
MAX_INTERNAL_RETRIES = 5          # per-title chapter-indexing retries
RETRY_DELAY = 5                   # seconds between internal retries

# ============================================================================
# SSL context (shared — self-signed certs everywhere on the LAN)
# ============================================================================

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


# ============================================================================
# Logging helpers
# ============================================================================

def _log_ts():
    """Format timestamp HH:MM:SS for log lines."""
    return datetime.now().strftime('%H:%M:%S')


def _log_global(msg):
    """Module-level logger usable before main() defines the local closure."""
    print(f"[{_log_ts()}] {msg}", flush=True)