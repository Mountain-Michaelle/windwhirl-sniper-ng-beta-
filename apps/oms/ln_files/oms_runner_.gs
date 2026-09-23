// File: WhatsApp Order Normalizer — Apps Script
// Paste into: Extensions > Apps Script > Code.gs (replace all)
// Updated: 2026-07-16 — GENERATE REPORT ENHANCEMENTS (isolated to the report
// feature only; parser, triggers, validation, duplicate detection, summaries,
// formulas, and every other part of this file are unchanged from the version
// this was based on):
//   - Package field in the generated report is now lightly normalized for
//     dispatch: "Free" and marketing phrases ("Free Doorstep Delivery", "Free
//     Home Delivery", "Free Delivery") are stripped, separators are cleaned
//     up, and the line always ends with "+ Doorstep Delivery" (added once,
//     never duplicated if already present). This ONLY affects the text
//     written into the generated report — the Product cell in the sheet
//     itself is never touched.
//   - A "Delivery #:" line (left blank for manual entry) now appears right
//     below the report's date line, before "Package:".
// See the GENERATE REPORT section below for both changes; nothing outside
// that section was modified.
//
// Updated: 2026-07-15 (rev 8)
// Changes this revision — business-logic fix to the duplicate rule ONLY.
// No column/structure changes, no other function touched, nothing that
// touches your existing data:
//   - Duplicate Order? now flags TRUE when phone number matches AND EITHER
//     (a) the customer name also matches (new, primary signal), OR
//     (b) product + address also match (unchanged rule, kept as a secondary
//     signal). Previously it required an exact phone+product+address match
//     every time, which under-flagged real repeat orders whenever the
//     product/address text differed even slightly (extra comma, missing
//     period, different price formatting).
//   - normalizeText() now also strips common punctuation (commas, periods,
//     etc.) before comparing, not just casing/whitespace — this is what lets
//     "Water board auchi, polosa street." and "Water board auchi polosa
//     street" match as the same address.
//   - Phone is checked first and short-circuits the rest of the comparison
//     on a miss, so this is not slower than before — most non-matching rows
//     now get skipped after one cheap check instead of three.
//
// Earlier revisions:
//   - Updated: 2026-07-15 (rev 6)
//   - Three targeted fixes only, no column/structure changes:
//   - FIXED the duplicate-detection bug: when a single paste contained more
//     than one order, inserting rows for the extra orders physically shifted
//     every row below the insertion point down by one, but the in-memory
//     duplicate-check snapshot (scanData) was never updated to match. A real
//     duplicate match against an earlier/pre-existing row could get its TRUE
//     flag written to the WRONG row (often a blank one just inserted), making
//     it look like detection had failed even though the match was found
//     correctly. processRow() now shifts scanData's row pointers by one every
//     time a row is inserted, so the flag always lands on the right row.
//   - Sped up loadPasteScanData_() by capping its scan at VALIDATION_LAST_ROW
//     instead of sheet.getLastRow(). Real order rows never legitimately go
//     past VALIDATION_LAST_ROW — but getLastRow() also picks up the ORDER
//     SUMMARY block + chart written further down the sheet, so every single
//     paste was scanning that block too, for zero benefit, and it only grows
//     over time. This is the same duplicate check, just not wasting reads.
//   - writeRecord() now writes its plain (non-formula) cell values in two
//     batched range writes (Name/Phone/WhatsApp/Note, then Date/Category/
//     Product/Price/Address/Delivery/Question) instead of eleven separate
//     single-cell writes. Same values, same cells, same order — just fewer
//     calls, which is where most of the per-row overhead was coming from.
//     Everything else (checkboxes, formulas, number formats, duplicate/invalid
//     flag logic) is untouched.
//
// Earlier revisions:
//   - Fixed Phone Number / WhatsApp Number columns (D, E) showing up as a
//     dropdown instead of accepting normal typed text. Nothing in this script
//     ever set a dropdown/checkbox rule on those two columns — but a validation
//     rule can outlive whatever set it, e.g. from a stale rule left over before
//     an earlier column shift, or a manual/dragged edit in the sheet itself.
//     initializeSheet(), applyRowValidation(), and writeRecord() now all
//     explicitly clearDataValidations() on columns D-E before formatting them —
//     this only strips the validation RULE, never touches the phone numbers
//     already sitting in those cells.
//   - Label matching in parseBlock() is no longer tied to one template's exact
//     wording (e.g. requiring "Input Your Full Name" verbatim). LABELS now
//     matches by keyword instead — "Name:", "Full Name:", "Customer Name:" all
//     resolve to the same field, same for Phone/Mobile, Address/Location,
//     Product/Package/Item, Price/Amount/Cost, and so on. This is what makes a
//     message like "Name: Francis onazi / Phone:07012259703 / Address: Lagos
//     state Ajah / Product: 1Sadeor collagen combo-set / 29,500," parse
//     correctly — nothing template-specific had to be added for it.
//   - resolvePrice() no longer requires a literal "=" before the price. It now
//     recognizes a trailing price-looking number (comma-grouped, or 3+ plain
//     digits) at the end of the product text regardless of what — if anything —
//     comes right before it, which is what catches a bare price on its own
//     line like "29,500," directly under a "Product:" line.
//
// Earlier revisions:
//   - S/N and Filtered # have swapped columns: S/N is now column R, Filtered #
//     is now column B. IMPORTANT: this needs a ONE-TIME manual step to move
//     your already-existing data — see swapSnAndFilteredColumns_migration()
//     near the bottom of the file for exactly how to run it (once, safely).
//   - Customer Name (column C) is now pinned as a sidebar while scrolling
//     right, via frozen columns A-C — same instant "stick" behavior the
//     header row already has while scrolling down.
//
// Earlier revisions:
//   - Updated: 2026-07-13 (rev 3)
//   - The Date column (I) now stores a real Date value instead of plain text
//     like "13th July" — parseOrderDate() converts it (assuming the current
//     year, rolling back a year if that would land more than a few days in
//     the future). If a message has no date or it can't be parsed, the column
//     falls back to today's actual date (not raw text, not blank) so every
//     row stays a real, uniformly filterable date with no exceptions.
//   - New column V "Today's Order?" + "Show Today's Orders" / "Clear Today's
//     Orders Filter" menu items — same saved-filter-view pattern as the other
//     Today/Overdue/Flagged shortcuts, but for the order date instead of the
//     check-up date.
//   - New "Show Orders For a Date…" / "Clear Date Filter": prompts for any
//     date and filters column I directly (a plain Basic Filter, not a saved
//     view — a saved view per possible day doesn't scale), so any single
//     day's orders can be isolated, not just today's.
//
// Earlier revisions:
//   - Removed the "Agent / Source" column entirely (it wasn't relevant) — every
//     column from Duplicate Order? onward shifted one letter left (Q->P, R->Q,
//     S->R, T->S, U->T, V->U). The old "From <name>" trailing line in a message
//     is now just skipped instead of being stored anywhere.
//   - Fixed price extraction: most messages never have a separate "Price:" line —
//     the amount is embedded at the end of the package line itself (e.g.
//     "...Free Collagen Hand Cream = #28,500"). Price (col L) was being left
//     blank in that case. A new resolvePrice() pulls the number out of the
//     product line whenever there's no explicit "Price:" label, so column L is
//     now populated for every order, not just the rare one with its own Price line.
//   - Fixed block splitting for raw WhatsApp chat-log pastes: messages copied
//     straight out of WhatsApp are prefixed per-line with "[7/13, 1:14 PM]
//     Sender Name: ...". splitIntoBlocks() didn't recognize that prefix, so a
//     multi-message paste could fail to split cleanly and fields from one order
//     (address, price, etc.) would bleed into the next row. It now detects and
//     splits on that "[date, time] Sender:" pattern first, stripping the prefix,
//     before falling back to the existing "####" / repeated "Select Your
//     Package" splitting.
//
// Earlier revisions:
//   - New column U "Flagged (Dup/Invalid)?" + "Show Flagged Orders" / "Clear Flagged
//     Filter" menu items: same one-tap saved-filter-view pattern as Today Check-ups,
//     but for rows where Duplicate Order? OR Invalid Number? is TRUE.
//   - New column V "Overdue Check-up?" + "Show Overdue Check-ups" / "Clear Overdue
//     Filter": flags rows where the check-up date has passed and Action is still
//     Scheduled or Pending — so a missed check-up doesn't just vanish from view.
//   - Conditional formatting: overdue rows tint red, Delivered/Cash Remitted tint
//     green, Cancelled/Deleted/Banned tint grey — scan the list without reading
//     the Action column word for word.
//   - Row-cap warning: a toast fires once you're within 20 rows of the 500-row
//     setup limit, so you're not caught off guard mid-paste.
//   - New "ORDER SUMMARY" report + bar chart, written below row 500: Total
//     Delivered, Total Pending, Total Invalid, Total Duplicate, Total Cancelled,
//     % Delivered, Total Orders. Built from live formulas — always current,
//     no manual recalculation needed. Menu: Order Tools > Refresh Summary Report.
//   - New Check-up Reminders: Order Tools > Enable Check-up Reminders turns on a
//     background check every 5 minutes; if this spreadsheet is open in your
//     browser/app when a still-unresolved check-up is within 5 minutes of its
//     time, you'll get a toast notification. Disable Check-up Reminders turns it off.
//
// Earlier revisions (kept for reference):
//   - Price now actually written to column L as a real number (was parsed but never saved)
//   - "Scheduled" (not "Pending") now triggers the check-up date jump
//   - Column S "Filtered #": live SUBTOTAL formula, temporary sequence for the current filter
//   - Column T "Today Check-up?" + "checkup_for_today" saved filter view (mobile-friendly)
//   - Mobile Quick Schedule menu: sets check-up date + Action in one tap, no date picker needed
//   - Removed the frozen "sidebar" column — only the header row stays frozen
//   - REQUIRES enabling the "Google Sheets API" Advanced Service once (see SETUP below)
//
// NEW, INDEPENDENT FEATURE (not part of any numbered revision above — bolted
// on separately and does not interact with the parser, duplicate detection,
// or anything else in this file):
//   - "Generate Report": type GR into column W (Report) on any order row and
//     it's replaced with a formatted, WhatsApp-ready delivery report built
//     purely from that row's already-parsed columns — no re-parsing, no
//     scanning, no duplicate check. See the dedicated section below.

/**
 * WhatsApp Order Normalizer
 * ---------------------------------------------------------------
 * Paste raw WhatsApp order text into Column A (any row, from row 2 down).
 * The script parses it and fills the rest of the row. The raw text stays
 * in column A for reference, but the column is kept narrow and the row
 * kept single-line height — click into the cell (or briefly widen the
 * column) whenever you need to read or copy it back. Pasting a new
 * message into that same cell overwrites and reprocesses it.
 *
 * Multiple orders pasted together in one go (separated by "####", or
 * just back-to-back) are split into their own rows automatically.
 *
 * COLUMN LAYOUT (most-used columns placed right up front):
 *  A: Raw Message         <- paste here, kept narrow; overwrite to reprocess
 *  B: Filtered #          <- live formula, temporary sequence for the current filter view
 *  C: Customer Name       <- pinned as a sidebar while scrolling (frozen columns A-C)
 *  D: Phone Number
 *  E: WhatsApp Number
 *  F: Note                <- your comments, always left blank by the script
 *  G: Action              <- dropdown, restricted to the fixed status list
 *  H: Check-up Date&Time  <- pick a date/time here when Action = Scheduled
 *  I: Date                <- real Date value (parsed from text like "13th July"),
 *                             not just display text — filterable/sortable
 *  J: Category             (e.g. "Tiktok Body lotion")
 *  K: Product
 *  L: Price (₦)           <- number, extracted automatically from the message
 *  M: Address
 *  N: Delivery Date
 *  O: Question
 *  P: Duplicate Order?    <- checkbox, TRUE/FALSE only, set automatically
 *  Q: Invalid Number?     <- checkbox, TRUE/FALSE only, set automatically
 *  R: S/N                 <- permanent record number, never changes
 *  S: Today Check-up?     <- live formula checkbox, TRUE when H falls on today's date
 *  T: Flagged (Dup/Invalid)? <- live formula checkbox, TRUE when P or Q is TRUE
 *  U: Overdue Check-up?   <- live formula checkbox, TRUE when H is in the past and
 *                             Action is still Scheduled or Pending
 *  V: Today's Order?      <- live formula checkbox, TRUE when I (Date) is today
 *  W: Report               <- type GR here to generate a delivery report (new, independent feature)
 *
 * Below row 500 (or wherever VALIDATION_LAST_ROW ends), a live ORDER SUMMARY
 * report and bar chart total up Delivered / Pending / Invalid / Duplicate /
 * Cancelled / % Delivered / Total Orders — see "Refresh Summary Report".
 *
 * SETUP:
 *  1. Extensions > Apps Script, replace all code with this file, Save.
 *  2. In the Apps Script editor, click "+ Services" (left sidebar) > select
 *     "Google Sheets API" > Add. (One-time — needed for the saved filter views;
 *     nothing else in this script uses it.)
 *  3. Reload the spreadsheet.
 *  4. Menu "Order Tools" > "Initialize Sheet" (run once — builds headers, the
 *     Action dropdown, the Check-up date picker, all flag/formula columns,
 *     conditional formatting, the default filter, and the summary report,
 *     for rows 2-500).
 *  5. Approve permissions the first time you're asked (one-time only).
 *  6. Click a single cell in column A and paste a WhatsApp order message.
 *
 * DAILY USE:
 *  - Column G (Action) is a dropdown — click the cell, pick a status.
 *  - Pick "Scheduled" and the script jumps you straight to column H so you
 *    can set the check-up date/time (native Sheets calendar picker on
 *    desktop — click the cell, a calendar pops up; type the time after
 *    the date if you need one, e.g. 2026-07-14 10:00).
 *  - ON MOBILE: the native calendar popup is a desktop-only Sheets feature —
 *    Google doesn't expose it the same way on the Android/iOS app, and Apps
 *    Script can't force it to appear. Use Order Tools > Mobile Quick Schedule
 *    instead: tap any cell in the order's row, then pick Today / Tomorrow /
 *    In 2 Days / In 1 Week. It sets the check-up time and flips Action to
 *    "Scheduled" for you — no typing, no picker needed.
 *  - "Duplicate Order?" is set automatically when phone number matches
 *    another row AND either the customer name also matches, or the product
 *    and address also match (all normalized — punctuation/casing ignored).
 *  - "Invalid Number?" is set automatically when the Phone (or WhatsApp)
 *    number doesn't normalize into a valid Nigerian mobile number.
 *  - Column B (Filtered #) is a live formula, not stored data: apply any
 *    filter and it renumbers 1, 2, 3... for just what's visible. Clear the
 *    filter and it reverts to matching the permanent S/N in column R.
 *  - Each day, Order Tools > "Show Today's Check-ups" builds/refreshes a saved
 *    filter view called "checkup_for_today". After that first run, switch to
 *    it any time from the Data icon (funnel) > Filter views > checkup_for_today
 *    — the fast, one-tap way to get there on mobile. Only rows whose check-up
 *    date (column H) is today are shown; newly pasted orders are untouched by
 *    it, since they have no check-up date yet.
 *  - "Show Overdue Check-ups" / filter view "overdue_checkups": rows whose
 *    check-up date has already passed and Action is still Scheduled or
 *    Pending — nothing missed silently drops off your radar.
 *  - "Show Flagged Orders" / filter view "flagged_orders": rows where
 *    Duplicate Order? or Invalid Number? is TRUE — a fast cleanup sweep.
 *  - "Enable Check-up Reminders": a background trigger checks every 5 minutes;
 *    if a still-unresolved (Scheduled/Pending) check-up is within 5 minutes,
 *    you get a toast — but only while the spreadsheet is actually open
 *    somewhere. It's a safety net for when you forget to filter, not a
 *    substitute for checking checkup_for_today each morning.
 *  - "Show Today's Orders" / filter view "todays_orders": rows whose order
 *    date (column I) is today.
 *  - "Show Orders For a Date…": type any date (e.g. 13/07/2026 or 13th July)
 *    to filter column I to just that day. "Clear Date Filter" resets it.
 *  - If data lands in column A without a normal paste (e.g. an import),
 *    run Order Tools > Reprocess Column A to catch it up.
 *  - Column W (Report): type GR into that cell on any order row and it's
 *    replaced with a formatted delivery report ready to paste into WhatsApp —
 *    built instantly from that row's already-parsed data, nothing re-parsed.
 *    The Package line in the report is lightly cleaned up for dispatch
 *    (marketing wording like "Free" removed, always ends with "+ Doorstep
 *    Delivery") and a blank "Delivery #:" line appears under the date for
 *    you to fill in by hand — see the GENERATE REPORT section for both.
 *
 * PASTE NOTE: click ONE cell in column A before pasting (not a range),
 * so the multi-line message stays inside that single cell.
 */

const CONFIG = {
  RAW_COL: 1,       // A
  FILTERED_COL: 2,  // B — swapped with S/N
  NAME_COL: 3,      // C
  PHONE_COL: 4,     // D
  WHATSAPP_COL: 5,  // E
  NOTE_COL: 6,      // F
  ACTION_COL: 7,    // G
  CHECKUP_COL: 8,   // H
  DATE_COL: 9,        // I
  CATEGORY_COL: 10,   // J
  PRODUCT_COL: 11,    // K
  PRICE_COL: 12,      // L
  ADDRESS_COL: 13,    // M
  DELIVERY_COL: 14,   // N
  QUESTION_COL: 15,   // O
  DUPLICATE_COL: 16,  // P
  INVALID_COL: 17,    // Q
  SN_COL: 18,          // R — swapped with Filtered #
  TODAY_COL: 19,       // S
  FLAGGED_COL: 20,     // T
  OVERDUE_COL: 21,     // U
  TODAY_ORDER_COL: 22, // V

  HEADER_ROW: 1,
  FIRST_DATA_ROW: 2,
  VALIDATION_LAST_ROW: 500, // how many rows to pre-apply dropdown/checkbox/date/formula validation to
  ROW_CAP_WARNING_THRESHOLD: 20, // toast once this many rows (or fewer) remain before the cap
  DEFAULT_ROW_HEIGHT: 21,  // Sheets' standard single-line row height
  QUICK_SCHEDULE_HOUR: 10, // default hour used by Mobile Quick Schedule (24h clock)
  REMINDER_WINDOW_MINUTES: 5, // toast fires when a check-up is this many minutes away or less
  ORDER_DATE_FORMAT: 'd mmm yyyy', // display format for the real Date value now stored in the Date column

  HEADERS: ['Raw Message','Filtered #','Customer Name','Phone Number','WhatsApp Number',
             'Note','Action','Check-up Date & Time','Date','Category','Product',
             'Price (₦)','Address','Delivery Date','Question',
             'Duplicate Order?','Invalid Number?','S/N','Today Check-up?',
             'Flagged (Dup/Invalid)?','Overdue Check-up?',"Today's Order?"]
};

// Shows a confirmation message. Prefers a real dialog when a document UI is
// available (running through the sheet's own "Order Tools" menu, or a manual
// Run while that sheet tab is genuinely open); falls back to a toast when it
// isn't — e.g. a function run directly from the Apps Script editor's ▶ Run
// button has no document window attached, and SpreadsheetApp.getUi() throws
// in that context. Using this instead of a raw getUi().alert() means the
// actual work a function already did (headers built, filter created, etc.)
// never gets undermined by a crash on the very last, purely cosmetic line.
function safeAlert(message, title) {
  try {
    SpreadsheetApp.getUi().alert(message);
  } catch (e) {
    SpreadsheetApp.getActiveSpreadsheet().toast(message, title || 'Notice', 6);
  }
}

const STATUS_LIST = [
  "Pending", "Confirmed", "Awaiting", "Delivered",
  "Commitment Fee Requested", "Not Picking Calls", "Switched Off",
  "Shipped", "Scheduled", "Failed", "Cancelled", "Returned",
  "Cash Remitted", "After-Sale Call", "Deleted", "Banned"
];

// The status that triggers the automatic jump to the check-up date cell.
const SCHEDULING_STATUS = "Scheduled";

// Statuses treated as "still needs a human to act on it" for overdue/reminder logic.
const UNRESOLVED_STATUSES = ["Scheduled", "Pending"];

// Nigerian mobile number after normalization: +234 then a 10-digit number starting 7/8/9
const NIGERIA_MOBILE_REGEX = /^\+234[7-9]\d{9}$/;

// Field labels, matched by KEYWORD rather than exact wording — this is what lets
// the parser handle "Name:" as easily as "Input Your Full Name:", or "Product:"
// as easily as "Select Your Package:", without hand-adding every template a
// customer or agent happens to type. normalizeLabel() has already lowercased
// and stripped punctuation/spaces from the text before a colon, so these are
// simple substring checks against that normalized text.
//
// Order matters: matchLabel() below returns the FIRST match, so more specific/
// narrower keys are listed first and the broadest, most generic one (name) is
// listed last — this keeps something like "Product Name:" resolving to product
// rather than name, since product is checked first.
const LABELS = [
  { key: 'whatsapp', test: n => n.indexOf('whatsapp') !== -1 },
  { key: 'phone',    test: n => n.indexOf('phone') !== -1 || n.indexOf('mobile') !== -1 || n.indexOf('tel') !== -1 },
  { key: 'address',  test: n => n.indexOf('address') !== -1 || n.indexOf('location') !== -1 },
  { key: 'delivery', test: n => n.indexOf('deliver') !== -1 },
  { key: 'question', test: n => n.indexOf('question') !== -1 || n.indexOf('query') !== -1 },
  { key: 'price',    test: n => n.indexOf('price') !== -1 || n.indexOf('amount') !== -1 || n.indexOf('cost') !== -1 },
  { key: 'product',  test: n => n.indexOf('product') !== -1 || n.indexOf('package') !== -1 || n.indexOf('item') !== -1 },
  { key: 'name',     test: n => n.indexOf('name') !== -1 }
];

// Real field labels — in every template seen so far — are short: "Select Your
// Package", "Input Full Address", "When do you want us to deliver to you?" (9
// words), "Do you have any questions?" (5 words). Genuine VALUE/content lines
// (product descriptions, addresses, delivery notes) run much longer. Keyword
// matching is only attempted on lines at or under these limits — this is what
// stops a long product line like "...Free doorstep delivery = #28,500" from
// being mistaken for the delivery-date label just because it contains the
// word "delivery" as part of an ordinary sentence.
const MAX_LABEL_WORDS = 10;
const MAX_LABEL_CHARS = 60;

// A line wrapped in single asterisks (WhatsApp's bold formatting), e.g.
// "*Tiktok Body lotion*" or "*WhatsApp order*", is always a heading/category
// line in every template seen — genuine field labels ("Select Your Package",
// "Input Phone Number", "Name:") are never bold-formatted this way. Without
// this check, a heading that happens to contain a keyword — e.g. "*WhatsApp
// order*" containing "WhatsApp" — gets mistaken for the WhatsApp-number field
// label itself, hijacking everything that follows it. Matches an optional
// trailing period too, since some messages end the heading "*...*.".
const BOLD_HEADING_LINE_REGEX = /^\*[\s\S]*\*\.?$/;

// Matches things like "13th July", "7th July", "13/07/2026"
const DATE_REGEX = /^\d{1,2}(st|nd|rd|th)?\s+[A-Za-z]+\.?$|^\d{1,2}[\/\-]\d{1,2}([\/\-]\d{2,4})?$/;

/* ---------------- TRIGGERS & MENU ---------------- */

function onOpen() {
  ensureReportColumnHeader_(); // new, independent feature — see GENERATE REPORT section below; touches only cell W1

  SpreadsheetApp.getUi()
    .createMenu('Order Tools')
    .addItem('Initialize Sheet', 'initializeSheet')
    .addItem('Reprocess Column A', 'reprocessAll')
    .addItem('Refresh Summary Report', 'buildSummaryReport')
    .addSeparator()
    .addSubMenu(SpreadsheetApp.getUi().createMenu('Mobile Quick Schedule')
      .addItem('Today', 'scheduleToday')
      .addItem('Tomorrow', 'scheduleTomorrow')
      .addItem('In 2 Days', 'scheduleIn2Days')
      .addItem('In 1 Week', 'scheduleInWeek'))
    .addSeparator()
    .addItem("Show Today's Check-ups", 'showTodayCheckups')
    .addItem('Clear Today Filter', 'clearTodayFilter')
    .addItem('Show Overdue Check-ups', 'showOverdueCheckups')
    .addItem('Clear Overdue Filter', 'clearOverdueFilter')
    .addItem('Show Flagged Orders (Dup/Invalid)', 'showFlaggedOrders')
    .addItem('Clear Flagged Filter', 'clearFlaggedFilter')
    .addSeparator()
    .addItem("Show Today's Orders", 'showTodaysOrders')
    .addItem("Clear Today's Orders Filter", 'clearTodaysOrdersFilter')
    .addItem('Show Orders For a Date…', 'showOrdersForDate')
    .addItem('Clear Date Filter', 'clearDateFilter')
    .addSeparator()
    .addItem('Enable Check-up Reminders', 'enableCheckupReminders')
    .addItem('Disable Check-up Reminders', 'disableCheckupReminders')
    .addSeparator()
    .addItem('⚠️ Repair S/N (one-time)', 'repairSnAfterPrematureInitialize')
    .addToUi();
}

// Simple trigger — fires automatically on user edits/pastes.
function onEdit(e) {
  try {
    if (!e || !e.range) return;
    const sheet = e.range.getSheet();
    const row = e.range.getRow();
    const col = e.range.getColumn();

    // Case 1: raw message pasted into column A
    if (col === CONFIG.RAW_COL && row >= CONFIG.FIRST_DATA_ROW) {
      if (e.range.getNumRows() > 1 || e.range.getNumColumns() > 1) return; // single-cell only
      const raw = sheet.getRange(row, CONFIG.RAW_COL).getValue();
      if (raw && String(raw).trim()) processRow(sheet, row, String(raw));
      return;
    }

    // Case 2: Action dropdown changed to "Scheduled" -> jump to check-up date/time
    if (col === CONFIG.ACTION_COL && row >= CONFIG.FIRST_DATA_ROW) {
      const value = e.range.getValue();
      if (value === SCHEDULING_STATUS) {
        const checkupCell = sheet.getRange(row, CONFIG.CHECKUP_COL);
        checkupCell.activate();
        SpreadsheetApp.getActiveSpreadsheet().toast('Pick the check-up date & time.', 'Scheduled order', 5);
      }
    }

    // Case 3: "GR" typed into the Report column (W) -> generate a delivery report.
    // NEW, INDEPENDENT FEATURE — see the GENERATE REPORT section below for the
    // isolated constants/helpers this calls. Exits immediately on anything
    // that doesn't exactly match; never touches the parser, never scans the
    // sheet, never reads/writes any row but this one.
    if (col === REPORT_COL && row >= CONFIG.FIRST_DATA_ROW) {
      if (e.range.getNumRows() !== 1 || e.range.getNumColumns() !== 1) return; // single-cell only
      const typedValue = e.value;
      if (typeof typedValue !== 'string' || typedValue.trim().toUpperCase() !== REPORT_TRIGGER_VALUE) return;
      const reportText = generateDeliveryReport_(sheet, row);
      e.range.setValue(reportText).setWrap(true).setVerticalAlignment('top');
      return;
    }
  } catch (err) {
    console.error(err);
  }
}

/* ---------------- GENERATE REPORT (new, independent feature) ---------------- */
// Completely separate from the parser, duplicate detection, and everything
// else in this file. Reads ONLY the six already-parsed columns it needs from
// the single edited row (one batched range read, O(1) regardless of sheet
// size), formats them into a WhatsApp-ready delivery report, and writes the
// result back into that same cell. Never re-parses the raw message, never
// scans other rows, never calls any existing parsing/duplicate-check function.

const REPORT_COL = 23;              // W — new column, does not shift or touch any existing column
const REPORT_TRIGGER_VALUE = 'GR';  // case-insensitive; matched against the trimmed, uppercased typed value
const REPORT_MONTH_NAMES = ['January','February','March','April','May','June',
  'July','August','September','October','November','December']; // full names, only used by the report — separate from the parser's MONTH_ABBR

// Product cells store "<description> — ₦<price>" (see writeRecord/resolvePrice
// above) — the report shows Price on its own line, so this strips that
// trailing "— ₦28,500" back off for the Package line. Pure string formatting,
// not a re-parse: it doesn't extract or interpret anything, just trims a
// known, fixed suffix pattern that this same script itself appended earlier.
const REPORT_PRODUCT_PRICE_SUFFIX_REGEX = /\s*—\s*₦[\d,]+(?:\.\d+)?\s*$/;

// ---- Report-only package cleanup (ENHANCEMENT 1) ----
// Purely presentational: only ever applied to the text going INTO the
// generated report string. Never writes back to the Product cell or any
// other part of the sheet, and is not used by the parser or duplicate check.
//
// Marketing phrases are matched and removed as whole phrases FIRST (so no
// orphaned words are left behind), then any remaining standalone "Free" is
// removed. "Doorstep Delivery" is treated separately — it's operational
// information for the rider, not marketing, so it's always guaranteed to be
// present exactly once at the end rather than stripped.
const REPORT_MARKETING_PHRASE_REGEX = /free\s+(?:doorstep|home)\s+delivery|free\s+delivery/gi;
const REPORT_FREE_WORD_REGEX = /\bfree\b/gi;
const REPORT_DOORSTEP_DELIVERY_REGEX = /doorstep\s+delivery/i;

function cleanPackageForReport_(packageText) {
  let text = String(packageText || '');

  text = text.replace(REPORT_MARKETING_PHRASE_REGEX, '');
  text = text.replace(REPORT_FREE_WORD_REGEX, '');

  // Rebuild the "+"-separated list, dropping anything the removals above left
  // empty — this is what collapses duplicate "+"s, strips a trailing "+", and
  // cleans up extra spacing, all in one pass.
  text = text.split('+')
    .map(function(part) { return part.trim(); })
    .filter(function(part) { return part.length > 0; })
    .join(' + ');
  text = text.replace(/\s+/g, ' ').trim();

  if (!REPORT_DOORSTEP_DELIVERY_REGEX.test(text)) {
    text = text ? (text + ' + Doorstep Delivery') : 'Doorstep Delivery';
  }

  return text;
}

// Sets W1's header once, only if it isn't already "Report" — called from
// onOpen(). Deliberately NOT added to CONFIG.HEADERS / initializeSheet(), so
// this stays fully isolated and Initialize Sheet's behavior is byte-for-byte
// unchanged. Touches exactly one cell, nothing else.
function ensureReportColumnHeader_() {
  const sheet = SpreadsheetApp.getActiveSheet();
  const headerCell = sheet.getRange(CONFIG.HEADER_ROW, REPORT_COL);
  if (headerCell.getValue() !== 'Report') {
    headerCell.setValue('Report').setFontWeight('bold').setBackground('#4a86e8').setFontColor('#ffffff');
  }
}

function ordinalSuffix_(day) {
  if (day % 10 === 1 && day !== 11) return 'st';
  if (day % 10 === 2 && day !== 12) return 'nd';
  if (day % 10 === 3 && day !== 13) return 'rd';
  return 'th';
}

function formatReportDate_(d) {
  return d.getDate() + ordinalSuffix_(d.getDate()) + ' ' + REPORT_MONTH_NAMES[d.getMonth()] + ' ' + d.getFullYear();
}

function cleanPackageText_(productCellValue) {
  return String(productCellValue || '').replace(REPORT_PRODUCT_PRICE_SUFFIX_REGEX, '').trim();
}

function formatReportPrice_(priceCellValue) {
  return (typeof priceCellValue === 'number') ? ('₦' + priceCellValue.toLocaleString()) : '';
}

// Builds the report text for one row. ONE range read spanning Name(C) through
// Address(M) — column offsets are computed from the existing CONFIG constants
// rather than hardcoded, so this stays correct even if those columns ever move.
function generateDeliveryReport_(sheet, row) {
  const width = CONFIG.ADDRESS_COL - CONFIG.NAME_COL + 1;
  const rowValues = sheet.getRange(row, CONFIG.NAME_COL, 1, width).getValues()[0];
  const at = function(col) { return rowValues[col - CONFIG.NAME_COL]; };

  // --- New: empty field detection for required columns ---
  const rawProduct  = at(CONFIG.PRODUCT_COL);
  const rawPrice    = at(CONFIG.PRICE_COL);
  const rawName     = at(CONFIG.NAME_COL);
  const rawAddress  = at(CONFIG.ADDRESS_COL);

  // Treat as empty if undefined, null, or trimmed string is blank.
  // For price we also consider a completely empty cell (not 0) as missing.
  const isEmpty = (val, isPrice = false) => {
    if (val === undefined || val === null) return true;
    if (isPrice) return String(val).trim() === '';  // 0 is valid
    return String(val).trim() === '';
  };

  if (isEmpty(rawProduct) || isEmpty(rawPrice, true) ||
      isEmpty(rawName)     || isEmpty(rawAddress)) {
    return 'An empty required field was detected. Please ensure the following columns are filled:\n' +
           '- Product (Column K)\n' +
           '- Price (Column L)\n' +
           '- Name (Column C)\n' +
           '- Address (Column M)';
  }
  // --- End of empty field detection ---

  const name = rawName;
  const phone = at(CONFIG.PHONE_COL);
  const whatsapp = at(CONFIG.WHATSAPP_COL);
  const packageText = cleanPackageForReport_(cleanPackageText_(rawProduct));
  const priceText = formatReportPrice_(rawPrice);
  const address = rawAddress;

  return [
    formatReportDate_(new Date()),
    '',
    'Today\'s Delivery #:',
    '',
    '',
    'Package:',
    packageText,
    '',
    'Price:',
    priceText,
    '',
    'Name:',
    name,
    '',
    'Phone Number:',
    phone,
    '',
    'Whatsapp Number:',
    whatsapp,
    '',
    'Address:',
    address    
  ].join('\n');
}
/* ---------------- MENU ACTIONS ---------------- */

function initializeSheet() {
  const sheet = SpreadsheetApp.getActiveSheet();

  const headerRange = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, CONFIG.HEADERS.length);
  headerRange.setValues([CONFIG.HEADERS]);
  headerRange.setFontWeight('bold').setBackground('#4a86e8').setFontColor('#ffffff');
  sheet.setFrozenRows(1);
  // Freezes columns A-C so Customer Name stays pinned to the left edge while
  // scrolling right — the same instant "hover" behavior the header row already
  // has while scrolling down. Sheets can only freeze a contiguous block
  // starting from column A, so Raw Message and Filtered # ride along with it;
  // both are narrow/unobtrusive, so this keeps a compact, always-visible strip.
  sheet.setFrozenColumns(CONFIG.NAME_COL);

  // Column A holds the full raw message for reference, but stays narrow —
  // click into a cell (or widen it temporarily) whenever you need to read/copy it.
  sheet.setColumnWidth(CONFIG.RAW_COL, 100);

  const numRows = CONFIG.VALIDATION_LAST_ROW - CONFIG.FIRST_DATA_ROW + 1;

  // Phone / WhatsApp columns stay as plain text (protects leading + / 0).
  // clearDataValidations() strips whatever dropdown/checkbox rule ended up on
  // these cells (values are untouched — this only removes the validation rule,
  // never the phone numbers themselves) — fixes the "Phone/WhatsApp turned into
  // a dropdown" issue regardless of how that rule got there in the first place.
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.PHONE_COL, numRows, 2).clearDataValidations();
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.PHONE_COL, numRows, 2).setNumberFormat('@');

  // Price column: real numbers, currency-formatted
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.PRICE_COL, numRows, 1).setNumberFormat('"₦"#,##0');

  // Date column: now holds a real Date value (not just display text like "13th
  // July") so it can be filtered/sorted/compared against TODAY() reliably.
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.DATE_COL, numRows, 1).setNumberFormat(CONFIG.ORDER_DATE_FORMAT);

  // Action column: restricted dropdown
  const statusRule = SpreadsheetApp.newDataValidation()
    .requireValueInList(STATUS_LIST, true)
    .setAllowInvalid(false)
    .build();
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.ACTION_COL, numRows, 1).setDataValidation(statusRule);

  // Check-up column: native date/time picker + readable format
  const dateRule = SpreadsheetApp.newDataValidation()
    .requireDate()
    .setAllowInvalid(true) // allow typing a time alongside the picked date
    .build();
  const checkupRange = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.CHECKUP_COL, numRows, 1);
  checkupRange.setDataValidation(dateRule);
  checkupRange.setNumberFormat('yyyy-mm-dd hh:mm');

  // Duplicate / Invalid flag columns: real checkboxes, TRUE/FALSE only
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.DUPLICATE_COL, numRows, 1).insertCheckboxes();
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.INVALID_COL, numRows, 1).insertCheckboxes();

  // Filtered # column: live SUBTOTAL formulas, one per row
  const filteredFormulas = [];
  for (let r = CONFIG.FIRST_DATA_ROW; r <= CONFIG.VALIDATION_LAST_ROW; r++) {
    filteredFormulas.push([filteredSeqFormula(r)]);
  }
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.FILTERED_COL, numRows, 1).setFormulas(filteredFormulas);

  // Today Check-up column: live formula, rendered as a checkbox
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.TODAY_COL, numRows, 1).insertCheckboxes();
  const todayFormulas = [];
  for (let r = CONFIG.FIRST_DATA_ROW; r <= CONFIG.VALIDATION_LAST_ROW; r++) {
    todayFormulas.push([todayCheckupFormula(r)]);
  }
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.TODAY_COL, numRows, 1).setFormulas(todayFormulas);

  // Flagged (Dup/Invalid) column: live formula, rendered as a checkbox
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.FLAGGED_COL, numRows, 1).insertCheckboxes();
  const flaggedFormulas = [];
  for (let r = CONFIG.FIRST_DATA_ROW; r <= CONFIG.VALIDATION_LAST_ROW; r++) {
    flaggedFormulas.push([flaggedFormula(r)]);
  }
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.FLAGGED_COL, numRows, 1).setFormulas(flaggedFormulas);

  // Overdue Check-up column: live formula, rendered as a checkbox
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.OVERDUE_COL, numRows, 1).insertCheckboxes();
  const overdueFormulas = [];
  for (let r = CONFIG.FIRST_DATA_ROW; r <= CONFIG.VALIDATION_LAST_ROW; r++) {
    overdueFormulas.push([overdueFormula(r)]);
  }
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.OVERDUE_COL, numRows, 1).setFormulas(overdueFormulas);

  // Today's Order column: live formula, rendered as a checkbox — TRUE when the
  // order date (column I) is today, so "Show Today's Orders" has something to filter on.
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.TODAY_ORDER_COL, numRows, 1).insertCheckboxes();
  const todaysOrderFormulas = [];
  for (let r = CONFIG.FIRST_DATA_ROW; r <= CONFIG.VALIDATION_LAST_ROW; r++) {
    todaysOrderFormulas.push([todaysOrderFormula(r)]);
  }
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.TODAY_ORDER_COL, numRows, 1).setFormulas(todaysOrderFormulas);

  // Default filter across every column (status, date, category, etc.)
  const existingFilter = sheet.getFilter();
  if (existingFilter) existingFilter.remove();
  sheet.getRange(CONFIG.HEADER_ROW, 1, CONFIG.VALIDATION_LAST_ROW, CONFIG.HEADERS.length).createFilter();

  applyConditionalFormatting(sheet);
  buildSummaryReport();

  safeAlert('Sheet initialized. Paste WhatsApp messages into column A, starting row 2.');
}

function reprocessAll() {
  const sheet = SpreadsheetApp.getActiveSheet();
  let lastRow = sheet.getLastRow();
  for (let r = CONFIG.FIRST_DATA_ROW; r <= lastRow; r++) {
    const raw = sheet.getRange(r, CONFIG.RAW_COL).getValue();
    if (raw && String(raw).trim()) {
      const before = sheet.getLastRow();
      processRow(sheet, r, String(raw));
      const after = sheet.getLastRow();
      const inserted = after - before;
      if (inserted > 0) { r += inserted; lastRow += inserted; }
    }
  }
  safeAlert('Reprocessing complete.');
}

/* ---------------- CONDITIONAL FORMATTING ---------------- */
// Lets you scan the list by color instead of reading the Action column word for word.
// Order matters: the first matching rule wins, so Overdue (most urgent) is checked first.

function applyConditionalFormatting(sheet) {
  sheet = sheet || SpreadsheetApp.getActiveSheet(); // guards against running this directly from the editor's function dropdown
  const dataRange = sheet.getRange(CONFIG.FIRST_DATA_ROW, 1, CONFIG.VALIDATION_LAST_ROW - CONFIG.FIRST_DATA_ROW + 1, CONFIG.HEADERS.length);
  const overdueCol = columnLetter_(CONFIG.OVERDUE_COL);
  const actionCol = columnLetter_(CONFIG.ACTION_COL);
  const r0 = CONFIG.FIRST_DATA_ROW;

  const overdueRule = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=$' + overdueCol + r0 + '=TRUE')
    .setBackground('#f4cccc') // light red
    .setRanges([dataRange])
    .build();

  const deliveredRule = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=OR($' + actionCol + r0 + '="Delivered",$' + actionCol + r0 + '="Cash Remitted")')
    .setBackground('#d9ead3') // light green
    .setRanges([dataRange])
    .build();

  const closedOutRule = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=OR($' + actionCol + r0 + '="Cancelled",$' + actionCol + r0 + '="Deleted",$' + actionCol + r0 + '="Banned")')
    .setBackground('#efefef') // light grey
    .setRanges([dataRange])
    .build();

  sheet.setConditionalFormatRules([overdueRule, deliveredRule, closedOutRule]);
}

function columnLetter_(colIndex1Based) {
  let n = colIndex1Based;
  let letters = '';
  while (n > 0) {
    const rem = (n - 1) % 26;
    letters = String.fromCharCode(65 + rem) + letters;
    n = Math.floor((n - 1) / 26);
  }
  return letters;
}

/* ---------------- ORDER SUMMARY REPORT ---------------- */
// Written a couple of rows below the data range. All live formulas — they stay
// current automatically as Action, Duplicate, and Invalid values change; nothing
// to manually recalculate. Re-run "Refresh Summary Report" only if you want the
// chart rebuilt (e.g. after changing VALIDATION_LAST_ROW).

function buildSummaryReport() {
  const sheet = SpreadsheetApp.getActiveSheet();
  const d0 = CONFIG.FIRST_DATA_ROW;
  const d1 = CONFIG.VALIDATION_LAST_ROW;
  const startRow = CONFIG.VALIDATION_LAST_ROW + 2; // one blank row gap below the data

  const actionCol = columnLetter_(CONFIG.ACTION_COL);
  const dupCol = columnLetter_(CONFIG.DUPLICATE_COL);
  const invalidCol = columnLetter_(CONFIG.INVALID_COL);
  const snCol = columnLetter_(CONFIG.SN_COL);

  const rows = [
    ['ORDER SUMMARY', ''],
    ['Total Delivered', '=COUNTIF(' + actionCol + d0 + ':' + actionCol + d1 + ',"Delivered")'],
    ['Total Pending', '=COUNTIF(' + actionCol + d0 + ':' + actionCol + d1 + ',"Pending")'],
    ['Total Invalid Orders', '=COUNTIF(' + invalidCol + d0 + ':' + invalidCol + d1 + ',TRUE)'],
    ['Total Duplicate Orders', '=COUNTIF(' + dupCol + d0 + ':' + dupCol + d1 + ',TRUE)'],
    ['Total Cancelled Orders', '=COUNTIF(' + actionCol + d0 + ':' + actionCol + d1 + ',"Cancelled")'],
    ['Total Orders', '=COUNTA(' + snCol + d0 + ':' + snCol + d1 + ')'],
    ['% Delivered', '=IFERROR(COUNTIF(' + actionCol + d0 + ':' + actionCol + d1 + ',"Delivered")/COUNTA(' + snCol + d0 + ':' + snCol + d1 + '),0)']
  ];

  const summaryRange = sheet.getRange(startRow, 1, rows.length, 2);
  summaryRange.setValues(rows);
  sheet.getRange(startRow, 1, 1, 2).setFontWeight('bold').setBackground('#4a86e8').setFontColor('#ffffff');
  sheet.getRange(startRow + 1, 1, rows.length - 1, 1).setFontWeight('bold');
  sheet.getRange(startRow + rows.length - 1, 2).setNumberFormat('0%'); // % Delivered row

  removeChartByTitle_(sheet, 'Order Summary');

  // Chart the 5 category counts (Delivered, Pending, Invalid, Duplicate, Cancelled) —
  // Total Orders and % Delivered are left out of the chart since they're on a
  // different scale and read better as plain numbers.
  const chartRange = sheet.getRange(startRow + 1, 1, 5, 2);
  const chart = sheet.newChart()
    .asColumnChart()
    .addRange(chartRange)
    .setPosition(startRow, 4, 0, 0)
    .setOption('title', 'Order Summary')
    .setOption('legend', { position: 'none' })
    .setOption('colors', ['#4a86e8'])
    .build();
  sheet.insertChart(chart);
}

function removeChartByTitle_(sheet, title) {
  const charts = sheet.getCharts();
  charts.forEach(function(c) {
    if (c.getOptions().get('title') === title) sheet.removeChart(c);
  });
}

/* ---------------- ROW CAP WARNING ---------------- */

function checkRowCapWarning(sheet) {
  sheet = sheet || SpreadsheetApp.getActiveSheet(); // guards against running this directly from the editor's function dropdown
  const lastRow = sheet.getLastRow();
  const remaining = CONFIG.VALIDATION_LAST_ROW - lastRow;
  if (remaining >= 0 && remaining <= CONFIG.ROW_CAP_WARNING_THRESHOLD) {
    SpreadsheetApp.getActiveSpreadsheet().toast(
      'Only ' + remaining + ' rows left before the ' + CONFIG.VALIDATION_LAST_ROW + '-row setup limit. Consider starting a new sheet soon.',
      'Row limit warning', 6
    );
  }
}

/* ---------------- MOBILE QUICK SCHEDULE ---------------- */
// Sets the check-up date/time and Action="Scheduled" on the currently
// selected row in one tap — avoids needing the native date picker at all,
// so it works the same on desktop and the mobile app.

function scheduleToday()    { scheduleQuick(0); }
function scheduleTomorrow() { scheduleQuick(1); }
function scheduleIn2Days()  { scheduleQuick(2); }
function scheduleInWeek()   { scheduleQuick(7); }

function scheduleQuick(daysFromNow) {
  const sheet = SpreadsheetApp.getActiveSheet();
  const activeRange = sheet.getActiveRange();
  if (!activeRange) {
    safeAlert('Tap a cell in the order\'s row first, then use this menu.');
    return;
  }
  const row = activeRange.getRow();
  if (row < CONFIG.FIRST_DATA_ROW) {
    safeAlert('Select a data row first (row 2 or below).');
    return;
  }

  const target = new Date();
  target.setDate(target.getDate() + daysFromNow);
  target.setHours(CONFIG.QUICK_SCHEDULE_HOUR, 0, 0, 0);

  sheet.getRange(row, CONFIG.CHECKUP_COL).setValue(target);
  sheet.getRange(row, CONFIG.ACTION_COL).setValue(SCHEDULING_STATUS);
  SpreadsheetApp.getActiveSpreadsheet().toast(
    'Check-up set for ' + target.toLocaleDateString() + ' ' + target.toLocaleTimeString(),
    'Scheduled', 4
  );
}

/* ---------------- SAVED FILTER VIEWS ---------------- */
// Shared engine behind all three "predefined filter" shortcuts. Filter Views —
// not a Basic Filter — are the thing the mobile Sheets app can switch to with
// one tap (Data/funnel icon > Filter views), which is why all three use this.
// REQUIRES a one-time setup: in the Apps Script editor, click "+ Services" in
// the left sidebar, choose "Google Sheets API", click Add. Once per project.

function todayCheckupFormula(row) {
  // TRUE when the check-up date (column H) falls on today's date, FALSE otherwise —
  // including when H is empty or a manually-typed value that isn't a real date.
  return '=IFERROR(IF($H' + row + '="",FALSE,INT($H' + row + ')=TODAY()),FALSE)';
}

function flaggedFormula(row) {
  // TRUE when either the Duplicate Order? or Invalid Number? checkbox is TRUE.
  return '=IFERROR(OR($P' + row + ',$Q' + row + '),FALSE)';
}

function overdueFormula(row) {
  // TRUE when the check-up date has passed and Action is still Scheduled or Pending.
  return '=IFERROR(AND($H' + row + '<>"",INT($H' + row + ')<TODAY(),OR($G' + row + '="Scheduled",$G' + row + '="Pending")),FALSE)';
}

function todaysOrderFormula(row) {
  // TRUE when the order date (column I) falls on today's date, FALSE otherwise —
  // including when I is empty or holds text that never resolved into a real date.
  return '=IFERROR(IF($I' + row + '="",FALSE,INT($I' + row + ')=TODAY()),FALSE)';
}

function showTodayCheckups()   { createFilterViewForColumn_('checkup_for_today', CONFIG.TODAY_COL); }
function clearTodayFilter()    { removeFilterViewByTitle_('checkup_for_today'); toastFilterCleared_('checkup_for_today'); }

function showOverdueCheckups() { createFilterViewForColumn_('overdue_checkups', CONFIG.OVERDUE_COL); }
function clearOverdueFilter()  { removeFilterViewByTitle_('overdue_checkups'); toastFilterCleared_('overdue_checkups'); }

function showFlaggedOrders()   { createFilterViewForColumn_('flagged_orders', CONFIG.FLAGGED_COL); }
function clearFlaggedFilter()  { removeFilterViewByTitle_('flagged_orders'); toastFilterCleared_('flagged_orders'); }

function showTodaysOrders()        { createFilterViewForColumn_('todays_orders', CONFIG.TODAY_ORDER_COL); }
function clearTodaysOrdersFilter() { removeFilterViewByTitle_('todays_orders'); toastFilterCleared_('todays_orders'); }

// "Show Orders For a Date" is deliberately NOT a saved Filter View like the
// others above — a view per possible day doesn't scale. Instead it applies a
// plain column filter (the built-in Basic Filter, no Advanced Sheets Service
// needed) directly on the Date column, so it works for literally any date.
function showOrdersForDate() {
  const sheet = SpreadsheetApp.getActiveSheet();
  let ui;
  try {
    ui = SpreadsheetApp.getUi();
  } catch (e) {
    // Unlike the other functions above, this one genuinely needs a live dialog
    // to ask "which date?" — there's no toast fallback possible for that, so
    // just explain clearly instead of surfacing the raw exception.
    SpreadsheetApp.getActiveSpreadsheet().toast(
      'Run this from the sheet itself: Order Tools > Show Orders For a Date… ' +
      "(it needs to pop up a dialog, so it can't run from the script editor's Run button).",
      'Needs the sheet open', 8
    );
    return;
  }
  const response = ui.prompt(
    'Show Orders For a Date',
    'Enter the order date, e.g. 13/07/2026 or 13th July:',
    ui.ButtonSet.OK_CANCEL
  );
  if (response.getSelectedButton() !== ui.Button.OK) return;

  const targetDate = parseOrderDate(response.getResponseText());
  if (!targetDate) {
    ui.alert('Could not understand that date. Try a format like 13/07/2026 or 13th July.');
    return;
  }

  let filter = sheet.getFilter();
  if (!filter) {
    filter = sheet.getRange(CONFIG.HEADER_ROW, 1, CONFIG.VALIDATION_LAST_ROW, CONFIG.HEADERS.length).createFilter();
  }
  const criteria = SpreadsheetApp.newFilterCriteria().whenDateEqualTo(targetDate).build();
  filter.setColumnFilterCriteria(CONFIG.DATE_COL, criteria);

  SpreadsheetApp.getActiveSpreadsheet().toast(
    'Showing orders dated ' + Utilities.formatDate(targetDate, Session.getScriptTimeZone(), 'd MMM yyyy') + '.',
    'Date filter applied', 5
  );
}

function clearDateFilter() {
  const sheet = SpreadsheetApp.getActiveSheet();
  const filter = sheet.getFilter();
  if (filter) filter.removeColumnFilterCriteria(CONFIG.DATE_COL);
  SpreadsheetApp.getActiveSpreadsheet().toast('Date filter cleared — showing all orders.', 'Cleared', 4);
}

function toastFilterCleared_(title) {
  SpreadsheetApp.getActiveSpreadsheet().toast('"' + title + '" filter view removed.', 'Cleared', 4);
}

// Creates (or refreshes) a saved Filter View named `title`, showing only rows
// where the given boolean column is TRUE.
function createFilterViewForColumn_(title, columnIndex1Based) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getActiveSheet();
  const spreadsheetId = ss.getId();
  const sheetId = sheet.getSheetId();

  removeFilterViewByTitle_(title);

  const criteria = {};
  criteria[String(columnIndex1Based - 1)] = { hiddenValues: ['FALSE'] };

  const request = {
    requests: [{
      addFilterView: {
        filter: {
          title: title,
          range: {
            sheetId: sheetId,
            startRowIndex: CONFIG.HEADER_ROW - 1,
            endRowIndex: CONFIG.VALIDATION_LAST_ROW,
            startColumnIndex: 0,
            endColumnIndex: CONFIG.HEADERS.length
          },
          criteria: criteria
        }
      }
    }]
  };

  Sheets.Spreadsheets.batchUpdate(request, spreadsheetId);
  safeAlert(
    '"' + title + '" filter view is ready.\n\n' +
    'Mobile: tap the Data/funnel icon > Filter views > ' + title + '.\n' +
    'Desktop: Data > Filter views > ' + title + '.\n\n' +
    'Re-run this menu item any morning to refresh which rows currently match.'
  );
}

function removeFilterViewByTitle_(title) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const spreadsheetId = ss.getId();
  const sheetId = ss.getActiveSheet().getSheetId();
  const meta = Sheets.Spreadsheets.get(spreadsheetId, { fields: 'sheets(properties.sheetId,filterViews)' });
  const sheetMeta = meta.sheets.filter(function(s) { return s.properties.sheetId === sheetId; })[0];
  if (!sheetMeta || !sheetMeta.filterViews) return;
  const existing = sheetMeta.filterViews.filter(function(fv) { return fv.title === title; })[0];
  if (existing) {
    Sheets.Spreadsheets.batchUpdate({ requests: [{ deleteFilterView: { filterId: existing.filterViewId } }] }, spreadsheetId);
  }
}

/* ---------------- CHECK-UP REMINDERS ---------------- */
// Background safety net: a toast fires if a still-unresolved (Scheduled/Pending)
// check-up is within REMINDER_WINDOW_MINUTES, but ONLY while this spreadsheet is
// open somewhere — there is no way for Apps Script to push a notification to a
// closed tab or a phone that isn't in the app. This is a catch for "I forgot to
// filter today", not a replacement for checking checkup_for_today each morning.

function enableCheckupReminders() {
  const sheet = SpreadsheetApp.getActiveSheet();
  PropertiesService.getScriptProperties().setProperty('REMINDER_SHEET_NAME', sheet.getName());
  removeCheckupReminderTriggers_();
  ScriptApp.newTrigger('checkForUpcomingCheckups').timeBased().everyMinutes(5).create();
  safeAlert(
    'Check-up reminders are ON for "' + sheet.getName() + '".\n\n' +
    "You'll get a toast about " + CONFIG.REMINDER_WINDOW_MINUTES + ' minutes before each scheduled ' +
    'check-up — but only while this spreadsheet is open in your browser or the Sheets app.'
  );
}

function disableCheckupReminders() {
  removeCheckupReminderTriggers_();
  safeAlert('Check-up reminders are OFF.');
}

function removeCheckupReminderTriggers_() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'checkForUpcomingCheckups') ScriptApp.deleteTrigger(t);
  });
}

// Runs every 5 minutes via an installable trigger (see enableCheckupReminders).
function checkForUpcomingCheckups() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheetName = PropertiesService.getScriptProperties().getProperty('REMINDER_SHEET_NAME');
  const sheet = sheetName ? ss.getSheetByName(sheetName) : ss.getActiveSheet();
  if (!sheet) return;

  const lastRow = sheet.getLastRow();
  if (lastRow < CONFIG.FIRST_DATA_ROW) return;
  const numRows = lastRow - CONFIG.FIRST_DATA_ROW + 1;

  const checkups = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.CHECKUP_COL, numRows, 1).getValues();
  const names = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.NAME_COL, numRows, 1).getValues();
  const actions = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.ACTION_COL, numRows, 1).getValues();

  const now = new Date();
  const dueSoon = [];

  for (let i = 0; i < numRows; i++) {
    const checkupVal = checkups[i][0];
    if (!(checkupVal instanceof Date)) continue;
    if (UNRESOLVED_STATUSES.indexOf(actions[i][0]) === -1) continue; // only Scheduled/Pending need a nudge

    const minutesUntil = (checkupVal.getTime() - now.getTime()) / 60000;
    if (minutesUntil >= 0 && minutesUntil <= CONFIG.REMINDER_WINDOW_MINUTES) {
      dueSoon.push((names[i][0] || 'Unnamed') + ' — ' + checkupVal.toLocaleTimeString());
    }
  }

  if (dueSoon.length > 0) {
    ss.toast('Check-up due soon:\n' + dueSoon.join('\n'), 'Upcoming check-up', 8);
  }
}

/* ---------------- CORE PROCESSING ---------------- */

function processRow(sheet, row, rawText) {
  const blocks = splitIntoBlocks(rawText);
  if (blocks.length === 0) return;

  // Performance: read the S/N and duplicate-check columns ONCE for this whole
  // paste, not once per order inside it. A 7-order paste used to re-scan the
  // entire sheet 7 separate times (once per order); it now scans once, then
  // keeps an in-memory copy updated as each new row is written, so later
  // orders in the same paste still see the ones written just before them.
  const scanData = loadPasteScanData_(sheet);

  writeRecord(sheet, row, blocks[0], scanData);

  for (let i = 1; i < blocks.length; i++) {
    const insertedAfterRow = row + i - 1;
    sheet.insertRowAfter(insertedAfterRow);

    // BUG FIX (rev 6): inserting a row physically pushes every row below it
    // down by one. scanData.rows was captured — or added to — before this
    // insert, so any entry pointing at a row below insertedAfterRow is now
    // stale by one. Left uncorrected, a genuine duplicate match against an
    // earlier/pre-existing row could get its TRUE flag written to the WRONG
    // row (often the freshly-inserted blank one), which is exactly why a real
    // match could look like it "wasn't detected." This keeps every row
    // reference in scanData in sync with where that row actually is now.
    scanData.rows.forEach(function(r) {
      if (r.row > insertedAfterRow) r.row++;
    });

    applyRowValidation(sheet, row + i); // new rows need their own dropdown/date/checkbox/formula validation
    writeRecord(sheet, row + i, blocks[i], scanData);
  }

  checkRowCapWarning(sheet);
}

function applyRowValidation(sheet, row) {
  const statusRule = SpreadsheetApp.newDataValidation()
    .requireValueInList(STATUS_LIST, true)
    .setAllowInvalid(false)
    .build();
  sheet.getRange(row, CONFIG.ACTION_COL).setDataValidation(statusRule);

  const dateRule = SpreadsheetApp.newDataValidation().requireDate().setAllowInvalid(true).build();
  const cell = sheet.getRange(row, CONFIG.CHECKUP_COL);
  cell.setDataValidation(dateRule);
  cell.setNumberFormat('yyyy-mm-dd hh:mm');

  sheet.getRange(row, CONFIG.DUPLICATE_COL).insertCheckboxes();
  sheet.getRange(row, CONFIG.INVALID_COL).insertCheckboxes();

  sheet.getRange(row, CONFIG.PHONE_COL, 1, 2).clearDataValidations();
  sheet.getRange(row, CONFIG.PHONE_COL, 1, 2).setNumberFormat('@');
  sheet.getRange(row, CONFIG.PRICE_COL).setNumberFormat('"₦"#,##0');
  sheet.getRange(row, CONFIG.DATE_COL).setNumberFormat(CONFIG.ORDER_DATE_FORMAT);
  sheet.getRange(row, CONFIG.FILTERED_COL).setFormula(filteredSeqFormula(row));
  sheet.getRange(row, CONFIG.TODAY_COL).insertCheckboxes();
  sheet.getRange(row, CONFIG.TODAY_COL).setFormula(todayCheckupFormula(row));
  sheet.getRange(row, CONFIG.FLAGGED_COL).insertCheckboxes();
  sheet.getRange(row, CONFIG.FLAGGED_COL).setFormula(flaggedFormula(row));
  sheet.getRange(row, CONFIG.OVERDUE_COL).insertCheckboxes();
  sheet.getRange(row, CONFIG.OVERDUE_COL).setFormula(overdueFormula(row));
  sheet.getRange(row, CONFIG.TODAY_ORDER_COL).insertCheckboxes();
  sheet.getRange(row, CONFIG.TODAY_ORDER_COL).setFormula(todaysOrderFormula(row));
}

function filteredSeqFormula(row) {
  // Counts only visible (non-filtered-out) S/N values from row 2 down to this row.
  // References the S/N column dynamically (not a hardcoded letter) so this stays
  // correct even if S/N's physical column position ever changes.
  const snLetter = columnLetter_(CONFIG.SN_COL);
  return '=IF($' + snLetter + row + '="","",SUBTOTAL(103,$' + snLetter + '$2:$' + snLetter + row + '))';
}

function writeRecord(sheet, row, blockText, scanData) {
  const fields = parseBlock(blockText);
  const sn = scanData ? (++scanData.maxSn) : nextSerialNumber(sheet, row);
  const phone = normalizePhone(fields.phone);
  const whatsapp = normalizePhone(fields.whatsapp);
  const priceInfo = resolvePrice(fields.product, fields.price);
  const priceNumber = extractPriceNumber(priceInfo.priceText);
  const productText = priceInfo.priceText ? `${priceInfo.product} — ₦${priceInfo.priceText}` : priceInfo.product;

  sheet.getRange(row, CONFIG.SN_COL).setValue(sn);

  // PERF (rev 6): Name, Phone, WhatsApp, Note are contiguous columns (C:F) —
  // one write instead of four. NOTE: this relies on CONFIG.NAME_COL,
  // PHONE_COL, WHATSAPP_COL, NOTE_COL staying contiguous and in this exact
  // order; if that layout is ever changed, this batched write and the array
  // below both need to be updated together.
  sheet.getRange(row, CONFIG.NAME_COL, 1, 4).setValues([[fields.name, phone, whatsapp, '']]);
  // Action (col G) intentionally left untouched if already set by you; default blank on new rows.

  // Date column always ends up a real Date — if the message didn't specify one
  // (or it couldn't be confidently parsed), fall back to today's actual date
  // (the day the order was processed) rather than leaving text or a blank, so
  // every row stays uniformly filterable/sortable with no exceptions.
  const orderDate = parseOrderDate(fields.date) || stripTime_(new Date());

  // PERF (rev 6): Date, Category, Product, Price, Address, Delivery, Question
  // are contiguous columns (I:O) — one write instead of seven. Same NOTE as
  // above: relies on CONFIG.DATE_COL...QUESTION_COL staying contiguous and in
  // this exact order.
  sheet.getRange(row, CONFIG.DATE_COL, 1, 7).setValues([[
    orderDate, fields.category, productText, priceNumber === '' ? '' : priceNumber,
    fields.address, fields.delivery, fields.question
  ]]);
  sheet.getRange(row, CONFIG.DATE_COL).setNumberFormat(CONFIG.ORDER_DATE_FORMAT);
  sheet.getRange(row, CONFIG.PRICE_COL).setNumberFormat('"₦"#,##0');

  sheet.getRange(row, CONFIG.PHONE_COL, 1, 2).clearDataValidations();
  sheet.getRange(row, CONFIG.PHONE_COL, 1, 2).setNumberFormat('@');
  sheet.getRange(row, CONFIG.FILTERED_COL).setFormula(filteredSeqFormula(row)); // idempotent, keeps rows beyond the pre-init range covered
  sheet.getRange(row, CONFIG.TODAY_COL).setFormula(todayCheckupFormula(row));
  sheet.getRange(row, CONFIG.FLAGGED_COL).setFormula(flaggedFormula(row));
  sheet.getRange(row, CONFIG.OVERDUE_COL).setFormula(overdueFormula(row));
  sheet.getRange(row, CONFIG.TODAY_ORDER_COL).setFormula(todaysOrderFormula(row));

  // Flags
  const isInvalid = !isValidNigerianNumber(phone) || (!!whatsapp && !isValidNigerianNumber(whatsapp));
  const isDuplicate = scanData
    ? findDuplicateInScanData_(sheet, scanData, row, phone, fields.name, productText, fields.address)
    : checkAndFlagDuplicates(sheet, row, phone, fields.name, productText, fields.address);
  sheet.getRange(row, CONFIG.INVALID_COL).setValue(isInvalid);
  sheet.getRange(row, CONFIG.DUPLICATE_COL).setValue(isDuplicate);

  // So later blocks in the SAME paste see this row too, without re-reading the sheet.
  if (scanData) {
    scanData.rows.push({
      row: row,
      phone: normalizeText(phone),
      name: normalizeText(fields.name),
      product: normalizeText(productText),
      address: normalizeText(fields.address)
    });
  }

  // Keep the raw text for reference, but keep the cell/row visually compact.
  // Text is clipped (not wrapped) so pasting a long message never stretches the row.
  const rawCell = sheet.getRange(row, CONFIG.RAW_COL);
  rawCell.setValue(blockText.trim());
  rawCell.setWrapStrategy(SpreadsheetApp.WrapStrategy.CLIP);
  sheet.setRowHeight(row, CONFIG.DEFAULT_ROW_HEIGHT);
}

// Reads the S/N column and the three duplicate-check columns (Phone, Product,
// Address) ONCE, for the whole paste — used by processRow so a multi-order
// paste doesn't re-scan the entire sheet separately for every single order.
function loadPasteScanData_(sheet) {
  // PERF (rev 6): cap the scan at VALIDATION_LAST_ROW, not sheet.getLastRow().
  // Real order rows never legitimately go past VALIDATION_LAST_ROW — but
  // getLastRow() also picks up the ORDER SUMMARY block + chart written further
  // down the sheet (see buildSummaryReport()), so every single paste was
  // scanning that block too, for zero benefit, and it only grows over time.
  const lastRow = Math.min(sheet.getLastRow(), CONFIG.VALIDATION_LAST_ROW);
  if (lastRow < CONFIG.FIRST_DATA_ROW) return { maxSn: 0, rows: [] };

  const numRows = lastRow - CONFIG.FIRST_DATA_ROW + 1;
  const snValues = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.SN_COL, numRows, 1).getValues();
  const phones = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.PHONE_COL, numRows, 1).getValues();
  const whatsapps = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.WHATSAPP_COL, numRows, 1).getValues();
  const names = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.NAME_COL, numRows, 1).getValues();
  const products = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.PRODUCT_COL, numRows, 1).getValues();
  const addresses = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.ADDRESS_COL, numRows, 1).getValues();

  let maxSn = 0;
  const rows = [];
  for (let i = 0; i < numRows; i++) {
    const sn = snValues[i][0];
    if (typeof sn === 'number' && sn > maxSn) maxSn = sn;
    rows.push({
      row: CONFIG.FIRST_DATA_ROW + i,
      phone: normalizeText(phones[i][0]),
      whatsapp: normalizeText(whatsapps[i][0]),
      name: normalizeText(names[i][0]),
      product: normalizeText(products[i][0]),
      address: normalizeText(addresses[i][0])
    });
  }
  return { maxSn: maxSn, rows: rows };
}

// In-memory equivalent of checkAndFlagDuplicates() — same matching rule, but
// checks against the scanData snapshot instead of re-reading the sheet. Still
// writes TRUE to a matching historical row when found — that part can't be
// avoided, since a newly-duplicated old row genuinely needs to be flagged on
// the sheet itself.
function findDuplicateInScanData_(sheet, scanData, row, phone, name, product, address) {
  const targetPhone = normalizeText(phone);
  if (!targetPhone) return false; // don't flag blank-phone rows against each other

  const targetName = normalizeText(name);
  const targetProduct = normalizeText(product);
  const targetAddress = normalizeText(address);

  let duplicateFound = false;
  for (let i = 0; i < scanData.rows.length; i++) {
    const r = scanData.rows[i];
    if (r.row === row) continue;
    if (r.phone !== targetPhone) continue; // phone is always the anchor — cheap check first

    // PRIMARY (rev 8): phone + name match.
    const nameMatches = !!targetName && r.name === targetName;
    // SECONDARY (unchanged rule, now better-normalized): phone + product + address match.
    const productAddressMatches = r.product === targetProduct && r.address === targetAddress;

    if (nameMatches || productAddressMatches) {
      duplicateFound = true;
      sheet.getRange(r.row, CONFIG.DUPLICATE_COL).setValue(true);
    }
  }
  return duplicateFound;
}

function nextSerialNumber(sheet, currentRow) {
  const lastRow = sheet.getLastRow();
  if (lastRow < CONFIG.FIRST_DATA_ROW) return 1;
  const numRows = lastRow - CONFIG.FIRST_DATA_ROW + 1;
  const values = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.SN_COL, numRows, 1).getValues();
  let max = 0;
  values.forEach((r, i) => {
    if (CONFIG.FIRST_DATA_ROW + i === currentRow) return;
    if (typeof r[0] === 'number' && r[0] > max) max = r[0];
  });
  return max + 1;
}

/* ---------------- FLAG LOGIC ---------------- */

function isValidNigerianNumber(phone) {
  return !!phone && NIGERIA_MOBILE_REGEX.test(phone);
}

function normalizeText(s) {
  return (s || '').toString().trim().toLowerCase()
    .replace(/[.,;:!?'"()]/g, '')  // punctuation shouldn't break a match (rev 8)
    .replace(/\s+/g, ' ')
    .trim();
}

// Flags the current row TRUE if phone matches another row AND EITHER the
// name also matches, or product+address also match — the sheet-reading
// fallback used only when scanData isn't available.
function checkAndFlagDuplicates(sheet, row, phone, name, product, address) {
  sheet = sheet || SpreadsheetApp.getActiveSheet(); // guards against running this directly from the editor's function dropdown
  const lastRow = sheet.getLastRow();
  if (lastRow < CONFIG.FIRST_DATA_ROW) return false;
  const numRows = lastRow - CONFIG.FIRST_DATA_ROW + 1;

  const targetPhone = normalizeText(phone);
  if (!targetPhone) return false; // don't flag blank-phone rows against each other

  const targetName = normalizeText(name);
  const targetProduct = normalizeText(product);
  const targetAddress = normalizeText(address);

  const phones = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.PHONE_COL, numRows, 1).getValues();
  const names = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.NAME_COL, numRows, 1).getValues();
  const products = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.PRODUCT_COL, numRows, 1).getValues();
  const addresses = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.ADDRESS_COL, numRows, 1).getValues();

  let duplicateFound = false;
  for (let i = 0; i < numRows; i++) {
    const r = CONFIG.FIRST_DATA_ROW + i;
    if (r === row) continue;
    if (normalizeText(phones[i][0]) !== targetPhone) continue; // phone is always the anchor

    const nameMatches = !!targetName && normalizeText(names[i][0]) === targetName;
    const productAddressMatches = normalizeText(products[i][0]) === targetProduct &&
                                    normalizeText(addresses[i][0]) === targetAddress;

    if (nameMatches || productAddressMatches) {
      duplicateFound = true;
      sheet.getRange(r, CONFIG.DUPLICATE_COL).setValue(true);
    }
  }
  return duplicateFound;
}

/* ---------------- PARSING ---------------- */

// Matches a WhatsApp chat-export message header at the start of a line, e.g.
// "[7/13, 1:14 PM] Sales Manager Nabeu: " — present when a whole chat log is
// copy-pasted straight out of WhatsApp instead of a single message at a time.
const WHATSAPP_EXPORT_PREFIX_REGEX = /^\[\d{1,2}\/\d{1,2}(?:\/\d{2,4})?,\s*\d{1,2}:\d{2}(?::\d{2})?\s*[AaPp]\.?[Mm]\.?\]\s*[^:\n]+:\s?/;
const WHATSAPP_EXPORT_SPLIT_REGEX = /\n(?=\[\d{1,2}\/\d{1,2}(?:\/\d{2,4})?,\s*\d{1,2}:\d{2}(?::\d{2})?\s*[AaPp]\.?[Mm]\.?\]\s*[^:\n]+:)/;

function splitIntoBlocks(rawText) {
  rawText = rawText || ''; // guards against running this directly from the editor's function dropdown
  let text = rawText.replace(/\r\n/g, '\n').trim();

  let chunks;
  if (WHATSAPP_EXPORT_PREFIX_REGEX.test(text)) {
    // Whole chat log pasted at once: split right before each "[date, time] Sender:"
    // header, then strip that header off the front of each resulting piece so it
    // doesn't get parsed as data.
    chunks = text.split(WHATSAPP_EXPORT_SPLIT_REGEX)
      .map(c => c.replace(WHATSAPP_EXPORT_PREFIX_REGEX, ''));
  } else {
    // Primary split: explicit "####" separator lines
    chunks = text.split(/\n\s*#{3,}\s*\n/);
  }

  // Secondary split: chunk containing more than one "Select Your Package"
  // means multiple messages were pasted back-to-back with no separator.
  const finalChunks = [];
  chunks.forEach(chunk => {
    const packageMatches = chunk.match(/select\s*your\s*package/gi);
    if (packageMatches && packageMatches.length > 1) {
      const sub = chunk.split(/\n(?=\*?\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\*?\s*\n)/);
      finalChunks.push(...sub);
    } else {
      finalChunks.push(chunk);
    }
  });

  return finalChunks.map(c => c.trim()).filter(c => c.length > 0);
}

function normalizeLabel(str) {
  return (str || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
}

function matchLabel(normalized) {
  for (const l of LABELS) {
    if (l.test(normalized)) return l.key;
  }
  return null;
}

function parseBlock(blockText) {
  blockText = blockText || ''; // guards against running this directly from the editor's function dropdown
  const fields = {
    date: '', category: '', product: '', price: '', name: '',
    phone: '', whatsapp: '', address: '', delivery: '', question: ''
  };

  const lines = blockText.split('\n')
    .map(l => l.replace(/\t/g, ' ').trim())
    .filter(l => l.length > 0 && !/^#{2,}$/.test(l));

  let currentKey = null;
  let sawFirstLabel = false;

  for (const line of lines) {
    // Trailing meta like "From Michael Follow-up" — agent/source tracking isn't
    // kept, so this is just discarded (and, importantly, not left to bleed into
    // whichever field was last active, e.g. Question).
    if (/^from\s+.+/i.test(line) && sawFirstLabel) {
      currentKey = null;
      continue;
    }

    const colonIdx = line.indexOf(':');
    const labelPart = colonIdx > -1 ? line.substring(0, colonIdx) : line;
    const restPart = colonIdx > -1 ? line.substring(colonIdx + 1).trim() : '';
    const normalized = normalizeLabel(labelPart);
    const looksLikeLabel = labelPart.length <= MAX_LABEL_CHARS &&
      labelPart.trim().split(/\s+/).length <= MAX_LABEL_WORDS &&
      !BOLD_HEADING_LINE_REGEX.test(line);
    const matchedKey = looksLikeLabel ? matchLabel(normalized) : null;

    if (matchedKey) {
      sawFirstLabel = true;
      currentKey = matchedKey;
      if (restPart) {
        fields[matchedKey] = fields[matchedKey] ? fields[matchedKey] + ' ' + restPart : restPart;
      }
      continue;
    }

    if (!sawFirstLabel) {
      const stripped = line.replace(/^\*+|\*+$/g, '').trim();
      if (!fields.date && DATE_REGEX.test(stripped)) {
        fields.date = stripped;
      } else if (!fields.category) {
        fields.category = stripped;
      } else {
        fields.category += ' | ' + stripped;
      }
      continue;
    }

    if (currentKey) {
      fields[currentKey] = fields[currentKey] ? fields[currentKey] + ' ' + line : line;
    }
  }

  fields.product = fields.product.trim();
  return fields;
}

// Month abbreviations used to parse date text like "13th July" into a real Date.
const MONTH_ABBR = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'];
// If a year-less date (e.g. "13th July") would land more than this many days in
// the future, assume it actually meant last year instead (handles reprocessing
// an old message like "31st December" after the new year has turned over).
const ORDER_DATE_FUTURE_SLACK_DAYS = 3;

function isValidDate_(d) {
  return d instanceof Date && !isNaN(d.getTime());
}

function stripTime_(d) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

function resolveOrderYear_(day, monthIndex, now) {
  const year = now.getFullYear();
  let candidate = new Date(year, monthIndex, day);
  if (!isValidDate_(candidate)) return null;
  const msPerDay = 24 * 60 * 60 * 1000;
  if ((candidate.getTime() - stripTime_(now).getTime()) / msPerDay > ORDER_DATE_FUTURE_SLACK_DAYS) {
    candidate = new Date(year - 1, monthIndex, day);
  }
  return candidate;
}

// Parses order-date text such as "13th July", "7 July", "13/07/2026", "13-07",
// or "2026-07-13" into a real Date (day-precision). Used both to convert the
// parsed message date into something the Date column can filter/sort on, and
// to parse whatever a person types into "Show Orders For a Date". Returns
// null if the text can't be confidently parsed as a date — callers should
// fall back to storing the raw text rather than lose the information.
function parseOrderDate(dateText, referenceDate) {
  if (!dateText) return null;
  const text = String(dateText).trim();
  const now = referenceDate || new Date();

  // "2026-07-13" (ISO year-month-day)
  let m = text.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (m) {
    const d = new Date(parseInt(m[1], 10), parseInt(m[2], 10) - 1, parseInt(m[3], 10));
    return isValidDate_(d) ? d : null;
  }

  // "13th July", "7 July", "13th July." (day + month name, no year)
  m = text.match(/^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\.?$/);
  if (m) {
    const day = parseInt(m[1], 10);
    const monthIndex = MONTH_ABBR.indexOf(m[2].toLowerCase().slice(0, 3));
    if (monthIndex === -1) return null;
    return resolveOrderYear_(day, monthIndex, now);
  }

  // "13/07/2026", "13/7/26", "13-07" (day/month[/year], day-first)
  m = text.match(/^(\d{1,2})[\/\-](\d{1,2})(?:[\/\-](\d{2,4}))?$/);
  if (m) {
    const day = parseInt(m[1], 10);
    const monthIndex = parseInt(m[2], 10) - 1;
    if (monthIndex < 0 || monthIndex > 11) return null;
    if (m[3]) {
      let year = parseInt(m[3], 10);
      if (year < 100) year += 2000;
      const d = new Date(year, monthIndex, day);
      return isValidDate_(d) ? d : null;
    }
    return resolveOrderYear_(day, monthIndex, now);
  }

  return null;
}

function normalizePhone(raw) {
  if (!raw) return '';
  let digits = raw.replace(/[^\d+]/g, '');
  if (digits.startsWith('+234')) return digits;
  if (digits.startsWith('234')) return '+' + digits;
  if (digits.startsWith('0')) return '+234' + digits.substring(1);
  return digits;
}

// Extracts the first numeric amount from raw price text (handles "5000",
// "₦5,000", "N5000", "5000 (negotiable)"). Returns a Number, or '' if none found.
function extractPriceNumber(raw) {
  if (!raw) return '';
  const cleaned = String(raw).replace(/,/g, '');
  const match = cleaned.match(/\d+(\.\d+)?/);
  return match ? parseFloat(match[0]) : '';
}

// Most order messages never have a separate "Price:" line — the amount just
// shows up as the trailing number in/after the product text, in whatever style
// the person typed it: "...Free Collagen Hand Cream = #28,500", or a bare
// number on its own line right after "Product: ...", like "29,500,". Rather
// than hard-coding each style, this looks at the END of the combined product
// text for a number that LOOKS like a price — either comma-grouped (29,500) or
// at least 3 plain digits (500+) — optionally preceded by =, -, #, ₦, or N,
// and optionally followed by a trailing comma. The 3-digit minimum and the
// requirement that it be preceded by whitespace/one of those separator
// characters (not glued directly onto a letter) are both deliberate: they
// keep this from misreading something like "Package v2" as a ₦2 price.
function resolvePrice(productText, explicitPrice) {
  if (explicitPrice) return { product: productText, priceText: explicitPrice };
  const text = (productText || '').trim();
  if (!text) return { product: text, priceText: '' };

  // Broadened to also accept a non-breaking space (U+00A0) as a separator —
  // some mobile keyboards/clients insert one instead of a normal space — and
  // to accept a bare currency symbol (# or ₦) with NO separator at all before
  // it, since the symbol itself is unambiguous enough on its own. "N"/"n" are
  // deliberately NOT included in that no-separator case, since a bare letter
  // is far more likely to false-positive than a real currency symbol is.
  // Everything the previous regex matched, this one still matches identically —
  // this only adds new cases, never removes any.
  const match = text.match(/(?:[\s=\-–—\u00A0]+[#₦Nn]?|[#₦])\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{3,}(?:\.\d+)?)\s*,?\s*$/);
  if (!match) return { product: text, priceText: '' };

  const cleanProduct = text.slice(0, match.index).trim().replace(/[-–—=]\s*$/, '').trim();
  return { product: cleanProduct || text, priceText: match[1] };
}

/* ---------------- ONE-TIME MIGRATION ---------------- */
// Run this exactly once, right after pasting this updated code in, to move
// your ALREADY-EXISTING S/N and Filtered # data into their new swapped
// positions (S/N: B -> R, Filtered #: R -> B). It is NOT added to the Order
// Tools menu on purpose — it's a single-use step, not a daily action.
//
// How to run it: in the Apps Script editor, use the function dropdown at the
// top (next to the ▶ Run button) to select "swapSnAndFilteredColumns_migration",
// then click Run. It's safe to run more than once by accident — running it a
// second time just re-does the same swap back to where it already is, and it
// never touches any column other than B and R, or any row past 500 (except
// refreshing the Order Summary numbers at the very end).
//
// ⚠️ ORDER MATTERS: run this BEFORE "Initialize Sheet" (or skip Initialize
// Sheet entirely until after this runs). If Initialize Sheet was already run
// first, it will have already overwritten column B's real S/N numbers with a
// fresh Filtered # formula — at that point this function has nothing valid
// left to move, and running it will just copy the broken/error value instead
// of real numbers. If that's already happened (both S/N and Filtered # show
// #REF! or a similar error), use repairSnAfterPrematureInitialize() below
// instead — it's built specifically to recover from that exact situation.
//
// Why this is safe: it reads your real S/N numbers into memory FIRST, before
// writing anything at all. Filtered # is never "read and moved" — it's a live
// formula with no real data of its own, so it's simply regenerated fresh at
// its new column using the exact same trusted formula the rest of the sheet
// already relies on. Nothing is ever overwritten before what it holds has
// already been safely captured elsewhere.
function swapSnAndFilteredColumns_migration() {
  const sheet = SpreadsheetApp.getActiveSheet();
  const OLD_SN_COL = 2;        // B — where S/N used to live
  const OLD_FILTERED_COL = 18; // R — where Filtered # used to live
  const numRows = CONFIG.VALIDATION_LAST_ROW - CONFIG.FIRST_DATA_ROW + 1;

  // 1. Capture everything that needs preserving — nothing has been written yet.
  const snValues = sheet.getRange(CONFIG.FIRST_DATA_ROW, OLD_SN_COL, numRows, 1).getValues();
  const snHeader = sheet.getRange(CONFIG.HEADER_ROW, OLD_SN_COL).getValue();
  const filteredHeader = sheet.getRange(CONFIG.HEADER_ROW, OLD_FILTERED_COL).getValue();

  // 2. Write S/N's real values into its NEW column (CONFIG.SN_COL, now R).
  sheet.getRange(CONFIG.HEADER_ROW, CONFIG.SN_COL).setValue(snHeader);
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.SN_COL, numRows, 1).setValues(snValues);

  // 3. Rebuild Filtered #'s live formulas fresh at ITS new column (CONFIG.FILTERED_COL, now B).
  sheet.getRange(CONFIG.HEADER_ROW, CONFIG.FILTERED_COL).setValue(filteredHeader);
  const filteredFormulas = [];
  for (let r = CONFIG.FIRST_DATA_ROW; r <= CONFIG.VALIDATION_LAST_ROW; r++) {
    filteredFormulas.push([filteredSeqFormula(r)]);
  }
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.FILTERED_COL, numRows, 1).setFormulas(filteredFormulas);

  // 4. Also pin the frozen-columns sidebar now, so you don't need a separate
  //    Initialize Sheet run just for that.
  sheet.setFrozenColumns(CONFIG.NAME_COL);

  // 5. Refresh the Order Summary report so Total Orders / % Delivered count
  //    against S/N's NEW column instead of the old one.
  buildSummaryReport();

  safeAlert(
    'Done — S/N is now column ' + columnLetter_(CONFIG.SN_COL) +
    ', Filtered # is now column ' + columnLetter_(CONFIG.FILTERED_COL) +
    '. Customer Name is now pinned as a sidebar while scrolling.'
  );
}

// Fixes the #REF! / circular-reference state that results if "Initialize
// Sheet" was run BEFORE swapSnAndFilteredColumns_migration() (see the warning
// above): column B ends up holding a Filtered # formula that reads column R,
// while column R still holds the OLD Filtered # formula reading column B —
// each depends on the other, so both show an error.
//
// The original S/N numbers that used to be in column B are gone from the live
// sheet at this point (overwritten by the formula) — Google Sheets' own
// Version History is the only place they might still exist, if you want to
// try recovering the literal original values instead of using this. This
// function does NOT touch Version History; it assigns a fresh, clean
// 1, 2, 3... sequence to every row that has a real order in it (detected by a
// non-blank Customer Name — a column nothing in this whole process ever
// touched), in the same top-to-bottom order the rows are already in. Since
// S/N never carried meaning beyond "a per-row counter used by Filtered # and
// Total Orders" — nothing reads the specific number for a specific row — this
// is functionally identical to what was there before, unless you'd manually
// sorted the sheet at some point.
//
// How to run it: Apps Script editor > function dropdown >
// "repairSnAfterPrematureInitialize" > Run. Safe to run more than once.
function repairSnAfterPrematureInitialize() {
  const sheet = SpreadsheetApp.getActiveSheet();
  const lastRow = sheet.getLastRow();
  if (lastRow < CONFIG.FIRST_DATA_ROW) {
    safeAlert('No data rows found — nothing to repair.');
    return;
  }
  const dataRows = lastRow - CONFIG.FIRST_DATA_ROW + 1;
  const names = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.NAME_COL, dataRows, 1).getValues();

  // 1. Fresh sequential S/N for every row that actually has an order in it.
  const snValues = [];
  let counter = 0;
  for (let i = 0; i < dataRows; i++) {
    if (names[i][0] && String(names[i][0]).trim()) {
      counter++;
      snValues.push([counter]);
    } else {
      snValues.push(['']);
    }
  }
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.SN_COL, dataRows, 1).setValues(snValues);

  // Clear out any lingering formula/error in S/N for the rest of the
  // pre-validated range (rows past your actual data, up to row 500).
  const fullRows = CONFIG.VALIDATION_LAST_ROW - CONFIG.FIRST_DATA_ROW + 1;
  if (fullRows > dataRows) {
    const blankTail = [];
    for (let i = dataRows; i < fullRows; i++) blankTail.push(['']);
    sheet.getRange(CONFIG.FIRST_DATA_ROW + dataRows, CONFIG.SN_COL, fullRows - dataRows, 1).setValues(blankTail);
  }

  // 2. Rebuild Filtered # fresh now that S/N (column R) holds real numbers
  //    again — the circular reference is broken as soon as this runs.
  const filteredFormulas = [];
  for (let r = CONFIG.FIRST_DATA_ROW; r <= CONFIG.VALIDATION_LAST_ROW; r++) {
    filteredFormulas.push([filteredSeqFormula(r)]);
  }
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.FILTERED_COL, fullRows, 1).setFormulas(filteredFormulas);

  // 3. Refresh Order Summary counts.
  buildSummaryReport();

  safeAlert(
    'Repaired. S/N (column ' + columnLetter_(CONFIG.SN_COL) + ') now holds a fresh 1, 2, 3... sequence for ' +
    counter + ' order row(s), and Filtered # (column ' + columnLetter_(CONFIG.FILTERED_COL) + ') is working again.'
  );
}