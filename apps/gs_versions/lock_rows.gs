// File: For WhatsApp Order Normalizer — Apps Script
// Paste into: Extensions > Apps Script > Code.gs (replace all)
//
// Updated: 2026-07-27 — CALL BUTTON FIX (v4): ANDROID SHEETS APP
// ---------------------------------------------------------------------
// ROOT CAUSE, CONFIRMED: v2/v3 below both still built a direct tel: link.
// That is correct and sufficient on desktop, but the Google Sheets client
// apps (this is a documented, longstanding limitation of the Sheets apps
// themselves, not something wrong with the formula) only ever forward
// http://, https://, and mailto: links when a hyperlink cell is tapped —
// tel: is silently swallowed, so nothing happens on tap even though the
// exact same formula opens the dialer fine from a desktop browser. There is
// no in-app setting that changes this, and no formula tweak fixes it either
// — ENCODEURL(), IFERROR() nesting, etc. were never the actual problem.
//
// THE FIX: callLinkFormula() now points the HYPERLINK at a tiny bridge page
// (this project's own doGet(), see "CALL REDIRECT BRIDGE" below) instead of
// directly at tel:. That bridge URL is a normal https:// link — which Sheets
// apps always forward correctly — and the page itself redirects to tel: the
// moment it loads in an actual browser context, where tel: is universally
// supported. This is the same standard workaround used by third-party
// "click-to-call for Sheets" tools, just self-hosted in this project instead
// of depending on someone else's site.
//
// ONE-TIME SETUP REQUIRED (not automatic — see Order Tools > "Call Button
// Setup (Android Fix)" for exact steps): this project must be deployed once
// as a Web App (Deploy > New deployment > Web app) before the bridge exists.
// Until that's done, callLinkFormula() automatically falls back to the exact
// same direct tel: link as before — nothing regresses, the Call button just
// isn't fixed yet. After deploying, run Order Tools > Repair Link Buttons
// once to update every existing row (new rows pick it up automatically).
//
// Nothing else about the Call or WhatsApp Chat columns changed: the
// self-heal on edit, the warning-only protection, and Repair Link Buttons
// all work exactly as described below, just building a different URL now.
//
// Updated: 2026-07-26 — CALL BUTTON FIX (v2) + SELF-HEALING LINK COLUMNS
// ---------------------------------------------------------------------
// IMPORTANT CONTEXT ON THIS FIX: an earlier attempt tried to make the Call
// button a genuine embedded hyperlink object via
// SpreadsheetApp.newRichTextValue().setLinkUrl('tel:...'). That threw
// "Exception: Illegal argument." on every row — RichTextValue.setLinkUrl()
// only accepts http:// or https:// URLs; Sheets' rich-link API has no
// support for tel: links at all. That approach cannot work, full stop.
//
// The only way to get a working tel: link in Google Sheets is a
// HYPERLINK() formula. The actual bug making Call not dial (while WhatsApp
// Chat worked fine) was that the raw phone number wasn't a well-formed URI
// component — ENCODEURL() fixes that. So this version reverts to a formula,
// with ENCODEURL() added, which is the correct and only working approach.
//
// KNOWN, UNAVOIDABLE LIMITATION: any formula cell in Google Sheets shows its
// formula in the formula bar the instant it's clicked/selected — this is
// built into Sheets itself and cannot be suppressed while the link stays
// functional. What IS guaranteed: the cell displays only "📞 Call" (not the
// formula) until clicked, and the two fixes below stop anyone from typing
// over it, pasting into it, or drag-filling through it by accident.
//
//   1. CALL BUTTON FIX: callLinkFormula() now wraps the phone number in
//      ENCODEURL() before building the tel: URI, so mobile devices reliably
//      treat it as a followable link and open the dialer.
//
//   2. SELF-HEALING LINK COLUMNS: WhatsApp Chat and Call are computed-only
//      columns — there's never a legitimate reason for a human to type a
//      value into them. onEdit now watches both columns and immediately
//      rewrites the correct formula the moment anything lands there —
//      single-cell edits, multi-cell pastes, and drag-fills are all healed.
//      Both columns also get warning-only native protection (a heads-up
//      before editing, same style the header row originally had — not a
//      hard lock, so it can never accidentally block anyone from placing a
//      call). Backstop: Order Tools > "Repair Link Buttons" rewrites every
//      row's formula in both columns on demand, for the rare case an edit
//      slips past onEdit entirely (e.g. a bulk API edit).
//
//   3. STATUS COLOR CODING (additive): Pending now highlights orange,
//      Cancelled red, Awaiting purple, and Not Picking Calls indigo — each
//      with a matching high-contrast text color — alongside the existing
//      Overdue (red), Delivered/Cash Remitted (green), and Deleted/Banned
//      (gray) rules.
//
// Everything below this point (earlier revision history) describes changes
// made before this update and is kept for reference.
//
// Updated: 2026-07-21 — DYNAMIC COLUMN RESOLUTION (architecture fix)
// ---------------------------------------------------------------------
// Root cause of the "scattered columns" bug: every part of this script used
// to trust a fixed number for each column (Address = 13, Question = 15,
// etc.) forever. The moment a column was inserted, deleted, or dragged to a
// new position directly in the sheet, the script had no way to know — it
// kept writing to the same numeric position, which by then belonged to a
// different field. Data didn't error out, it just landed in the wrong cell.
//
// What changed:
//   1. Columns are now located by READING THE HEADER ROW, not by a fixed
//      number. CONFIG.ADDRESS_COL etc. are still used everywhere else in the
//      file exactly as before, but they are now populated at the START of
//      every operation by resolveColumns_(sheet), which scans row 1 for the
//      exact header text ("Address", "Product", "Question", ...) and maps it
//      to wherever it currently lives. Move the column, and the script just
//      follows it.
//   2. If a required header is missing or renamed (deleted column, typo,
//      accidental rename), resolveColumns_() throws immediately and nothing
//      is written. You get a clear toast telling you exactly which header is
//      missing, instead of silent data landing in the wrong cell. Menu:
//      Order Tools > Verify Column Layout runs this check on demand and
//      shows you the full current mapping.
//   3. FOUND AND FIXED A SECOND, HIDDEN VERSION OF THE SAME BUG: the four
//      "live formula" columns (Today Check-up?, Flagged?, Overdue?, Today's
//      Order?) had their formulas built with hardcoded column letters
//      ($H, $G, $P, $Q, $I) baked directly into the formula text. Even after
//      fixing CONFIG by hand, these formulas would keep pointing at the OLD
//      letters forever, silently reading the wrong column. They now build
//      their letters from columnLetter_(CONFIG.X_COL) at write time, so they
//      track the real position too.
//   4. The Report column (used to be hardcoded to column 23/W) is now also
//      located by header text ("Report"), and created automatically right
//      after your last existing header if it's ever missing.
//   5. writeRecord() and the report generator no longer assume Name/Phone/
//      WhatsApp/Note or Date/Category/Product/Price/Address/Delivery/
//      Question sit in contiguous columns. Each field is now written/read by
//      its own resolved column number. This costs a little speed (more
//      individual read/write calls) in exchange for surviving a column being
//      inserted in the middle of that block, or the block being reordered —
//      which is exactly the scenario that broke things. For your order
//      volume this trade-off is the right one.
//   6. NEW protective layer: the header row (row 1) is now protected
//      (warning-only, so it won't lock your dispatch team out by accident,
//      but it does warn before an edit). NEW menu items: "Enable/Disable
//      Structure Change Alerts" installs a trigger that toasts you the
//      moment anyone inserts/deletes a column, so you find out immediately
//      instead of days later when the sheet is scattered.
//
// Nothing about parsing, duplicate detection, the report content, triggers,
// or daily workflow changed — this update is purely about making column
// position resilient instead of a fixed assumption.
//
// Updated: 2026-07-17 — LABEL MATCHING FIX (isolated to normalizeLabel(),
// matchLabel(), and the LABELS list only): label keywords are matched by
// WORD, not by raw substring, so "Hotel" no longer false-matches "tel"
// (phone). See labelWordStartsWith_() below.
//
// Updated: 2026-07-16 — GENERATE REPORT ENHANCEMENTS: Package line in the
// generated report is normalized for dispatch (marketing wording stripped,
// always ends with "+ Doorstep Delivery"); a blank "Delivery #:" line was
// added under the report's date line.
//
// Updated: 2026-07-15 (rev 8) — Duplicate Order? now flags TRUE when phone
// matches AND EITHER name also matches, OR product+address also match.
// normalizeText() also strips punctuation before comparing.
//
// Earlier revisions (rev 6 and before): row-insert duplicate-flag pointer
// fix, scan-once-per-paste performance fix, batched writes (since replaced
// by per-column writes for column-shift safety — see item 5 above), phone/
// WhatsApp dropdown-validation clearing fix, keyword-based label matching,
// bare trailing-price detection, S/N and Filtered # column swap + frozen
// sidebar, real Date values in the Date column, WhatsApp chat-log block
// splitting, flagged/overdue/today filter views, conditional formatting,
// row-cap warning, Order Summary report + chart, check-up reminders.

/**
 * WhatsApp Order Normalizer
 * ---------------------------------------------------------------
 * Paste raw WhatsApp order text into Column A (any row, from row 2 down).
 * The script parses it and fills the rest of the row. Pasting a new message
 * into that same cell overwrites and reprocesses it.
 *
 * Multiple orders pasted together (separated by "####", or just back-to-back)
 * are split into their own rows automatically.
 *
 * COLUMN LAYOUT — this is now a DEFAULT, not a hard requirement of position.
 * "Initialize Sheet" builds row 1 with these exact header labels in this
 * order. After that, every part of the script finds each column by reading
 * its header text in row 1, wherever it currently sits — so moving, deleting,
 * or inserting a column no longer breaks anything, AS LONG AS these exact
 * header labels still exist somewhere in row 1. Renaming or deleting a header
 * label itself is what will stop the script (with a clear message telling
 * you which one) — see resolveColumns_() and "Verify Column Layout" below.
 *
 *  Raw Message | Filtered # | Customer Name | Phone Number | WhatsApp Number |
 *  Note | Action | Check-up Date & Time | Date | Category | Product |
 *  Price (₦) | Address | Delivery Date | Question | Duplicate Order? |
 *  Invalid Number? | S/N | Today Check-up? | Flagged (Dup/Invalid)? |
 *  Overdue Check-up? | Today's Order? | Report (auto-created if missing)
 *
 * Below row 500 (VALIDATION_LAST_ROW), a live ORDER SUMMARY report and bar
 * chart total up Delivered / Pending / Invalid / Duplicate / Cancelled /
 * % Delivered / Total Orders.
 *
 * SETUP:
 *  1. Extensions > Apps Script, replace all code with this file, Save.
 *  2. In the Apps Script editor, click "+ Services" > "Google Sheets API" > Add.
 *     (One-time — needed for saved filter views.)
 *  3. Reload the spreadsheet.
 *  4. Menu "Order Tools" > "Initialize Sheet" (run once).
 *  5. Approve permissions the first time you're asked.
 *  6. Click a single cell in column A and paste a WhatsApp order message.
 *
 * PROTECTING YOUR LAYOUT:
 *  - "Order Tools > Verify Column Layout" — checks that every required
 *    header still exists somewhere in row 1 and shows you exactly which
 *    physical column each field currently resolves to. Run this any time
 *    something looks off, or right after you or your team edits the sheet
 *    structure.
 *  - "Order Tools > Enable Structure Change Alerts" — turns on a background
 *    watcher that toasts you the instant a column is inserted or removed
 *    anywhere in the sheet, so you notice immediately rather than after data
 *    has already scattered.
 *  - Row 1 (the header row) is protected as "warning only" once you run
 *    Initialize Sheet — editors get a heads-up before changing it, but your
 *    dispatch team is never hard-locked out by accident.
 *  - WhatsApp Chat and Call columns are computed-only and self-heal: any
 *    edit landing in them is immediately overwritten back to the correct
 *    working formula, and both carry warning-only protection too.
 *    "Order Tools > Repair Link Buttons" also rewrites both columns for
 *    every row on demand, as a manual backstop.
 *  - Clicking directly into a Call or WhatsApp Chat cell will show its
 *    underlying formula in the formula bar — that's normal Google Sheets
 *    behavior for any formula cell and can't be hidden while the link stays
 *    functional. The cell itself only ever DISPLAYS "📞 Call" / "💬 Chat".
 *  - CALL BUTTON ON ANDROID: the Google Sheets app doesn't forward tel:
 *    links (only http/https/mailto), so tapping Call there does nothing
 *    until you complete a one-time setup step. Run "Order Tools > Call
 *    Button Setup (Android Fix)" for exact instructions and current status.
 *  - If you ever need to physically reorder columns on purpose, that's fine
 *    — just make sure the exact header text moves with the column (which it
 *    will, if you drag the whole column) and re-run "Verify Column Layout"
 *    afterward to confirm.
 *
 * DAILY USE: unchanged from before — see in-app menu items under Order Tools.
 *
 * PASTE NOTE: click ONE cell in column A before pasting (not a range), so
 * the multi-line message stays inside that single cell.
 */

/* ---------------- COLUMN DEFINITIONS (header-driven) ---------------- */

// The single source of truth for what each managed column is CALLED.
// CONFIG.HEADERS (built below) and every header row Initialize Sheet writes
// are derived from this — so the text used to BUILD the header row and the
// text used to LOOK UP each column later can never drift apart.
const CANONICAL_HEADERS = {
  RAW_COL: 'Raw Message',
  FILTERED_COL: 'Filtered #',
  NAME_COL: 'Customer Name',
  PHONE_COL: 'Phone Number',
  WHATSAPP_COL: 'WhatsApp Number',
  NOTE_COL: 'Note',
  ACTION_COL: 'Action',
  CHECKUP_COL: 'Check-up Date & Time',
  DATE_COL: 'Date',
  CATEGORY_COL: 'Category',
  PRODUCT_COL: 'Product',
  PRICE_COL: 'Price (₦)',
  ADDRESS_COL: 'Address',
  DELIVERY_COL: 'Delivery Date',
  QUESTION_COL: 'Question',
  DUPLICATE_COL: 'Duplicate Order?',
  INVALID_COL: 'Invalid Number?',
  SN_COL: 'S/N',
  TODAY_COL: 'Today Check-up?',
  FLAGGED_COL: 'Flagged (Dup/Invalid)?',
  OVERDUE_COL: 'Overdue Check-up?',
  TODAY_ORDER_COL: "Today's Order?"
};

// Display/build order for Initialize Sheet — matches the documented layout.
const COLUMN_ORDER = ['RAW_COL','FILTERED_COL','NAME_COL','PHONE_COL','WHATSAPP_COL','NOTE_COL',
  'ACTION_COL','CHECKUP_COL','DATE_COL','CATEGORY_COL','PRODUCT_COL','PRICE_COL','ADDRESS_COL',
  'DELIVERY_COL','QUESTION_COL','DUPLICATE_COL','INVALID_COL','SN_COL','TODAY_COL','FLAGGED_COL',
  'OVERDUE_COL','TODAY_ORDER_COL'];

// CONFIG.*_COL values below are only FALLBACK/INITIAL defaults — the exact
// same layout Initialize Sheet has always built. The moment any operation
// runs (onEdit, Reprocess, Refresh Summary, etc.), resolveColumns_() rereads
// row 1 and overwrites these with the REAL current position of each column.
const CONFIG = {
  RAW_COL: 1, FILTERED_COL: 2, NAME_COL: 3, PHONE_COL: 4, WHATSAPP_COL: 5,
  NOTE_COL: 6, ACTION_COL: 7, CHECKUP_COL: 8, DATE_COL: 9, CATEGORY_COL: 10,
  PRODUCT_COL: 11, PRICE_COL: 12, ADDRESS_COL: 13, DELIVERY_COL: 14,
  QUESTION_COL: 15, DUPLICATE_COL: 16, INVALID_COL: 17, SN_COL: 18,
  TODAY_COL: 19, FLAGGED_COL: 20, OVERDUE_COL: 21, TODAY_ORDER_COL: 22,

  HEADER_ROW: 1,
  FIRST_DATA_ROW: 2,
  VALIDATION_LAST_ROW: 500,
  ROW_CAP_WARNING_THRESHOLD: 20,
  DEFAULT_ROW_HEIGHT: 21,
  QUICK_SCHEDULE_HOUR: 10,
  REMINDER_WINDOW_MINUTES: 5,
  ORDER_DATE_FORMAT: 'd mmm yyyy',

  HEADERS: null // filled in immediately below, derived from CANONICAL_HEADERS
};
CONFIG.HEADERS = COLUMN_ORDER.map(function(k) { return CANONICAL_HEADERS[k]; });

const HEADER_PROTECTION_DESCRIPTION = 'Order Normalizer — managed header row (do not rename, delete, or reorder these labels)';

// Description tags for the warning-only protections on the two link columns
// — separate from HEADER_PROTECTION_DESCRIPTION so they can be found/
// removed/re-applied independently without touching header protection.
const WHATSAPP_LINK_PROTECTION_DESCRIPTION = 'Order Normalizer — computed WhatsApp Chat column (self-heals on edit)';
const CALL_LINK_PROTECTION_DESCRIPTION = 'Order Normalizer — computed Call column (self-heals on edit)';

// Google Sheets' native Range.protect() can NEVER fully block the file
// owner — that's a hard platform rule, not something Apps Script can
// override. So the owner-inclusive lock is enforced by the script itself:
// a snapshot of the last known-good header row is kept in Document
// Properties (scoped to this specific spreadsheet), and onEdit reverts ANY
// edit landing on row 1 — including the owner's own — back to that
// snapshot, unless HEADER_LOCK_PROP has been explicitly set to 'true' via
// Order Tools > Unlock Header Row. Native protection still blocks everyone
// else at the UI level too, as a first line of defense.
const HEADER_LOCK_PROP = 'HEADER_ROW_UNLOCKED';       // 'true' = editable right now, including by the owner
const HEADER_SNAPSHOT_PROP = 'HEADER_ROW_SNAPSHOT';   // JSON array — last known-good header row values

// Report column is fully dynamic (found by header text "Report"), never a
// fixed number — see findOrCreateReportColumn_(). REPORT_TRIGGER_VALUE and
// the report-formatting constants live further down near the report code.
let REPORT_COL = null;

// Three more bonus columns, added the exact same safe way as Report: never
// part of CANONICAL_HEADERS (so an already-initialized sheet is never broken
// by their absence), located by header text, auto-created and appended after
// whatever already exists the moment the sheet is opened.
let WHATSAPP_LINK_COL = null;   // "WhatsApp Chat" — tap to open a chat with this customer
let CALL_LINK_COL = null;       // "Call" — tap to dial this customer directly
let CALL_ATTEMPTS_COL = null;   // "Call Attempts" — auto-incremented, see onEdit Case 2

// ANDROID CALL FIX (2026-07-27) — see doGet() further down and the changelog
// entry at the top of this file for the full explanation. This is the query
// parameter name the bridge page reads the phone number from.
const CALL_REDIRECT_PARAM_NAME = 'call';

// Cached per-execution only (Apps Script executions are short-lived, so this
// never goes stale within a single run; the next run just asks again — cheap,
// no external call involved, ScriptApp.getService().getUrl() is local).
let _callRedirectBaseUrlCache = null;

// Returns this project's own deployed Web App /exec URL, or '' if it has
// never been deployed as a Web App yet (Deploy > New deployment > Web app —
// see Order Tools > "Call Button Setup (Android Fix)" for exact steps).
// callLinkFormula() uses this to decide whether to route through the
// call-redirect bridge or fall back to a direct tel: link.
function getCallRedirectBaseUrl_() {
  if (_callRedirectBaseUrlCache !== null) return _callRedirectBaseUrlCache;
  try {
    _callRedirectBaseUrlCache = ScriptApp.getService().getUrl() || '';
  } catch (e) {
    _callRedirectBaseUrlCache = '';
  }
  return _callRedirectBaseUrlCache;
}

// Generic version of the same lookup findOrCreateReportColumn_ already used
// for Report — reused here so all bonus columns behave identically without
// touching the original working Report logic.
function findOrCreateOptionalColumn_(sheet, headerLabel) {
  const lastCol = Math.max(sheet.getLastColumn(), 1);
  const headerRow = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, lastCol).getValues()[0];

  for (let i = 0; i < headerRow.length; i++) {
    if (String(headerRow[i] || '').trim().toLowerCase() === headerLabel.toLowerCase()) {
      return i + 1;
    }
  }

  const looksInitialized = headerRow.some(function(h) {
    const norm = String(h || '').trim().toLowerCase();
    return Object.keys(CANONICAL_HEADERS).some(function(k) { return CANONICAL_HEADERS[k].toLowerCase() === norm; });
  });
  if (!looksInitialized) return null;

  const newCol = Math.max(sheet.getLastColumn(), 1) + 1;
  sheet.getRange(CONFIG.HEADER_ROW, newCol)
    .setValue(headerLabel).setFontWeight('bold').setBackground('#4a86e8').setFontColor('#ffffff');
  return newCol;
}

function findOrCreateWhatsAppLinkColumn_(sheet, createIfMissing) {
  const found = findOrCreateOptionalColumnLookup_(sheet, 'WhatsApp Chat', createIfMissing);
  if (found) WHATSAPP_LINK_COL = found;
  return found;
}

function findOrCreateCallLinkColumn_(sheet, createIfMissing) {
  const found = findOrCreateOptionalColumnLookup_(sheet, 'Call', createIfMissing);
  if (found) CALL_LINK_COL = found;
  return found;
}

function findOrCreateCallAttemptsColumn_(sheet, createIfMissing) {
  const found = findOrCreateOptionalColumnLookup_(sheet, 'Call Attempts', createIfMissing);
  if (found) CALL_ATTEMPTS_COL = found;
  return found;
}

// Shared lookup-only-vs-create logic, matching findOrCreateReportColumn_'s
// behavior exactly: look it up regardless; only create it if createIfMissing
// is true AND the sheet looks initialized already.
function findOrCreateOptionalColumnLookup_(sheet, headerLabel, createIfMissing) {
  const lastCol = Math.max(sheet.getLastColumn(), 1);
  const headerRow = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, lastCol).getValues()[0];
  for (let i = 0; i < headerRow.length; i++) {
    if (String(headerRow[i] || '').trim().toLowerCase() === headerLabel.toLowerCase()) {
      return i + 1;
    }
  }
  if (!createIfMissing) return null;
  return findOrCreateOptionalColumn_(sheet, headerLabel);
}

// Live-formula generators for the two link columns. Both degrade to a blank
// cell (never an error string) when the relevant phone field is empty.
//
// CALL FIX (2026-07-26, v2): the tel: URI now wraps the phone number in
// ENCODEURL(). This is the actual, correct fix — see the top-of-file note
// for why a "real embedded hyperlink" via RichTextValue is not possible for
// tel: links in Google Sheets (setLinkUrl only accepts http/https and throws
// "Illegal argument" on anything else).
function whatsappLinkFormula(row) {
  const waCol = columnLetter_(CONFIG.WHATSAPP_COL);
  const phoneCol = columnLetter_(CONFIG.PHONE_COL);
  // Prefers the WhatsApp Number field; falls back to Phone Number if that's blank.
  return '=IFERROR(IF(AND($' + waCol + row + '="",$' + phoneCol + row + '=""),"",' +
    'HYPERLINK("https://wa.me/"&SUBSTITUTE(IF($' + waCol + row + '<>"",$' + waCol + row + ',$' + phoneCol + row + '),"+",""),"💬 Chat")),"")';
}

// CALL FIX (2026-07-26, v3): ENCODEURL() was removed. It was percent-
// encoding the "+" in "+234..." into "%2B", producing "tel:%2B234..." —
// phone dialers expect a literal "+" (E.164 format) in a tel: URI, so the
// encoded version was not recognized as a valid number and nothing
// happened when tapped. normalizePhone() already guarantees the stored
// number is clean digits-and-plus-only with no spaces, so no encoding is
// needed or wanted here — the raw value is a valid tel: URI as-is.
//
// CALL FIX (2026-07-27, v4 — ANDROID SHEETS APP): v3's direct tel: link is
// still correct for a browser, but Sheets client apps only forward http(s)/
// mailto hyperlinks and silently swallow tel: taps — see the changelog entry
// at the top of this file. Once this project is deployed as a Web App (see
// Order Tools > "Call Button Setup (Android Fix)"), the button instead
// points at that Web App's own doGet() bridge, passing the phone number as
// an ordinary query-string parameter. THIS is where ENCODEURL() belongs —
// unlike v2's mistake, this isn't encoding a tel: URI, it's encoding a query
// parameter of a normal https:// URL, which is exactly what ENCODEURL() is
// for; the "+" becomes "%2B" here on purpose, and doGet()'s e.parameter
// automatically decodes it back to "+" on the other end.
function callLinkFormula(row) {
  const phoneCol = columnLetter_(CONFIG.PHONE_COL);
  const baseUrl = getCallRedirectBaseUrl_();

  if (baseUrl) {
    return '=IFERROR(IF($' + phoneCol + row + '="","",HYPERLINK("' + baseUrl + '?' +
      CALL_REDIRECT_PARAM_NAME + '="&ENCODEURL($' + phoneCol + row + '),"📞 Call")),"")';
  }

  // Fallback — used only until the one-time Web App deployment is done, so
  // nothing regresses for anyone who hasn't set that up yet. Identical to
  // the v3 formula above.
  return '=IFERROR(IF($' + phoneCol + row + '="","",HYPERLINK("tel:"&$' + phoneCol + row + ',"📞 Call")),"")';
}

// Writes the correct formula into a single link-column cell. Shared by
// writeRecord/applyRowValidation/initializeSheet_impl (normal writes) AND
// the self-heal path in onEdit / repairLinkButtons (recovery writes) — one
// function, so there is only ever one place that defines "correct."
function healLinkCell_(sheet, row, col, formulaFn) {
  sheet.getRange(row, col).setFormula(formulaFn(row));
}

/* ---------------- CALL REDIRECT BRIDGE (Android Call-button fix) ---------------- */
// Requires a one-time manual step: Deploy > New deployment > Web app
// (Execute as: Me, Who has access: Anyone) — see Order Tools > "Call Button
// Setup (Android Fix)" for exact steps and current status. Once deployed,
// callLinkFormula() automatically routes through this page instead of a raw
// tel: link, because Sheets client apps only forward http(s)/mailto
// hyperlinks and silently ignore tel: taps (see changelog at top of file).
//
// This page never touches spreadsheet data — it only reads the phone number
// handed to it in the URL, re-sanitizes it independently (digits and a
// leading + only, regardless of what was in the query string), and
// redirects to tel:<number>. A plain tappable fallback link is included in
// case the automatic redirect is blocked, since some browsers only allow a
// tel: navigation as the direct result of the user's own tap, not a script
// that runs after the page has already loaded.
//
// IMPORTANT FOR FUTURE EDITS: if you change this function (or anything else
// in the project) later, redeploy via Deploy > Manage deployments > (pencil
// icon) > Version: New version > Deploy — NOT "New deployment" — so the /exec
// URL never changes. If the URL ever does change, run Order Tools > Repair
// Link Buttons afterward to update every row.
function doGet(e) {
  const raw = (e && e.parameter && e.parameter[CALL_REDIRECT_PARAM_NAME])
    ? String(e.parameter[CALL_REDIRECT_PARAM_NAME]) : '';
  const phone = raw.replace(/[^\d+]/g, ''); // digits and + only — defense in depth

  const escapedPhone = phone.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const body = phone
    ? '<p>Opening your dialer for ' + escapedPhone + '…</p>' +
      '<a id="callLink" href="tel:' + phone + '">📞 Tap to call ' + escapedPhone + '</a>' +
      '<script>window.location.href = "tel:' + phone + '";</script>'
    : '<p>No phone number was provided.</p>';

  const html =
    '<!DOCTYPE html><html><head><base target="_top">' +
    '<style>body{font-family:sans-serif;text-align:center;padding-top:15vh;}' +
    'a{display:inline-block;font-size:20px;padding:14px 24px;background:#1a73e8;' +
    'color:#fff;border-radius:8px;text-decoration:none;margin-top:16px;}</style>' +
    '</head><body>' + body + '</body></html>';

  return HtmlService.createHtmlOutput(html)
    .addMetaTag('viewport', 'width=device-width, initial-scale=1')
    .setTitle('Call ' + (phone || ''));
}

// Menu action + status check for the Android call-redirect bridge above.
// Tells you plainly whether it's set up yet, and gives exact steps if not.
function showCallRedirectSetupInstructions() {
  const url = getCallRedirectBaseUrl_();
  if (url) {
    safeAlert(
      'Call redirect is ACTIVE.\n\nWeb app URL:\n' + url + '\n\n' +
      'The Call button routes through this page so it opens the dialer correctly from the ' +
      'Google Sheets Android app (and any other Sheets client). If you ever edit this project\'s ' +
      'code again, redeploy using Deploy > Manage deployments > (pencil icon) > Version: New ' +
      'version > Deploy — NOT "New deployment" — so this URL never changes. If the URL ever does ' +
      'change anyway, run Order Tools > Repair Link Buttons afterward to update every row.',
      'Call redirect active'
    );
  } else {
    safeAlert(
      'The Call button currently falls back to a plain tel: link, which the Google Sheets app ' +
      '(Android, and possibly other clients) cannot open directly — it only forwards http(s)/' +
      'mailto links, and silently ignores tel: taps. This is a known Sheets app limitation, not a ' +
      'bug in this script, and there is no in-app setting that changes it — the fix needs a small ' +
      'bridge page, which this project can host itself.\n\n' +
      'ONE-TIME FIX (about 2 minutes):\n' +
      '1. In this Apps Script project, click "Deploy" (top right) > "New deployment".\n' +
      '2. Click the gear icon next to "Select type" and choose "Web app".\n' +
      '3. Execute as: Me.  Who has access: Anyone.\n' +
      '4. Click "Deploy", and authorize the project if you\'re asked to.\n' +
      '5. Come back here and run Order Tools > "Call Button Setup (Android Fix)" again to confirm ' +
      'it\'s picked up, then run Order Tools > "Repair Link Buttons" once to update every existing ' +
      'row. New rows will use the fix automatically from then on.\n\n' +
      'If you skip this step, the Call button keeps working exactly as it does today — including ' +
      'on desktop, where a direct tel: link already opens fine.',
      'Call button setup needed'
    );
  }
}

// Statuses that count as a "failed contact attempt" for the auto-incrementing
// Call Attempts column (see onEdit Case 2). Edit this list directly if your
// definition of "attempted but didn't connect" should include more statuses.
const CALL_ATTEMPT_TRIGGER_STATUSES = ['Not Picking Calls', 'Switched Off'];

// Thrown by resolveColumns_() when a required header can't be found. Kept as
// its own type so callers can show a friendly message instead of a raw stack
// trace, and so it's never confused with an unrelated runtime error.
function ColumnResolutionError_(message) {
  this.name = 'ColumnResolutionError_';
  this.message = message;
}
ColumnResolutionError_.prototype = Object.create(Error.prototype);

// THE CORE FIX. Reads row 1 once, maps header text -> physical column, and
// overwrites CONFIG.*_COL with wherever each field ACTUALLY is right now.
// Throws ColumnResolutionError_ (without writing anything) if any required
// header can't be found, so a renamed/deleted header stops the script loudly
// instead of letting it write to whatever number used to be correct.
function resolveColumns_(sheet) {
  const lastCol = Math.max(sheet.getLastColumn(), 1);
  const headerRow = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, lastCol).getValues()[0];

  const byName = {};
  headerRow.forEach(function(h, i) {
    const norm = String(h || '').trim().toLowerCase();
    if (norm && !(norm in byName)) byName[norm] = i + 1; // first occurrence wins
  });

  const missing = [];
  const resolved = {};
  Object.keys(CANONICAL_HEADERS).forEach(function(key) {
    const label = CANONICAL_HEADERS[key];
    const col = byName[label.toLowerCase()];
    if (col) { resolved[key] = col; } else { missing.push(label); }
  });

  if (missing.length > 0) {
    throw new ColumnResolutionError_(
      'Sheet header row is missing: "' + missing.join('", "') + '". Nothing was ' +
      'written, to avoid corrupting the wrong cells. Restore these exact header ' +
      'label(s) in row ' + CONFIG.HEADER_ROW + ', or run Order Tools > ' +
      'Verify Column Layout for the full picture.'
    );
  }

  Object.keys(resolved).forEach(function(key) { CONFIG[key] = resolved[key]; });
  return resolved;
}

// Convenience wrapper for menu-triggered actions: resolves columns, and on
// failure shows a friendly alert (instead of throwing) and returns false so
// the caller can just bail out cleanly.
function requireColumns_(sheet) {
  try {
    resolveColumns_(sheet);
    return true;
  } catch (e) {
    if (e instanceof ColumnResolutionError_) {
      safeAlert(e.message, 'Column layout problem');
      return false;
    }
    throw e;
  }
}

// Width to use for any range that should span "all managed columns" (filters,
// conditional formatting, header protection) — never narrower than what
// Initialize Sheet builds, but grows if extra columns exist to the right.
function getManagedColumnCount_(sheet) {
  return Math.max(sheet.getLastColumn(), CONFIG.HEADERS.length);
}

// Locates the Report column by header text, wherever it is. If it doesn't
// exist yet and createIfMissing is true, appends it right after the last
// existing header — but only if the sheet already looks initialized (has at
// least one of the other managed headers), so a blank new sheet doesn't get
// a stray "Report" column before Initialize Sheet has even run.
function findOrCreateReportColumn_(sheet, createIfMissing) {
  const lastCol = Math.max(sheet.getLastColumn(), 1);
  const headerRow = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, lastCol).getValues()[0];

  for (let i = 0; i < headerRow.length; i++) {
    if (String(headerRow[i] || '').trim().toLowerCase() === 'report') {
      REPORT_COL = i + 1;
      return REPORT_COL;
    }
  }

  if (!createIfMissing) return null;

  const looksInitialized = headerRow.some(function(h) {
    const norm = String(h || '').trim().toLowerCase();
    return Object.keys(CANONICAL_HEADERS).some(function(k) { return CANONICAL_HEADERS[k].toLowerCase() === norm; });
  });
  if (!looksInitialized) return null;

  const newCol = lastCol + 1;
  sheet.getRange(CONFIG.HEADER_ROW, newCol)
    .setValue('Report').setFontWeight('bold').setBackground('#4a86e8').setFontColor('#ffffff');
  REPORT_COL = newCol;
  return REPORT_COL;
}

// Protects the header row as a HARD lock — column labels genuinely cannot be
// edited, cleared, or deleted from the sheet UI by anyone but the owner, not
// just warned about. Re-run-safe: removes any prior protection with the same
// description before re-adding, so it doesn't stack up. IMPORTANT: this must
// only ever be called AFTER the Report column already exists (see
// initializeSheet_impl / onOpen) — getManagedColumnCount_() only protects
// columns that already exist, so protecting too early leaves Report
// unprotected, which was the original gap.
function protectHeaderRow_(sheet) {
  const existing = sheet.getProtections(SpreadsheetApp.ProtectionType.RANGE);
  existing.forEach(function(p) {
    if (p.getDescription() === HEADER_PROTECTION_DESCRIPTION) p.remove();
  });
  const width = getManagedColumnCount_(sheet);
  const protection = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, width).protect();
  protection.setDescription(HEADER_PROTECTION_DESCRIPTION);
  protection.setWarningOnly(false); // blocks everyone except the file owner at the UI level

  try {
    protection.removeEditors(protection.getEditors());
    if (protection.canDomainEdit && protection.canDomainEdit()) protection.setDomainEdit(false);
  } catch (e) {
    console.error(e);
  }
}

// Warning-only protection for a single computed column (WhatsApp Chat or
// Call). Warning-only by design — unlike the header row, these columns
// don't need a hard owner-inclusive lock, because the onEdit self-heal below
// already fixes any edit the instant it happens. This is just an extra
// heads-up layer, and it can never accidentally block anyone from placing a
// call or opening a chat. Re-run-safe: removes any prior protection with the
// same description before re-adding.
function protectComputedColumn_(sheet, colIndex, description) {
  if (!colIndex) return;
  const existing = sheet.getProtections(SpreadsheetApp.ProtectionType.RANGE);
  existing.forEach(function(p) {
    if (p.getDescription() === description) p.remove();
  });
  const numRows = CONFIG.VALIDATION_LAST_ROW - CONFIG.FIRST_DATA_ROW + 1;
  const protection = sheet.getRange(CONFIG.FIRST_DATA_ROW, colIndex, numRows, 1).protect();
  protection.setDescription(description);
  protection.setWarningOnly(true);
}

// If the header row was already locked before (e.g. a returning user who ran
// Initialize Sheet earlier), re-applies the SAME lock + refreshes the
// snapshot after the Report column is (re)confirmed — so newly appearing
// columns don't slip through unprotected. Does nothing if never locked.
function refreshHeaderProtectionIfLocked_(sheet) {
  const alreadyLocked = sheet.getProtections(SpreadsheetApp.ProtectionType.RANGE)
    .some(function(p) { return p.getDescription() === HEADER_PROTECTION_DESCRIPTION; });
  if (alreadyLocked) {
    protectHeaderRow_(sheet);
    snapshotHeaderRow_(sheet);
  }
}

/* ---- Owner-inclusive enforcement (native protection alone can't do this) ---- */

function isHeaderRowUnlocked_() {
  return PropertiesService.getDocumentProperties().getProperty(HEADER_LOCK_PROP) === 'true';
}

function hasHeaderSnapshot_() {
  return !!PropertiesService.getDocumentProperties().getProperty(HEADER_SNAPSHOT_PROP);
}

function snapshotHeaderRow_(sheet) {
  const width = getManagedColumnCount_(sheet);
  const values = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, width).getValues()[0];
  PropertiesService.getDocumentProperties().setProperty(HEADER_SNAPSHOT_PROP, JSON.stringify(values));
}

function restoreHeaderSnapshot_(sheet) {
  const raw = PropertiesService.getDocumentProperties().getProperty(HEADER_SNAPSHOT_PROP);
  if (!raw) return false;
  const values = JSON.parse(raw);
  sheet.getRange(CONFIG.HEADER_ROW, 1, 1, values.length).setValues([values]);
  SpreadsheetApp.flush(); // force the revert to actually land before anything else can happen
  return true;
}

// THE enforcement itself — called from onEdit for every edit landing on row
// 1, for ANY user, including the file owner. This is what actually makes the
// lock "brutal": native Range protection can never block the owner (a hard
// Google Sheets platform rule), so ownership-inclusive blocking has to be
// done here, in script logic, not through protect() alone.
//
// Runs immediately when the edit is committed (the moment the user leaves
// the cell — exactly when onEdit fires), overwrites row 1 back to the last
// known-good snapshot, and then shows a real, blocking alert dialog — not a
// toast that's easy to miss — so it reads unambiguously as "this was
// rejected," not "here's a note."
function guardHeaderRowEdit_(sheet, e) {
  if (isHeaderRowUnlocked_()) return;   // explicitly unlocked via the menu — allow
  if (!hasHeaderSnapshot_()) return;    // never locked in the first place — nothing to enforce yet

  restoreHeaderSnapshot_(sheet);

  try {
    SpreadsheetApp.getUi().alert(
      'Cannot edit header row',
      'Column headers are locked. Your change was reverted immediately — the header row is back ' +
      'to its correct values. Run Order Tools > Unlock Header Row first if you genuinely need to ' +
      'change a label.',
      SpreadsheetApp.getUi().ButtonSet.OK
    );
  } catch (uiErr) {
    // No UI context available (rare for a real user edit, but guards against
    // a crash if it ever happens) — fall back to a toast so it's still seen.
    SpreadsheetApp.getActiveSpreadsheet().toast(
      'Header row is locked — your change was reverted. Use Order Tools > Unlock Header Row to edit it.',
      'Cannot edit header', 8
    );
  }
}

// Menu action: lock the header row on demand (also used by Initialize Sheet).
// Applies BOTH layers: native protection (blocks everyone except the owner
// at the UI level) and the snapshot-based guard above (blocks the owner too).
function lockHeaderRow() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  try { findOrCreateReportColumn_(sheet, true); } catch (e) { console.error(e); }
  protectHeaderRow_(sheet);
  snapshotHeaderRow_(sheet);
  PropertiesService.getDocumentProperties().setProperty(HEADER_LOCK_PROP, 'false');
  safeAlert('Header row locked — for everyone, including you. Any edit to row 1 (including a paste, clear, or delete) will be reverted immediately and blocked with an error. Use "Unlock Header Row" first if you genuinely need to change a label.');
}

// Menu action: temporarily lift BOTH layers so you (the owner) can fix a
// label by hand. Nothing else about column resolution changes — the script
// still finds columns by header text either way.
function unlockHeaderRow() {
  const sheet = SpreadsheetApp.getActiveSheet();
  PropertiesService.getDocumentProperties().setProperty(HEADER_LOCK_PROP, 'true');
  const existing = sheet.getProtections(SpreadsheetApp.ProtectionType.RANGE)
    .filter(function(p) { return p.getDescription() === HEADER_PROTECTION_DESCRIPTION; });
  existing.forEach(function(p) { p.remove(); });
  safeAlert('Header row unlocked — you can edit labels now. Run "Lock Header Row" again once you\'re done, which also takes a fresh snapshot of whatever the headers are at that point.');
}

// Pins Raw Message, Filtered #, and Customer Name together as a fixed
// left-hand sidebar while scrolling right — the same instant "stick"
// behavior the header row already has while scrolling down.
//
// Google Sheets can only freeze a contiguous block starting at column A, so
// this only produces a clean 3-column sidebar when those three headers are
// physically the first three columns in the sheet. If they've been moved
// elsewhere (or something sits between them), this still freezes up through
// the furthest-right of the three — so they stay visible either way — and
// tells you plainly via a toast, rather than either failing silently or
// throwing an error that makes the whole setup look broken.
function pinSidebarColumns_(sheet) {
  try {
    const positions = [CONFIG.RAW_COL, CONFIG.FILTERED_COL, CONFIG.NAME_COL];
    const target = Math.max.apply(null, positions);
    const isCleanSidebar = target === 3 && Math.min.apply(null, positions) === 1;

    sheet.setFrozenColumns(target);

    if (!isCleanSidebar) {
      SpreadsheetApp.getActiveSpreadsheet().toast(
        'Raw Message, Filtered #, and Customer Name aren\'t currently the first three ' +
        'columns, so columns 1–' + target + ' were pinned to keep all three visible. ' +
        'For a clean 3-column sidebar, move those three headers to be the very first ' +
        'columns, then run Initialize Sheet again.',
        'Sidebar note', 8
      );
    }
  } catch (e) {
    // Freezing can occasionally fail (very narrow sheet, transient API hiccup)
    // — that's cosmetic, not a setup failure, so it's reported as a plain
    // notice rather than an alarming error dialog.
    console.error(e);
    SpreadsheetApp.getActiveSpreadsheet().toast(
      'Could not pin the sidebar columns automatically — everything else is set up fine. ' +
      'You can pin them by hand: select columns A–C, then View > Freeze > up to current column.',
      'Sidebar note', 8
    );
  }
}

// Manual, on-demand check — shows exactly which physical column each managed
// field currently resolves to, or the specific missing header(s) if any.
function verifyColumnLayout() {
  const sheet = SpreadsheetApp.getActiveSheet();
  try {
    const resolved = resolveColumns_(sheet);
    const reportCol = findOrCreateReportColumn_(sheet, false);
    const lines = COLUMN_ORDER.map(function(key) {
      return CANONICAL_HEADERS[key] + '  →  column ' + columnLetter_(resolved[key]);
    });
    lines.push('Report  →  ' + (reportCol ? 'column ' + columnLetter_(reportCol) : '(not created yet — will appear once the sheet looks initialized)'));
    safeAlert('All required columns found:\n\n' + lines.join('\n'), 'Column layout OK');
  } catch (e) {
    if (e instanceof ColumnResolutionError_) {
      safeAlert(e.message, 'Column layout problem');
    } else {
      safeAlert('Unexpected error while checking column layout: ' + e.message);
    }
  }
}

// Manual backstop for the link columns — rewrites the WhatsApp Chat and Call
// formula for every row in the managed range, and (re)applies their
// warning-only protection. Use this any time the buttons look "off" after a
// mass paste, sort, or a bulk API edit that bypassed onEdit entirely. The
// self-heal in onEdit should already catch normal day-to-day edits, so this
// is a safety net, not the primary defense.
function repairLinkButtons() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  findOrCreateWhatsAppLinkColumn_(sheet, true);
  findOrCreateCallLinkColumn_(sheet, true);

  const numRows = CONFIG.VALIDATION_LAST_ROW - CONFIG.FIRST_DATA_ROW + 1;
  if (WHATSAPP_LINK_COL) setFormulaColumn_(sheet, WHATSAPP_LINK_COL, numRows, whatsappLinkFormula);
  if (CALL_LINK_COL) setFormulaColumn_(sheet, CALL_LINK_COL, numRows, callLinkFormula);

  protectComputedColumn_(sheet, WHATSAPP_LINK_COL, WHATSAPP_LINK_PROTECTION_DESCRIPTION);
  protectComputedColumn_(sheet, CALL_LINK_COL, CALL_LINK_PROTECTION_DESCRIPTION);

  safeAlert('WhatsApp Chat and Call buttons have been rebuilt and re-protected for every row.');
}

// Shows a confirmation message. Prefers a real dialog when available, falls
// back to a toast when running without a document UI attached (e.g. the
// Apps Script editor's ▶ Run button).
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

const SCHEDULING_STATUS = "Scheduled";
const UNRESOLVED_STATUSES = ["Scheduled", "Pending"];

// DAILY CARRYOVER (additive feature) — every order whose check-up date has
// passed and whose Action is NOT one of these three "truly done" states gets
// its check-up date rolled forward to today. Broad by design: everything in
// STATUS_LIST except these three is treated as still needing a check-up,
// including e.g. Confirmed, Awaiting, Switched Off, Failed, etc.
const CARRYOVER_EXCLUDED_STATUSES = ["Delivered", "Not Picking Calls", "Cancelled"];
const CARRYOVER_NOTE_TEXT = "Automatic Carryover";

// Builds "Automatic Carryover | <existing note>" — appending, not overwriting,
// so an agent's own comment survives. Strips any PRIOR carryover prefix first
// (idempotent): if this same order gets carried over again on a later day
// because it's still unresolved, the tag doesn't stack into "Automatic
// Carryover | Automatic Carryover | ...", it just stays as one clean tag in
// front of whatever the agent has actually written.
function buildCarryoverNote_(existingNote) {
  const text = String(existingNote || '').trim();
  if (!text) return CARRYOVER_NOTE_TEXT;
  if (text === CARRYOVER_NOTE_TEXT) return CARRYOVER_NOTE_TEXT; // was carryover-only already
  const prefixPattern = new RegExp('^' + CARRYOVER_NOTE_TEXT.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\|\\s*');
  const withoutPriorTag = text.replace(prefixPattern, '').trim();
  return withoutPriorTag ? (CARRYOVER_NOTE_TEXT + ' | ' + withoutPriorTag) : CARRYOVER_NOTE_TEXT;
}
const NIGERIA_MOBILE_REGEX = /^\+234[7-9]\d{9}$/;

// Field labels, matched by KEYWORD (word-boundary safe — see labelWordStartsWith_).
const LABELS = [
  { key: 'whatsapp', test: w => labelWordStartsWith_(w, 'whatsapp') },
  { key: 'phone',    test: w => labelWordStartsWith_(w, 'phone') || labelWordStartsWith_(w, 'mobile') || labelWordStartsWith_(w, 'tel') },
  { key: 'address',  test: w => labelWordStartsWith_(w, 'address') || labelWordStartsWith_(w, 'location') },
  { key: 'delivery', test: w => labelWordStartsWith_(w, 'deliver') },
  { key: 'question', test: w => labelWordStartsWith_(w, 'question') || labelWordStartsWith_(w, 'query') },
  { key: 'price',    test: w => labelWordStartsWith_(w, 'price') || labelWordStartsWith_(w, 'amount') || labelWordStartsWith_(w, 'cost') },
  { key: 'product',  test: w => labelWordStartsWith_(w, 'product') || labelWordStartsWith_(w, 'package') || labelWordStartsWith_(w, 'item') },
  { key: 'name',     test: w => labelWordStartsWith_(w, 'name') }
];

function labelWordStartsWith_(words, keyword) {
  return words.some(function(w) { return w.indexOf(keyword) === 0; });
}

const MAX_LABEL_WORDS = 10;
const MAX_LABEL_CHARS = 60;
const BOLD_HEADING_LINE_REGEX = /^\*[\s\S]*\*\.?$/;
const DATE_REGEX = /^\d{1,2}(st|nd|rd|th)?\s+[A-Za-z]+\.?$|^\d{1,2}[\/\-]\d{1,2}([\/\-]\d{2,4})?$/;

/* ---------------- TRIGGERS & MENU ---------------- */

function onOpen() {
  const sheet = SpreadsheetApp.getActiveSheet();
  try {
    findOrCreateReportColumn_(sheet, true);
    findOrCreateWhatsAppLinkColumn_(sheet, true);
    findOrCreateCallLinkColumn_(sheet, true);
    findOrCreateCallAttemptsColumn_(sheet, true);
    refreshHeaderProtectionIfLocked_(sheet); // keeps all bonus columns covered if any were just (re)created on an already-locked sheet
    // Keep the warning-only link-column protections in sync every time the
    // sheet opens — cheap, idempotent, and means a fresh sheet gets
    // protected immediately without needing a manual repair.
    protectComputedColumn_(sheet, WHATSAPP_LINK_COL, WHATSAPP_LINK_PROTECTION_DESCRIPTION);
    protectComputedColumn_(sheet, CALL_LINK_COL, CALL_LINK_PROTECTION_DESCRIPTION);
  } catch (e) { console.error(e); }

  SpreadsheetApp.getUi()
    .createMenu('Order Tools')
    .addItem('Initialize Sheet', 'initializeSheet')
    .addItem('Verify Column Layout', 'verifyColumnLayout')
    .addItem('Lock Header Row', 'lockHeaderRow')
    .addItem('Unlock Header Row (temporary)', 'unlockHeaderRow')
    .addItem('Reprocess Column A', 'reprocessAll')
    .addItem('Refresh Summary Report', 'buildSummaryReport')
    .addItem('Repair Link Buttons', 'repairLinkButtons')
    .addItem('Call Button Setup (Android Fix)', 'showCallRedirectSetupInstructions')
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
    .addItem('Enable Daily Carryover', 'enableDailyCarryover')
    .addItem('Disable Daily Carryover', 'disableDailyCarryover')
    .addItem('Run Daily Carryover Now (test)', 'runDailyCarryoverManual')
    .addItem('Enable Structure Change Alerts', 'enableStructureChangeAlerts')
    .addItem('Disable Structure Change Alerts', 'disableStructureChangeAlerts')
    .addSeparator()
    .addItem('Archive Old Resolved Orders', 'archiveOldOrders')
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

    // HEADER ROW GUARD — checked FIRST, before column resolution, because an
    // edit to row 1 might be exactly what would break resolveColumns_() in
    // the first place. CONFIG.HEADER_ROW is a fixed constant (always 1), not
    // something resolved dynamically, so this check is always safe to run.
    if (row === CONFIG.HEADER_ROW) {
      guardHeaderRowEdit_(sheet, e);
      return;
    }

    // Resolve every column's REAL current position before doing anything
    // else. This is what makes the whole script immune to a column being
    // inserted, deleted, or dragged elsewhere in the sheet. If a required
    // header is missing, stop immediately instead of writing to a guess.
    try {
      resolveColumns_(sheet);
    } catch (colErr) {
      if (colErr instanceof ColumnResolutionError_) {
        SpreadsheetApp.getActiveSpreadsheet().toast(colErr.message, 'Column layout problem', 10);
        return;
      }
      throw colErr;
    }
    // Lookup-only (no creation mid-edit) — these should already exist from
    // onOpen; resolving them here just populates the globals for this
    // specific execution so downstream functions (applyRowValidation, etc.)
    // have the right column numbers.
    findOrCreateWhatsAppLinkColumn_(sheet, false);
    findOrCreateCallLinkColumn_(sheet, false);
    findOrCreateCallAttemptsColumn_(sheet, false);

    // SELF-HEAL: WhatsApp Chat and Call are computed-only columns. Any edit
    // landing anywhere in either column — a stray keypress, a paste, a
    // drag-fill that dragged a formula sideways, a value pasted over a whole
    // block — is immediately overwritten back to the correct working
    // formula for every affected row. This runs BEFORE the row>=... cases
    // below and returns, since there is nothing else these columns should
    // ever do on edit.
    if (row >= CONFIG.FIRST_DATA_ROW &&
        ((WHATSAPP_LINK_COL && col <= WHATSAPP_LINK_COL && WHATSAPP_LINK_COL <= e.range.getLastColumn()) ||
         (CALL_LINK_COL && col <= CALL_LINK_COL && CALL_LINK_COL <= e.range.getLastColumn()))) {
      const startRow = Math.max(e.range.getRow(), CONFIG.FIRST_DATA_ROW);
      const endRow = Math.min(e.range.getLastRow(), CONFIG.VALIDATION_LAST_ROW);
      for (let rr = startRow; rr <= endRow; rr++) {
        if (WHATSAPP_LINK_COL && col <= WHATSAPP_LINK_COL && WHATSAPP_LINK_COL <= e.range.getLastColumn()) {
          healLinkCell_(sheet, rr, WHATSAPP_LINK_COL, whatsappLinkFormula);
        }
        if (CALL_LINK_COL && col <= CALL_LINK_COL && CALL_LINK_COL <= e.range.getLastColumn()) {
          healLinkCell_(sheet, rr, CALL_LINK_COL, callLinkFormula);
        }
      }
      return;
    }

    // Case 1: raw message pasted into column A
    if (col === CONFIG.RAW_COL && row >= CONFIG.FIRST_DATA_ROW) {
      if (e.range.getNumRows() > 1 || e.range.getNumColumns() > 1) return; // single-cell only
      const raw = sheet.getRange(row, CONFIG.RAW_COL).getValue();
      if (raw && String(raw).trim()) processRow(sheet, row, String(raw));
      return;
    }

    // Case 2: Action dropdown changed to "Scheduled" -> jump to check-up date/time.
    // ALSO (additive): if changed TO a failed-contact status (see
    // CALL_ATTEMPT_TRIGGER_STATUSES), auto-increment Call Attempts for this
    // row. Touches ONLY the Call Attempts cell — nothing else on the row.
    // Guarded by e.oldValue so re-selecting the same status twice in a row
    // (e.g. an accidental double-tap) doesn't double-count.
    if (col === CONFIG.ACTION_COL && row >= CONFIG.FIRST_DATA_ROW) {
      const value = e.range.getValue();
      if (value === SCHEDULING_STATUS) {
        const checkupCell = sheet.getRange(row, CONFIG.CHECKUP_COL);
        checkupCell.activate();
        SpreadsheetApp.getActiveSpreadsheet().toast('Pick the check-up date & time.', 'Scheduled order', 5);
      }

      if (CALL_ATTEMPTS_COL && CALL_ATTEMPT_TRIGGER_STATUSES.indexOf(value) !== -1 && e.oldValue !== value) {
        const attemptsCell = sheet.getRange(row, CALL_ATTEMPTS_COL);
        const current = attemptsCell.getValue();
        const currentNum = (typeof current === 'number') ? current : 0;
        attemptsCell.setValue(currentNum + 1);
        SpreadsheetApp.getActiveSpreadsheet().toast('Call attempt #' + (currentNum + 1) + ' logged for this order.', 'Call Attempts', 4);
      }
      return;
    }

    // Case 3: "GR" typed into the Report column -> generate a delivery report.
    // Report column is located dynamically by header text, never a fixed number.
    const reportCol = findOrCreateReportColumn_(sheet, false);
    if (reportCol && col === reportCol && row >= CONFIG.FIRST_DATA_ROW) {
      if (e.range.getNumRows() !== 1 || e.range.getNumColumns() !== 1) return; // single-cell only
      const typedValue = e.value;
      if (typeof typedValue !== 'string' || typedValue.trim().toUpperCase() !== REPORT_TRIGGER_VALUE) return;
      const reportText = generateDeliveryReport_(sheet, row);
      e.range.setValue(reportText).setWrap(true).setVerticalAlignment('top');
      return;
    }

    // Case 4: Phone Number edited directly by an agent (correcting a typo,
    // etc.) -> refresh the Call (and WhatsApp Chat, if it's falling back to
    // Phone Number) link for that row immediately, rather than waiting for
    // the next full reprocess.
    if (col === CONFIG.PHONE_COL && row >= CONFIG.FIRST_DATA_ROW) {
      if (e.range.getNumRows() !== 1 || e.range.getNumColumns() !== 1) return; // single-cell only
      if (CALL_LINK_COL) healLinkCell_(sheet, row, CALL_LINK_COL, callLinkFormula);
      if (WHATSAPP_LINK_COL) healLinkCell_(sheet, row, WHATSAPP_LINK_COL, whatsappLinkFormula);
      return;
    }
  } catch (err) {
    console.error(err);
    // Something unexpected (not a known column-layout problem, which is
    // already handled above) — surface it as a calm toast rather than
    // letting the edit silently fail with no feedback at all.
    try {
      SpreadsheetApp.getActiveSpreadsheet().toast(
        'That edit didn\'t process cleanly. Nothing should be corrupted — check ' +
        'Executions (clock icon) in the Apps Script editor for details, or just try again.',
        'Something went wrong', 6
      );
    } catch (toastErr) { /* no UI available; already logged above */ }
  }
}

/* ---------------- STRUCTURE CHANGE ALERTS ---------------- */
// An installable onChange trigger — fires the moment a column (or row/sheet)
// is inserted or removed anywhere in the spreadsheet, so you find out right
// away instead of noticing days later that data has scattered.

function enableStructureChangeAlerts() {
  removeStructureChangeTriggers_();
  ScriptApp.newTrigger('onStructureChange_')
    .forSpreadsheet(SpreadsheetApp.getActive())
    .onChange()
    .create();
  safeAlert('Structure change alerts are ON. You\'ll get a toast if a column is inserted or removed anywhere in this spreadsheet.');
}

function disableStructureChangeAlerts() {
  removeStructureChangeTriggers_();
  safeAlert('Structure change alerts are OFF.');
}

function removeStructureChangeTriggers_() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'onStructureChange_') ScriptApp.deleteTrigger(t);
  });
}

function onStructureChange_(e) {
  try {
    if (!e) return;
    const structural = ['INSERT_COLUMN', 'REMOVE_COLUMN', 'INSERT_GRID', 'REMOVE_GRID'];
    if (structural.indexOf(e.changeType) !== -1) {
      SpreadsheetApp.getActiveSpreadsheet().toast(
        'Sheet structure changed (' + e.changeType + '). Run Order Tools > Verify Column Layout to confirm everything still lines up.',
        'Structure change detected', 8
      );
    }
  } catch (err) {
    console.error(err);
  }
}

/* ---------------- GENERATE REPORT ---------------- */

const REPORT_TRIGGER_VALUE = 'GR'; // case-insensitive
const REPORT_MONTH_NAMES = ['January','February','March','April','May','June',
  'July','August','September','October','November','December'];

const REPORT_PRODUCT_PRICE_SUFFIX_REGEX = /\s*—\s*₦[\d,]+(?:\.\d+)?\s*$/;
const REPORT_MARKETING_PHRASE_REGEX = /free\s+(?:doorstep|home)\s+delivery|free\s+delivery/gi;
const REPORT_FREE_WORD_REGEX = /\bfree\b/gi;
const REPORT_DOORSTEP_DELIVERY_REGEX = /doorstep\s+delivery/i;

function cleanPackageForReport_(packageText) {
  let text = String(packageText || '');
  text = text.replace(REPORT_MARKETING_PHRASE_REGEX, '');
  text = text.replace(REPORT_FREE_WORD_REGEX, '');
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

// Reads each field by its OWN resolved column — no longer assumes Name
// through Address sit in one contiguous block, so a column inserted or
// reordered inside that span can't misalign this read anymore.
function generateDeliveryReport_(sheet, row) {
  const rawProduct = sheet.getRange(row, CONFIG.PRODUCT_COL).getValue();
  const rawPrice = sheet.getRange(row, CONFIG.PRICE_COL).getValue();
  const rawName = sheet.getRange(row, CONFIG.NAME_COL).getValue();
  const rawAddress = sheet.getRange(row, CONFIG.ADDRESS_COL).getValue();

  const isEmpty = function(val) {
    return val === undefined || val === null || String(val).trim() === '';
  };

  if (isEmpty(rawProduct) || isEmpty(rawPrice) || isEmpty(rawName) || isEmpty(rawAddress)) {
    return 'An empty required field was detected. Please ensure the following columns are filled:\n' +
      '- Product (column ' + columnLetter_(CONFIG.PRODUCT_COL) + ')\n' +
      '- Price (column ' + columnLetter_(CONFIG.PRICE_COL) + ')\n' +
      '- Name (column ' + columnLetter_(CONFIG.NAME_COL) + ')\n' +
      '- Address (column ' + columnLetter_(CONFIG.ADDRESS_COL) + ')';
  }

  const name = rawName;
  const phone = sheet.getRange(row, CONFIG.PHONE_COL).getValue();
  const whatsapp = sheet.getRange(row, CONFIG.WHATSAPP_COL).getValue();
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
  try {
    initializeSheet_impl();
  } catch (e) {
    if (e instanceof ColumnResolutionError_) { safeAlert(e.message, 'Column layout problem'); return; }
    console.error(e);
    safeAlert(
      'Setup hit an unexpected snag: ' + e.message + '\n\nAny steps completed before this ' +
      'point are fine — check Executions (clock icon) in the Apps Script editor for the full ' +
      'trace, or try Initialize Sheet again.',
      'Unexpected error'
    );
  }
}

function initializeSheet_impl() {
  const sheet = SpreadsheetApp.getActiveSheet();

  const headerRange = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, CONFIG.HEADERS.length);
  headerRange.setValues([CONFIG.HEADERS]);
  headerRange.setFontWeight('bold').setBackground('#4a86e8').setFontColor('#ffffff');
  sheet.setFrozenRows(1);

  // Now that the canonical headers are freshly written in their default
  // order, sync CONFIG to match (should equal the defaults, but this keeps
  // one single code path instead of two). Report column MUST be created
  // before protection is applied, or it's left outside the locked range —
  // this ordering is what fixes that gap.
  resolveColumns_(sheet);
  findOrCreateReportColumn_(sheet, true);
  findOrCreateWhatsAppLinkColumn_(sheet, true);
  findOrCreateCallLinkColumn_(sheet, true);
  findOrCreateCallAttemptsColumn_(sheet, true);
  protectHeaderRow_(sheet);
  snapshotHeaderRow_(sheet);
  PropertiesService.getDocumentProperties().setProperty(HEADER_LOCK_PROP, 'false');
  pinSidebarColumns_(sheet); // Raw Message, Filtered #, Customer Name — dynamic, degrades gracefully

  sheet.setColumnWidth(CONFIG.RAW_COL, 100);

  const numRows = CONFIG.VALIDATION_LAST_ROW - CONFIG.FIRST_DATA_ROW + 1;

  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.PHONE_COL, numRows, 1).clearDataValidations();
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.PHONE_COL, numRows, 1).setNumberFormat('@');
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.WHATSAPP_COL, numRows, 1).clearDataValidations();
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.WHATSAPP_COL, numRows, 1).setNumberFormat('@');

  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.PRICE_COL, numRows, 1).setNumberFormat('"₦"#,##0');
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.DATE_COL, numRows, 1).setNumberFormat(CONFIG.ORDER_DATE_FORMAT);

  const statusRule = SpreadsheetApp.newDataValidation()
    .requireValueInList(STATUS_LIST, true)
    .setAllowInvalid(false)
    .build();
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.ACTION_COL, numRows, 1).setDataValidation(statusRule);

  const dateRule = SpreadsheetApp.newDataValidation().requireDate().setAllowInvalid(true).build();
  const checkupRange = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.CHECKUP_COL, numRows, 1);
  checkupRange.setDataValidation(dateRule);
  checkupRange.setNumberFormat('yyyy-mm-dd hh:mm');

  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.DUPLICATE_COL, numRows, 1).insertCheckboxes();
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.INVALID_COL, numRows, 1).insertCheckboxes();

  setFormulaColumn_(sheet, CONFIG.FILTERED_COL, numRows, filteredSeqFormula);
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.TODAY_COL, numRows, 1).insertCheckboxes();
  setFormulaColumn_(sheet, CONFIG.TODAY_COL, numRows, todayCheckupFormula);
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.FLAGGED_COL, numRows, 1).insertCheckboxes();
  setFormulaColumn_(sheet, CONFIG.FLAGGED_COL, numRows, flaggedFormula);
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.OVERDUE_COL, numRows, 1).insertCheckboxes();
  setFormulaColumn_(sheet, CONFIG.OVERDUE_COL, numRows, overdueFormula);
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.TODAY_ORDER_COL, numRows, 1).insertCheckboxes();
  setFormulaColumn_(sheet, CONFIG.TODAY_ORDER_COL, numRows, todaysOrderFormula);

  if (WHATSAPP_LINK_COL) setFormulaColumn_(sheet, WHATSAPP_LINK_COL, numRows, whatsappLinkFormula);
  if (CALL_LINK_COL) setFormulaColumn_(sheet, CALL_LINK_COL, numRows, callLinkFormula);
  if (CALL_ATTEMPTS_COL) sheet.getRange(CONFIG.FIRST_DATA_ROW, CALL_ATTEMPTS_COL, numRows, 1).setNumberFormat('0');

  // Apply the warning-only protections on the link columns right after
  // (re)building their formulas, same pattern as the header row protection
  // just above.
  protectComputedColumn_(sheet, WHATSAPP_LINK_COL, WHATSAPP_LINK_PROTECTION_DESCRIPTION);
  protectComputedColumn_(sheet, CALL_LINK_COL, CALL_LINK_PROTECTION_DESCRIPTION);

  const existingFilter = sheet.getFilter();
  if (existingFilter) existingFilter.remove();
  sheet.getRange(CONFIG.HEADER_ROW, 1, CONFIG.VALIDATION_LAST_ROW, getManagedColumnCount_(sheet)).createFilter();

  applyConditionalFormatting(sheet);
  buildSummaryReport();

  safeAlert('Sheet initialized. Paste WhatsApp messages into column A, starting row 2.\n\nHeader row (including Report) is now LOCKED for everyone, including you — any edit to row 1 is reverted immediately and blocked with an error the moment you leave the cell. Use Order Tools > Unlock Header Row first if you ever need to change a label, and Verify Column Layout any time to see the live mapping.\n\nWhatsApp Chat and Call buttons now self-heal automatically if anything is ever typed or pasted into them, and both are also warning-protected.');
}

// Small helper: writes the same per-row formula down a whole column in one
// batched call, given a formula-builder function like filteredSeqFormula.
function setFormulaColumn_(sheet, col, numRows, formulaFn) {
  const formulas = [];
  for (let r = CONFIG.FIRST_DATA_ROW; r <= CONFIG.FIRST_DATA_ROW + numRows - 1; r++) {
    formulas.push([formulaFn(r)]);
  }
  sheet.getRange(CONFIG.FIRST_DATA_ROW, col, numRows, 1).setFormulas(formulas);
}

function reprocessAll() {
  try {
    reprocessAll_impl();
  } catch (e) {
    console.error(e);
    safeAlert(
      'Reprocessing hit an unexpected snag: ' + e.message + '\n\nRows already reprocessed before ' +
      'this point are fine — check Executions (clock icon) in the Apps Script editor for the full ' +
      'trace, or try again.',
      'Unexpected error'
    );
  }
}

function reprocessAll_impl() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  findOrCreateWhatsAppLinkColumn_(sheet, false);
  findOrCreateCallLinkColumn_(sheet, false);
  findOrCreateCallAttemptsColumn_(sheet, false);
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

// Added Pending (orange), Awaiting (purple), Not Picking Calls (indigo), and
// a dedicated Cancelled rule (red). Overdue/Delivered/Cash Remitted logic is
// untouched. Deleted and Banned stay gray. Cancelled is placed BEFORE the
// grey closed-out rule so it wins (shows red) instead of falling into the
// grey group Cancelled used to share with Deleted/Banned.
function applyConditionalFormatting(sheet) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  try { resolveColumns_(sheet); } catch (e) { console.error(e); return; }

  const dataRange = sheet.getRange(CONFIG.FIRST_DATA_ROW, 1, CONFIG.VALIDATION_LAST_ROW - CONFIG.FIRST_DATA_ROW + 1, getManagedColumnCount_(sheet));
  const overdueCol = columnLetter_(CONFIG.OVERDUE_COL);
  const actionCol = columnLetter_(CONFIG.ACTION_COL);
  const r0 = CONFIG.FIRST_DATA_ROW;

  const overdueRule = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=$' + overdueCol + r0 + '=TRUE')
    .setBackground('#f4cccc')
    .setRanges([dataRange])
    .build();

  const cancelledRule = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=$' + actionCol + r0 + '="Cancelled"')
    .setBackground('#EA9999').setFontColor('#660000').setBold(true)
    .setRanges([dataRange])
    .build();

  const pendingRule = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=$' + actionCol + r0 + '="Pending"')
    .setBackground('#FCE5CD').setFontColor('#B45F06').setBold(true)
    .setRanges([dataRange])
    .build();

  const awaitingRule = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=$' + actionCol + r0 + '="Awaiting"')
    .setBackground('#D9D2E9').setFontColor('#351C75').setBold(true)
    .setRanges([dataRange])
    .build();

  const notPickingRule = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=$' + actionCol + r0 + '="Not Picking Calls"')
    .setBackground('#C5CAE9').setFontColor('#283593').setBold(true)
    .setRanges([dataRange])
    .build();

  const deliveredRule = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=OR($' + actionCol + r0 + '="Delivered",$' + actionCol + r0 + '="Cash Remitted")')
    .setBackground('#d9ead3')
    .setRanges([dataRange])
    .build();

  const closedOutRule = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=OR($' + actionCol + r0 + '="Deleted",$' + actionCol + r0 + '="Banned")')
    .setBackground('#efefef')
    .setRanges([dataRange])
    .build();

  sheet.setConditionalFormatRules([
    overdueRule, cancelledRule, deliveredRule, closedOutRule,
    pendingRule, awaitingRule, notPickingRule
  ]);
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

function buildSummaryReport() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;

  const d0 = CONFIG.FIRST_DATA_ROW;
  const d1 = CONFIG.VALIDATION_LAST_ROW;
  const startRow = CONFIG.VALIDATION_LAST_ROW + 2;

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
  sheet.getRange(startRow + rows.length - 1, 2).setNumberFormat('0%');

  removeChartByTitle_(sheet, 'Order Summary');

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
  sheet = sheet || SpreadsheetApp.getActiveSheet();
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

function scheduleToday()    { scheduleQuick(0); }
function scheduleTomorrow() { scheduleQuick(1); }
function scheduleIn2Days()  { scheduleQuick(2); }
function scheduleInWeek()   { scheduleQuick(7); }

function scheduleQuick(daysFromNow) {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;

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

// These four formulas build column LETTERS fresh from the resolved CONFIG
// column every time a row's formula is written, so a column shift can never
// silently break them.

function todayCheckupFormula(row) {
  const h = columnLetter_(CONFIG.CHECKUP_COL);
  return '=IFERROR(IF($' + h + row + '="",FALSE,INT($' + h + row + ')=TODAY()),FALSE)';
}

function flaggedFormula(row) {
  const p = columnLetter_(CONFIG.DUPLICATE_COL);
  const q = columnLetter_(CONFIG.INVALID_COL);
  return '=IFERROR(OR($' + p + row + ',$' + q + row + '),FALSE)';
}

function overdueFormula(row) {
  const h = columnLetter_(CONFIG.CHECKUP_COL);
  const g = columnLetter_(CONFIG.ACTION_COL);
  return '=IFERROR(AND($' + h + row + '<>"",INT($' + h + row + ')<TODAY(),OR($' + g + row + '="Scheduled",$' + g + row + '="Pending")),FALSE)';
}

function todaysOrderFormula(row) {
  const i = columnLetter_(CONFIG.DATE_COL);
  return '=IFERROR(IF($' + i + row + '="",FALSE,INT($' + i + row + ')=TODAY()),FALSE)';
}

function showTodayCheckups()   { const s = SpreadsheetApp.getActiveSheet(); if (!requireColumns_(s)) return; createFilterViewForColumn_('checkup_for_today', CONFIG.TODAY_COL); }
function clearTodayFilter()    { removeFilterViewByTitle_('checkup_for_today'); toastFilterCleared_('checkup_for_today'); }

function showOverdueCheckups() { const s = SpreadsheetApp.getActiveSheet(); if (!requireColumns_(s)) return; createFilterViewForColumn_('overdue_checkups', CONFIG.OVERDUE_COL); }
function clearOverdueFilter()  { removeFilterViewByTitle_('overdue_checkups'); toastFilterCleared_('overdue_checkups'); }

function showFlaggedOrders()   { const s = SpreadsheetApp.getActiveSheet(); if (!requireColumns_(s)) return; createFilterViewForColumn_('flagged_orders', CONFIG.FLAGGED_COL); }
function clearFlaggedFilter()  { removeFilterViewByTitle_('flagged_orders'); toastFilterCleared_('flagged_orders'); }

function showTodaysOrders()        { const s = SpreadsheetApp.getActiveSheet(); if (!requireColumns_(s)) return; createFilterViewForColumn_('todays_orders', CONFIG.TODAY_ORDER_COL); }
function clearTodaysOrdersFilter() { removeFilterViewByTitle_('todays_orders'); toastFilterCleared_('todays_orders'); }

function showOrdersForDate() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;

  let ui;
  try {
    ui = SpreadsheetApp.getUi();
  } catch (e) {
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
    filter = sheet.getRange(CONFIG.HEADER_ROW, 1, CONFIG.VALIDATION_LAST_ROW, getManagedColumnCount_(sheet)).createFilter();
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
  if (!requireColumns_(sheet)) return;
  const filter = sheet.getFilter();
  if (filter) filter.removeColumnFilterCriteria(CONFIG.DATE_COL);
  SpreadsheetApp.getActiveSpreadsheet().toast('Date filter cleared — showing all orders.', 'Cleared', 4);
}

function toastFilterCleared_(title) {
  SpreadsheetApp.getActiveSpreadsheet().toast('"' + title + '" filter view removed.', 'Cleared', 4);
}

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
            endColumnIndex: getManagedColumnCount_(sheet)
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

function checkForUpcomingCheckups() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheetName = PropertiesService.getScriptProperties().getProperty('REMINDER_SHEET_NAME');
  const sheet = sheetName ? ss.getSheetByName(sheetName) : ss.getActiveSheet();
  if (!sheet) return;
  try { resolveColumns_(sheet); } catch (e) { console.error(e); return; } // silent — no UI in a background trigger

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
    if (UNRESOLVED_STATUSES.indexOf(actions[i][0]) === -1) continue;

    const minutesUntil = (checkupVal.getTime() - now.getTime()) / 60000;
    if (minutesUntil >= 0 && minutesUntil <= CONFIG.REMINDER_WINDOW_MINUTES) {
      dueSoon.push((names[i][0] || 'Unnamed') + ' — ' + checkupVal.toLocaleTimeString());
    }
  }

  if (dueSoon.length > 0) {
    ss.toast('Check-up due soon:\n' + dueSoon.join('\n'), 'Upcoming check-up', 8);
  }
}

/* ---------------- DAILY CARRYOVER (additive feature) ---------------- */
// Every morning, any order whose check-up date has already passed AND whose
// Action is NOT Delivered / Not Picking Calls / Cancelled gets its check-up
// date moved to today (same time of day kept, only the date changes), and
// its Note field overwritten with "Automatic Carryover" so you can filter on
// that text and see exactly what got auto-rescheduled.
//
// Self-healing by design: this compares against "today" every run, not
// "yesterday" specifically — so if a morning is ever missed, the next run
// still catches everything still overdue in one pass, rather than only
// picking up the most recent day. Once caught up, it stays caught up daily.
//
// Deliberately isolated from everything else: reads/writes only CHECKUP_COL
// and NOTE_COL, never touches Action, Duplicate/Invalid flags, S/N, or any
// parsing logic. Runs via its own installable trigger — completely separate
// from onEdit, the parser, and the check-up reminder trigger above.
//
// PLATFORM LIMIT (cannot be engineered around): Apps Script daily triggers
// don't guarantee an exact minute. atHour(0) fires SOMEWHERE in the
// 12:00–1:00 AM window, not precisely at 12:01 — this is a Google Apps
// Script limitation, not a bug in this code.

function enableDailyCarryover() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  PropertiesService.getScriptProperties().setProperty('CARRYOVER_SHEET_NAME', sheet.getName());
  removeDailyCarryoverTriggers_();
  ScriptApp.newTrigger('runDailyCarryover')
    .timeBased()
    .atHour(0)
    .nearMinute(1)
    .everyDays(1)
    .create();
  safeAlert(
    'Daily Carryover is ON for "' + sheet.getName() + '".\n\n' +
    'Every day, as close to 12:01 AM as Apps Script allows (Google only guarantees the ' +
    '12:00–1:00 AM window for daily triggers, not an exact minute), any order whose check-up ' +
    'date has passed and whose status is NOT Delivered, Not Picking Calls, or Cancelled will have ' +
    'its check-up date moved to today (same time of day) and "' + CARRYOVER_NOTE_TEXT + '" added to ' +
    'the front of its Note — whatever the agent already wrote stays, just after the tag — so you can ' +
    'filter on "' + CARRYOVER_NOTE_TEXT + '" to see exactly what was auto-rescheduled.\n\n' +
    'Use "Run Daily Carryover Now (test)" to see it work immediately without waiting for midnight.'
  );
}

function disableDailyCarryover() {
  removeDailyCarryoverTriggers_();
  safeAlert('Daily Carryover is OFF.');
}

function removeDailyCarryoverTriggers_() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'runDailyCarryover') ScriptApp.deleteTrigger(t);
  });
}

// Manual menu action — runs the exact same logic as the overnight trigger,
// immediately, so you can verify it works without waiting for midnight.
// Shows a summary alert (the real trigger runs silently, since no one's
// watching at 12am — see runDailyCarryover below).
function runDailyCarryoverManual() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  const count = runDailyCarryover_impl(sheet);
  safeAlert(
    count > 0
      ? 'Carried over ' + count + ' order(s) — check-up date moved to today, "' + CARRYOVER_NOTE_TEXT + '" prepended to each Note.'
      : 'Nothing to carry over right now — no overdue orders outside Delivered / Not Picking Calls / Cancelled.'
  );
}

// The actual overnight trigger target — deliberately silent (no safeAlert),
// since no one is watching the sheet at 12am. Logs to Executions for later
// review; each affected row is also visibly tagged in the Note column.
function runDailyCarryover() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheetName = PropertiesService.getScriptProperties().getProperty('CARRYOVER_SHEET_NAME');
  const sheet = sheetName ? ss.getSheetByName(sheetName) : ss.getActiveSheet();
  if (!sheet) { console.error('Daily Carryover: no target sheet found.'); return; }

  try {
    const count = runDailyCarryover_impl(sheet);
    console.log('Daily Carryover: rescheduled ' + count + ' order(s) to today.');
  } catch (e) {
    console.error('Daily Carryover failed: ' + e.message);
  }
}

// Shared logic used by both the manual test action and the silent overnight
// trigger. Returns the number of rows carried over.
function runDailyCarryover_impl(sheet) {
  try { resolveColumns_(sheet); } catch (e) { console.error(e); return 0; }

  const lastRow = Math.min(sheet.getLastRow(), CONFIG.VALIDATION_LAST_ROW);
  if (lastRow < CONFIG.FIRST_DATA_ROW) return 0;
  const numRows = lastRow - CONFIG.FIRST_DATA_ROW + 1;

  const names = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.NAME_COL, numRows, 1).getValues();
  const checkups = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.CHECKUP_COL, numRows, 1).getValues();
  const actions = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.ACTION_COL, numRows, 1).getValues();
  const notes = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.NOTE_COL, numRows, 1).getValues();

  const today = stripTime_(new Date());
  let carriedCount = 0;

  for (let i = 0; i < numRows; i++) {
    const row = CONFIG.FIRST_DATA_ROW + i;
    const name = names[i][0];
    const checkupVal = checkups[i][0];
    const action = actions[i][0];

    if (!name || !String(name).trim()) continue;                    // not a real order row
    if (!(checkupVal instanceof Date)) continue;                     // nothing scheduled to carry over
    if (CARRYOVER_EXCLUDED_STATUSES.indexOf(action) !== -1) continue; // Delivered / Not Picking Calls / Cancelled — leave alone

    const checkupDateOnly = stripTime_(checkupVal);
    if (checkupDateOnly.getTime() >= today.getTime()) continue;      // today or future already — not overdue, leave alone

    const newCheckup = new Date(
      today.getFullYear(), today.getMonth(), today.getDate(),
      checkupVal.getHours(), checkupVal.getMinutes(), checkupVal.getSeconds()
    );

    sheet.getRange(row, CONFIG.CHECKUP_COL).setValue(newCheckup);
    sheet.getRange(row, CONFIG.NOTE_COL).setValue(buildCarryoverNote_(notes[i][0]));
    carriedCount++;
  }

  return carriedCount;
}

/* ---------------- ARCHIVE OLD RESOLVED ORDERS (additive feature) ---------------- */
// Manual-only, confirmation-gated — this never runs unattended, unlike Daily
// Carryover, because it's a bigger operation (moving data between sheets).
//
// CRITICAL DESIGN CHOICE: this does NOT use sheet.deleteRow(). Deleting rows
// would shift every row below it up — including the ORDER SUMMARY block and
// chart, which are written at a FIXED position (VALIDATION_LAST_ROW + 2).
// Instead, matching rows are copied to an "Archive" tab, then CLEARED
// (contents only) in place and re-validated via applyRowValidation() — so
// the row count, the Summary block's position, and all pre-applied
// dropdown/checkbox/formula validation stay exactly as Initialize Sheet
// built them. Cleared rows become open capacity: paste a new order directly
// into any of them, same as any other row.

const ARCHIVE_SHEET_NAME = 'Archive';
// Configurable — edit this array directly if your definition of "resolved
// enough to archive" should include/exclude different statuses.
const ARCHIVE_ELIGIBLE_STATUSES = ['Delivered', 'Cash Remitted', 'Cancelled', 'Deleted', 'Banned'];
// Configurable — orders must be BOTH in an eligible status AND have an order
// date at least this many days in the past before they're archived.
const ARCHIVE_AFTER_DAYS = 30;

function getOrCreateArchiveSheet_(mainSheet) {
  const ss = mainSheet.getParent();
  let archiveSheet = ss.getSheetByName(ARCHIVE_SHEET_NAME);
  if (!archiveSheet) {
    archiveSheet = ss.insertSheet(ARCHIVE_SHEET_NAME);
    const width = getManagedColumnCount_(mainSheet);
    mainSheet.getRange(CONFIG.HEADER_ROW, 1, 1, width)
      .copyTo(archiveSheet.getRange(1, 1, 1, width)); // mirrors exact header labels + formatting
    archiveSheet.setFrozenRows(1);
  }
  return archiveSheet;
}

function archiveOldOrders() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  findOrCreateWhatsAppLinkColumn_(sheet, false);
  findOrCreateCallLinkColumn_(sheet, false);
  findOrCreateCallAttemptsColumn_(sheet, false);

  const lastRow = Math.min(sheet.getLastRow(), CONFIG.VALIDATION_LAST_ROW);
  if (lastRow < CONFIG.FIRST_DATA_ROW) { safeAlert('No data rows found.'); return; }
  const numRows = lastRow - CONFIG.FIRST_DATA_ROW + 1;

  const names = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.NAME_COL, numRows, 1).getValues();
  const actions = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.ACTION_COL, numRows, 1).getValues();
  const dates = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.DATE_COL, numRows, 1).getValues();

  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - ARCHIVE_AFTER_DAYS);
  const cutoffDateOnly = stripTime_(cutoff);

  const eligibleRows = [];
  for (let i = 0; i < numRows; i++) {
    const row = CONFIG.FIRST_DATA_ROW + i;
    const name = names[i][0];
    const action = actions[i][0];
    const dateVal = dates[i][0];
    if (!name || !String(name).trim()) continue;                         // not a real order row
    if (ARCHIVE_ELIGIBLE_STATUSES.indexOf(action) === -1) continue;       // not a "done" status
    if (!(dateVal instanceof Date)) continue;                             // no order date to compare
    if (stripTime_(dateVal).getTime() > cutoffDateOnly.getTime()) continue; // not old enough yet
    eligibleRows.push(row);
  }

  if (eligibleRows.length === 0) {
    safeAlert(
      'Nothing eligible to archive right now. An order needs status in [' +
      ARCHIVE_ELIGIBLE_STATUSES.join(', ') + '] AND an order date older than ' +
      ARCHIVE_AFTER_DAYS + ' days.'
    );
    return;
  }

  let ui;
  try {
    ui = SpreadsheetApp.getUi();
  } catch (e) {
    SpreadsheetApp.getActiveSpreadsheet().toast(
      'Run Archive Old Resolved Orders from the sheet itself — it needs to show a confirmation dialog.',
      'Needs the sheet open', 8
    );
    return;
  }

  const response = ui.alert(
    'Archive ' + eligibleRows.length + ' order(s)?',
    eligibleRows.length + ' order(s) match [' + ARCHIVE_ELIGIBLE_STATUSES.join(', ') + '] and are older ' +
    'than ' + ARCHIVE_AFTER_DAYS + ' days. They will be copied to the "' + ARCHIVE_SHEET_NAME + '" tab, ' +
    'then cleared and re-validated in this sheet — ready for a new order to be pasted straight into them. ' +
    'Rows are NOT deleted, so the Order Summary block and chart below row ' + CONFIG.VALIDATION_LAST_ROW +
    ' never shift position.',
    ui.ButtonSet.YES_NO
  );
  if (response !== ui.Button.YES) return;

  const archiveSheet = getOrCreateArchiveSheet_(sheet);
  const width = getManagedColumnCount_(sheet);
  let destRow = archiveSheet.getLastRow() + 1;

  // Phase 1: copy everything first (formulas + values, via copyTo — this
  // preserves the WhatsApp Chat / Call formulas correctly, since a plain
  // getValues() read would only capture their display text, not the
  // underlying formula).
  eligibleRows.forEach(function(row) {
    sheet.getRange(row, 1, 1, width).copyTo(archiveSheet.getRange(destRow, 1, 1, width));
    destRow++;
  });

  // Phase 2: only after every row is safely copied, clear and re-validate
  // the originals. Splitting into two phases means a failure partway through
  // copying never results in data being cleared without a backup existing.
  eligibleRows.forEach(function(row) {
    sheet.getRange(row, 1, 1, width).clearContent();
    applyRowValidation(sheet, row); // restores dropdown/checkboxes/live formulas so the row is ready for reuse
  });

  safeAlert(
    'Archived ' + eligibleRows.length + ' order(s) to the "' + ARCHIVE_SHEET_NAME + '" tab. ' +
    'Those rows in this sheet are now cleared and ready for new orders.'
  );
}

/* ---------------- CORE PROCESSING ---------------- */

function processRow(sheet, row, rawText) {
  sheet = sheet || SpreadsheetApp.getActiveSheet(); // guards against running this directly from the editor's function dropdown
  if (!row) {
    safeAlert('processRow needs a row — run this from the sheet (paste into column A) rather than the ▶ Run button.');
    return;
  }
  const blocks = splitIntoBlocks(rawText);
  if (blocks.length === 0) return;

  const scanData = loadPasteScanData_(sheet);

  writeRecord(sheet, row, blocks[0], scanData);

  for (let i = 1; i < blocks.length; i++) {
    const insertedAfterRow = row + i - 1;
    sheet.insertRowAfter(insertedAfterRow);

    scanData.rows.forEach(function(r) {
      if (r.row > insertedAfterRow) r.row++;
    });

    applyRowValidation(sheet, row + i);
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

  sheet.getRange(row, CONFIG.PHONE_COL).clearDataValidations().setNumberFormat('@');
  sheet.getRange(row, CONFIG.WHATSAPP_COL).clearDataValidations().setNumberFormat('@');
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

  // Bonus columns (WhatsApp Chat / Call / Call Attempts) — guarded, since a
  // caller might run this before onOpen has ever created them. Call Attempts
  // deliberately gets ONLY a number format here, never a value — it should
  // start blank/0 for a genuinely new or reused row, never carry over.
  if (WHATSAPP_LINK_COL) sheet.getRange(row, WHATSAPP_LINK_COL).setFormula(whatsappLinkFormula(row));
  if (CALL_LINK_COL) sheet.getRange(row, CALL_LINK_COL).setFormula(callLinkFormula(row));
  if (CALL_ATTEMPTS_COL) sheet.getRange(row, CALL_ATTEMPTS_COL).setNumberFormat('0');
}

function filteredSeqFormula(row) {
  const snLetter = columnLetter_(CONFIG.SN_COL);
  return '=IF($' + snLetter + row + '="","",SUBTOTAL(103,$' + snLetter + '$2:$' + snLetter + row + '))';
}

// Writes each field to its OWN resolved column individually rather than one
// batched write across an assumed-contiguous block. This is intentionally
// less "efficient" than the old version — it trades a handful of extra API
// calls per order for surviving a column being inserted/reordered inside
// what used to be a fixed block. At WhatsApp order volumes this is the right
// trade: correctness over a few milliseconds.
function writeRecord(sheet, row, blockText, scanData) {
  sheet = sheet || SpreadsheetApp.getActiveSheet(); // guards against running this directly from the editor's function dropdown
  if (!row) {
    safeAlert('writeRecord needs a row — run this from the sheet (paste into column A) rather than the ▶ Run button.');
    return;
  }
  const fields = parseBlock(blockText);
  const sn = scanData ? (++scanData.maxSn) : nextSerialNumber(sheet, row);
  const phone = normalizePhone(fields.phone);
  const whatsapp = normalizePhone(fields.whatsapp);
  const priceInfo = resolvePrice(fields.product, fields.price);
  const priceNumber = extractPriceNumber(priceInfo.priceText);
  const productText = priceInfo.priceText ? `${priceInfo.product} — ₦${priceInfo.priceText}` : priceInfo.product;

  sheet.getRange(row, CONFIG.SN_COL).setValue(sn);
  sheet.getRange(row, CONFIG.NAME_COL).setValue(fields.name);
  sheet.getRange(row, CONFIG.PHONE_COL).setValue(phone);
  sheet.getRange(row, CONFIG.WHATSAPP_COL).setValue(whatsapp);
  sheet.getRange(row, CONFIG.NOTE_COL).setValue(''); // deliberately left blank for the human agent
  // Action column intentionally left untouched if already set; default blank on new rows.

  const orderDate = parseOrderDate(fields.date) || stripTime_(new Date());
  sheet.getRange(row, CONFIG.DATE_COL).setValue(orderDate).setNumberFormat(CONFIG.ORDER_DATE_FORMAT);
  sheet.getRange(row, CONFIG.CATEGORY_COL).setValue(fields.category);
  sheet.getRange(row, CONFIG.PRODUCT_COL).setValue(productText);
  sheet.getRange(row, CONFIG.PRICE_COL).setValue(priceNumber === '' ? '' : priceNumber).setNumberFormat('"₦"#,##0');
  sheet.getRange(row, CONFIG.ADDRESS_COL).setValue(fields.address);
  sheet.getRange(row, CONFIG.DELIVERY_COL).setValue(fields.delivery);
  sheet.getRange(row, CONFIG.QUESTION_COL).setValue(fields.question);

  sheet.getRange(row, CONFIG.PHONE_COL).clearDataValidations().setNumberFormat('@');
  sheet.getRange(row, CONFIG.WHATSAPP_COL).clearDataValidations().setNumberFormat('@');
  sheet.getRange(row, CONFIG.FILTERED_COL).setFormula(filteredSeqFormula(row));
  sheet.getRange(row, CONFIG.TODAY_COL).setFormula(todayCheckupFormula(row));
  sheet.getRange(row, CONFIG.FLAGGED_COL).setFormula(flaggedFormula(row));
  sheet.getRange(row, CONFIG.OVERDUE_COL).setFormula(overdueFormula(row));
  sheet.getRange(row, CONFIG.TODAY_ORDER_COL).setFormula(todaysOrderFormula(row));

  // Write/refresh the two link-column formulas for this row directly —
  // belt-and-suspenders alongside applyRowValidation and the onEdit
  // self-heal, so the very first paste into a row always gets a correct,
  // working button without depending on a prior bulk write.
  if (WHATSAPP_LINK_COL) sheet.getRange(row, WHATSAPP_LINK_COL).setFormula(whatsappLinkFormula(row));
  if (CALL_LINK_COL) sheet.getRange(row, CALL_LINK_COL).setFormula(callLinkFormula(row));

  const isInvalid = !isValidNigerianNumber(phone) || (!!whatsapp && !isValidNigerianNumber(whatsapp));
  const isDuplicate = scanData
    ? findDuplicateInScanData_(sheet, scanData, row, phone, fields.name, productText, fields.address)
    : checkAndFlagDuplicates(sheet, row, phone, fields.name, productText, fields.address);
  sheet.getRange(row, CONFIG.INVALID_COL).setValue(isInvalid);
  sheet.getRange(row, CONFIG.DUPLICATE_COL).setValue(isDuplicate);

  if (scanData) {
    scanData.rows.push({
      row: row,
      phone: normalizeText(phone),
      name: normalizeText(fields.name),
      product: normalizeText(productText),
      address: normalizeText(fields.address)
    });
  }

  const rawCell = sheet.getRange(row, CONFIG.RAW_COL);
  rawCell.setValue(blockText.trim());
  rawCell.setWrapStrategy(SpreadsheetApp.WrapStrategy.CLIP);
  sheet.setRowHeight(row, CONFIG.DEFAULT_ROW_HEIGHT);
}

function loadPasteScanData_(sheet) {
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

function findDuplicateInScanData_(sheet, scanData, row, phone, name, product, address) {
  const targetPhone = normalizeText(phone);
  if (!targetPhone) return false;

  const targetName = normalizeText(name);
  const targetProduct = normalizeText(product);
  const targetAddress = normalizeText(address);

  let duplicateFound = false;
  for (let i = 0; i < scanData.rows.length; i++) {
    const r = scanData.rows[i];
    if (r.row === row) continue;
    if (r.phone !== targetPhone) continue;

    const nameMatches = !!targetName && r.name === targetName;
    const productAddressMatches = r.product === targetProduct && r.address === targetAddress;

    if (nameMatches || productAddressMatches) {
      duplicateFound = true;
      sheet.getRange(r.row, CONFIG.DUPLICATE_COL).setValue(true);
    }
  }
  return duplicateFound;
}

function nextSerialNumber(sheet, currentRow) {
  sheet = sheet || SpreadsheetApp.getActiveSheet(); // guards against running this directly from the editor's function dropdown
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
    .replace(/[.,;:!?'"()]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function checkAndFlagDuplicates(sheet, row, phone, name, product, address) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  const lastRow = sheet.getLastRow();
  if (lastRow < CONFIG.FIRST_DATA_ROW) return false;
  const numRows = lastRow - CONFIG.FIRST_DATA_ROW + 1;

  const targetPhone = normalizeText(phone);
  if (!targetPhone) return false;

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
    if (normalizeText(phones[i][0]) !== targetPhone) continue;

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

const WHATSAPP_EXPORT_PREFIX_REGEX = /^\[\d{1,2}\/\d{1,2}(?:\/\d{2,4})?,\s*\d{1,2}:\d{2}(?::\d{2})?\s*[AaPp]\.?[Mm]\.?\]\s*[^:\n]+:\s?/;
const WHATSAPP_EXPORT_SPLIT_REGEX = /\n(?=\[\d{1,2}\/\d{1,2}(?:\/\d{2,4})?,\s*\d{1,2}:\d{2}(?::\d{2})?\s*[AaPp]\.?[Mm]\.?\]\s*[^:\n]+:)/;

function splitIntoBlocks(rawText) {
  rawText = rawText || '';
  let text = rawText.replace(/\r\n/g, '\n').trim();

  let chunks;
  if (WHATSAPP_EXPORT_PREFIX_REGEX.test(text)) {
    chunks = text.split(WHATSAPP_EXPORT_SPLIT_REGEX)
      .map(c => c.replace(WHATSAPP_EXPORT_PREFIX_REGEX, ''));
  } else {
    chunks = text.split(/\n\s*#{3,}\s*\n/);
  }

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
  return (str || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}

function matchLabel(normalized) {
  const words = normalized.split(' ').filter(function(w) { return w.length > 0; });
  for (const l of LABELS) {
    if (l.test(words)) return l.key;
  }
  return null;
}

function parseBlock(blockText) {
  blockText = blockText || '';
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

const MONTH_ABBR = ['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'];
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

function parseOrderDate(dateText, referenceDate) {
  if (!dateText) return null;
  const text = String(dateText).trim();
  const now = referenceDate || new Date();

  let m = text.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (m) {
    const d = new Date(parseInt(m[1], 10), parseInt(m[2], 10) - 1, parseInt(m[3], 10));
    return isValidDate_(d) ? d : null;
  }

  m = text.match(/^(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\.?$/);
  if (m) {
    const day = parseInt(m[1], 10);
    const monthIndex = MONTH_ABBR.indexOf(m[2].toLowerCase().slice(0, 3));
    if (monthIndex === -1) return null;
    return resolveOrderYear_(day, monthIndex, now);
  }

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

function extractPriceNumber(raw) {
  if (!raw) return '';
  const cleaned = String(raw).replace(/,/g, '');
  const match = cleaned.match(/\d+(\.\d+)?/);
  return match ? parseFloat(match[0]) : '';
}

function resolvePrice(productText, explicitPrice) {
  if (explicitPrice) return { product: productText, priceText: explicitPrice };
  const text = (productText || '').trim();
  if (!text) return { product: text, priceText: '' };

  const match = text.match(/(?:[\s=\-–—\u00A0]+[#₦Nn]?|[#₦])\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{3,}(?:\.\d+)?)\s*,?\s*$/);
  if (!match) return { product: text, priceText: '' };

  const cleanProduct = text.slice(0, match.index).trim().replace(/[-–—=]\s*$/, '').trim();
  return { product: cleanProduct || text, priceText: match[1] };
}

/* ---------------- ONE-TIME MIGRATION (legacy, unrelated to dynamic columns) ---------------- */
// These two functions operate on truly historical, fixed positions from a
// past layout migration and are unrelated to the dynamic-resolution system
// above. Left as one-off manual tools, not part of the daily menu.

function swapSnAndFilteredColumns_migration() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;

  const OLD_SN_COL = 2;
  const OLD_FILTERED_COL = 18;
  const numRows = CONFIG.VALIDATION_LAST_ROW - CONFIG.FIRST_DATA_ROW + 1;

  const snValues = sheet.getRange(CONFIG.FIRST_DATA_ROW, OLD_SN_COL, numRows, 1).getValues();
  const snHeader = sheet.getRange(CONFIG.HEADER_ROW, OLD_SN_COL).getValue();
  const filteredHeader = sheet.getRange(CONFIG.HEADER_ROW, OLD_FILTERED_COL).getValue();

  sheet.getRange(CONFIG.HEADER_ROW, CONFIG.SN_COL).setValue(snHeader);
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.SN_COL, numRows, 1).setValues(snValues);

  sheet.getRange(CONFIG.HEADER_ROW, CONFIG.FILTERED_COL).setValue(filteredHeader);
  setFormulaColumn_(sheet, CONFIG.FILTERED_COL, numRows, filteredSeqFormula);

  pinSidebarColumns_(sheet); // was: setFrozenColumns(CONFIG.NAME_COL) — only worked by coincidence when Name sat at column 3
  buildSummaryReport();

  safeAlert(
    'Done — S/N is now column ' + columnLetter_(CONFIG.SN_COL) +
    ', Filtered # is now column ' + columnLetter_(CONFIG.FILTERED_COL) +
    '. Customer Name is now pinned as a sidebar while scrolling.'
  );
}

function repairSnAfterPrematureInitialize() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;

  const lastRow = sheet.getLastRow();
  if (lastRow < CONFIG.FIRST_DATA_ROW) {
    safeAlert('No data rows found — nothing to repair.');
    return;
  }
  const dataRows = lastRow - CONFIG.FIRST_DATA_ROW + 1;
  const names = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.NAME_COL, dataRows, 1).getValues();

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

  const fullRows = CONFIG.VALIDATION_LAST_ROW - CONFIG.FIRST_DATA_ROW + 1;
  if (fullRows > dataRows) {
    const blankTail = [];
    for (let i = dataRows; i < fullRows; i++) blankTail.push(['']);
    sheet.getRange(CONFIG.FIRST_DATA_ROW + dataRows, CONFIG.SN_COL, fullRows - dataRows, 1).setValues(blankTail);
  }

  setFormulaColumn_(sheet, CONFIG.FILTERED_COL, fullRows, filteredSeqFormula);
  buildSummaryReport();

  safeAlert(
    'Repaired. S/N (column ' + columnLetter_(CONFIG.SN_COL) + ') now holds a fresh 1, 2, 3... sequence for ' +
    counter + ' order row(s), and Filtered # (column ' + columnLetter_(CONFIG.FILTERED_COL) + ') is working again.'
  );
}