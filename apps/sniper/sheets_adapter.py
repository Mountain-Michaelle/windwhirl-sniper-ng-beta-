"""
sheets_adapter.py

Google Sheets order loader for production mode.

Authenticates via a Google service-account credential supplied through
environment variables, reads the target spreadsheet/worksheet for the
selected client, normalizes headers, and returns order dicts in the
exact same normalized structure as the Excel adapter (load_orders()).

Read-only: never writes to the spreadsheet. Never logs credentials.
"""

from __future__ import annotations

import json
import os
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# Column header mapping from Google Sheet headers to internal field names.
# Must match COLUMN_MAP in run_upload.py exactly so the downstream pipeline
# (planning, state_engine, add_products) receives identical structures.
COLUMN_MAP = {
    "Customer Name": "customer_name",
    "Phone Number": "phone_number",
    "WhatsApp Number": "whatsapp_number",
    "Address": "address",
    "Product": "product",
    "Price (₦)": "price",
}

REQUIRED_ORDER_FIELDS = ("customer_name", "phone_number", "product", "address", "price")


def _is_blank(value) -> bool:
    if value is None:
        return True
    return str(value).strip() in ("", "NaT", "NaN", "None", "")


def _clean_for_report(value) -> str:
    if _is_blank(value):
        return ""
    return " ".join(str(value).split())


def _missing_required_fields(order: dict) -> list[str]:
    """Same gating logic as run_upload.py:_missing_required_fields."""
    problems: list[str] = []
    if _is_blank(order.get("product")) and _is_blank(order.get("price")):
        problems.append("product and price")
    if _is_blank(order.get("customer_name")):
        problems.append("customer_name")
    if _is_blank(order.get("phone_number")) and _is_blank(order.get("whatsapp_number")):
        problems.append("phone_number/whatsapp_number")
    return problems


def _load_service_account_credentials() -> Credentials:
    """Load Google service account credentials from environment variable."""
    creds_json = os.environ.get("GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise RuntimeError(
            "GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON environment variable is required "
            "for Google Sheets production mode but was not set. "
            "Set it to the full service-account JSON key contents."
        )
    try:
        info = json.loads(creds_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}"
        ) from exc
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def load_orders_from_sheets(
    spreadsheet_id: str,
    worksheet_name: Optional[str] = None,
    client_id: Optional[str] = None,
) -> list[dict]:
    """
    Load orders from a Google Sheet and return them in the same normalized
    structure as load_orders() from run_upload.py.

    Args:
        spreadsheet_id: The Google Sheet ID (the long alphanumeric string in
            the Sheet URL, e.g. the part after /d/ and before /edit).
        worksheet_name: Optional tab/worksheet name. Defaults to the first
            sheet (sheet1).
        client_id: If provided, only rows whose client-identification column
            matches this client_id are returned. The client column is detected
            by looking for a header named "Client" or "Client ID". If the
            sheet has no such column, all rows are returned and a warning is
            printed — the caller must not assume client-specific filtering is
            happening just because client_id was supplied.

    Returns:
        list[dict] with keys matching COLUMN_MAP internal names:
        customer_name, phone_number, whatsapp_number, address, product, price
        plus excel_row for human-facing cell references (1-based, counting
        the header row as row 1).

    Raises:
        ValueError: If required columns are missing.
        RuntimeError: If credentials are missing or invalid.
    """
    creds = _load_service_account_credentials()
    gc = gspread.authorize(creds)

    sh = gc.open_by_key(spreadsheet_id)

    if worksheet_name:
        ws = sh.worksheet(worksheet_name)
    else:
        ws = sh.sheet1

    # Read all values in one request -- no per-row API calls.
    all_values = ws.get_all_values()
    if not all_values:
        return []

    headers = [h.strip() for h in all_values[0]]
    header_indices: dict[str, int] = {}
    for i, h in enumerate(headers):
        header_indices[h] = i

    # Check required columns exist in the sheet
    missing = [excel_col for excel_col in COLUMN_MAP if excel_col not in header_indices]
    if missing:
        raise ValueError(
            f"Google Sheet '{sh.title}' worksheet '{ws.title}' is missing "
            f"expected column(s): {missing}. Found columns: {headers}"
        )

    # Detect client-identification column (if any)
    client_col_name: Optional[str] = None
    for candidate in ("Client", "Client ID", "client_id", "client"):
        if candidate in header_indices:
            client_col_name = candidate
            break

    if client_id is not None and client_col_name is None:
        print(
            f"[sheets] WARNING: sheet '{sh.title}' / '{ws.title}' has no "
            f"client-identification column ('Client', 'Client ID', "
            f"'client_id', or 'client'). All rows will be returned; "
            f"client_id={client_id!r} filtering cannot be applied at the "
            f"sheet level. Confirm this sheet is client-specific or add a "
            f"client column before relying on client-scoped filtering.",
            flush=True,
        )

    orders: list[dict] = []
    skipped_non_client = 0
    for pos, row in enumerate(all_values[1:], start=2):  # excel_row = pos
        # Skip fully blank rows
        if all(_is_blank(cell) for cell in row):
            continue

        # Client-level filtering: skip rows belonging to a different client
        if client_id is not None and client_col_name is not None:
            col_idx = header_indices[client_col_name]
            row_client = row[col_idx].strip() if col_idx < len(row) else ""
            if row_client != client_id:
                skipped_non_client += 1
                continue

        order: dict = {}
        for excel_col, internal in COLUMN_MAP.items():
            col_idx = header_indices[excel_col]
            value = row[col_idx] if col_idx < len(row) else ""
            order[internal] = value

        # Skip rows missing required fields (same gate as Excel loader)
        missing_fields = _missing_required_fields(order)
        if missing_fields:
            continue

        order["excel_row"] = pos
        orders.append(order)

    if skipped_non_client:
        print(
            f"[sheets] client_id={client_id!r}: skipped {skipped_non_client} "
            f"row(s) belonging to other clients.", flush=True,
        )

    return orders