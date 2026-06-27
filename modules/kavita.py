"""Kavita integration — skip check against existing library.

Uses MCP endpoint GET /series/all?limit=1000 to fetch the library.
Auth (JWT) is handled transparently by the MCP wrapper.

Falls back to MANUAL_KAVITA_SKIP set when API is unreachable.
"""

import json
import re
import urllib.request

from .config import KAVITA_MCP, MANUAL_KAVITA_SKIP, ctx as _ssl_ctx, _log_global


def get_kavita_titles():
    """Fetch all normalized series names from Kavita.
    
    Returns a set of lowercase normalized titles.
    Returns empty set on failure.
    """
    kavita_titles = set()
    try:
        url = f"{KAVITA_MCP}/series/all?limit=1000"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        resp = json.loads(urllib.request.urlopen(req, context=_ssl_ctx, timeout=30).read())
        series_list = resp.get("series", [])
        for s in series_list:
            for name_field in ("name", "originalName", "localizedName", "sortName", "folderPath", "lowestFolderPath"):
                n = s.get(name_field)
                if n:
                    # Extract basename from folder path
                    if "/" in n:
                        n = n.rsplit("/", 1)[-1]
                    # Clean: remove underscores
                    n = n.replace("_", " ").strip()
                    # Normalize: remove bracketed content
                    norm_n = re.sub(r'[\(\[\【《〔].*?[】)》〕\)\]]', '', n.lower()).strip()
                    norm_n = re.sub(r'\s+', ' ', norm_n).strip()
                    if norm_n:
                        kavita_titles.add(norm_n)
                    kavita_titles.add(n.lower())
    except Exception as e:
        _log_global(f"  ⚠️  Could not fetch Kavita library: {e}")
    return kavita_titles


def is_in_manual_kavita_skip(title):
    """Check if title matches any of the manual skip entries."""
    norm = title.lower().strip()
    norm = re.sub(r'[\(\[\【《〔].*?[】)》〕\)\]]', '', norm).strip()
    norm = re.sub(r'\s+', ' ', norm).strip()
    for fragment in MANUAL_KAVITA_SKIP:
        if fragment in norm:
            return True
    return False


def is_in_kavita(title, kavita_titles):
    """Check if a title exists in Kavita using fuzzy matching.
    
    Three-tier check:
    1. Exact match
    2. First 3 words match
    3. Reverse: kavita title starts with our title
    """
    norm = title.lower().strip()
    norm = re.sub(r'[\(\[\【《〔].*?[】)》〕\)\]]', '', norm)
    norm = re.sub(r'\b(official|english|indo|indonesia|jpn|japan|raw|colored|color)\b', '', norm)
    norm = re.sub(r'\s+', ' ', norm).strip()

    # Exact match
    if norm in kavita_titles:
        return True

    # First 3 words match
    words = norm.split()[:3]
    if len(words) >= 2:
        partial = ' '.join(words)
        for k in kavita_titles:
            k_norm = re.sub(r'\s+', ' ', k.lower().strip())
            if partial in k_norm or k_norm.startswith(partial):
                return True

    # Reverse: check if any kavita title starts with our title
    for k in kavita_titles:
        k_norm = re.sub(r'\s+', ' ', k.lower().strip())
        if norm and k_norm.startswith(norm):
            return True

    return False