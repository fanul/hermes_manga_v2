"""manga_auto_search v2 — modular package.

Factorization goal: split the 1400-line monolith into single-responsibility modules.
Each module exposes a small, well-named API. The orchestrator (modules/orchestrator.py)
composes them.

Module map:
    config        — logging + shared constants
    state         — load/save state JSON + atomic write
    spreadsheet   — title list loader from xlsx
    suwayomi      — GraphQL client + manga/chapter/queue API
    queue_ops     — restart container, clear queue (recovery)
    library       — undownloaded-chapter scan + retry enqueue
    scan          — _scan_and_mark_done (catches stale 'downloading' states)
    search        — variant generation, fuzzy matching, score, spinoff filter
    kavita        — Kavita lookup + manual skip list
    orchestrator  — process_title() + main() composition
"""

__version__ = "2.0.0"