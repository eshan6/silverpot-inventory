"""Google Sheets sink. Append-only snapshots plus a derived current view."""
import json
import os

import gspread
from google.oauth2.service_account import Credentials

from .config import SHEET_NAME, TAB_CURRENT, TAB_SNAPSHOTS

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SNAPSHOT_HEADER = [
    "snapshot_date", "internal_code", "product_name", "node", "state", "qty",
]
CURRENT_HEADER = [
    "internal_code", "product_name", "sku", "walmart_sku", "asin",
    "fba_fulfillable", "wfs_available", "raw_available", "safety_buffer",
    "published_available", "fba_inbound", "fba_reserved", "fba_unfulfillable",
    "daily_depletion", "days_of_cover", "last_updated",
]


def _client() -> gspread.Client:
    raw = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _tab(sh, title: str, header: list[str]):
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1000, cols=max(len(header), 12))
        ws.update("A1", [header])
        return ws
    existing = ws.row_values(1)
    if existing != header:
        ws.update("A1", [header])
    return ws


def open_sheet():
    gc = _client()
    key = os.getenv("GOOGLE_SHEET_ID")
    return gc.open_by_key(key) if key else gc.open(SHEET_NAME)


def append_snapshots(sh, rows: list[list]) -> int:
    ws = _tab(sh, TAB_SNAPSHOTS, SNAPSHOT_HEADER)
    if rows:
        ws.append_rows(rows, value_input_option="RAW")
    return len(rows)


def write_current(sh, rows: list[list]) -> None:
    ws = _tab(sh, TAB_CURRENT, CURRENT_HEADER)
    ws.batch_clear([f"A2:{chr(64 + len(CURRENT_HEADER))}10000"])
    if rows:
        ws.update("A2", rows, value_input_option="RAW")


def read_prior_published(sh, days_back: int = 8) -> dict[str, list[tuple[str, int]]]:
    """Return {internal_code: [(date, published_qty), ...]} from snapshot history."""
    try:
        ws = sh.worksheet(TAB_SNAPSHOTS)
    except gspread.WorksheetNotFound:
        return {}
    values = ws.get_all_values()[1:]
    hist: dict[str, dict[str, int]] = {}
    for row in values:
        if len(row) < 6:
            continue
        date, code, _name, node, state, qty = row[:6]
        if node != "PUBLISHED" or state != "available":
            continue
        try:
            hist.setdefault(code, {})[date] = int(qty)
        except ValueError:
            continue
    out = {}
    for code, by_date in hist.items():
        series = sorted(by_date.items())[-days_back:]
        out[code] = series
    return out
