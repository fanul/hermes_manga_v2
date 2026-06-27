"""Spreadsheet loader — reads title list from xlsx."""

import openpyxl
from .config import SOURCE_DOC


def load_titles():
    """Read title column (index=1) from SOURCE_DOC, skipping header row.
    
    Returns list[str] of titles.
    """
    try:
        wb = openpyxl.load_workbook(SOURCE_DOC, data_only=True)
        ws = wb.active
        titles = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row[1]:
                titles.append(row[1])
        return titles
    except Exception as e:
        print(f"[load_titles] Error loading {SOURCE_DOC}: {e}")
        return []