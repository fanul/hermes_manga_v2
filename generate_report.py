#!/usr/bin/env python3
"""Generate a comprehensive Excel report of the manga auto-search processing results.

This utility reads the current state JSON and generating a detailed Excel file
with the processing status, scores, manga IDs, and step-by-step logic flows.
"""

import json
import os
import pandas as pd

STATE_FILE = "/config/.hermes_2/state/manga_search_state.json"
CHECKLIST_FILE = "/config/.hermes_2/cache/manga_xref_checklist.xlsx"
OUTPUT_FILE = "/config/manga_auto_search/data/manga_processing_report.xlsx"

def get_flow_description(status, score=None):
    if status == "kavita_skip":
        return "📚 Search Kavita -> Found -> Skip"
    elif status == "done":
        score_str = f"Score {score}" if score is not None else "Match Found"
        return f"✅ Search Kavita -> Not Found -> Search Suwayomi -> {score_str} -> Chapters fully downloaded"
    elif status == "downloading":
        score_str = f"Score {score}" if score is not None else "Match Found"
        return f"📥 Search Kavita -> Not Found -> Search Suwayomi -> {score_str} -> Chapters enqueued"
    elif status == "download_pending":
        score_str = f"Score {score}" if score is not None else "Match Found"
        return f"⏳ Search Kavita -> Not Found -> Search Suwayomi -> {score_str} -> Queue full, waiting"
    elif status == "not_found":
        score_str = f"Score {score}" if score is not None else "< 40"
        return f"❌ Search Kavita -> Not Found -> Search Suwayomi -> Not Found ({score_str}) -> Skip"
    elif status == "download_error":
        return "⚠️ Search Kavita -> Not Found -> Search Suwayomi -> Match Found -> Error during download/enqueue"
    else:
        return "⏳ Pending (Waiting to be processed)"

def main():
    if not os.path.exists(STATE_FILE):
        print(f"Error: State file not found at {STATE_FILE}")
        return
        
    if not os.path.exists(CHECKLIST_FILE):
        print(f"Error: Checklist file not found at {CHECKLIST_FILE}")
        return

    # Load state
    with open(STATE_FILE) as f:
        state = json.load(f)

    # Read original titles checklist
    df = pd.read_excel(CHECKLIST_FILE)
    
    # Extract data from state
    statuses = state.get("status", {})
    manga_ids = state.get("manga_ids", {})
    scores = state.get("scores", {})
    
    # Align rows with state
    report_data = []
    for idx, row in df.iterrows():
        title = row["Title"]
        no = row["#"]
        
        status = statuses.get(title, "pending")
        manga_id = manga_ids.get(title, "-")
        score = scores.get(title, None)
        
        flow = get_flow_description(status, score)
        
        report_data.append({
            "No": no,
            "Title": title,
            "Current Status": status,
            "Suwayomi Manga ID": manga_id,
            "Match Score": score if score is not None else "-",
            "Processing Flow": flow
        })

    # Create new DataFrame
    report_df = pd.DataFrame(report_data)
    
    # Save to Excel
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    report_df.to_excel(OUTPUT_FILE, index=False)
    print(f"Successfully generated processing report at: {OUTPUT_FILE}")
    print(f"Total rows written: {len(report_df)}")

if __name__ == "__main__":
    main()
