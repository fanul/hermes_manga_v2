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


def _normalize_for_match(s):
    """Normalize a string for fuzzy kavita matching.
    
    - lowercase
    - strip bracketed content (... [... 【... 《... 〔...)
    - strip language tags (official/english/indo/...)
    - collapse whitespace
    - strip Japanese particles (no, wa, ga, wo, ni, de) for Japanese-title matching
    """
    s = s.lower().strip()
    s = re.sub(r'[\(\[\【《〔].*?[】)》〕\)\]]', '', s)
    s = re.sub(r'\b(official|english|indo|indonesia|jpn|japan|raw|colored|color)\b', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    # For Japanese titles: also try without particles (ao no exorcist → ao exorcist)
    s_no_particles = re.sub(r'\b(no|wa|ga|wo|ni|de|e|to|ya|ka|na)\b', '', s)
    s_no_particles = re.sub(r'\s+', ' ', s_no_particles).strip()
    return s, s_no_particles


def is_in_kavita(title, kavita_titles):
    """Check if a title exists in Kavita using fuzzy matching.
    
    Multi-tier check:
    1. Exact normalized match
    2. First 3 words match (handles "Ao no Exorcist (Official)" vs "Ao no Exorcist")
    3. Reverse: kavita title starts with our title
    4. Bidirectional substring match (catches "Blue Exorcist" vs "Ao no Exorcist" via partial overlap)
    5. Japanese particle-stripped match (handles "Ao no Exorcist" ↔ "Ao Exorcist")
    """
    norm, norm_no_particles = _normalize_for_match(title)

    # Build a pre-normalized lookup for kavita titles (cache-friendly)
    kavita_normalized = {
        k: _normalize_for_match(k) for k in kavita_titles
    }

    # 1. Exact normalized match
    if norm in kavita_titles or norm in {k[0] for k in kavita_normalized.values()}:
        return True

    # 2. First 3 words match
    words = norm.split()[:3]
    if len(words) >= 2:
        partial = ' '.join(words)
        for k_norm, k_norm_no_particles in kavita_normalized.values():
            if partial in k_norm or k_norm.startswith(partial):
                return True

    # 3. Reverse: any kavita title starts with our title (after normalization)
    for k_norm, _ in kavita_normalized.values():
        if k_norm.startswith(norm):
            return True

    # 4. Bidirectional substring match — catches e.g. "Blue Exorcist" ↔ "Exorcist"
    norm_compact = norm.replace(' ', '')
    if len(norm_compact) >= 5:  # avoid false positives on tiny words
        for k_norm, _ in kavita_normalized.values():
            k_compact = k_norm.replace(' ', '')
            # Substring either way (need min 5-char overlap to avoid "A" matching "A I")
            if len(norm_compact) >= 5 and norm_compact in k_compact:
                return True
            if len(k_compact) >= 5 and k_compact in norm_compact:
                return True

    # 5. Japanese particle-stripped match (handles "Ao no Exorcist" ↔ "Ao Exorcist")
    if norm_no_particles != norm and len(norm_no_particles) >= 3:
        if norm_no_particles in {k[1] for k in kavita_normalized.values()}:
            return True
        for k_norm_no_particles in (k[1] for k in kavita_normalized.values()):
            if norm_no_particles in k_norm_no_particles or k_norm_no_particles in norm_no_particles:
                return True

    return False