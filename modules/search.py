"""Search — variant generation, scoring, spinoff filtering, fuzzy match.

The brain of the search process. Generates smart variants of titles
(stripping parens, translating JP→EN romanization hints), then scores
candidates by keyword overlap, chapter count, and spinoff penalty.
"""

import re

from .config import STOP_WORDS, SPINOFF_MARKERS, MANGAFIRE_SOURCES
from .suwayomi import search_manga, get_chapter_count


# ============================================================================
# Variant generation
# ============================================================================

def generate_variants(title):
    """Generate search variants of a title to maximize match chances.
    
    Returns list of strings, ordered by specificity (most specific first):
    - exact title
    - stripped of (...) and [...]
    - first N meaningful words
    - JP romanization guesses
    """
    variants = [title]

    # Strip parens
    stripped = re.sub(r'\s*[\(\[\【《〔].*?[】)》〕\)\]]\s*', ' ', title).strip()
    if stripped and stripped != title:
        variants.append(stripped)

    # First 3 meaningful words
    words = [w for w in stripped.split() if w.lower() not in STOP_WORDS and len(w) > 1]
    if len(words) >= 2:
        variants.append(" ".join(words[:3]))

    # If there's a comma, try first half (for "X, Y" series)
    if "," in stripped:
        first = stripped.split(",")[0].strip()
        if len(first) >= 4:
            variants.append(first)

    # JP→EN hints (rough)
    jphints = {
        "boku": "i", "ore": "i", "watashi": "i", "kimi": "you",
        "omae": "you", "kare": "he", "kanojo": "she",
        "shounen": "boy", "shoujo": "girl", "seinen": "young man",
        "sensei": "teacher", "kun": " ", "chan": " ", "san": " ",
        "no": " ", "wa": " ", "ga": " ", "wo": " ", "ni": " ",
        "de": " ", "to": " ", "ka": " ", "ne": " ", "yo": " ",
        "desu": "is", "suru": "do", "yatta": "did",
        "yume": "dream", "koi": "love", "ai": "love",
        "tenshi": "angel", "akuma": "demon", "mahou": "magic",
    }
    romanized = stripped.lower()
    for jp, en in jphints.items():
        romanized = re.sub(rf'\b{jp}\b', en, romanized)
    romanized = re.sub(r'\s+', ' ', romanized).strip()
    if romanized and romanized != stripped.lower():
        variants.append(romanized)

    # Dedupe while preserving order
    seen = set()
    out = []
    for v in variants:
        v_norm = v.lower().strip()
        if v_norm and v_norm not in seen:
            seen.add(v_norm)
            out.append(v)
    return out


# ============================================================================
# Scoring
# ============================================================================

def title_match_score(candidate_title, target_title):
    """Score how well `candidate_title` matches `target_title`.
    
    Score 0-100. Higher = better.
    Based on:
    - word overlap (60%)
    - exact substring (40%)
    """
    c = candidate_title.lower().strip()
    t = target_title.lower().strip()

    # Exact match
    if c == t:
        return 100

    # Strip brackets for substring check
    c_clean = re.sub(r'[\(\[\【《〔].*?[】)》〕\)\]]', '', c).strip()
    t_clean = re.sub(r'[\(\[\【《〔].*?[】)》〕\)\]]', '', t).strip()

    # Substring match
    if t_clean in c_clean or c_clean in t_clean:
        return 90

    # Word overlap
    c_words = set(w for w in c_clean.split() if w not in STOP_WORDS and len(w) > 1)
    t_words = set(w for w in t_clean.split() if w not in STOP_WORDS and len(w) > 1)
    if not t_words:
        return 0
    overlap = len(c_words & t_words)
    return int(60 * overlap / len(t_words))


def is_spinoff_title(title):
    """Check if a candidate title looks like a spin-off or side-story."""
    t = title.lower()
    return any(m in t for m in SPINOFF_MARKERS)


# ============================================================================
# Search orchestration
# ============================================================================

def find_best_match(title, log, allow_fuzzy=False):
    """Search for a manga across all sources and return best match.

    Strategy:
      - Try all variants × all sources
      - Filter candidates by spinoff relationship (target=spinoff → only spinoff matches)
      - Score by keyword overlap + chapter count
      - Pick highest score

    Returns:
        ((manga_dict, source_id, matched_variant, ch_count), score) or
        (None, 0)
    """
    variants = generate_variants(title)
    target_is_spinoff = is_spinoff_title(title)

    candidates = []
    threshold = 30 if allow_fuzzy else 70

    for variant in variants:
        for source_id in MANGAFIRE_SOURCES:
            results = search_manga(variant, source_id)
            for r in results:
                cand_title = r["title"]
                cand_is_spinoff = is_spinoff_title(cand_title)
                score = title_match_score(cand_title, title)

                # CRITICAL: if target is spinoff, reject non-spinoff candidates
                if target_is_spinoff and not cand_is_spinoff:
                    continue

                # Penalize spinoff-vs-non-spinoff mismatches (avoid confusion)
                # But only penalize the OTHER direction (target not spinoff, candidate is)
                if not target_is_spinoff and cand_is_spinoff:
                    score -= 50
                    if score < threshold:
                        continue

                # Threshold check
                eff_threshold = 30 if allow_fuzzy else 40
                if score >= eff_threshold:
                    candidates.append((score, r, source_id, variant))

    if not candidates:
        return None, 0

    # If target is spinoff, prefer spinoff candidates
    # (in case both spinoff and non-spinoff got in somehow)
    if target_is_spinoff:
        spinoff_cands = [c for c in candidates if is_spinoff_title(c[1]["title"])]
        if spinoff_cands:
            candidates = spinoff_cands

    # Boost by chapter count — main series tend to have many chapters
    boosted = []
    for score, r, src, var in candidates:
        ch_count = get_chapter_count(r["id"])
        chapter_bonus = min(ch_count * 0.1, 30)
        boosted.append((score + chapter_bonus, r, src, var, ch_count))

    boosted.sort(key=lambda x: (-x[0], len(x[1]["title"]), x[2]))
    best = boosted[0]
    return (best[1], best[2], best[3], best[4]), best[0] + 0  # don't include bonus in returned score


def find_fuzzy_match(title, log):
    """Fallback: lower threshold, find best partial match (e.g. JP vs EN romanized)."""
    log(f"   🔎 Trying fuzzy fallback...")
    match, score = find_best_match(title, log, allow_fuzzy=True)
    if match:
        ch_count = match[3] if len(match) > 3 else 0
        if score >= 40:
            return match, score, ch_count
    return None, 0, 0