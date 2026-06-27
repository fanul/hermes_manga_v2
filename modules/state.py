"""State persistence — load, save, and atomic write utilities."""

import json
import os
from .config import STATE_DIR, STATE_FILE, PROGRESS_FILE


def load_state():
    """Load state from JSON, or return empty initial state."""
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "current_index": 0,
        "titles": [],
        "status": {},
        "manga_ids": {},
        "results": {},
    }


def _atomic_write(path, mode, content_fn):
    """Write file atomically: write to temp, then rename. Prevents corruption on crash."""
    tmp = path + ".tmp"
    try:
        with open(tmp, mode) as f:
            content_fn(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        raise


def save_state(state):
    """Persist state to STATE_FILE atomically."""
    _atomic_write(STATE_FILE, 'w', lambda f: json.dump(state, f, indent=2))


def save_progress(text):
    """Persist progress log to PROGRESS_FILE atomically."""
    os.makedirs(STATE_DIR, exist_ok=True)
    _atomic_write(PROGRESS_FILE, 'w', lambda f: f.write(text))