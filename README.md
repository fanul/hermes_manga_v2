# 📚 Manga Auto-Search v2 (Modular)

**Automatically search, score, and enqueue manga from a Google Spreadsheet into Suwayomi — skipping anything already in Kavita.**

v2 is a full modular rewrite. The v1 monolith (`manga_auto_search.py`) is gone; every concern now lives in its own file under `modules/`.

---

## 🚀 Quick Start

```bash
cd /config/.hermes_2/scripts
python3 manga_auto_search.py
```

Cron runs it every ~20 minutes. Each tick:

1. Resumes from where the last tick stopped (`current_index`)
2. Skips titles with terminal/in-progress status (instant bookkeeping)
3. Processes up to **1 new title** (enqueue budget protection)
4. Scans in-progress titles to mark completion or Kavita-presence
5. Writes summary report

---

## 📁 Project Structure

```
manga_auto_search.py        # Entry point (5 lines — calls orchestrator.main())
modules/
├── __init__.py
├── config.py               # Constants, endpoints, tuning knobs
├── state.py                # Atomic JSON state persistence
├── spreadsheet.py          # Load titles from Google Sheet xlsx
├── kavita.py               # Kavita skip-check + fuzzy matching
├── suwayomi.py             # GraphQL client (search, manga, chapters, queue)
├── search.py               # Variant generation + scoring + fuzzy fallback
├── library.py              # Retry/re-enqueue (used after queue clear)
├── queue_ops.py            # Container restart + queue clear (recovery)
├── scan.py                 # Scan-and-mark-done between ticks
├── orchestrator.py         # ⭐ The brain — composes all modules
└── report.py               # Formatted tick summary table
state/
├── manga_search_state.json     # Persistent state (titles, statuses, results)
├── manga_search_progress.txt   # Last tick log
└── manga_search_summary.json   # Last tick summary (for delta calc)
```

---

## 🏛️ Architecture

```mermaid
graph TB
    Entry[manga_auto_search.py] --> Orch[orchestrator.py]
    Orch --> State[state.py]
    Orch --> Sheet[spreadsheet.py]
    Orch --> Kavita[kavita.py]
    Orch --> Search[search.py]
    Orch --> Suwayomi[suwayomi.py]
    Orch --> Library[library.py]
    Orch --> QueueOps[queue_ops.py]
    Orch --> Scan[scan.py]
    Orch --> Report[report.py]
    Orch --> Config[config.py - shared]

    Suwayomi -->|GraphQL| SuwayomiServer[(Suwayomi :4567)]
    Kavita -->|REST| KavitaServer[(Kavita MCP :8765)]
    QueueOps -->|REST| Portainer[(Portainer :9443)]
    Sheet -->|xlsx| GDrive[(Google Sheet)]
```

---

## 🧠 Module: `orchestrator.py` — The Brain

The only module that knows the full lifecycle. Composes all others into the per-title pipeline and tick loop.

### Key Responsibilities
- Kavita skip check (cache + manual list)
- Per-title: search → fetch → chapter probe → enqueue
- Internal retry (5 attempts) for chapter indexing lag
- Live tick budget enforcement
- Summary emission

### Per-Title Pipeline

```mermaid
flowchart TD
    Start([Tick starts]) --> LoadState[load_state]
    LoadState --> LoadKavita[get_kavita_titles]
    LoadKavita --> Recovery{q:<br/>all_error + size>0?}
    Recovery -->|Yes| Restart[restart_suwayomi_container]
    Restart --> ClearQ[clear_download_queue]
    ClearQ --> Retry[retry_error_chapters]
    Retry --> ScanLoop
    Recovery -->|No| ScanLoop[scan_and_mark_done]
    ScanLoop --> Loop{For each title<br/>at index}

    Loop --> CheckStatus{Existing status?}
    CheckStatus -->|done / not_found / kavita_skip| Advance1[Advance index]
    CheckStatus -->|downloading| Advance2[Advance - scan handles it]
    CheckStatus -->|download_pending| Stay[Stay on same index - retry next tick]
    CheckStatus -->|pending| Process[Process title]

    Process --> KavitaCheck{Is in Kavita?}
    KavitaCheck -->|Yes| MarkSkip[status=kavita_skip, advance]
    KavitaCheck -->|No| SearchCall[find_best_match]

    SearchCall --> Score{Score >= 40?}
    Score -->|No| Fuzzy[find_fuzzy_match]
    Fuzzy -->|Match| Enqueue
    Fuzzy -->|No match| MarkNF[status=not_found, advance]
    Score -->|Yes| Enqueue

    Enqueue[enqueue_chapters_bulk<br/>start_downloader] --> Verify{q: size=0 + active=0?}
    Verify -->|Yes| MarkDone[status=done, advance]
    Verify -->|No| MarkDL[status=downloading, advance]
    Verify --> QueueActive[Queue already active]
    QueueActive --> MarkPending[status=download_pending, STAY on index]

    Advance1 --> Budget{budget_ok + index<total?}
    Advance2 --> Budget
    Stay --> Budget
    MarkSkip --> Budget
    MarkDone --> Budget
    MarkDL --> Budget
    MarkPending --> Budget
    MarkNF --> Budget

    Budget -->|Yes + enqueue < MAX| Loop
    Budget -->|No| Summary[emit_summary_table]
    Summary --> End([Tick ends])
```

### Tick Budget Logic (Two-Layer)

```mermaid
graph LR
    subgraph "Layer 1: Wall-Time Budget"
        T1[TICK_BUDGET_SECONDS = 1200s] --> T1Check{budget_ok?}
        T1Check -->|No| Exit1[Stop tick]
        T1Check -->|<180s left| Exit2[Stop tick - safety net]
    end

    subgraph "Layer 2: Enqueue Budget"
        T2[ENQUEUE_BUDGET_SECONDS = 300s] --> T2Check{remaining > 0?}
        T2Check -->|No| CheckSuccess{Any success?}
        CheckSuccess -->|No| ForceNF[Mark not_found - stuck]
        CheckSuccess -->|Yes| Exit3[Stop batch]
        T2Check -->|Yes| Continue[Process next title]
    end

    Success[Successful enqueue] -.->|RESET| T2
    Bookkeeping[done/not_found/kavita_skip] -.->|no cost| T2
```

**Adaptive rules:**
- **Bookkeeping** (instant status checks, kavita skip) does **NOT** consume enqueue budget
- **Successful enqueue** (status=`downloading`) **RESETS** the enqueue budget → tick keeps searching
- **MAX_ENQUEUES_PER_TICK = 1** → stops after 1 successful enqueue (prevents Suwayomi overload)

---

## 🔍 Module: `search.py` — Variant Generation + Scoring

### What Happens
Generates smart search variants of titles (strip parens, JP→EN romanization hints), then scores candidates by keyword overlap, chapter count, and spinoff penalty.

### Variant Generation

```mermaid
flowchart LR
    Title["Ao no Exorcist (Official)"] --> V1[1. Exact: full title]
    Title --> V2[2. Strip parens: Ao no Exorcist]
    V2 --> V3[3. First 3 words: Ao no Exorcist]
    V2 --> V4[4. Comma split]
    V2 --> V5[5. JP→EN hints: replace particles]

    V1 --> Search[Search all variants × all sources]
    V2 --> Search
    V3 --> Search
    V4 --> Search
    V5 --> Search
    Search --> Score[Score each candidate]
```

### Scoring Pipeline

```mermaid
flowchart TD
    Candidate[candidate title] --> Exact{c == t?}
    Exact -->|Yes| S100[Score: 100]
    Exact -->|No| Substr{Substring match?}
    Substr -->|Yes| S90[Score: 90]
    Substr -->|No| Overlap[Word overlap ratio]
    Overlap --> S60[Score: 60 × overlap/total]

    S100 --> SpinoffCheck{Is spinoff?}
    S90 --> SpinoffCheck
    S60 --> SpinoffCheck

    SpinoffCheck -->|Yes| Penalty[score -= 50]
    SpinoffCheck -->|No| Boost[chapter bonus: min ch × 0.1, max 30]

    Penalty --> Filter{eff_threshold met?}
    Boost --> Filter
    Filter -->|Yes| AddCandidate[Add to candidates]
    Filter -->|No| Drop[Drop]

    AddCandidate --> FinalSort[Sort: highest score, shortest title]
    Drop --> End1([End])
    FinalSort --> Best[Best match]
```

---

## 🔄 Module: `suwayomi.py` — GraphQL Client

Single point of contact with Suwayomi. All higher-level modules import from here.

### Function Categories

```mermaid
graph TB
    subgraph "Low-Level Transport"
        GQL[graphql_query]
    end

    subgraph "Search & Manga"
        Search[search_manga]
        Fetch[fetch_manga]
        Update[update_manga_in_library]
    end

    subgraph "Chapter Probes"
        ChCount[get_chapter_count]
        ChIDs[get_chapter_ids]
        UndownCh[get_undownloaded_chapters]
    end

    subgraph "Download Queue"
        Enqueue[enqueue_chapters_bulk]
        Start[start_downloader]
        QInfo[get_queue_info]
    end

    subgraph "Library"
        LibIDs[get_library_manga_ids]
        Prog[get_manga_download_progress]
    end

    GQL --> Search
    GQL --> Fetch
    GQL --> Update
    GQL --> ChCount
    GQL --> ChIDs
    GQL --> UndownCh
    GQL --> Enqueue
    GQL --> Start
    GQL --> QInfo
    GQL --> LibIDs
    GQL --> Prog
```

### GraphQL Pitfall
**MUST** wrap in JSON envelope: `json.dumps({"query": q}).encode()`. Raw query string → HTTP 400. Empty `errors[].message` is the symptom.

---

## 📚 Module: `kavita.py` — Skip Check + Fuzzy Match

### What Happens
Fetches Kavita library via MCP, normalizes all titles, and matches incoming titles using 5-tier fuzzy matching.

### 5-Tier Match Algorithm

```mermaid
flowchart TD
    Title["Incoming: 'Ao no Exorcist'"] --> N1[Tier 1: Exact normalized match]
    N1 -->|No| N2[Tier 2: First 3 words match]
    N2 -->|No| N3[Tier 3: Reverse startswith]
    N3 -->|No| N4[Tier 4: Bidirectional substring]
    N4 -->|No| N5[Tier 5: Japanese particle-stripped]
    N5 -->|No| NotInKavita[NOT in Kavita - proceed with search]
    N5 -->|Yes| InKavita[IN Kavita - skip]

    N1 -->|Yes| InKavita
    N2 -->|Yes| InKavita
    N3 -->|Yes| InKavita
    N4 -->|Yes| InKavita
```

### Normalization Steps

```mermaid
graph LR
    Raw[Raw title] --> Lower[lowercase]
    Lower --> StripParen[Strip bracketed content]
    StripParen --> StripLang[Strip language tags: official/english/indo/jpn/...]
    StripLang --> Collapse[Collapse whitespace]
    Collapse --> StripParticles[Strip JP particles: no/wa/ga/wo/ni/de/e/to]
    StripParticles --> Norm[Normalized form A]
    StripParticles --> NormNoP[Normalized form B - no particles]
```

### Kavita Library Fetch

```mermaid
sequenceDiagram
    participant Orch as orchestrator
    participant Kav as kavita.py
    participant API as Kavita MCP :8765
    Orch->>Kav: get_kavita_titles()
    Kav->>API: GET /series/all?limit=1000
    API-->>Kav: {series: [...]}
    Kav->>Kav: For each series, extract 6 name fields
    Kav->>Kav: Normalize each (basename, replace _, strip brackets)
    Kav-->>Orch: Set of normalized titles
    Orch->>Kav: is_in_kavita(title, kavita_titles)
    Kav-->>Orch: True / False
```

---

## 🔄 Module: `scan.py` — Scan-and-Mark-Done

### What Happens
Catches manga that finished downloading between ticks. Without this, the "downloading" status would pile up forever.

### Two-Pass Logic

```mermaid
flowchart TD
    Start([scan_and_mark_done]) --> Collect[Collect titles with<br/>status=downloading or download_pending]
    Collect --> Empty{Empty?}
    Empty -->|Yes| Return1[Return state]
    Empty -->|No| FetchKav[Fetch Kavita library]

    FetchKav --> KavOK{Kavita available?}
    KavOK -->|No| SkipKavPass[Skip Pass 1 - only Pass 2]
    KavOK -->|Yes| Pass1[Pass 1: Re-check Kavita]

    SkipKavPass --> Pass2[Pass 2: Check download completion]
    Pass1 --> Pass1Result{In Kavita?}
    Pass1Result -->|Yes| MarkSkip[status=kavita_skip]
    Pass1Result -->|No| Pass2
    MarkSkip --> Pass2

    Pass2 --> CheckCh[get_undownloaded_chapters]
    CheckCh --> AllDown{All downloaded?}
    AllDown -->|Yes| MarkDone[status=done]
    AllDown -->|No| KeepStatus[Keep downloading/download_pending]

    MarkSkip --> Log[Log: kavita_skipped + marked_done + still_pending]
    MarkDone --> Log
    KeepStatus --> Log
    Log --> SaveState[save_state if changes]
    SaveState --> Return2[Return state]
```

**Why both passes?** If Kavita API was down last tick, a title might have been wrongly marked `downloading`. Pass 1 catches that — if it's actually in Kavita, mark `kavita_skip` so it doesn't sit forever.

---

## 🛠️ Module: `queue_ops.py` — Recovery

### When It Runs
Only when `get_queue_info().all_error == True and queue_size > 0` — meaning the entire queue is deadlocked.

### Recovery Sequence

```mermaid
sequenceDiagram
    participant Orch as orchestrator
    participant QOps as queue_ops.py
    participant Portainer as Portainer :9443
    participant Suwayomi as Suwayomi :4567
    participant Lib as library.py

    Orch->>Orch: Detect: q.all_error && q.size > 0
    Orch->>QOps: restart_suwayomi_container()
    QOps->>Portainer: POST /containers/{id}/restart
    Portainer-->>QOps: 204 No Content
    QOps-->>Orch: True

    Orch->>QOps: clear_download_queue()
    QOps->>Suwayomi: mutation clearDownloader
    Suwayomi-->>QOps: success
    QOps-->>Orch: True

    Orch->>Lib: retry_error_chapters(tracked_ids, state)
    Lib->>Suwayomi: For each manga_id, get_undownloaded_chapters
    Lib->>Suwayomi: enqueue_chapters_bulk(undownloaded)
    Lib->>Suwayomi: start_downloader()
    Lib-->>Orch: (enqueued, checked, skipped, log_msg)
```

---

## 📊 Module: `report.py` — Tick Summary Table

### Output Format

```
==============================================================================
📊 MANGA AUTO-SEARCH TICK #42 — 2026-06-27 14:32:11
==============================================================================

| Status              | Count | Δ vs prev |
|---------------------|-------|-----------|
| ✅ done              |    17 |    +3 ✅  |
| 📥 downloading       |    18 |    +1 📥  |
| ⏳ download_pending  |     0 | (n/a)     |
| ❌ not_found         |    24 |    +2 ❌  |
| 📚 kavita_skip       |    41 |   +17 📚  |
| ⏳ pending           |     0 | (n/a)     |
| **total**           | **100** |             |

📍 Index: 42/100 (42%)

🆕 New since last tick:
   ✅ done: +3 | ❌ not_found: +2 | 📥 downloading: +1 | 📚 kavita_skip: +17

📝 Notes:
   • MAX_ENQUEUES_PER_TICK = 1 (Suwayomi overload protection)
   • Next tick resumes at #43
   • Already-downloading titles: 18 (preserved, not re-enqueued)
==============================================================================
```

---

## 💾 Module: `state.py` — Atomic Persistence

### What Happens
Reads/writes JSON state with atomic write (tmp + rename) to prevent corruption on crash.

```mermaid
graph LR
    Load[load_state] --> Check{STATE_FILE<br/>exists?}
    Check -->|Yes| Read[Read JSON]
    Check -->|No| Init[Initialize empty state]
    Read --> Return[Return state dict]
    Init --> Return

    Save[save_state] --> Tmp[Write to .tmp file]
    Tmp --> Fsync[fsync + flush]
    Fsync --> Rename[os.replace .tmp → STATE_FILE]
    Rename --> Done([Done])
```

---

## ⚙️ Module: `config.py` — Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `SUWAYOMI_DIRECT` | `http://1.2.3.33:4567/api/graphql` | Suwayomi GraphQL endpoint |
| `KAVITA_MCP` | `http://1.2.3.131:8765` | Kavita MCP endpoint |
| `TICK_BUDGET_SECONDS` | `1200` (20 min) | Hard wall-time limit |
| `ENQUEUE_BUDGET_SECONDS` | `300` (5 min) | Network-I/O budget per batch |
| `MAX_ENQUEUES_PER_TICK` | `1` | Stop after 1 successful enqueue |
| `MAX_INTERNAL_RETRIES` | `5` | Per-title chapter-index retries |
| `RETRY_DELAY` | `5` | Seconds between retries |
| `MANUAL_KAVITA_SKIP` | `set(...)` | Fallback skip list when Kavita API down |
| `STOP_WORDS` | `set(...)` | Ignored in title matching |
| `SPINOFF_MARKERS` | `list(...)` | Heavily penalized in scoring |

---

## 🔄 Status Lifecycle

```mermaid
stateDiagram-v2
    [*] --> pending: First loaded
    pending --> downloading: Enqueue succeeded
    pending --> kavita_skip: Found in Kavita
    pending --> not_found: No match found
    pending --> download_pending: Queue active OR chapter lag

    downloading --> done: scan_and_mark_done (all ch downloaded)
    downloading --> kavita_skip: scan re-check (found in Kavita)
    downloading --> downloading: still downloading next tick

    download_pending --> downloading: Queue freed, re-enqueue
    download_pending --> download_pending: Same title, retry next tick
    download_pending --> kavita_skip: scan re-check (found in Kavita)
    download_pending --> done: scan (all ch downloaded)

    done --> [*]
    not_found --> [*]
    kavita_skip --> [*]
```

---

## 🐛 Debugging

### Check Current State
```bash
cat /config/.hermes_2/state/manga_search_state.json | python3 -m json.tool | head -30
```

### Check Last Tick Summary
```bash
cat /config/.hermes_2/state/manga_search_summary.json
```

### View Progress Log
```bash
tail -50 /config/.hermes_2/state/manga_search_progress.txt
```

### Manual Run
```bash
cd /config/.hermes_2/scripts
python3 manga_auto_search.py
```

---

## 📝 Push History (v2)

Each push below shows what was changed and why:

### Commit 95c5ba4 — fix: download_pending retry logic + kavita re-check in scan + improved fuzzy matching
**What happened:** User reported that Blue Exorcist was stuck in `downloading` queue forever, even though it was already in Kavita.

**Diagnosis:**
1. `save_and_advance()` was incrementing index for `download_pending` status → title never got re-checked next tick
2. `scan_and_mark_done()` only checked if chapters were downloaded, not if Kavita had it now
3. Fuzzy matching in `is_in_kavita()` couldn't handle "Ao no Exorcist" (JP) ↔ "Blue Exorcist" (EN)

**Fix:**
- `orchestrator.py`: `download_pending` no longer advances index → same title retries next tick
- `orchestrator.py`: `save_and_advance()` skips index increment when status=`download_pending`
- `orchestrator.py`: `process_title()` checks `get_queue_info()` BEFORE enqueue → if queue active, returns `download_pending` (prevents flooding Suwayomi)
- `scan.py`: Added Pass 1 (re-check Kavita) → if title now in Kavita, mark `kavita_skip`
- `kavita.py`: Added 5-tier fuzzy matching with Japanese particle stripping (handles "Ao no Exorcist" → "Ao Exorcist" → matches "Ao no Exorcist" in Kavita)

**Result:** Tick #27 successfully marked 17 stuck titles as `kavita_skip` and enqueued 1 new title.