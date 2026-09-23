import logging
import re
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


class DataReader:
    """
    Reads the customer Excel file and returns clean, normalized
    customer dicts ready for database insertion.

    This class has no database dependency — it only reads and cleans data.
    The caller (CLI command or FastAPI endpoint) decides what to do
    with the returned list.

    Usage:
        reader    = DataReader(country_code="234")
        customers = reader.read_and_filter(
            excel_path=Path("data/customers.xlsx"),
            target_product="sadoer"
        )
        # customers → list of dicts, each matching Customer model columns
        for c in customers:
            db.upsert_customer(c)
    """

    # ── Honorifics to strip from the front of names ────────────
    # Case-insensitive match. "Dr." (with dot) also handled.
    # Add more here if you encounter them in your customer data.
    HONORIFICS = {
        "mr", "ma", "madam", "mrs", "miss", "ms", "dr", "prof", "rev",
        "barr", "engr", "chief", "pastor", "alhaji", "alhaja",
    }

    # ── HTML / formatting noise in the Product column ──────────
    # Your Excel has <br> tags and newlines in product names.
    # This pattern removes all of them before filtering by keyword.
    HTML_NOISE = re.compile(
        r"<br\s*/?>|&amp;|&nbsp;|&lt;|&gt;|\r\n|\r|\n",
        re.IGNORECASE
    )

    def __init__(self, country_code: str = "234"):
        """
        Args:
            country_code: The calling code for phone normalization.
                          Nigeria = "234". Change per client country.
        """
        self._cc  = country_code
        self._log = logging.getLogger(self.__class__.__name__)

    # ── MAIN ENTRY POINT ───────────────────────────────────────

    def read_and_filter(self, excel_path: Path, target_product: str) -> list:
        """
        Read Excel file, filter by product keyword, clean and return customers.

        Args:
            excel_path:     Path to the .xlsx file (e.g. Path("data/customers.xlsx"))
            target_product: Keyword to match in Product column (case-insensitive)
                            "sadoer" matches all three Sadoer product variants.

        Returns:
            List of customer dicts. Each dict has these keys:
                order_id, customer_name, first_name,
                raw_phone, normalized_phone, phone_valid,
                product_raw, product_clean, order_date

        Raises:
            FileNotFoundError: If the Excel file does not exist.

        Note:
            Never raises on a single bad row.
            Bad rows are logged and skipped — the rest continue.
        """
        if not excel_path.exists():
            raise FileNotFoundError(
                f"Excel file not found: {excel_path}\n"
                f"Drop your file into the data/ folder as '{excel_path.name}'."
            )

        self._log.info(f"Reading Excel: {excel_path}")

        # ── Read the Excel file twice ──────────────────────────
        # df_str: all columns forced to string
        #   → prevents phone numbers from becoming floats automatically
        #   → "08038365784" stays "08038365784" not 8038365784.0
        #
        # df_raw: original pandas types (float, datetime, etc.)
        #   → needed for WhatsApp Number (float) and Order Date (datetime)
        #   → pandas converts these correctly when we let it
        df_str = pd.read_excel(excel_path, dtype=str, engine="openpyxl")
        df_raw = pd.read_excel(excel_path, engine="openpyxl")

        total_rows = len(df_str)
        self._log.info(
            f"Rows: {total_rows} | "
            f"Columns: {df_str.columns.tolist()}"
        )

        # Track counts for the summary log at the end
        matched         = []   # Customers that passed the filter
        skipped_product = 0    # Rows skipped due to product mismatch
        invalid_phones  = 0    # Rows where phone could not be normalized
        error_rows      = 0    # Rows that caused unexpected exceptions

        for idx in range(total_rows):
            try:
                row     = df_str.iloc[idx]   # String-typed row
                row_raw = df_raw.iloc[idx]   # Original-typed row

                # ── STEP 1: Filter by product ──────────────────
                product_raw   = str(row.get("Product", "") or "")
                product_clean = self._strip_html(product_raw)

                if target_product.lower() not in product_clean.lower():
                    skipped_product += 1
                    self._log.debug(
                        f"Row {idx}: skipped — "
                        f"product='{product_clean[:50]}' "
                        f"does not contain '{target_product}'"
                    )
                    continue

                # ── STEP 2: Order ID ───────────────────────────
                # Required field — skip the row if missing
                order_id = str(row.get("Order ID", "") or "").strip()
                if not order_id or order_id.lower() == "nan":
                    self._log.warning(
                        f"Row {idx}: skipped — missing Order ID"
                    )
                    continue

                # ── STEP 3: Clean name ─────────────────────────
                full_name  = str(row.get("Name", "") or "").strip()
                first_name = self._clean_name(full_name)

                # ── STEP 4: Normalize phone numbers ───────────
                # WhatsApp Number column is a float in the raw read
                # Phone Number column is a string in the str read
                wa_raw  = row_raw.get("WhatsApp Number") \
                          if "WhatsApp Number" in row_raw.index else None
                std_raw = str(row.get("Phone Number", "") or "").strip()

                # Normalize both and pick the best one
                norm_wa,  wa_ok  = self._normalize_phone(wa_raw,  is_float=True)
                norm_std, std_ok = self._normalize_phone(std_raw, is_float=False)

                # Priority: WhatsApp Number > Phone Number > invalid
                if wa_ok:
                    normalized_phone = norm_wa
                    raw_phone        = str(wa_raw)
                    phone_valid      = True
                elif std_ok:
                    normalized_phone = norm_std
                    raw_phone        = std_raw
                    phone_valid      = True
                else:
                    # Both failed — still import but flag as invalid
                    # Customer will be skipped during sending
                    normalized_phone = None
                    raw_phone        = std_raw or str(wa_raw or "")
                    phone_valid      = False
                    invalid_phones  += 1
                    self._log.warning(
                        f"Row {idx} ({first_name}): "
                        f"phone unresolvable — "
                        f"WA={wa_raw!r}, STD={std_raw!r}"
                    )

                # ── STEP 5: Parse order date ───────────────────
                # Non-critical — used for send_order="recent_first"
                # If missing or unparseable, leave as None (sorting still works)
                order_date = None
                raw_date = row_raw.get("Order Date") \
                           if "Order Date" in row_raw.index else None
                if raw_date is not None:
                    try:
                        if not pd.isnull(raw_date):
                            order_date = pd.to_datetime(raw_date).to_pydatetime()
                    except (TypeError, ValueError):
                        pass  # Non-critical — skip silently

                # ── STEP 6: Build customer dict ────────────────
                # Keys match the Customer ORM model columns exactly.
                # This dict is passed directly to db.upsert_customer()
                matched.append({
                    "order_id":         order_id,
                    "customer_name":    full_name,
                    "first_name":       first_name,
                    "raw_phone":        raw_phone,
                    "normalized_phone": normalized_phone,
                    "phone_valid":      phone_valid,
                    "product_raw":      product_raw,
                    "product_clean":    product_clean,
                    "order_date":       order_date,
                })

            except Exception as e:
                # A single bad row must NEVER crash the whole import.
                # Log it and continue to the next row.
                error_rows += 1
                self._log.error(
                    f"Row {idx}: unexpected error — {e}",
                    exc_info=True
                )
                continue

        # ── Log import summary ─────────────────────────────────
        self._log.info(
            f"Import complete: "
            f"{len(matched)} matched '{target_product}' | "
            f"{skipped_product} skipped (other products) | "
            f"{invalid_phones} invalid phones | "
            f"{error_rows} row errors"
        )

        return matched

    # ── PRIVATE HELPERS ────────────────────────────────────────

    def _strip_html(self, text: str) -> str:
        """
        Remove HTML tags and whitespace noise from product name strings.

        Your Excel has values like:
          "Sadoer Collagen Combo Set<br>Color: Natural Glow"
        This becomes:
          "Sadoer Collagen Combo Set Color: Natural Glow"

        Args:
            text: Raw product string from Excel cell.

        Returns:
            Cleaned string with HTML removed and whitespace collapsed.
        """
        cleaned = self.HTML_NOISE.sub(" ", text)
        return " ".join(cleaned.split()).strip()

    def _normalize_phone(self, raw, is_float: bool = False) -> tuple:
        """
        Normalize any Nigerian phone format to E.164: 234XXXXXXXXXX
        That is exactly 13 digits starting with the country code.

        Handles every format found in the real Excel file:
          "+08038365784"     →  "2348038365784"   (wrong + prefix)
          "+2348053968527"   →  "2348053968527"   (already correct)
          "08130571075"      →  "2348130571075"   (local 0 prefix)
          8068526757.0       →  "2348068526757"   (float)
          2.348077e+12       →  "2348068526757"   (scientific float)
          NaN / None / ""    →  (None, False)     (invalid)

        Args:
            raw:      The raw value from the Excel cell.
            is_float: True for WhatsApp Number column (pandas reads as float).
                      False for Phone Number column (already string).

        Returns:
            Tuple of (normalized_string_or_None, is_valid_bool)
            Example: ("2348038365784", True)
            Example: (None, False)
        """
        cc = self._cc  # "234" for Nigeria

        if is_float:
            if raw is None:
                return None, False
            try:
                if pd.isnull(raw):
                    return None, False
            except (TypeError, ValueError):
                pass
            try:
                raw = str(int(float(raw)))
            except (ValueError, TypeError, OverflowError):
                return None, False
        else:
            if not raw or str(raw).strip() in ("", "nan", "None", "NaN"):
                return None, False
            raw = str(raw).strip()

        has_plus = str(raw).startswith("+")
        digits   = re.sub(r"[^\d]", "", str(raw))

        if has_plus and digits.startswith("0"):
            pass

        if digits.startswith("0") and not digits.startswith(cc):
            digits = digits[1:]

        if len(digits) == 10 and not digits.startswith(cc):
            digits = cc + digits

        if len(digits) == 13 and digits.startswith(cc):
            return digits, True

        self._log.debug(
            f"Phone normalization failed: "
            f"raw={raw!r} → digits={digits!r} "
            f"(length={len(digits)}, expected 13 starting with {cc})"
        )
        return None, False

    def _clean_name(self, full_name: str) -> str:
        """
        Extract a clean first name suitable for "Hi {first_name}!" greeting.

        Rules:
          - If the name starts with a known honorific (Mr, Mrs, Dr, etc.),
            preserve the name exactly as the user typed it, only fixing
            capitalisation (e.g. "mrs blessing okafor" → "Mrs Blessing Okafor",
            "MRS BLESSING" → "Mrs Blessing"). The honorific stays attached.
          - If there is no honorific, take only the first word and title-case it.
          - Always title-cases the result so every word starts with a capital.
          - Falls back to "Customer" if the name is empty or garbage.

        Examples:
          "mrs blessing okafor"  → "Mrs Blessing Okafor"
          "MRS BLESSING"         → "Mrs Blessing"
          "Dr. EMEKA Chukwu"     → "Dr. Emeka Chukwu"
          "ALHAJA fatima sale"   → "Alhaja Fatima Sale"
          "TITILAYO ADELEYE"     → "Titilayo"
          "blessing"             → "Blessing"
          ""                     → "Customer"

        Args:
            full_name: Raw name string from the Excel Name column.

        Returns:
            Clean name string, always non-empty.
        """
        if not full_name or full_name.strip().lower() in ("nan", "none", ""):
            return "Customer"

        parts = full_name.strip().split()

        # Check if the first word is a known honorific
        has_honorific = parts[0].lower().rstrip(".") in self.HONORIFICS

        if has_honorific:
            # Keep the full name as the user typed it, just fix capitalisation
            return " ".join(word.capitalize() for word in parts)
        else:
            # No honorific — take only the first name and title-case it
            return parts[0].strip().title()