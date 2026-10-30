// WhatsApp Order Normalizer — Apps Script
// Paste raw WhatsApp order text into column A from row 2 down; auto-fills the rest of the row.
// Multiple orders separated by "####" (or back-to-back) split into their own rows.
// Columns are resolved by HEADER TEXT (resolveColumns_), not fixed position — reordering/inserting
// columns never breaks anything as long as the header labels still exist in row 1.
// Setup: Extensions > Apps Script > paste this in > Save > add "Google Sheets API" service >
// reload sheet > Order Tools > Initialize Sheet > approve permissions > paste an order into A2.

/* ---------------- COLUMN DEFINITIONS (header-driven) ---------------- */

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

const COLUMN_ORDER = ['RAW_COL','FILTERED_COL','NAME_COL','PHONE_COL','WHATSAPP_COL','NOTE_COL',
  'ACTION_COL','CHECKUP_COL','DATE_COL','CATEGORY_COL','PRODUCT_COL','PRICE_COL','ADDRESS_COL',
  'DELIVERY_COL','QUESTION_COL','DUPLICATE_COL','INVALID_COL','SN_COL','TODAY_COL','FLAGGED_COL',
  'OVERDUE_COL','TODAY_ORDER_COL'];

// Fallback defaults only — resolveColumns_() overwrites these with real positions every run.
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
  DEFAULT_ROW_HEIGHT: 36,
  QUICK_SCHEDULE_HOUR: 10,
  REMINDER_WINDOW_MINUTES: 5,
  ORDER_DATE_FORMAT: 'd mmm yyyy',

  HEADERS: null
};
CONFIG.HEADERS = COLUMN_ORDER.map(function(k) { return CANONICAL_HEADERS[k]; });

const HEADER_PROTECTION_DESCRIPTION = 'Order Normalizer — managed header row (do not rename, delete, or reorder these labels)';
const WHATSAPP_LINK_PROTECTION_DESCRIPTION = 'Order Normalizer — computed WhatsApp Chat column (self-heals on edit)';
const SCHEDULE_SOURCE_PROTECTION_DESCRIPTION = 'Order Normalizer — script-managed Schedule Source column (warning-only)';

// Native Range.protect() can't block the owner — enforced here instead via a snapshot in
// Document Properties; onEdit reverts any row-1 edit (including the owner's) unless unlocked.
const HEADER_LOCK_PROP = 'HEADER_ROW_UNLOCKED';
const HEADER_SNAPSHOT_PROP = 'HEADER_ROW_SNAPSHOT';
const HEADER_LOCK_SHEET_PROP = 'HEADER_LOCK_SHEET_NAME';

// Flags when the script itself is legitimately appending a bonus column, so the column-insert
// guard doesn't delete its own new column.
const COLUMN_OP_IN_PROGRESS_PROP = 'COLUMN_OP_IN_PROGRESS';
function beginScriptColumnOp_() { PropertiesService.getDocumentProperties().setProperty(COLUMN_OP_IN_PROGRESS_PROP, 'true'); }
function endScriptColumnOp_()   { PropertiesService.getDocumentProperties().deleteProperty(COLUMN_OP_IN_PROGRESS_PROP); }
function isScriptColumnOpInProgress_() {
  return PropertiesService.getDocumentProperties().getProperty(COLUMN_OP_IN_PROGRESS_PROP) === 'true';
}

// onChange carries no sheet/range info — guard only acts when the active sheet matches the
// locked sheet name.
function ensureStructureChangeTriggerInstalled_() {
  const already = ScriptApp.getProjectTriggers().some(function(t) { return t.getHandlerFunction() === 'onStructureChange_'; });
  if (already) return;
  ScriptApp.newTrigger('onStructureChange_').forSpreadsheet(SpreadsheetApp.getActive()).onChange().create();
}

function isPermutationOf_(a, b) {
  if (a.length !== b.length) return false;
  const sa = a.map(String).slice().sort();
  const sb = b.map(String).slice().sort();
  for (let i = 0; i < sa.length; i++) if (sa[i] !== sb[i]) return false;
  return true;
}

// Restores column order via whole-column moves so data travels with its header.
function restoreColumnOrder_(sheet, snapshot) {
  let current = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, snapshot.length).getValues()[0].map(String);
  for (let i = 0; i < snapshot.length; i++) {
    if (current[i] === snapshot[i]) continue;
    const j = current.indexOf(snapshot[i], i);
    if (j === -1) return false;
    sheet.moveColumns(sheet.getRange(1, j + 1, 1, 1), i + 1);
    const moved = current.splice(j, 1)[0];
    current.splice(i, 0, moved);
  }
  return true;
}

function findInsertedColumnSpan_(snapshot, current) {
  if (current.length <= snapshot.length) return null;
  let prefix = 0;
  while (prefix < snapshot.length && String(snapshot[prefix]) === String(current[prefix])) prefix++;
  let suffix = 0;
  while (suffix < snapshot.length - prefix &&
         String(snapshot[snapshot.length - 1 - suffix]) === String(current[current.length - 1 - suffix])) suffix++;
  if (prefix + suffix < snapshot.length) return null;
  return { startCol: prefix + 1, count: current.length - snapshot.length };
}

// Report column: located by header text "Report", never a fixed number.
let REPORT_COL = null;

// Bonus columns — always appended, never inserted, located by header text.
let WHATSAPP_LINK_COL = null;
let CALL_ATTEMPTS_COL = null;
let CALENDAR_EVENT_COL = null;
let SCHEDULE_SOURCE_COL = null;   // "Automated Schedule" / "Manual Schedule" / blank

// Time-in-Status + Stalled Order Alerts
let STATUS_SINCE_COL = null;
let STATUS_AGE_COL = null;
let STALL_ALERT_STARTED_COL = null;
let STALL_EVENT_COL = null;
let CARRYOVER_COUNT_COL = null;
let PENDING_WATCH_EVENT_COL = null;

// Guaranteed-real sheet, with a clear error if run outside a live spreadsheet session.
function requireWorkingSheet_(sheet) {
  const resolved = sheet || SpreadsheetApp.getActiveSheet();
  if (!resolved) {
    throw new Error(
      'No active sheet available. This usually means a function was run directly from the Apps Script ' +
      'editor\'s ▶ Run button instead of from the spreadsheet\'s own Order Tools menu.'
    );
  }
  return resolved;
}

// ---------------------------------------------------------------------
// HEADER ROW CACHE — perf only, no behavior change. Two safety layers:
// (1) getLastColumn() change invalidates automatically (every bonus column is appended, never
//     inserted, so appending always changes it); (2) explicit invalidateHeaderRowCache_() call
//     after any write that changes header TEXT without changing column COUNT.
// Scoped to one script execution — Apps Script starts fresh per trigger, so nothing leaks.
// ---------------------------------------------------------------------
let _headerRowCache = null; // { sheetId, lastColSeen, values } | null
const HEADER_ROW_CACHE_KEY = 'HDRCACHE_row1';
const HEADER_ROW_CACHE_TTL_SECONDS = 300;

function invalidateHeaderRowCache_() {
  _headerRowCache = null;
  _headerIndexMapCache = null;
  try {
    const cache = CacheService.getDocumentCache();
    if (cache) cache.remove(HEADER_ROW_CACHE_KEY);
  } catch (e) { console.error('Header cache clear skipped: ' + e.message); }
}

// Single read path for row 1: in-memory cache -> CacheService cross-execution cache -> real read.
function getHeaderRowCached_(sheet, minWidth) {
  sheet = requireWorkingSheet_(sheet);
  const sheetId = sheet.getSheetId();
  const currentLastCol = sheet.getLastColumn();
  const requiredWidth = Math.max(currentLastCol, minWidth || 0, 1);

  const memFresh = _headerRowCache &&
    _headerRowCache.sheetId === sheetId &&
    _headerRowCache.lastColSeen === currentLastCol &&
    _headerRowCache.values.length >= requiredWidth;
  if (memFresh) return _headerRowCache.values.slice(0, requiredWidth);

  try {
    const docCache = CacheService.getDocumentCache();
    if (docCache) {
      const raw = docCache.get(HEADER_ROW_CACHE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed.sheetId === sheetId && parsed.lastColSeen === currentLastCol && parsed.values.length >= requiredWidth) {
          _headerRowCache = parsed;
          return parsed.values.slice(0, requiredWidth);
        }
      }
    }
  } catch (e) { console.error('Header cache read skipped: ' + e.message); }

  const values = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, requiredWidth).getValues()[0];
  _headerRowCache = { sheetId: sheetId, lastColSeen: currentLastCol, values: values };
  try {
    const docCache = CacheService.getDocumentCache();
    if (docCache) docCache.put(HEADER_ROW_CACHE_KEY, JSON.stringify(_headerRowCache), HEADER_ROW_CACHE_TTL_SECONDS);
  } catch (e) { console.error('Header cache write skipped: ' + e.message); }
  return values;
}

// Header text -> column index map, built once per execution instead of ~10 linear scans.
let _headerIndexMapCache = null;

function getHeaderIndexMap_(sheet) {
  sheet = requireWorkingSheet_(sheet);
  const headerRow = getHeaderRowCached_(sheet);
  const sheetId = sheet.getSheetId();
  const currentLastCol = sheet.getLastColumn();
  if (_headerIndexMapCache && _headerIndexMapCache.sheetId === sheetId && _headerIndexMapCache.lastColSeen === currentLastCol) {
    return _headerIndexMapCache.map;
  }
  const map = {};
  headerRow.forEach(function(h, i) {
    const norm = String(h || '').trim().toLowerCase();
    if (norm && !(norm in map)) map[norm] = i + 1;
  });
  _headerIndexMapCache = { sheetId: sheetId, lastColSeen: currentLastCol, map: map };
  return map;
}

// Generic bonus-column lookup/create, reused for every column below.
function findOrCreateOptionalColumn_(sheet, headerLabel) {
  sheet = requireWorkingSheet_(sheet);
  const headerRow = getHeaderRowCached_(sheet);

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

  const newCol = headerRow.length + 1;
  beginScriptColumnOp_();
  try {
    sheet.getRange(CONFIG.HEADER_ROW, newCol)
      .setValue(headerLabel).setFontWeight('bold').setBackground('#4a86e8').setFontColor('#ffffff');
  } finally { endScriptColumnOp_(); }
  invalidateHeaderRowCache_();
  return newCol;
}

function findOrCreateWhatsAppLinkColumn_(sheet, createIfMissing) {
  const found = findOrCreateOptionalColumnLookup_(sheet, 'WhatsApp Chat', createIfMissing);
  if (found) WHATSAPP_LINK_COL = found;
  return found;
}

function findOrCreateCallAttemptsColumn_(sheet, createIfMissing) {
  const found = findOrCreateOptionalColumnLookup_(sheet, 'Call Attempts', createIfMissing);
  if (found) CALL_ATTEMPTS_COL = found;
  return found;
}

// "Calendar Event ID" — internal bookkeeping, safe to hide, never touched by a human.
function findOrCreateCalendarEventColumn_(sheet, createIfMissing) {
  const found = findOrCreateOptionalColumnLookup_(sheet, 'Calendar Event ID', createIfMissing);
  if (found) CALENDAR_EVENT_COL = found;
  return found;
}

// "Schedule Source" — who most recently set the CURRENT Check-up value: Daily Carryover
// (AUTOMATED, only written by runDailyCarryover_impl) or a human (MANUAL, only written by
// onEdit Case 5 / scheduleQuick). Blank = untouched. A plain status column, not inferred from Note.
const AUTO_CARRIED_STATUS = {
  AUTOMATED: 'Automated Schedule',
  MANUAL: 'Manual Schedule',
  BLANK: ''
};

function buildAutoCarriedStatusRule_() {
  return SpreadsheetApp.newDataValidation()
    .requireValueInList([AUTO_CARRIED_STATUS.AUTOMATED, AUTO_CARRIED_STATUS.MANUAL], true)
    .setAllowInvalid(true) // false made Apps Script reject the script's own blank-default writes
    .build();
}

function applyAutoCarriedValidation_(sheet, col, numRows) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  const range = sheet.getRange(CONFIG.FIRST_DATA_ROW, col, numRows, 1);
  range.clearDataValidations();
  range.setDataValidation(buildAutoCarriedStatusRule_());
}

function applyAutoCarriedValidationSingleRow_(sheet, row, col) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  const range = sheet.getRange(row, col);
  range.clearDataValidations();
  range.setDataValidation(buildAutoCarriedStatusRule_());
  return range;
}

// One-time-per-session migration: converts legacy TRUE/FALSE values to the status strings.
function migrateAutoCarriedColumnToStatus_(sheet, col) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  if (!col) return;
  const numRows = CONFIG.VALIDATION_LAST_ROW - CONFIG.FIRST_DATA_ROW + 1;
  const range = sheet.getRange(CONFIG.FIRST_DATA_ROW, col, numRows, 1);
  range.clearDataValidations();
  const values = range.getValues();
  let anyBoolean = false;
  const converted = values.map(function(r) {
    const v = r[0];
    if (v === true) { anyBoolean = true; return [AUTO_CARRIED_STATUS.AUTOMATED]; }
    if (v === false) { anyBoolean = true; return [AUTO_CARRIED_STATUS.BLANK]; }
    return [v];
  });
  if (anyBoolean) range.setValues(converted);
  applyAutoCarriedValidation_(sheet, col, numRows);
}

// One-time cleanup for a leftover legacy "Auto Carried Over?" column: blanks it out so
// "Remove Empty Unlabeled Columns" can find and remove it on confirmation.
function neutralizeLegacyAutoCarriedOverColumn_(sheet) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  const legacyCol = findOrCreateOptionalColumnLookup_(sheet, 'Auto Carried Over?', false);
  if (!legacyCol || legacyCol === SCHEDULE_SOURCE_COL) return;
  const numRows = CONFIG.VALIDATION_LAST_ROW - CONFIG.HEADER_ROW + 1;
  sheet.getRange(CONFIG.HEADER_ROW, legacyCol, numRows, 1).clearContent().clearDataValidations();
  invalidateHeaderRowCache_();
  const stale = sheet.getProtections(SpreadsheetApp.ProtectionType.RANGE)
    .filter(function(p) { return p.getRange().getColumn() === legacyCol; });
  stale.forEach(function(p) { p.remove(); });
  SpreadsheetApp.getActiveSpreadsheet().toast(
    'Found a leftover "Auto Carried Over?" column and cleared it. Run Order Tools > 🧹 Remove Empty Unlabeled Columns to remove it.',
    'Cleanup', 8
  );
}

// Fast path for onEdit: pure lookup, no migration/cleanup overhead on every keystroke.
function findScheduleSourceColumnFast_(sheet) {
  const found = findOrCreateOptionalColumnLookup_(sheet, 'Schedule Source', false);
  if (found) { SCHEDULE_SOURCE_COL = found; return found; }
  return findOrCreateScheduleSourceColumn_(sheet, false);
}

// Finds/creates "Schedule Source", handling the upgrade path from every earlier version
// (rename a legacy "Auto Carried Over?" column, or create fresh).
function findOrCreateScheduleSourceColumn_(sheet, createIfMissing) {
  let found = findOrCreateOptionalColumnLookup_(sheet, 'Schedule Source', false);

  if (!found) {
    const legacyCol = findOrCreateOptionalColumnLookup_(sheet, 'Auto Carried Over?', false);
    if (legacyCol) {
      sheet.getRange(CONFIG.HEADER_ROW, legacyCol).setValue('Schedule Source');
      invalidateHeaderRowCache_();
      found = legacyCol;
    }
  }

  if (!found) {
    if (!createIfMissing) return null;
    found = findOrCreateOptionalColumn_(sheet, 'Schedule Source');
    if (!found) return null;
    const numRows = CONFIG.VALIDATION_LAST_ROW - CONFIG.FIRST_DATA_ROW + 1;
    applyAutoCarriedValidation_(sheet, found, numRows);

    // One-time backfill from any pre-existing "Automatic Carryover" Note tag.
    try {
      const noteCol = resolveColumns_(sheet).NOTE_COL;
      const existingNotes = sheet.getRange(CONFIG.FIRST_DATA_ROW, noteCol, numRows, 1).getValues();
      const backfill = existingNotes.map(function(r) {
        return [String(r[0] || '').indexOf(CARRYOVER_NOTE_TEXT) === 0 ? AUTO_CARRIED_STATUS.AUTOMATED : AUTO_CARRIED_STATUS.BLANK];
      });
      sheet.getRange(CONFIG.FIRST_DATA_ROW, found, numRows, 1).setValues(backfill);
    } catch (e) {
      console.error('Schedule Source backfill skipped: ' + e.message);
    }
  } else {
    migrateAutoCarriedColumnToStatus_(sheet, found);
  }

  SCHEDULE_SOURCE_COL = found;
  protectComputedColumn_(sheet, SCHEDULE_SOURCE_COL, SCHEDULE_SOURCE_PROTECTION_DESCRIPTION);
  try { neutralizeLegacyAutoCarriedOverColumn_(sheet); } catch (e) { console.error(e); }
  return SCHEDULE_SOURCE_COL;
}

function findOrCreateOptionalColumnLookup_(sheet, headerLabel, createIfMissing) {
  sheet = requireWorkingSheet_(sheet);
  const map = getHeaderIndexMap_(sheet);
  const found = map[headerLabel.toLowerCase()];
  if (found) return found;
  if (!createIfMissing) return null;
  return findOrCreateOptionalColumn_(sheet, headerLabel);
}

// Time-in-Status + Stalled Order Alerts bonus columns.
function findOrCreateStatusSinceColumn_(sheet, createIfMissing) {
  const found = findOrCreateOptionalColumnLookup_(sheet, 'Status Since', createIfMissing);
  if (found) STATUS_SINCE_COL = found;
  return found;
}
function findOrCreateStatusAgeColumn_(sheet, createIfMissing) {
  const found = findOrCreateOptionalColumnLookup_(sheet, 'Time in Status', createIfMissing);
  if (found) STATUS_AGE_COL = found;
  return found;
}
function findOrCreateStallAlertStartedColumn_(sheet, createIfMissing) {
  const found = findOrCreateOptionalColumnLookup_(sheet, 'Stall Alert Started', createIfMissing);
  if (found) STALL_ALERT_STARTED_COL = found;
  return found;
}
function findOrCreateStallEventColumn_(sheet, createIfMissing) {
  const found = findOrCreateOptionalColumnLookup_(sheet, 'Stall Event ID', createIfMissing);
  if (found) STALL_EVENT_COL = found;
  return found;
}
function findOrCreateCarryoverCountColumn_(sheet, createIfMissing) {
  const found = findOrCreateOptionalColumnLookup_(sheet, 'Carryover Count', createIfMissing);
  if (found) CARRYOVER_COUNT_COL = found;
  return found;
}
function findOrCreatePendingWatchEventColumn_(sheet, createIfMissing) {
  const found = findOrCreateOptionalColumnLookup_(sheet, 'Pending Watch Event ID', createIfMissing);
  if (found) PENDING_WATCH_EVENT_COL = found;
  return found;
}

// WhatsApp Chat link formula — degrades to blank (never an error) if both phone fields are empty.
function whatsappLinkFormula(row) {
  const waCol = columnLetter_(CONFIG.WHATSAPP_COL);
  const phoneCol = columnLetter_(CONFIG.PHONE_COL);
  return '=IFERROR(IF(AND($' + waCol + row + '="",$' + phoneCol + row + '=""),"",' +
    'HYPERLINK("https://wa.me/"&SUBSTITUTE(IF($' + waCol + row + '<>"",$' + waCol + row + ',$' + phoneCol + row + '),"+",""),"💬 Chat")),"")';
}

function healLinkCell_(sheet, row, col, formulaFn) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  sheet.getRange(row, col).setFormula(formulaFn(row));
}

// Statuses counted as a "failed contact attempt" for auto-incrementing Call Attempts.
const CALL_ATTEMPT_TRIGGER_STATUSES = ['Not Picking Calls', 'Switched Off'];

function ColumnResolutionError_(message) {
  this.name = 'ColumnResolutionError_';
  this.message = message;
}
ColumnResolutionError_.prototype = Object.create(Error.prototype);

// Reads row 1 once, maps header text -> physical column, overwrites CONFIG.*_COL with real
// positions. Throws (without writing) if a required header is missing.
function resolveColumns_(sheet) {
  sheet = requireWorkingSheet_(sheet);
  const headerRow = getHeaderRowCached_(sheet);

  const byName = {};
  headerRow.forEach(function(h, i) {
    const norm = String(h || '').trim().toLowerCase();
    if (norm && !(norm in byName)) byName[norm] = i + 1;
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
      'written. Restore these header label(s) in row ' + CONFIG.HEADER_ROW + ', or run Order Tools > ' +
      'Verify Column Layout for the full picture.'
    );
  }

  Object.keys(resolved).forEach(function(key) { CONFIG[key] = resolved[key]; });
  return resolved;
}

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

function getManagedColumnCount_(sheet) {
  sheet = requireWorkingSheet_(sheet);
  return Math.max(sheet.getLastColumn(), CONFIG.HEADERS.length);
}

// Locates/creates the Report column, appended after the last existing header. Reuses the
// shared hashmap-backed lookup (findOrCreateOptionalColumnLookup_) instead of its own linear
// scan — same behavior (exact header-text match, same "looksInitialized" gate, same styling),
// less duplicate code to maintain.
function findOrCreateReportColumn_(sheet, createIfMissing) {
  const found = findOrCreateOptionalColumnLookup_(sheet, 'Report', createIfMissing);
  if (found) REPORT_COL = found;
  return found;
}

// Hard-locks the header row (blocks everyone including the owner at the UI level).
// Must run AFTER the Report column exists, or it's left outside the protected range.
function protectHeaderRow_(sheet) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  const width = getManagedColumnCount_(sheet);
  const existing = sheet.getProtections(SpreadsheetApp.ProtectionType.RANGE)
    .filter(function(p) { return p.getDescription() === HEADER_PROTECTION_DESCRIPTION; });

  if (existing.length === 1 && protectionRangeMatches_(existing[0], CONFIG.HEADER_ROW, 1, 1, width)) {
    return;
  }

  existing.forEach(function(p) { p.remove(); });
  const protection = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, width).protect();
  protection.setDescription(HEADER_PROTECTION_DESCRIPTION);
  protection.setWarningOnly(false);

  try {
    protection.removeEditors(protection.getEditors());
    if (protection.canDomainEdit && protection.canDomainEdit()) protection.setDomainEdit(false);
  } catch (e) {
    console.error(e);
  }
}

function protectionRangeMatches_(protection, row, col, numRows, numCols) {
  const r = protection.getRange();
  return r.getRow() === row && r.getColumn() === col &&
         r.getNumRows() === numRows && r.getNumColumns() === numCols;
}

// Warning-only protection for a computed column — extra heads-up, never blocks the self-heal.
function protectComputedColumn_(sheet, colIndex, description) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  if (!colIndex) return;
  const numRows = CONFIG.VALIDATION_LAST_ROW - CONFIG.FIRST_DATA_ROW + 1;
  const existing = sheet.getProtections(SpreadsheetApp.ProtectionType.RANGE)
    .filter(function(p) { return p.getDescription() === description; });

  if (existing.length === 1 && protectionRangeMatches_(existing[0], CONFIG.FIRST_DATA_ROW, colIndex, numRows, 1)) {
    return;
  }

  existing.forEach(function(p) { p.remove(); });
  const protection = sheet.getRange(CONFIG.FIRST_DATA_ROW, colIndex, numRows, 1).protect();
  protection.setDescription(description);
  protection.setWarningOnly(true);
}

// Applies every protection this project manages to `sheet`: locks the header row (and
// repoints the lock to THIS sheet -- HEADER_LOCK_SHEET_PROP is a single global pointer, so
// after Monthly Rollover this is what makes the header-edit guard and Structure Change
// enforcement actually cover the NEW sheet instead of silently continuing to protect the old,
// now-archived one), plus the two computed-column protections (WhatsApp Chat, Schedule
// Source). Mirrors initializeSheet_impl's own protection calls exactly, just factored out so
// Monthly Rollover and "Enable All Features" can reuse it without duplicating the logic.
// Safe to call repeatedly -- protectHeaderRow_ and protectComputedColumn_ are both already
// idempotent no-ops when nothing has changed.
function applySheetProtections_(sheet) {
  sheet = requireWorkingSheet_(sheet);
  protectHeaderRow_(sheet);
  snapshotHeaderRow_(sheet);
  PropertiesService.getDocumentProperties().setProperty(HEADER_LOCK_PROP, 'false');
  PropertiesService.getDocumentProperties().setProperty(HEADER_LOCK_SHEET_PROP, sheet.getName());
  ensureStructureChangeTriggerInstalled_();
  protectComputedColumn_(sheet, WHATSAPP_LINK_COL, WHATSAPP_LINK_PROTECTION_DESCRIPTION);
  protectComputedColumn_(sheet, SCHEDULE_SOURCE_COL, SCHEDULE_SOURCE_PROTECTION_DESCRIPTION);
}

function refreshHeaderProtectionIfLocked_(sheet) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  const width = getManagedColumnCount_(sheet);
  const existing = sheet.getProtections(SpreadsheetApp.ProtectionType.RANGE)
    .filter(function(p) { return p.getDescription() === HEADER_PROTECTION_DESCRIPTION; });

  if (existing.length === 0) return;

  if (existing.length === 1 && protectionRangeMatches_(existing[0], CONFIG.HEADER_ROW, 1, width)) {
    snapshotHeaderRow_(sheet);
    return;
  }

  protectHeaderRow_(sheet);
  snapshotHeaderRow_(sheet);
}

/* ---- Owner-inclusive header enforcement ---- */

function isHeaderRowUnlocked_() {
  return PropertiesService.getDocumentProperties().getProperty(HEADER_LOCK_PROP) === 'true';
}

function hasHeaderSnapshot_() {
  return !!PropertiesService.getDocumentProperties().getProperty(HEADER_SNAPSHOT_PROP);
}

function snapshotHeaderRow_(sheet) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  const width = getManagedColumnCount_(sheet);
  const values = getHeaderRowCached_(sheet, width);
  PropertiesService.getDocumentProperties().setProperty(HEADER_SNAPSHOT_PROP, JSON.stringify(values));
}

function restoreHeaderSnapshot_(sheet) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  const raw = PropertiesService.getDocumentProperties().getProperty(HEADER_SNAPSHOT_PROP);
  if (!raw) return false;
  const values = JSON.parse(raw);
  sheet.getRange(CONFIG.HEADER_ROW, 1, 1, values.length).setValues([values]);
  SpreadsheetApp.flush();
  invalidateHeaderRowCache_();
  return true;
}

// Called from onEdit for any edit landing on row 1, for any user including the owner.
function guardHeaderRowEdit_(sheet, e) {
  if (isHeaderRowUnlocked_()) return;
  if (!hasHeaderSnapshot_()) return;

  const snapshot = JSON.parse(PropertiesService.getDocumentProperties().getProperty(HEADER_SNAPSHOT_PROP)).map(String);
  const currentLastCol = Math.max(sheet.getLastColumn(), snapshot.length);
  let handled = false;

  if (currentLastCol > snapshot.length) {
    const current = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, currentLastCol).getValues()[0];
    const span = findInsertedColumnSpan_(snapshot, current);
    if (span) {
      sheet.deleteColumns(span.startCol, span.count);
      SpreadsheetApp.flush();
      invalidateHeaderRowCache_();
      handled = true;
    }
  } else {
    const current = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, snapshot.length).getValues()[0].map(String);
    if (JSON.stringify(current) !== JSON.stringify(snapshot) && isPermutationOf_(current, snapshot)) {
      if (restoreColumnOrder_(sheet, snapshot)) {
        SpreadsheetApp.flush();
        invalidateHeaderRowCache_();
        try { buildSummaryReport(); } catch (sumErr) { console.error(sumErr); }
        handled = true;
      }
    }
  }

  if (!handled) {
    restoreHeaderSnapshot_(sheet);
  }

  try {
    SpreadsheetApp.getUi().alert(
      'Cannot edit header row',
      'Column headers/structure are locked. Your change was reverted. Run Order Tools > Unlock Header Row first if you need to change a label or reorder a column.',
      SpreadsheetApp.getUi().ButtonSet.OK
    );
  } catch (uiErr) {
    SpreadsheetApp.getActiveSpreadsheet().toast(
      'Header row/structure is locked — your change was reverted. Use Order Tools > Unlock Header Row to edit it.',
      'Cannot edit header', 8
    );
  }
}

function lockHeaderRow() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  try { findOrCreateReportColumn_(sheet, true); } catch (e) { console.error(e); }
  protectHeaderRow_(sheet);
  snapshotHeaderRow_(sheet);
  PropertiesService.getDocumentProperties().setProperty(HEADER_LOCK_PROP, 'false');
  PropertiesService.getDocumentProperties().setProperty(HEADER_LOCK_SHEET_PROP, sheet.getName());
  ensureStructureChangeTriggerInstalled_();
  safeAlert('Header row AND column structure are now locked — for everyone, including you. Use "Unlock Header Row" first if you need to change a label or add a column.');
}

function unlockHeaderRow() {
  const sheet = SpreadsheetApp.getActiveSheet();
  PropertiesService.getDocumentProperties().setProperty(HEADER_LOCK_PROP, 'true');
  const existing = sheet.getProtections(SpreadsheetApp.ProtectionType.RANGE)
    .filter(function(p) { return p.getDescription() === HEADER_PROTECTION_DESCRIPTION; });
  existing.forEach(function(p) { p.remove(); });
  safeAlert('Header row unlocked — you can edit labels now. Run "Lock Header Row" again once done.');
}

// Pins Raw Message / Filtered # / Customer Name as a left sidebar while scrolling. Only clean
// if those are physically the first 3 columns; otherwise pins through the furthest of the three.
function pinSidebarColumns_(sheet) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  try {
    const positions = [CONFIG.RAW_COL, CONFIG.FILTERED_COL, CONFIG.NAME_COL];
    const target = Math.max.apply(null, positions);
    const isCleanSidebar = target === 3 && Math.min.apply(null, positions) === 1;

    sheet.setFrozenColumns(target);

    if (!isCleanSidebar) {
      SpreadsheetApp.getActiveSpreadsheet().toast(
        'Raw Message, Filtered #, and Customer Name aren\'t the first three columns, so columns 1–' + target +
        ' were pinned. Move them to the front, then run Initialize Sheet again for a clean sidebar.',
        'Sidebar note', 8
      );
    }
  } catch (e) {
    console.error(e);
    SpreadsheetApp.getActiveSpreadsheet().toast(
      'Could not pin the sidebar columns automatically. Pin by hand: select columns A–C, then View > Freeze > up to current column.',
      'Sidebar note', 8
    );
  }
}

// Shows exactly which physical column every managed field resolves to, and flags any
// unrecognized column in the sheet's used range.
function verifyColumnLayout() {
  const sheet = SpreadsheetApp.getActiveSheet();
  try {
    const resolved = resolveColumns_(sheet);
    const lines = COLUMN_ORDER.map(function(key) {
      return CANONICAL_HEADERS[key] + '  →  column ' + columnLetter_(resolved[key]);
    });

    const bonusColumns = [
      ['Report', findOrCreateReportColumn_(sheet, false)],
      ['WhatsApp Chat', findOrCreateWhatsAppLinkColumn_(sheet, false)],
      ['Call Attempts', findOrCreateCallAttemptsColumn_(sheet, false)],
      ['Calendar Event ID', findOrCreateCalendarEventColumn_(sheet, false)],
      ['Schedule Source', findScheduleSourceColumnFast_(sheet)],
      ['Status Since', findOrCreateStatusSinceColumn_(sheet, false)],
      ['Time in Status', findOrCreateStatusAgeColumn_(sheet, false)],
      ['Stall Alert Started', findOrCreateStallAlertStartedColumn_(sheet, false)],
      ['Stall Event ID', findOrCreateStallEventColumn_(sheet, false)],
      ['Carryover Count', findOrCreateCarryoverCountColumn_(sheet, false)],
      ['Pending Watch Event ID', findOrCreatePendingWatchEventColumn_(sheet, false)]
    ];
    bonusColumns.forEach(function(pair) {
      lines.push(pair[0] + '  →  ' + (pair[1] ? 'column ' + columnLetter_(pair[1]) : '(not created yet)'));
    });

    const recognizedLabels = COLUMN_ORDER.map(function(key) { return CANONICAL_HEADERS[key].toLowerCase(); })
      .concat(['report', 'whatsapp chat', 'call attempts', 'calendar event id', 'schedule source',
        'status since', 'time in status', 'stall alert started', 'stall event id', 'carryover count',
        'pending watch event id']);
    const headerRow = getHeaderRowCached_(sheet);
    const unrecognized = [];
    headerRow.forEach(function(h, i) {
      const norm = String(h || '').trim().toLowerCase();
      if (recognizedLabels.indexOf(norm) === -1) {
        unrecognized.push(columnLetter_(i + 1) + ': ' + (norm ? '"' + h + '"' : '(blank header)'));
      }
    });

    let message = 'All required columns found:\n\n' + lines.join('\n');
    if (unrecognized.length > 0) {
      message += '\n\n⚠️ Unrecognized column(s):\n' + unrecognized.join('\n');
    }
    safeAlert(message, 'Column layout OK');
  } catch (e) {
    if (e instanceof ColumnResolutionError_) {
      safeAlert(e.message, 'Column layout problem');
    } else {
      safeAlert('Unexpected error while checking column layout: ' + e.message);
    }
  }
}

// Removes ONLY columns that are blank-headered AND completely empty (rows 1–500). Confirms before deleting.
function cleanupEmptyUnlabeledColumns() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;

  const lastCol = sheet.getLastColumn();
  if (lastCol < 1) { safeAlert('Nothing to check.'); return; }

  const numRows = CONFIG.VALIDATION_LAST_ROW - CONFIG.HEADER_ROW + 1;
  const fullRange = sheet.getRange(CONFIG.HEADER_ROW, 1, numRows, lastCol);
  const values = fullRange.getValues();
  const formulas = fullRange.getFormulas();

  const emptyColIndexes = [];
  for (let c = 0; c < lastCol; c++) {
    let isEmpty = true;
    for (let r = 0; r < values.length; r++) {
      const v = values[r][c];
      const f = formulas[r][c];
      if ((v !== '' && v !== null) || (f && f !== '')) { isEmpty = false; break; }
    }
    if (isEmpty) emptyColIndexes.push(c);
  }

  if (emptyColIndexes.length === 0) {
    safeAlert('No empty unlabeled columns found within row 1–' + CONFIG.VALIDATION_LAST_ROW + '.');
    return;
  }

  let ui;
  try {
    ui = SpreadsheetApp.getUi();
  } catch (e) {
    SpreadsheetApp.getActiveSpreadsheet().toast('Run this from the sheet itself — it needs to show a confirmation dialog.', 'Needs the sheet open', 8);
    return;
  }

  const columnLetters = emptyColIndexes.map(function(c) { return columnLetter_(c + 1); }).join(', ');
  const response = ui.alert(
    'Delete ' + emptyColIndexes.length + ' completely empty column(s)?',
    'Column(s) ' + columnLetters + ' have a blank header and are completely empty in every row from 1 to ' +
    CONFIG.VALIDATION_LAST_ROW + '. Delete ' + (emptyColIndexes.length > 1 ? 'them' : 'it') + '?',
    ui.ButtonSet.YES_NO
  );
  if (response !== ui.Button.YES) return;

  emptyColIndexes.slice().reverse().forEach(function(c) {
    sheet.deleteColumn(c + 1);
  });

  safeAlert('Removed ' + emptyColIndexes.length + ' empty column(s). Run Verify Column Layout to confirm.');
}

// Rewrites the WhatsApp Chat formula for every row and re-applies protection — manual backstop.
function repairLinkButtons() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  findOrCreateWhatsAppLinkColumn_(sheet, true);

  const numRows = CONFIG.VALIDATION_LAST_ROW - CONFIG.FIRST_DATA_ROW + 1;
  if (WHATSAPP_LINK_COL) setFormulaColumn_(sheet, WHATSAPP_LINK_COL, numRows, whatsappLinkFormula);

  protectComputedColumn_(sheet, WHATSAPP_LINK_COL, WHATSAPP_LINK_PROTECTION_DESCRIPTION);

  safeAlert('The WhatsApp Chat button has been rebuilt and re-protected for every row.');
}

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
  "Cash Remitted", "Cross Sales", "Deleted", "Double Order"
];

const SCHEDULING_STATUS = "Scheduled";
const UNRESOLVED_STATUSES = ["Scheduled", "Pending"];

// Daily Carryover: any overdue order not in these statuses gets its check-up rolled to today.
const CARRYOVER_EXCLUDED_STATUSES = ["Delivered", "Not Picking Calls", "Cancelled", "Double Order", "Cash Remitted", "Deleted"];
const CARRYOVER_NOTE_TEXT = "Automatic Carryover";

// After this many carryovers since a human last touched the row, Daily Carryover stops
// rolling it forward — Stalled Order Alerts takes over. Resets on any human status change.
const CARRYOVER_MAX_TIMES = 3;

// Fully resolved statuses — clears leftover carryover Note tag / Schedule Source when reached.
// Not Picking Calls stays out on purpose (active problem, not resolved).
const CARRYOVER_RESET_STATUSES = ["Delivered", "Cancelled", "Double Order", "Cash Remitted", "Deleted"];

// Builds "Automatic Carryover | <existing note>", idempotent (won't stack the tag).
function buildCarryoverNote_(existingNote) {
  const text = String(existingNote || '').trim();
  if (!text) return CARRYOVER_NOTE_TEXT;
  if (text === CARRYOVER_NOTE_TEXT) return CARRYOVER_NOTE_TEXT;
  const prefixPattern = new RegExp('^' + CARRYOVER_NOTE_TEXT.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\|\\s*');
  const withoutPriorTag = text.replace(prefixPattern, '').trim();
  return withoutPriorTag ? (CARRYOVER_NOTE_TEXT + ' | ' + withoutPriorTag) : CARRYOVER_NOTE_TEXT;
}

function stripCarryoverNote_(existingNote) {
  const text = String(existingNote || '').trim();
  if (!text) return '';
  if (text === CARRYOVER_NOTE_TEXT) return '';
  const prefixPattern = new RegExp('^' + CARRYOVER_NOTE_TEXT.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\|\\s*');
  return text.replace(prefixPattern, '').trim();
}

// Clears carryover artifacts once a row reaches a resolved status — used by onEdit Case 2 and
// the Daily Carryover sweep.
function resetCarryoverArtifactsIfResolved_(sheet, row, action, currentNote, preloaded, pending) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  if (CARRYOVER_RESET_STATUSES.indexOf(action) === -1) return false;

  let changed = false;
  const hasCarryoverTag = String(currentNote || '').indexOf(CARRYOVER_NOTE_TEXT) === 0;
  if (hasCarryoverTag) {
    const stripped = stripCarryoverNote_(currentNote);
    if (pending) pending[CONFIG.NOTE_COL] = stripped; else sheet.getRange(row, CONFIG.NOTE_COL).setValue(stripped);
    changed = true;
  }
  if (SCHEDULE_SOURCE_COL) {
    const currentSource = preloaded ? preloaded.get(SCHEDULE_SOURCE_COL) : sheet.getRange(row, SCHEDULE_SOURCE_COL).getValue();
    if (String(currentSource || '').trim() !== '') {
      if (pending) pending[SCHEDULE_SOURCE_COL] = AUTO_CARRIED_STATUS.BLANK; else sheet.getRange(row, SCHEDULE_SOURCE_COL).setValue(AUTO_CARRIED_STATUS.BLANK);
      changed = true;
    }
  }
  if (CARRYOVER_COUNT_COL) {
    const currentCount = preloaded ? preloaded.get(CARRYOVER_COUNT_COL) : sheet.getRange(row, CARRYOVER_COUNT_COL).getValue();
    if (currentCount) {
      if (pending) pending[CARRYOVER_COUNT_COL] = ''; else sheet.getRange(row, CARRYOVER_COUNT_COL).setValue('');
      changed = true;
    }
  }
  return changed;
}

/* ---------------- TIME-IN-STATUS ---------------- */
// "Time in Status" is a written (not live-formula) catchy aging message per status, refreshed
// on status change (onEdit Case 2) and daily by the Stalled Order Alerts sweep.
const STATUS_AGE_EXCLUDED_STATUSES = CARRYOVER_RESET_STATUSES.concat(['Scheduled']);

// Pending has its own dedicated watch (see PENDING WATCH below), so it's excluded from the
// general Stall Alert eligibility (though Time in Status still shows for it).
const STALL_ALERT_EXCLUDED_STATUSES = STATUS_AGE_EXCLUDED_STATUSES.concat(['Pending']);

function daysWord_(days) {
  return days === 1 ? '1 day' : days + ' days';
}

// One catchy phrase-builder per status; unlisted statuses fall through to a generic builder.
const STATUS_AGE_PHRASES = {
  'Pending':                  function(d) { return d <= 0 ? '⏳ Just went Pending' : '⏳ Pending for ' + daysWord_(d) + ' — still needs a decision'; },
  'Confirmed':                function(d) { return d <= 0 ? '👍 Just Confirmed' : '👍 Confirmed ' + daysWord_(d) + ' ago — hasn\'t moved since'; },
  'Awaiting':                 function(d) { return d <= 0 ? '🕰️ Just started Awaiting' : '🕰️ Awaiting for ' + daysWord_(d) + ' — no word from the customer'; },
  'Commitment Fee Requested': function(d) { return d <= 0 ? '💸 Fee just requested' : '💸 Fee requested ' + daysWord_(d) + ' ago — still unpaid'; },
  'Not Picking Calls':        function(d) { return d <= 0 ? '📵 Not picking up (just now)' : '📵 Not picking for ' + daysWord_(d) + ' straight'; },
  'Switched Off':             function(d) { return d <= 0 ? '🔌 Switched off (just now)' : '🔌 Switched off for ' + daysWord_(d); },
  'Shipped':                  function(d) { return d <= 0 ? '🚚 Just shipped' : '🚚 Shipped ' + daysWord_(d) + ' ago — worth confirming it landed'; },
  'Failed':                   function(d) { return d <= 0 ? '⚠️ Just failed' : '⚠️ Failed for ' + daysWord_(d) + ' — needs a retry decision'; },
  'Returned':                 function(d) { return d <= 0 ? '↩️ Just returned' : '↩️ Returned ' + daysWord_(d) + ' ago — needs closing out'; }
};
const EMPTY_STATUS_AGE_PHRASE_ = function(d) { return d <= 0 ? '🆕 Just added, no status yet' : '❔ Empty status for ' + daysWord_(d) + ' now — never actioned'; };

function computeStatusAgeText_(action, statusSinceDate, now) {
  const actionText = String(action || '').trim();
  if (actionText && STATUS_AGE_EXCLUDED_STATUSES.indexOf(actionText) !== -1) return '';
  if (!(statusSinceDate instanceof Date) || isNaN(statusSinceDate.getTime())) return '';

  const days = Math.floor((stripTime_(now).getTime() - stripTime_(statusSinceDate).getTime()) / 86400000);
  if (!actionText) return EMPTY_STATUS_AGE_PHRASE_(days);

  const phraseFn = STATUS_AGE_PHRASES[actionText];
  if (phraseFn) return phraseFn(days);
  return '📋 ' + actionText + ' for ' + daysWord_(Math.max(days, 0)) + (days <= 0 ? ' (just now)' : ' — check on this one');
}

function refreshStatusAgeCell_(sheet, row, preloadedAction, preloadedSince, pending) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  if (!STATUS_AGE_COL || !STATUS_SINCE_COL) return;
  const action = (preloadedAction !== undefined) ? preloadedAction : sheet.getRange(row, CONFIG.ACTION_COL).getValue();
  const since = (preloadedSince !== undefined) ? preloadedSince : sheet.getRange(row, STATUS_SINCE_COL).getValue();
  const text = computeStatusAgeText_(action, since, new Date());
  if (pending) pending[STATUS_AGE_COL] = text; else sheet.getRange(row, STATUS_AGE_COL).setValue(text);
}

/* ---------------- STALLED ORDER ALERTS ---------------- */
// Catches orders that never got a check-up scheduled at all. Lifecycle: sits open
// STALL_THRESHOLD_DAYS -> Calendar alert created; STALL_CLEAR_AFTER_HOURS unresolved -> clears;
// STALL_RETRIGGER_AFTER_HOURS still unresolved -> raises again; any status change resets it.

const STALL_THRESHOLD_DAYS = 2;
const STALL_ANCHOR_HOUR = 15; // 3:00 PM
const STALL_CLEAR_AFTER_HOURS = 42;
const STALL_RETRIGGER_AFTER_HOURS = 24 * 7;

// Next 3pm slot at/after fromDate, never a Sunday.
function computeNextAfternoonSlot_(fromDate) {
  const tz = getBusinessTimeZone_();
  const y = parseInt(Utilities.formatDate(fromDate, tz, 'yyyy'), 10);
  const m = parseInt(Utilities.formatDate(fromDate, tz, 'M'), 10) - 1;
  const d = parseInt(Utilities.formatDate(fromDate, tz, 'd'), 10);
  let slot = new Date(y, m, d, STALL_ANCHOR_HOUR, 0, 0);
  if (fromDate.getTime() >= slot.getTime()) {
    slot = new Date(y, m, d + 1, STALL_ANCHOR_HOUR, 0, 0);
  }
  if (slot.getDay() === 0) {
    slot = new Date(slot.getFullYear(), slot.getMonth(), slot.getDate() + 1, STALL_ANCHOR_HOUR, 0, 0);
  }
  return slot;
}

// One reminder per calendar day between slotTime and deadline, skipping Sunday.
function buildStallReminderTimes_(slotTime, deadline) {
  const times = [];
  let candidate = new Date(slotTime.getTime());
  while (candidate.getTime() <= deadline.getTime()) {
    if (candidate.getDay() !== 0) times.push(candidate);
    candidate = new Date(candidate.getTime() + 24 * 3600000);
  }
  return times;
}

// Deletes any active stall event for this row and clears both stall columns — self-healing.
function resetStallCycle_(sheet, row, preloaded, pending) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  if (!STALL_EVENT_COL || !STALL_ALERT_STARTED_COL) return;
  const raw = preloaded ? preloaded.get(STALL_EVENT_COL) : sheet.getRange(row, STALL_EVENT_COL).getValue();
  const existingId = String(raw || '').trim();
  if (existingId) {
    deleteCalendarEventSafe_(existingId);
    if (pending) pending[STALL_EVENT_COL] = ''; else sheet.getRange(row, STALL_EVENT_COL).setValue('');
  }
  if (pending) pending[STALL_ALERT_STARTED_COL] = ''; else sheet.getRange(row, STALL_ALERT_STARTED_COL).setValue('');
}

// Creates the Calendar event for one stall cycle, Note highlighted front and center.
function createStallCalendarEvent_(sheet, row, name, action, slotTime) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  try {
    const note = String(sheet.getRange(row, CONFIG.NOTE_COL).getValue() || '').trim();
    const phone = sheet.getRange(row, CONFIG.PHONE_COL).getValue();
    const product = sheet.getRange(row, CONFIG.PRODUCT_COL).getValue();
    const statusLabel = action || 'no status set';
    const deadline = new Date(slotTime.getTime() + STALL_CLEAR_AFTER_HOURS * 3600000);
    const end = new Date(deadline.getTime() + CALENDAR_EVENT_DURATION_MINUTES * 60000);
    const deadlineText = Utilities.formatDate(deadline, getBusinessTimeZone_(), 'EEE d MMM, h:mm a');

    const title = '🚨 ' + name + ' has gone quiet — ' + statusLabel + ' with nothing scheduled';

    const descriptionLines = [
      '👋 ' + name + ' has been sitting in "' + statusLabel + '" for ' + STALL_THRESHOLD_DAYS +
      '+ days with no check-up ever scheduled — this order may have been quietly forgotten.',
      '',
      '📝 NOTE: ' + (note || '(no note left on this order)'),
      '',
      '📌 Status: ' + statusLabel,
      '📞 Phone: ' + (phone || '—'),
      '📦 Product: ' + (product || '—'),
      '',
      '⏰ Reminders repeat every working day (Mon–Sat) until ' + deadlineText +
      ', then this clears itself automatically. Still unresolved a week from now? It\'ll raise again. ' +
      'Change this order\'s status at any time and it clears immediately.'
    ];

    const event = CalendarApp.getDefaultCalendar().createEvent(title, deadline, end, { description: descriptionLines.join('\n') });
    event.removeAllReminders();
    buildStallReminderTimes_(slotTime, deadline).forEach(function(reminderTime) {
      const minutesBefore = Math.round((deadline.getTime() - reminderTime.getTime()) / 60000);
      event.addPopupReminder(Math.max(minutesBefore, 0));
    });

    if (STALL_EVENT_COL) sheet.getRange(row, STALL_EVENT_COL).setValue(event.getId());
  } catch (e) {
    console.error('createStallCalendarEvent_ failed for row ' + row + ': ' + e.message);
  }
}

function startStallCycle_(sheet, row, name, action, now) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  if (!STALL_ALERT_STARTED_COL) return;
  const slot = computeNextAfternoonSlot_(now);
  sheet.getRange(row, STALL_ALERT_STARTED_COL).setValue(slot);
  createStallCalendarEvent_(sheet, row, name, action, slot);
}

// Daily sweep: refreshes Time in Status for every order row, runs the stall lifecycle for
// non-excluded rows. Returns { flagged, cleared, retriggered }.
function runStalledOrderCheck_impl(sheet) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  try { resolveColumns_(sheet); } catch (e) { console.error(e); return { flagged: 0, cleared: 0, retriggered: 0 }; }
  findOrCreateCalendarEventColumn_(sheet, true);
  findOrCreateStatusSinceColumn_(sheet, true);
  findOrCreateStatusAgeColumn_(sheet, true);
  findOrCreateStallAlertStartedColumn_(sheet, true);
  findOrCreateStallEventColumn_(sheet, true);

  const lastRow = Math.min(sheet.getLastRow(), CONFIG.VALIDATION_LAST_ROW);
  if (lastRow < CONFIG.FIRST_DATA_ROW) return { flagged: 0, cleared: 0, retriggered: 0 };
  const numRows = lastRow - CONFIG.FIRST_DATA_ROW + 1;

  const names = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.NAME_COL, numRows, 1).getValues();
  const actions = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.ACTION_COL, numRows, 1).getValues();
  const sinces = sheet.getRange(CONFIG.FIRST_DATA_ROW, STATUS_SINCE_COL, numRows, 1).getValues();
  const stallStarts = sheet.getRange(CONFIG.FIRST_DATA_ROW, STALL_ALERT_STARTED_COL, numRows, 1).getValues();
  const stallEventIds = sheet.getRange(CONFIG.FIRST_DATA_ROW, STALL_EVENT_COL, numRows, 1).getValues();

  const now = new Date();
  let flagged = 0, cleared = 0, retriggered = 0;

  for (let i = 0; i < numRows; i++) {
    const row = CONFIG.FIRST_DATA_ROW + i;
    const name = names[i][0];
    if (!name || !String(name).trim()) continue;

    const action = String(actions[i][0] || '').trim();
    refreshStatusAgeCell_(sheet, row, action, sinces[i][0]);

    if (action && STALL_ALERT_EXCLUDED_STATUSES.indexOf(action) !== -1) {
      if (stallStarts[i][0] || stallEventIds[i][0]) { resetStallCycle_(sheet, row); cleared++; }
      continue;
    }

    const since = sinces[i][0];
    const stallStart = stallStarts[i][0];
    const existingEventId = String(stallEventIds[i][0] || '').trim();

    if (!(stallStart instanceof Date)) {
      if (since instanceof Date) {
        const daysSinceChange = (now.getTime() - since.getTime()) / 86400000;
        if (daysSinceChange >= STALL_THRESHOLD_DAYS) {
          startStallCycle_(sheet, row, name, action, now);
          flagged++;
        }
      }
      continue;
    }

    const hoursSinceStart = (now.getTime() - stallStart.getTime()) / 3600000;

    if (hoursSinceStart >= STALL_RETRIGGER_AFTER_HOURS) {
      if (existingEventId) deleteCalendarEventSafe_(existingEventId);
      startStallCycle_(sheet, row, name, action, now);
      retriggered++;
    } else if (hoursSinceStart >= STALL_CLEAR_AFTER_HOURS) {
      if (existingEventId) {
        deleteCalendarEventSafe_(existingEventId);
        sheet.getRange(row, STALL_EVENT_COL).setValue('');
        cleared++;
      }
    } else if (!existingEventId) {
      createStallCalendarEvent_(sheet, row, name, action, stallStart);
    }
  }

  return { flagged: flagged, cleared: cleared, retriggered: retriggered };
}

function enableStalledOrderAlerts_impl(sheet) {
  PropertiesService.getScriptProperties().setProperty('STALL_SHEET_NAME', sheet.getName());
  removeStalledOrderTriggers_();
  ScriptApp.newTrigger('runStalledOrderCheck')
    .timeBased()
    .atHour(13)
    .nearMinute(30)
    .everyDays(1)
    .create();
}

function enableStalledOrderAlerts() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  enableStalledOrderAlerts_impl(sheet);
  safeAlert(
    'Stalled Order Alerts are ON for "' + sheet.getName() + '".\n\n' +
    'Checked once daily, near 1:30 PM. Any order sitting in the same open status for ' + STALL_THRESHOLD_DAYS +
    '+ days gets a Calendar alert at ' + STALL_ANCHOR_HOUR + ':00, repeating every working day (Mon–Sat) until ' +
    STALL_CLEAR_AFTER_HOURS + ' hours pass, then it clears. Still unresolved a week later? It raises again. ' +
    'Change the order\'s status and it clears immediately.\n\nUse "Run Stalled Order Check Now (test)" to test it.'
  );
}

function disableStalledOrderAlerts() {
  removeStalledOrderTriggers_();
  safeAlert('Stalled Order Alerts are OFF.');
}

function removeStalledOrderTriggers_() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'runStalledOrderCheck') ScriptApp.deleteTrigger(t);
  });
}

function runStalledOrderCheckManual() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  const result = runStalledOrderCheck_impl(sheet);
  const parts = [];
  if (result.flagged > 0) parts.push(result.flagged + ' order(s) just crossed ' + STALL_THRESHOLD_DAYS + ' days with no progress — a Calendar alert was raised for each.');
  if (result.retriggered > 0) parts.push(result.retriggered + ' order(s) were still unresolved a full week after their last alert — raised again.');
  if (result.cleared > 0) parts.push(result.cleared + ' stall alert(s) cleared.');
  safeAlert(parts.length > 0 ? parts.join('\n') : 'Nothing to do right now.');
}

function runStalledOrderCheck() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheetName = PropertiesService.getScriptProperties().getProperty('STALL_SHEET_NAME');
  const sheet = sheetName ? ss.getSheetByName(sheetName) : ss.getActiveSheet();
  if (!sheet) { console.error('Stalled Order Alerts: no target sheet found.'); return; }
  try {
    const result = runStalledOrderCheck_impl(sheet);
    console.log('Stalled Order Alerts: ' + result.flagged + ' newly flagged, ' + result.retriggered + ' retriggered, ' + result.cleared + ' cleared.');
  } catch (e) {
    console.error('Stalled Order Alerts failed: ' + e.message);
  }
}

/* ---------------- PENDING ORDER WATCH ---------------- */
// Dedicated, more aggressive lane for Pending: fires 2h after becoming Pending, repeats daily
// (Sundays included), stops the instant it's no longer Pending. Built on a genuine Calendar
// recurring event series, so "every day" is native, not script-driven.

const PENDING_WATCH_DELAY_HOURS = 2;

function buildPendingWatchDescription_(name, note, action, phone, product) {
  return [
    '👋 ' + name + '\'s order has been sitting in Pending since it was first marked that way — this repeats ' +
    'every day, including Sundays, until it\'s moved to Not Picking Calls, Delivered, Cancelled, or any other status.',
    '',
    '📝 NOTE: ' + (note || '(no note left on this order)'),
    '',
    '📌 Status: ' + (action || 'Pending'),
    '📞 Phone: ' + (phone || '—'),
    '📦 Product: ' + (product || '—'),
    '',
    '🔁 This reminder is on repeat, once a day, until this order is resolved — change its status at any time ' +
    'and it stops immediately.'
  ].join('\n');
}

function createPendingWatchEventSeries_(sheet, row, name, startTime, preloaded, pending) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  try {
    const note = String((preloaded ? preloaded.get(CONFIG.NOTE_COL) : sheet.getRange(row, CONFIG.NOTE_COL).getValue()) || '').trim();
    const action = preloaded ? preloaded.get(CONFIG.ACTION_COL) : sheet.getRange(row, CONFIG.ACTION_COL).getValue();
    const phone = preloaded ? preloaded.get(CONFIG.PHONE_COL) : sheet.getRange(row, CONFIG.PHONE_COL).getValue();
    const product = preloaded ? preloaded.get(CONFIG.PRODUCT_COL) : sheet.getRange(row, CONFIG.PRODUCT_COL).getValue();
    const end = new Date(startTime.getTime() + CALENDAR_EVENT_DURATION_MINUTES * 60000);

    const title = '⚠️ PENDING — ' + name + ' still needs a decision';
    const description = buildPendingWatchDescription_(name, note, action, phone, product);
    const recurrence = CalendarApp.newRecurrence().addDailyRule();

    const series = CalendarApp.getDefaultCalendar().createEventSeries(title, startTime, end, recurrence, { description: description });
    try { series.setColor(CalendarApp.EventColor.TOMATO); } catch (colorErr) { console.error('Pending Watch color set failed: ' + colorErr.message); }

    if (PENDING_WATCH_EVENT_COL) {
      if (pending) pending[PENDING_WATCH_EVENT_COL] = series.getId();
      else sheet.getRange(row, PENDING_WATCH_EVENT_COL).setValue(series.getId());
    }
  } catch (e) {
    console.error('createPendingWatchEventSeries_ failed for row ' + row + ': ' + e.message);
  }
}

function stopPendingWatch_(sheet, row, preloaded, pending) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  if (!PENDING_WATCH_EVENT_COL) return;
  const raw = preloaded ? preloaded.get(PENDING_WATCH_EVENT_COL) : sheet.getRange(row, PENDING_WATCH_EVENT_COL).getValue();
  const existingId = String(raw || '').trim();
  if (!existingId) return;
  try {
    const event = CalendarApp.getDefaultCalendar().getEventSeriesById(existingId);
    if (event) event.deleteEventSeries();
  } catch (e) {
    console.error('stopPendingWatch_ failed to delete series for row ' + row + ': ' + e.message);
  }
  if (pending) pending[PENDING_WATCH_EVENT_COL] = ''; else sheet.getRange(row, PENDING_WATCH_EVENT_COL).setValue('');
}

// Keeps the Note on an active watch series current — called from onEdit Case 6.
function refreshPendingWatchDescription_(sheet, row, preloaded) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  if (!PENDING_WATCH_EVENT_COL) return;
  const rawId = preloaded ? preloaded.get(PENDING_WATCH_EVENT_COL) : sheet.getRange(row, PENDING_WATCH_EVENT_COL).getValue();
  const existingId = String(rawId || '').trim();
  if (!existingId) return;
  try {
    const series = CalendarApp.getDefaultCalendar().getEventSeriesById(existingId);
    if (!series) return;
    const get = function(col) { return preloaded ? preloaded.get(col) : sheet.getRange(row, col).getValue(); };
    const name = get(CONFIG.NAME_COL);
    const note = String(get(CONFIG.NOTE_COL) || '').trim();
    const action = get(CONFIG.ACTION_COL);
    const phone = get(CONFIG.PHONE_COL);
    const product = get(CONFIG.PRODUCT_COL);
    series.setDescription(buildPendingWatchDescription_(name, note, action, phone, product));
  } catch (e) {
    console.error('refreshPendingWatchDescription_ failed for row ' + row + ': ' + e.message);
  }
}

// Once-daily backstop — catches rows onEdit missed. Returns { started, stopped }.
function runPendingOrderWatch_impl(sheet) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  try { resolveColumns_(sheet); } catch (e) { console.error(e); return { started: 0, stopped: 0 }; }
  findOrCreatePendingWatchEventColumn_(sheet, true);
  findOrCreateStatusSinceColumn_(sheet, true);

  const lastRow = Math.min(sheet.getLastRow(), CONFIG.VALIDATION_LAST_ROW);
  if (lastRow < CONFIG.FIRST_DATA_ROW) return { started: 0, stopped: 0 };
  const numRows = lastRow - CONFIG.FIRST_DATA_ROW + 1;

  const names = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.NAME_COL, numRows, 1).getValues();
  const actions = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.ACTION_COL, numRows, 1).getValues();
  const sinces = STATUS_SINCE_COL ? sheet.getRange(CONFIG.FIRST_DATA_ROW, STATUS_SINCE_COL, numRows, 1).getValues() : null;
  const watchEventIds = sheet.getRange(CONFIG.FIRST_DATA_ROW, PENDING_WATCH_EVENT_COL, numRows, 1).getValues();

  const now = new Date();
  let started = 0, stopped = 0;

  for (let i = 0; i < numRows; i++) {
    const row = CONFIG.FIRST_DATA_ROW + i;
    const name = names[i][0];
    if (!name || !String(name).trim()) continue;

    const action = String(actions[i][0] || '').trim();
    const existingId = String(watchEventIds[i][0] || '').trim();

    if (action !== 'Pending') {
      if (existingId) { stopPendingWatch_(sheet, row); stopped++; }
      continue;
    }

    if (existingId) continue;

    const since = sinces ? sinces[i][0] : null;
    let startTime = new Date(now.getTime());
    if (since instanceof Date) {
      const candidate = new Date(since.getTime() + PENDING_WATCH_DELAY_HOURS * 3600000);
      if (candidate.getTime() > now.getTime()) startTime = candidate;
    }
    createPendingWatchEventSeries_(sheet, row, name, startTime);
    started++;
  }

  return { started: started, stopped: stopped };
}

function enablePendingOrderWatch_impl(sheet) {
  PropertiesService.getScriptProperties().setProperty('PENDING_WATCH_SHEET_NAME', sheet.getName());
  removePendingOrderWatchTriggers_();
  ScriptApp.newTrigger('runPendingOrderWatch')
    .timeBased()
    .atHour(9)
    .nearMinute(0)
    .everyDays(1)
    .create();
}

function enablePendingOrderWatch() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  enablePendingOrderWatch_impl(sheet);
  safeAlert(
    'Pending Order Watch is ON for "' + sheet.getName() + '".\n\n' +
    'The moment an order becomes Pending, a Calendar reminder starts ' + PENDING_WATCH_DELAY_HOURS +
    ' hours later and repeats every day (Sundays included) until it\'s moved off Pending. ' +
    'Starts instantly on the edit itself — the ~9 AM daily trigger is just a backstop.\n\n' +
    'Use "Run Pending Order Watch Now (test)" to catch already-Pending orders immediately.'
  );
}

function disablePendingOrderWatch() {
  removePendingOrderWatchTriggers_();
  safeAlert('Pending Order Watch is OFF. Existing recurring reminders stay running — resolve those orders (or run the test action once more) to clear them.');
}

function removePendingOrderWatchTriggers_() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'runPendingOrderWatch') ScriptApp.deleteTrigger(t);
  });
}

function runPendingOrderWatchManual() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  const result = runPendingOrderWatch_impl(sheet);
  const parts = [];
  if (result.started > 0) parts.push(result.started + ' Pending order(s) just started their daily watch.');
  if (result.stopped > 0) parts.push(result.stopped + ' watch(es) stopped (order no longer Pending).');
  safeAlert(parts.length > 0 ? parts.join('\n') : 'Nothing to do.');
}

function runPendingOrderWatch() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheetName = PropertiesService.getScriptProperties().getProperty('PENDING_WATCH_SHEET_NAME');
  const sheet = sheetName ? ss.getSheetByName(sheetName) : ss.getActiveSheet();
  if (!sheet) { console.error('Pending Order Watch: no target sheet found.'); return; }
  try {
    const result = runPendingOrderWatch_impl(sheet);
    console.log('Pending Order Watch: ' + result.started + ' started, ' + result.stopped + ' stopped.');
  } catch (e) {
    console.error('Pending Order Watch failed: ' + e.message);
  }
}

/* ---------------- MONTHLY SHEET ROLLOVER ---------------- */
// At the month boundary, clones the active order sheet into a new "MonthName YYYY" sheet
// (formatting/conditional formatting/charts/validations all travel natively via copyTo), wipes
// its data rows, and reapplies the same batched formulas/validations used elsewhere in this
// file. The daily trigger does a pure in-memory date/hour check first — no sheet read at all —
// so a "no-op" day costs nothing. The old sheet is never touched.

const ROLLOVER_TRIGGER_HOUR = 16; // fires daily at 16:00; only acts on the last day of the month
const ROLLOVER_SHEET_NAME_PROP = 'ROLLOVER_SHEET_NAME';
const ROLLOVER_SUFFIX_SCAN_MAX = 9; // idempotency check covers base name through "#09"

// Pure date/hour check — takes no sheet, does no I/O. Safe to call on every trigger fire.
// `now` is optional (same pattern as parseOrderDate's referenceDate) so this is deterministically testable.
function isRolloverWindowNow_(now) {
  now = now || new Date();
  if (now.getHours() < ROLLOVER_TRIGGER_HOUR) return false;
  const tomorrow = new Date(now.getTime() + 24 * 3600000);
  return now.getMonth() !== tomorrow.getMonth();
}

// "MonthName YYYY" for the upcoming month (the month "tomorrow" falls in). Reuses
// REPORT_MONTH_NAMES rather than duplicating a month-name list.
function resolveRolloverMonthName_(now) {
  now = now || new Date();
  const tomorrow = new Date(now.getTime() + 24 * 3600000);
  return REPORT_MONTH_NAMES[tomorrow.getMonth()] + ' ' + tomorrow.getFullYear();
}

function rolloverSuffix_(n) {
  return n < 10 ? ('#0' + n) : ('#' + n);
}

// base name plus every "#02".."#09" variant this feature would ever create.
function rolloverNameCandidates_(baseName) {
  const names = [baseName];
  for (let n = 2; n <= ROLLOVER_SUFFIX_SCAN_MAX; n++) names.push(baseName + ' ' + rolloverSuffix_(n));
  return names;
}

// Idempotency gate: true if this month's rollover (or a prior collision-suffixed copy of it)
// already exists — safe to call this any number of times on the same day.
function rolloverTargetAlreadyExists_(ss, baseName) {
  return rolloverNameCandidates_(baseName).some(function(name) { return !!ss.getSheetByName(name); });
}

// First unused name starting at baseName, walking #02, #03, ... on collision.
function pickAvailableRolloverName_(ss, baseName) {
  if (!ss.getSheetByName(baseName)) return baseName;
  for (let n = 2; n <= 99; n++) {
    const candidate = baseName + ' ' + rolloverSuffix_(n);
    if (!ss.getSheetByName(candidate)) return candidate;
  }
  return baseName + ' ' + Utilities.getUuid(); // practically unreachable
}

// Wipes the copy's data rows and puts back exactly what a freshly initialized sheet has:
// batched validations/checkboxes (via the same helper processRow's paste path uses) plus the
// per-row formula columns that helper doesn't cover (Filtered #, WhatsApp Chat, and the
// checkbox+formula columns Today/Flagged/Overdue/Today's Order) — mirroring initializeSheet_impl.
function initRolledOverSheet_(sheet) {
  resolveColumns_(sheet);
  findOrCreateWhatsAppLinkColumn_(sheet, false);
  findOrCreateCallAttemptsColumn_(sheet, false);
  findOrCreateCalendarEventColumn_(sheet, false);
  findScheduleSourceColumnFast_(sheet);
  findOrCreateStatusSinceColumn_(sheet, false);
  findOrCreateStatusAgeColumn_(sheet, false);
  findOrCreateStallAlertStartedColumn_(sheet, false);
  findOrCreateStallEventColumn_(sheet, false);
  findOrCreateCarryoverCountColumn_(sheet, false);
  findOrCreatePendingWatchEventColumn_(sheet, false);

  const numRows = CONFIG.VALIDATION_LAST_ROW - CONFIG.FIRST_DATA_ROW + 1;
  const width = getManagedColumnCount_(sheet);
  sheet.getRange(CONFIG.FIRST_DATA_ROW, 1, numRows, width).clearContent();

  // Keep every row on the rolled-over sheet at the fixed default height from the start --
  // same CLIP + setRowHeightsForced pairing processRow uses per order, applied here to the
  // whole sheet so a fresh month never starts in "Fit to data" mode.
  sheet.getRange(CONFIG.FIRST_DATA_ROW, 1, numRows, width).setWrapStrategy(SpreadsheetApp.WrapStrategy.CLIP);
  sheet.setRowHeightsForced(CONFIG.FIRST_DATA_ROW, numRows, CONFIG.DEFAULT_ROW_HEIGHT);

  applyRowValidationBatch_(sheet, CONFIG.FIRST_DATA_ROW, numRows);

  setFormulaColumn_(sheet, CONFIG.FILTERED_COL, numRows, filteredSeqFormula);
  setFormulaColumn_(sheet, CONFIG.TODAY_COL, numRows, todayCheckupFormula);
  setFormulaColumn_(sheet, CONFIG.FLAGGED_COL, numRows, flaggedFormula);
  setFormulaColumn_(sheet, CONFIG.OVERDUE_COL, numRows, overdueFormula);
  setConstantBooleanColumn_(sheet, CONFIG.TODAY_ORDER_COL, numRows, false);
  if (WHATSAPP_LINK_COL) setFormulaColumn_(sheet, WHATSAPP_LINK_COL, numRows, whatsappLinkFormula);

  invalidateHeaderRowCache_();
}

// Returns { rolled, reason, sheetName }. Never throws — callers (trigger + manual test) log
// instead. `now` is optional, defaulting to the real clock, so this is deterministically testable.
function runMonthlyRollover_impl(sheet, now) {
  sheet = requireWorkingSheet_(sheet);
  const ss = sheet.getParent();
  const baseName = resolveRolloverMonthName_(now);

  if (rolloverTargetAlreadyExists_(ss, baseName)) {
    return { rolled: false, reason: 'already-exists', sheetName: null };
  }

  const newSheet = sheet.copyTo(ss);
  const targetName = pickAvailableRolloverName_(ss, baseName);
  newSheet.setName(targetName);

  initRolledOverSheet_(newSheet);
  try {
    enableAllFeaturesForSheet_(newSheet);
  } catch (e) {
    console.error('Monthly Rollover: enableAllFeaturesForSheet_ failed: ' + e.message);
  }
  ss.setActiveSheet(newSheet);

  return { rolled: true, reason: null, sheetName: targetName };
}

function enableMonthlyRollover_impl(sheet) {
  PropertiesService.getScriptProperties().setProperty(ROLLOVER_SHEET_NAME_PROP, sheet.getName());
  removeMonthlyRolloverTriggers_();
  ScriptApp.newTrigger('runMonthlyRolloverCheck')
    .timeBased()
    .atHour(ROLLOVER_TRIGGER_HOUR)
    .nearMinute(0)
    .everyDays(1)
    .create();
}

function enableMonthlyRollover() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  enableMonthlyRollover_impl(sheet);
  safeAlert(
    'Monthly Rollover is ON for "' + sheet.getName() + '".\n\n' +
    'Checked once daily near ' + ROLLOVER_TRIGGER_HOUR + ':00 — a cheap date check with no sheet reads on ' +
    'every day but the last day of the month. On the last day, it clones this sheet (formatting, conditional ' +
    'formatting, charts, and validations all copy natively), names the copy after the upcoming month, wipes its ' +
    'data rows, and reapplies formulas/validations in batch. This sheet is never modified.\n\n' +
    'Use "Run Monthly Rollover Now (test)" to try it without waiting for month-end.'
  );
}

function disableMonthlyRollover() {
  removeMonthlyRolloverTriggers_();
  safeAlert('Monthly Rollover is OFF.');
}

function removeMonthlyRolloverTriggers_() {
  ScriptApp.getProjectTriggers().forEach(function(t) {
    if (t.getHandlerFunction() === 'runMonthlyRolloverCheck') ScriptApp.deleteTrigger(t);
  });
}

// Trigger entry point — the only function the time-based trigger calls. Cheap check first;
// only touches the sheet at all if today is the last day of the month AND it's past 16:00.
function runMonthlyRolloverCheck() {
  try {
    if (!isRolloverWindowNow_()) return;
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const sheetName = PropertiesService.getScriptProperties().getProperty(ROLLOVER_SHEET_NAME_PROP);
    const sheet = sheetName ? ss.getSheetByName(sheetName) : ss.getActiveSheet();
    if (!sheet) { console.error('Monthly Rollover: no target sheet found.'); return; }
    const result = runMonthlyRollover_impl(sheet);
    console.log('Monthly Rollover: ' + (result.rolled ? ('rolled over to "' + result.sheetName + '"') : ('skipped (' + result.reason + ')')));
  } catch (e) {
    console.error('Monthly Rollover failed: ' + e.message);
  }
}

function runMonthlyRolloverManual() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  const result = runMonthlyRollover_impl(sheet);
  if (result.rolled) {
    safeAlert('Rolled over to a new sheet: "' + result.sheetName + '". This sheet was left untouched.');
  } else {
    safeAlert('Skipped — a sheet for the upcoming month (or a numbered variant of it) already exists. Nothing was changed.');
  }
}

/* ---------------- ENABLE ALL FEATURES (protections + every automation, one shot) ---------------- */
// One-shot re-enable for a sheet: header lock + computed-column protections, plus every
// automation trigger, all repointed at `sheet`. Two uses: (1) the "🔧 Enable All Features" menu
// item, for a sheet that's lost its protections/automations (a copy of the spreadsheet, a fresh
// script deployment, or just wanting to double-check); (2) called silently by Monthly Rollover
// right after a new sheet is created, so the new sheet comes out exactly as protected and
// automated as the one it replaced -- no manual follow-up step, and every trigger now points at
// the new sheet instead of the old, now-archived one.
//
// Pure logic, no alert/toast -- same silent-core/alerting-wrapper split as every enable*_impl
// above, and the same split already used for run*_impl vs run*Manual elsewhere in this file.
function enableAllFeaturesForSheet_(sheet) {
  sheet = requireWorkingSheet_(sheet);
  resolveColumns_(sheet);

  findOrCreateReportColumn_(sheet, true);
  findOrCreateWhatsAppLinkColumn_(sheet, true);
  findOrCreateCallAttemptsColumn_(sheet, true);
  findOrCreateCalendarEventColumn_(sheet, true);
  findOrCreateScheduleSourceColumn_(sheet, true);
  findOrCreateStatusSinceColumn_(sheet, true);
  findOrCreateStatusAgeColumn_(sheet, true);
  findOrCreateStallAlertStartedColumn_(sheet, true);
  findOrCreateStallEventColumn_(sheet, true);
  findOrCreateCarryoverCountColumn_(sheet, true);
  findOrCreatePendingWatchEventColumn_(sheet, true);

  applySheetProtections_(sheet);

  enableCheckupReminders_impl(sheet);
  enableDailyCarryover_impl(sheet);
  enableStalledOrderAlerts_impl(sheet);
  enablePendingOrderWatch_impl(sheet);
  enableMonthlyRollover_impl(sheet);

  return {
    sheetName: sheet.getName(),
    protections: ['Header row lock', 'WhatsApp Chat column', 'Schedule Source column', 'Structure Change Alerts'],
    features: ['Check-up Reminders', 'Daily Carryover', 'Stalled Order Alerts', 'Pending Order Watch', 'Monthly Rollover']
  };
}

function enableAllFeatures() {
  const sheet = SpreadsheetApp.getActiveSheet();
  let result;
  try {
    result = enableAllFeaturesForSheet_(sheet);
  } catch (e) {
    if (e instanceof ColumnResolutionError_) { safeAlert(e.message, 'Column layout problem'); return; }
    throw e;
  }
  safeAlert(
    'All protections and automations are now ON for "' + result.sheetName + '":\n\n' +
    '🔒 Protected: ' + result.protections.join(', ') + '\n\n' +
    '⚙️ Enabled: ' + result.features.join(', ') + '\n\n' +
    'Safe to run again any time — nothing stacks or duplicates.'
  );
}

/* ---------------- CALENDAR SYNC FOR CHECK-UPS ---------------- */
// Run "Sync All Check-ups to Calendar" once by hand first — onEdit (a simple trigger) can't
// itself prompt for the Calendar permission this needs.
const CALENDAR_REMINDER_MINUTES_BEFORE = 30;
const CALENDAR_EVENT_DURATION_MINUTES = 15;

const CALENDAR_SYNC_STOP_STATUSES = ['Delivered', 'Cash Remitted', 'Cancelled', 'Deleted', 'Double Order'];

// Creates/updates/deletes the linked Calendar event to match the row's state. Never throws.
function syncCalendarEvent_(sheet, row, preloaded, pending) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  try {
    if (!CALENDAR_EVENT_COL) return;

    const get = function(col) { return preloaded ? preloaded.get(col) : sheet.getRange(row, col).getValue(); };

    const name = get(CONFIG.NAME_COL);
    if (!name || !String(name).trim()) return;

    const checkupVal = get(CONFIG.CHECKUP_COL);
    const action = get(CONFIG.ACTION_COL);
    const existingId = String(get(CALENDAR_EVENT_COL) || '').trim();

    const hasValidCheckup = checkupVal instanceof Date && !isNaN(checkupVal.getTime());
    const isResolved = CALENDAR_SYNC_STOP_STATUSES.indexOf(action) !== -1;
    const shouldHaveEvent = hasValidCheckup && !isResolved;

    if (!shouldHaveEvent) {
      if (existingId) {
        deleteCalendarEventSafe_(existingId);
        if (pending) pending[CALENDAR_EVENT_COL] = ''; else sheet.getRange(row, CALENDAR_EVENT_COL).setValue('');
      }
      return;
    }

    const phone = get(CONFIG.PHONE_COL);
    const product = get(CONFIG.PRODUCT_COL);
    const address = get(CONFIG.ADDRESS_COL);
    const note = String(get(CONFIG.NOTE_COL) || '').trim();
    const title = '📞 Check-up time: ' + name + ' (' + (action || 'no status') + ')';
    const description = [
      '📝 NOTE: ' + (note || '(no note left on this order)'),
      '',
      '📌 Status: ' + (action || '—'),
      '📞 Phone: ' + (phone || '—'),
      '📦 Product: ' + (product || '—'),
      '📍 Address: ' + (address || '—')
    ].join('\n');
    const start = checkupVal;
    const end = new Date(start.getTime() + CALENDAR_EVENT_DURATION_MINUTES * 60000);

    let event = existingId ? getCalendarEventSafe_(existingId) : null;

    if (event) {
      event.setTitle(title);
      event.setDescription(description);
      event.setTime(start, end);
      event.removeAllReminders();
      event.addPopupReminder(CALENDAR_REMINDER_MINUTES_BEFORE);
      event.addPopupReminder(0);
    } else {
      event = CalendarApp.getDefaultCalendar().createEvent(title, start, end, { description: description });
      event.removeAllReminders();
      event.addPopupReminder(CALENDAR_REMINDER_MINUTES_BEFORE);
      event.addPopupReminder(0);
      if (pending) pending[CALENDAR_EVENT_COL] = event.getId(); else sheet.getRange(row, CALENDAR_EVENT_COL).setValue(event.getId());
    }
  } catch (e) {
    console.error('Calendar sync failed for row ' + row + ': ' + e.message);
  }
}

function getCalendarEventSafe_(eventId) {
  try {
    return CalendarApp.getDefaultCalendar().getEventById(eventId);
  } catch (e) {
    return null;
  }
}

function deleteCalendarEventSafe_(eventId) {
  try {
    const event = CalendarApp.getDefaultCalendar().getEventById(eventId);
    if (event) event.deleteEvent();
  } catch (e) {
    console.error('Could not delete calendar event ' + eventId + ': ' + e.message);
  }
}

// Manual backstop + first-authorization trigger. Safe to re-run; updates in place.
function syncAllCalendarEvents() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  findOrCreateCalendarEventColumn_(sheet, true);

  const lastRow = Math.min(sheet.getLastRow(), CONFIG.VALIDATION_LAST_ROW);
  if (lastRow < CONFIG.FIRST_DATA_ROW) { safeAlert('No data rows found.'); return; }

  let touched = 0;
  for (let row = CONFIG.FIRST_DATA_ROW; row <= lastRow; row++) {
    const name = sheet.getRange(row, CONFIG.NAME_COL).getValue();
    if (!name || !String(name).trim()) continue;
    syncCalendarEvent_(sheet, row);
    touched++;
  }

  safeAlert(
    'Calendar sync complete — checked ' + touched + ' order row(s). Every order with a Check-up ' +
    'Date & Time now has a matching Calendar reminder, removed once an order reaches ' +
    CALENDAR_SYNC_STOP_STATUSES.join(', ') + '.'
  );
}

// Isolated smoke test — one throwaway event, 3 minutes out. Delete it yourself once confirmed.
function testCalendarSync() {
  const testTime = new Date(Date.now() + 3 * 60000);
  const endTime = new Date(testTime.getTime() + CALENDAR_EVENT_DURATION_MINUTES * 60000);

  try {
    const event = CalendarApp.getDefaultCalendar().createEvent(
      '🧪 Order Normalizer test event (safe to delete)',
      testTime,
      endTime,
      { description: 'Created by Order Tools > Test Calendar Sync to confirm Calendar access works. Not linked to any order row.' }
    );
    event.removeAllReminders();
    event.addPopupReminder(CALENDAR_REMINDER_MINUTES_BEFORE);
    event.addPopupReminder(0);

    safeAlert(
      'Test event created for ' + testTime.toLocaleTimeString() + ' today, on your default calendar. ' +
      'Check Google Calendar to confirm it\'s there with a reminder. If a permission prompt appeared and ' +
      'you approved it, everything else (onEdit, Daily Carryover, Sync All Check-ups) works automatically now.',
      'Calendar test event created'
    );
  } catch (e) {
    safeAlert(
      'Calendar test failed: ' + e.message + '\n\nIf this mentions authorization, try again — the first click sometimes only surfaces the permission prompt.',
      'Calendar test failed'
    );
  }
}

const NIGERIA_MOBILE_REGEX = /^\+234[7-9]\d{9}$/;

// Field labels, matched by keyword (word-boundary safe).
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

/* ---------------- BATCHED CELL WRITES (perf) ---------------- */
function flushPendingWrites_(sheet, row, pending) {
  const cols = Object.keys(pending || {});
  if (cols.length === 0) return;

  const plainCols = {};
  cols.forEach(function(c) {
    const v = pending[c];
    if (v instanceof Date) {
      sheet.getRange(row, Number(c)).setValue(v); // native Date write — always correct
    } else {
      plainCols[c] = v;
    }
  });

  batchWriteRowCells_(sheet, row, plainCols);
}

function batchWriteRowCells_(sheet, row, colValueMap) {
  const cols = Object.keys(colValueMap || {});
  if (cols.length === 0) return;

  if (cols.length === 1) {
    sheet.getRange(row, Number(cols[0])).setValue(colValueMap[cols[0]]);
    return;
  }

  const sheetName = sheet.getName().replace(/'/g, "''");
  const data = cols.map(function(c) {
    return {
      range: "'" + sheetName + "'!" + columnLetter_(Number(c)) + row,
      values: [[colValueMap[c]]]
    };
  });

  try {
    Sheets.Spreadsheets.Values.batchUpdate(
      { valueInputOption: 'RAW', data: data },
      sheet.getParent().getId()
    );
  } catch (e) {
    console.error('batchWriteRowCells_ fell back to per-cell writes: ' + e.message);
    cols.forEach(function(c) {
      sheet.getRange(row, Number(c)).setValue(colValueMap[c]);
    });
  }
}

/* ---------------- TRIGGERS & MENU ---------------- */

function onOpen() {
  const sheet = SpreadsheetApp.getActiveSheet();

  try {
    resolveColumns_(sheet);
  } catch (e) { console.error(e); }

  // Keeps the Recent_Orders named range current without moving the viewport — active-range
  // scrolling on open is unreliable on the Sheets mobile client (races the client's own
  // load/restore-scroll behavior and can cause a visible jump to A1 instead of helping).
  // Scrolling now only happens on user-initiated actions: pasting a new order, or tapping
  // "📍 Jump to Recent Orders" in the menu.
  try { updateRecentOrdersNamedRange_(sheet); } catch (e) { console.error(e); }

  try {
    findOrCreateReportColumn_(sheet, true);
    findOrCreateWhatsAppLinkColumn_(sheet, true);
    findOrCreateCallAttemptsColumn_(sheet, true);
    findOrCreateCalendarEventColumn_(sheet, true);
    findOrCreateScheduleSourceColumn_(sheet, true);
    findOrCreateStatusSinceColumn_(sheet, true);
    findOrCreateStatusAgeColumn_(sheet, true);
    findOrCreateStallAlertStartedColumn_(sheet, true);
    findOrCreateStallEventColumn_(sheet, true);
    findOrCreateCarryoverCountColumn_(sheet, true);
    findOrCreatePendingWatchEventColumn_(sheet, true);
    refreshHeaderProtectionIfLocked_(sheet);
    protectComputedColumn_(sheet, WHATSAPP_LINK_COL, WHATSAPP_LINK_PROTECTION_DESCRIPTION);
  } catch (e) { console.error(e); }

  SpreadsheetApp.getUi()
    .createMenu('Order Tools')
    .addItem('📍 Jump to Recent Orders', 'jumpToRecentOrders')
    .addItem('Initialize Sheet', 'initializeSheet')
    .addItem('🔧 Enable All Features (Protections + Automations)', 'enableAllFeatures')
    .addItem('Verify Column Layout', 'verifyColumnLayout')
    .addItem('🧹 Remove Empty Unlabeled Columns (safe cleanup)', 'cleanupEmptyUnlabeledColumns')
    .addItem('Lock Header Row', 'lockHeaderRow')
    .addItem('Unlock Header Row (temporary)', 'unlockHeaderRow')
    .addItem('Reprocess Column A', 'reprocessAll')
    .addItem('Refresh Summary Report', 'buildSummaryReport')
    .addItem('Repair Link Buttons', 'repairLinkButtons')
    .addItem('Enable Fast Report Cache', 'enableReportRowCache')
    .addItem('Disable Fast Report Cache', 'disableReportRowCache')
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
    .addItem('Test Calendar Sync (1 throwaway event)', 'testCalendarSync')
    .addItem('Sync All Check-ups to Calendar', 'syncAllCalendarEvents')
    .addSeparator()
    .addItem('Enable Daily Carryover', 'enableDailyCarryover')
    .addItem('Disable Daily Carryover', 'disableDailyCarryover')
    .addItem('Run Daily Carryover Now (test)', 'runDailyCarryoverManual')
    .addSeparator()
    .addItem('Enable Stalled Order Alerts', 'enableStalledOrderAlerts')
    .addItem('Disable Stalled Order Alerts', 'disableStalledOrderAlerts')
    .addItem('Run Stalled Order Check Now (test)', 'runStalledOrderCheckManual')
    .addSeparator()
    .addItem('Enable Pending Order Watch', 'enablePendingOrderWatch')
    .addItem('Disable Pending Order Watch', 'disablePendingOrderWatch')
    .addItem('Run Pending Order Watch Now (test)', 'runPendingOrderWatchManual')
    .addSeparator()
    .addItem('Enable Monthly Rollover', 'enableMonthlyRollover')
    .addItem('Disable Monthly Rollover', 'disableMonthlyRollover')
    .addItem('Run Monthly Rollover Now (test)', 'runMonthlyRolloverManual')
    .addSeparator()
    .addItem('Enable Structure Change Alerts', 'enableStructureChangeAlerts')
    .addItem('Disable Structure Change Alerts', 'disableStructureChangeAlerts')
    .addSeparator()
    .addItem('Archive Old Resolved Orders', 'archiveOldOrders')
    .addSeparator()
    .addItem('⚠️ Repair S/N (one-time)', 'repairSnAfterPrematureInitialize')
    .addToUi();
}

function readRowFields_(sheet, row, colNumbers) {
  const valid = colNumbers.filter(function(c) { return typeof c === 'number' && c > 0; });
  if (valid.length === 0) return { get: function() { return ''; } };
  const minCol = Math.min.apply(null, valid);
  const maxCol = Math.max.apply(null, valid);
  const width = maxCol - minCol + 1;
  const values = sheet.getRange(row, minCol, 1, width).getValues()[0];
  return {
    get: function(colNum) {
      if (typeof colNum !== 'number' || colNum < minCol || colNum > maxCol) return '';
      return values[colNum - minCol];
    }
  };
}

function reportRowFieldColumns_() {
  return [
    CONFIG.PRODUCT_COL,
    CONFIG.PRICE_COL,
    CONFIG.NAME_COL,
    CONFIG.ADDRESS_COL,
    CONFIG.PHONE_COL,
    CONFIG.WHATSAPP_COL
  ];
}

function onEdit(e) {
  try {
    if (!e || !e.range) return;
    const sheet = e.range.getSheet();
    const row = e.range.getRow();
    const col = e.range.getColumn();

    if (row === CONFIG.HEADER_ROW) {
      guardHeaderRowEdit_(sheet, e);
      return;
    }

    try {
      resolveColumns_(sheet);
    } catch (colErr) {
      if (colErr instanceof ColumnResolutionError_) {
        SpreadsheetApp.getActiveSpreadsheet().toast(colErr.message, 'Column layout problem', 10);
        return;
      }
      throw colErr;
    }
    findOrCreateWhatsAppLinkColumn_(sheet, false);
    findOrCreateCallAttemptsColumn_(sheet, false);
    findOrCreateCalendarEventColumn_(sheet, false);
    findScheduleSourceColumnFast_(sheet);
    findOrCreateStatusSinceColumn_(sheet, false);
    findOrCreateStatusAgeColumn_(sheet, false);
    findOrCreateStallAlertStartedColumn_(sheet, false);
    findOrCreateStallEventColumn_(sheet, false);
    findOrCreateCarryoverCountColumn_(sheet, false);
    findOrCreatePendingWatchEventColumn_(sheet, false);

    if (row >= CONFIG.FIRST_DATA_ROW &&
        WHATSAPP_LINK_COL && col <= WHATSAPP_LINK_COL && WHATSAPP_LINK_COL <= e.range.getLastColumn()) {
      const startRow = Math.max(e.range.getRow(), CONFIG.FIRST_DATA_ROW);
      const endRow = Math.min(e.range.getLastRow(), CONFIG.VALIDATION_LAST_ROW);
      const formulas = [];
      for (let rr = startRow; rr <= endRow; rr++) formulas.push([whatsappLinkFormula(rr)]);
      sheet.getRange(startRow, WHATSAPP_LINK_COL, endRow - startRow + 1, 1).setFormulas(formulas);
      return;
    }

    if (row >= CONFIG.FIRST_DATA_ROW) {
      handleReportCacheFieldEdit_(sheet, e, col, row);
    }

    if (col === CONFIG.RAW_COL && row >= CONFIG.FIRST_DATA_ROW) {
      if (e.range.getNumRows() > 1 || e.range.getNumColumns() > 1) return;
      const raw = sheet.getRange(row, CONFIG.RAW_COL).getValue();
      if (raw && String(raw).trim()) processRow(sheet, row, String(raw));
      return;
    }

    if (col === CONFIG.ACTION_COL && row >= CONFIG.FIRST_DATA_ROW) {
      const value = e.range.getValue();
      if (value === SCHEDULING_STATUS) {
        const checkupCell = sheet.getRange(row, CONFIG.CHECKUP_COL);
        checkupCell.activate();
        SpreadsheetApp.getActiveSpreadsheet().toast('Pick the check-up date & time.', 'Scheduled order', 5);
      }

      const rowData = readRowFields_(sheet, row, [
        CONFIG.NOTE_COL, CONFIG.NAME_COL, CONFIG.PHONE_COL, CONFIG.PRODUCT_COL,
        CONFIG.ADDRESS_COL, CONFIG.CHECKUP_COL, CALL_ATTEMPTS_COL, SCHEDULE_SOURCE_COL,
        CARRYOVER_COUNT_COL, STALL_EVENT_COL, PENDING_WATCH_EVENT_COL, CALENDAR_EVENT_COL
      ]);
      const currentNote = rowData.get(CONFIG.NOTE_COL);

      const pending = {};

      if (CALL_ATTEMPTS_COL && CALL_ATTEMPT_TRIGGER_STATUSES.indexOf(value) !== -1 && e.oldValue !== value) {
        const current = rowData.get(CALL_ATTEMPTS_COL);
        const currentNum = (typeof current === 'number') ? current : 0;
        pending[CALL_ATTEMPTS_COL] = currentNum + 1;
        SpreadsheetApp.getActiveSpreadsheet().toast('Call attempt #' + (currentNum + 1) + ' logged for this order.', 'Call Attempts', 4);
      }

      resetCarryoverArtifactsIfResolved_(sheet, row, value, currentNote, rowData, pending);

      if (e.oldValue !== value) {
        const now = new Date();
        if (STATUS_SINCE_COL) pending[STATUS_SINCE_COL] = now;
        resetStallCycle_(sheet, row, rowData, pending);
        refreshStatusAgeCell_(sheet, row, value, now, pending);
        if (CARRYOVER_COUNT_COL) pending[CARRYOVER_COUNT_COL] = '';

        if (value === 'Pending') {
          const name = rowData.get(CONFIG.NAME_COL);
          if (name && String(name).trim()) {
            const startTime = new Date(Date.now() + PENDING_WATCH_DELAY_HOURS * 3600000);
            createPendingWatchEventSeries_(sheet, row, name, startTime, rowData, pending);
          }
        } else if (e.oldValue === 'Pending') {
          stopPendingWatch_(sheet, row, rowData, pending);
        }
      }

      syncCalendarEvent_(sheet, row, rowData, pending);
      flushPendingWrites_(sheet, row, pending);
      return;
    }

    const reportCol = findOrCreateReportColumn_(sheet, false);
    if (reportCol && col === reportCol && row >= CONFIG.FIRST_DATA_ROW) {
      if (e.range.getNumRows() !== 1 || e.range.getNumColumns() !== 1) return;
      const typedValue = e.value;
      if (typeof typedValue !== 'string') return;
      const trimmedUpper = typedValue.trim().toUpperCase();
      const forceFresh = trimmedUpper === REPORT_TRIGGER_FORCE_FRESH_VALUE;
      if (trimmedUpper !== REPORT_TRIGGER_VALUE && !forceFresh) return;
      if (forceFresh) invalidateReportRowCache_(sheet, row);
      const reportText = generateDeliveryReport_(sheet, row);
      e.range.setValue(reportText).setWrap(true).setVerticalAlignment('top');
      return;
    }

    if (col === CONFIG.PHONE_COL && row >= CONFIG.FIRST_DATA_ROW) {
      if (e.range.getNumRows() !== 1 || e.range.getNumColumns() !== 1) return;
      if (WHATSAPP_LINK_COL) healLinkCell_(sheet, row, WHATSAPP_LINK_COL, whatsappLinkFormula);
      return;
    }

    if (col === CONFIG.CHECKUP_COL && row >= CONFIG.FIRST_DATA_ROW) {
      if (e.range.getNumRows() !== 1 || e.range.getNumColumns() !== 1) return;
      const pending = {};
      if (SCHEDULE_SOURCE_COL) pending[SCHEDULE_SOURCE_COL] = AUTO_CARRIED_STATUS.MANUAL;
      const rowData = readRowFields_(sheet, row, [
        CONFIG.NAME_COL, CONFIG.CHECKUP_COL, CONFIG.ACTION_COL, CONFIG.PHONE_COL,
        CONFIG.PRODUCT_COL, CONFIG.ADDRESS_COL, CONFIG.NOTE_COL, CALENDAR_EVENT_COL
      ]);
      syncCalendarEvent_(sheet, row, rowData, pending);
      flushPendingWrites_(sheet, row, pending);
      return;
    }

    if (col === CONFIG.NOTE_COL && row >= CONFIG.FIRST_DATA_ROW) {
      if (e.range.getNumRows() !== 1 || e.range.getNumColumns() !== 1) return;
      const rowData = readRowFields_(sheet, row, [
        PENDING_WATCH_EVENT_COL, CONFIG.NAME_COL, CONFIG.NOTE_COL, CONFIG.ACTION_COL, CONFIG.PHONE_COL, CONFIG.PRODUCT_COL
      ]);
      refreshPendingWatchDescription_(sheet, row, rowData);
      return;
    }
  } catch (err) {
    console.error(err);
    try {
      SpreadsheetApp.getActiveSpreadsheet().toast(
        'That edit didn\'t process cleanly. Check Executions in the Apps Script editor for details, or try again.',
        'Something went wrong', 6
      );
    } catch (toastErr) { }
  }
}

/* ---------------- STRUCTURE CHANGE ALERTS ---------------- */

function enableStructureChangeAlerts() {
  removeStructureChangeTriggers_();
  ScriptApp.newTrigger('onStructureChange_')
    .forSpreadsheet(SpreadsheetApp.getActive())
    .onChange()
    .create();
  safeAlert('Structure change alerts are ON. You\'ll get a toast if a column is inserted or removed.');
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
    console.log('onStructureChange_ fired with changeType: ' + e.changeType);

    const structural = ['INSERT_COLUMN', 'REMOVE_COLUMN', 'INSERT_GRID', 'REMOVE_GRID', 'OTHER'];
    if (structural.indexOf(e.changeType) === -1) return;

    const sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
    const reverted = enforceColumnLock_(sheet);

    if (reverted) {
      SpreadsheetApp.getActiveSpreadsheet().toast(
        'Column structure is locked on this sheet — a change was detected and reverted automatically.',
        'Reverted', 8
      );
    } else if (e.changeType === 'REMOVE_COLUMN') {
      SpreadsheetApp.getActiveSpreadsheet().toast(
        'A column was removed. Run Order Tools > Verify Column Layout to confirm everything still lines up.',
        'Structure change detected', 8
      );
    }
  } catch (err) {
    console.error(err);
  }
}

function enforceColumnLock_(sheet) {
  if (!sheet) return false;
  if (isScriptColumnOpInProgress_()) return false;
  if (isHeaderRowUnlocked_()) return false;
  if (!hasHeaderSnapshot_()) return false;

  const lockedSheetName = PropertiesService.getDocumentProperties().getProperty(HEADER_LOCK_SHEET_PROP);
  if (lockedSheetName && sheet.getName() !== lockedSheetName) return false;

  const snapshot = JSON.parse(PropertiesService.getDocumentProperties().getProperty(HEADER_SNAPSHOT_PROP)).map(String);
  const currentLastCol = Math.max(sheet.getLastColumn(), snapshot.length);

  if (currentLastCol > snapshot.length) {
    const current = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, currentLastCol).getValues()[0];
    const span = findInsertedColumnSpan_(snapshot, current);
    if (span) {
      sheet.deleteColumns(span.startCol, span.count);
      SpreadsheetApp.flush();
      invalidateHeaderRowCache_();
      return true;
    }
    return false;
  }

  const current = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, snapshot.length).getValues()[0].map(String);
  if (JSON.stringify(current) === JSON.stringify(snapshot)) return false;

  if (isPermutationOf_(current, snapshot)) {
    if (restoreColumnOrder_(sheet, snapshot)) {
      SpreadsheetApp.flush();
      invalidateHeaderRowCache_();
      try { buildSummaryReport(); } catch (sumErr) { console.error(sumErr); }
      return true;
    }
    return false;
  }

  restoreHeaderSnapshot_(sheet);
  return true;
}

/* ---------------- GENERATE REPORT ---------------- */

const REPORT_TRIGGER_VALUE = 'GR';
const REPORT_TRIGGER_FORCE_FRESH_VALUE = 'GRF';

const REPORT_MONTH_NAMES = ['January','February','March','April','May','June',
  'July','August','September','October','November','December'];

const REPORT_PRODUCT_PRICE_SUFFIX_REGEX = /\s*—\s*₦[\d,]+(?:\.\d+)?\s*$/;
const REPORT_MARKETING_PHRASE_REGEX = /free\s+(?:doorstep|home)\s+delivery|free\s+delivery/gi;
const REPORT_FREE_WORD_REGEX = /\bfree\b/gi;

function cleanPackageForReport_(packageText) {
  let text = String(packageText || '');
  text = text.replace(REPORT_MARKETING_PHRASE_REGEX, '');
  text = text.replace(REPORT_FREE_WORD_REGEX, '');
  text = text.split('+')
    .map(function(part) { return part.trim(); })
    .filter(function(part) { return part.length > 0; })
    .join(' + ');
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

const REPORT_ROW_CACHE_ENABLED_PROP = 'REPORT_ROW_CACHE_ENABLED';
const REPORT_ROW_CACHE_TTL_SECONDS = 1800;

function isReportRowCacheEnabled_() {
  return PropertiesService.getDocumentProperties().getProperty(REPORT_ROW_CACHE_ENABLED_PROP) === 'true';
}

function reportRowCacheKey_(sheet, row) {
  return 'RRC_' + sheet.getSheetId() + '_' + row;
}

function writeReportRowCache_(sheet, row, fields) {
  if (!isReportRowCacheEnabled_()) return;
  try {
    const cache = CacheService.getDocumentCache();
    if (!cache) return;
    cache.put(reportRowCacheKey_(sheet, row), JSON.stringify(fields), REPORT_ROW_CACHE_TTL_SECONDS);
  } catch (e) { console.error('Report row cache write skipped: ' + e.message); }
}

function readReportRowCache_(sheet, row) {
  if (!isReportRowCacheEnabled_()) return null;
  try {
    const cache = CacheService.getDocumentCache();
    if (!cache) return null;
    const raw = cache.get(reportRowCacheKey_(sheet, row));
    return raw ? JSON.parse(raw) : null;
  } catch (e) { console.error('Report row cache read skipped: ' + e.message); return null; }
}

function invalidateReportRowCache_(sheet, row) {
  try {
    const cache = CacheService.getDocumentCache();
    if (cache) cache.remove(reportRowCacheKey_(sheet, row));
  } catch (e) { console.error('Report row cache invalidate skipped: ' + e.message); }
}

function reportCacheColumnFieldPairs_() {
  return [
    [CONFIG.PRODUCT_COL, 'product'],
    [CONFIG.PRICE_COL, 'price'],
    [CONFIG.NAME_COL, 'name'],
    [CONFIG.ADDRESS_COL, 'address'],
    [CONFIG.PHONE_COL, 'phone'],
    [CONFIG.WHATSAPP_COL, 'whatsapp']
  ];
}

function handleReportCacheFieldEdit_(sheet, e, col, row) {
  if (!isReportRowCacheEnabled_()) return;
  const pair = reportCacheColumnFieldPairs_().filter(function(p) { return p[0] === col; })[0];
  if (!pair) return;

  const isSingleCell = e.range.getNumRows() === 1 && e.range.getNumColumns() === 1;
  if (isSingleCell) {
    const cached = readReportRowCache_(sheet, row);
    if (!cached) return;
    cached[pair[1]] = e.value !== undefined ? e.value : '';
    writeReportRowCache_(sheet, row, cached);
  } else {
    const startRow = Math.max(e.range.getRow(), CONFIG.FIRST_DATA_ROW);
    const endRow = Math.min(e.range.getLastRow(), CONFIG.VALIDATION_LAST_ROW);
    for (let r = startRow; r <= endRow; r++) invalidateReportRowCache_(sheet, r);
  }
}

function enableReportRowCache() {
  PropertiesService.getDocumentProperties().setProperty(REPORT_ROW_CACHE_ENABLED_PROP, 'true');
  safeAlert(
    'Fast Report Cache is ON. Pressing "GR" can now skip the read and generate straight from cache. ' +
    'Type "GRF" any time to force a genuinely fresh read and re-warm it for that row. ' +
    'Turn off any time with "Disable Fast Report Cache."'
  );
}

function disableReportRowCache() {
  PropertiesService.getDocumentProperties().deleteProperty(REPORT_ROW_CACHE_ENABLED_PROP);
  safeAlert('Fast Report Cache is OFF. "GR" always reads fresh now.');
}

function generateDeliveryReport_(sheet, row, rowData) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();

  if (!rowData) {
    const cached = readReportRowCache_(sheet, row);
    if (cached) {
      rowData = { get: function(col) {
        const pair = reportCacheColumnFieldPairs_().filter(function(p) { return p[0] === col; })[0];
        return pair ? cached[pair[1]] : '';
      }};
    } else {
      rowData = readRowFields_(sheet, row, reportRowFieldColumns_());
      writeReportRowCache_(sheet, row, {
        product: rowData.get(CONFIG.PRODUCT_COL),
        price: rowData.get(CONFIG.PRICE_COL),
        name: rowData.get(CONFIG.NAME_COL),
        address: rowData.get(CONFIG.ADDRESS_COL),
        phone: rowData.get(CONFIG.PHONE_COL),
        whatsapp: rowData.get(CONFIG.WHATSAPP_COL)
      });
    }
  }

  const rawProduct = rowData.get(CONFIG.PRODUCT_COL);
  const rawPrice = rowData.get(CONFIG.PRICE_COL);
  const rawName = rowData.get(CONFIG.NAME_COL);
  const rawAddress = rowData.get(CONFIG.ADDRESS_COL);

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
  const phone = rowData.get(CONFIG.PHONE_COL);
  const whatsapp = rowData.get(CONFIG.WHATSAPP_COL);
  const packageText = cleanPackageForReport_(cleanPackageText_(rawProduct));
  const priceText = formatReportPrice_(rawPrice);
  const address = rawAddress;

  return [
    formatReportDate_(new Date()),
    '',
    'Today\'s Delivery #',
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
      'Setup hit an unexpected snag: ' + e.message + '\n\nSteps completed before this point are fine — check Executions for the trace, or try again.',
      'Unexpected error'
    );
  }
}

function initializeSheet_impl() {
  const sheet = SpreadsheetApp.getActiveSheet();

  const headerRange = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, CONFIG.HEADERS.length);
  headerRange.setValues([CONFIG.HEADERS]);
  headerRange.setFontWeight('bold').setBackground('#4a86e8').setFontColor('#ffffff');
  invalidateHeaderRowCache_();
  sheet.setFrozenRows(1);

  resolveColumns_(sheet);
  findOrCreateReportColumn_(sheet, true);
  findOrCreateWhatsAppLinkColumn_(sheet, true);
  findOrCreateCallAttemptsColumn_(sheet, true);
  findOrCreateCalendarEventColumn_(sheet, true);
  findOrCreateScheduleSourceColumn_(sheet, true);
  findOrCreateStatusSinceColumn_(sheet, true);
  findOrCreateStatusAgeColumn_(sheet, true);
  findOrCreateStallAlertStartedColumn_(sheet, true);
  findOrCreateStallEventColumn_(sheet, true);
  findOrCreateCarryoverCountColumn_(sheet, true);
  findOrCreatePendingWatchEventColumn_(sheet, true);
  protectHeaderRow_(sheet);
  snapshotHeaderRow_(sheet);
  PropertiesService.getDocumentProperties().setProperty(HEADER_LOCK_PROP, 'false');
  PropertiesService.getDocumentProperties().setProperty(HEADER_LOCK_SHEET_PROP, sheet.getName());
  ensureStructureChangeTriggerInstalled_();
  pinSidebarColumns_(sheet);

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
  setConstantBooleanColumn_(sheet, CONFIG.TODAY_ORDER_COL, numRows, false);

  if (WHATSAPP_LINK_COL) setFormulaColumn_(sheet, WHATSAPP_LINK_COL, numRows, whatsappLinkFormula);
  if (CALL_ATTEMPTS_COL) sheet.getRange(CONFIG.FIRST_DATA_ROW, CALL_ATTEMPTS_COL, numRows, 1).setNumberFormat('0');
  if (STATUS_SINCE_COL) sheet.getRange(CONFIG.FIRST_DATA_ROW, STATUS_SINCE_COL, numRows, 1).setNumberFormat('yyyy-mm-dd hh:mm');
  if (STALL_ALERT_STARTED_COL) sheet.getRange(CONFIG.FIRST_DATA_ROW, STALL_ALERT_STARTED_COL, numRows, 1).setNumberFormat('yyyy-mm-dd hh:mm');
  if (CARRYOVER_COUNT_COL) sheet.getRange(CONFIG.FIRST_DATA_ROW, CARRYOVER_COUNT_COL, numRows, 1).setNumberFormat('0');

  protectComputedColumn_(sheet, WHATSAPP_LINK_COL, WHATSAPP_LINK_PROTECTION_DESCRIPTION);
  protectComputedColumn_(sheet, SCHEDULE_SOURCE_COL, SCHEDULE_SOURCE_PROTECTION_DESCRIPTION);

  // Every data row starts at the fixed default height, forced (never "Fit to data") -- the
  // same pairing processRow uses per order, applied here up front so a brand-new sheet is
  // already consistent before a single order is ever pasted into it.
  sheet.getRange(CONFIG.FIRST_DATA_ROW, 1, numRows, getManagedColumnCount_(sheet)).setWrapStrategy(SpreadsheetApp.WrapStrategy.CLIP);
  sheet.setRowHeightsForced(CONFIG.FIRST_DATA_ROW, numRows, CONFIG.DEFAULT_ROW_HEIGHT);

  const existingFilter = sheet.getFilter();
  if (existingFilter) existingFilter.remove();
  sheet.getRange(CONFIG.HEADER_ROW, 1, CONFIG.VALIDATION_LAST_ROW, getManagedColumnCount_(sheet)).createFilter();

  applyConditionalFormatting(sheet);
  buildSummaryReport();

  safeAlert('Sheet initialized. Paste WhatsApp messages into column A, starting row 2.\n\nHeader row is now locked for everyone, including you. Use Order Tools > Unlock Header Row first if you need to change a label, and Verify Column Layout any time to see the live mapping.');
}

function setFormulaColumn_(sheet, col, numRows, formulaFn) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  const formulas = [];
  for (let r = CONFIG.FIRST_DATA_ROW; r <= CONFIG.FIRST_DATA_ROW + numRows - 1; r++) {
    formulas.push([formulaFn(r)]);
  }
  sheet.getRange(CONFIG.FIRST_DATA_ROW, col, numRows, 1).setFormulas(formulas);
}

// Batched equivalent of setFormulaColumn_ for a plain (non-formula) value repeated down a
// whole column -- one setValues() call instead of one per row. Used for Today's Order? on a
// fresh/empty sheet (Initialize Sheet, Monthly Rollover), where every row's Date is still
// blank, so the badge is uniformly false until an order is actually pasted into it.
function setConstantBooleanColumn_(sheet, col, numRows, value) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  const values = [];
  for (let r = 0; r < numRows; r++) values.push([value]);
  sheet.getRange(CONFIG.FIRST_DATA_ROW, col, numRows, 1).setValues(values);
}

function reprocessAll() {
  try {
    reprocessAll_impl();
  } catch (e) {
    console.error(e);
    safeAlert(
      'Reprocessing hit an unexpected snag: ' + e.message + '\n\nRows already reprocessed are fine — check Executions or try again.',
      'Unexpected error'
    );
  }
}

function reprocessAll_impl() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  findOrCreateWhatsAppLinkColumn_(sheet, false);
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

  const doubleOrderRule = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=$' + actionCol + r0 + '="Double Order"')
    .setBackground('#AD1457').setFontColor('#FFFFFF').setBold(true)
    .setRanges([dataRange])
    .build();

  const deliveredRule = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=OR($' + actionCol + r0 + '="Delivered",$' + actionCol + r0 + '="Cash Remitted")')
    .setBackground('#d9ead3')
    .setRanges([dataRange])
    .build();

  const crossSale = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=$' + actionCol + r0 + '="Cross Sales"')
    .setBackground('#45c717').setFontColor('#4effa7').setBold(true)
    .setRanges([dataRange])
    .build();

  const scheduled = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=$' + actionCol + r0 + '="Scheduled"')
    .setBackground('#197f8cbf').setFontColor('#FFF200').setBold(true)
    .setRanges([dataRange])
    .build();

  const closedOutRule = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=$' + actionCol + r0 + '="Deleted"')
    .setBackground('#efefef')
    .setRanges([dataRange])
    .build();

  sheet.setConditionalFormatRules([
    overdueRule, cancelledRule, doubleOrderRule, deliveredRule, closedOutRule,
    pendingRule, awaitingRule, notPickingRule, crossSale, scheduled
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
  sheet = sheet || SpreadsheetApp.getActiveSheet();
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

/* ---------------- JUMP TO RECENT ORDERS (scroll-on-open) ---------------- */
const RECENT_ORDERS_WINDOW = 10;
const RECENT_ORDERS_RANGE_NAME = 'Recent_Orders';

function findLastOrderRow_(sheet) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  const lastRow = Math.min(sheet.getLastRow(), CONFIG.VALIDATION_LAST_ROW);
  if (lastRow < CONFIG.FIRST_DATA_ROW) return 0;
  const numRows = lastRow - CONFIG.FIRST_DATA_ROW + 1;
  const filteredValues = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.FILTERED_COL, numRows, 1).getValues();

  for (let i = numRows - 1; i >= 0; i--) {
    const v = filteredValues[i][0];
    if (v !== '' && v !== null) return CONFIG.FIRST_DATA_ROW + i;
  }
  return 0;
}

function getRecentOrdersTargetRange_(sheet) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  const lastOrderRow = findLastOrderRow_(sheet);
  if (!lastOrderRow) return null;
  const startRow = Math.max(CONFIG.FIRST_DATA_ROW, lastOrderRow - RECENT_ORDERS_WINDOW + 1);
  return sheet.getRange(startRow, CONFIG.NAME_COL, lastOrderRow - startRow + 1, 1);
}

function refreshRecentOrdersNav_(sheet) {
  try { scrollToRecentOrders_(sheet); } catch (e) { console.error(e); }
  try { updateRecentOrdersNamedRange_(sheet); } catch (e) { console.error(e); }
}

function scrollToRecentOrders_(sheet) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  const target = getRecentOrdersTargetRange_(sheet);
  if (!target) return;
  const ss = sheet.getParent();
  ss.setActiveSheet(sheet);
  ss.setActiveRange(target);
  SpreadsheetApp.flush();
}

function updateRecentOrdersNamedRange_(sheet) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  try {
    const ss = sheet.getParent();
    ss.getNamedRanges().forEach(function(nr) {
      if (nr.getName() === RECENT_ORDERS_RANGE_NAME) nr.remove();
    });
    const target = getRecentOrdersTargetRange_(sheet);
    if (target) ss.setNamedRange(RECENT_ORDERS_RANGE_NAME, target);
  } catch (e) {
    console.error('updateRecentOrdersNamedRange_ failed: ' + e.message);
  }
}

function jumpToRecentOrders() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  if (!findLastOrderRow_(sheet)) {
    safeAlert('No orders yet — nothing to jump to.');
    return;
  }
  refreshRecentOrdersNav_(sheet);
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
  findOrCreateCalendarEventColumn_(sheet, true);
  findOrCreateScheduleSourceColumn_(sheet, true);
  if (SCHEDULE_SOURCE_COL) sheet.getRange(row, SCHEDULE_SOURCE_COL).setValue(AUTO_CARRIED_STATUS.MANUAL);
  syncCalendarEvent_(sheet, row);
  SpreadsheetApp.getActiveSpreadsheet().toast(
    'Check-up set for ' + target.toLocaleDateString() + ' ' + target.toLocaleTimeString(),
    'Scheduled', 4
  );
}

/* ---------------- SAVED FILTER VIEWS ---------------- */

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

// Whether `dateVal` falls on the same calendar day as `now`, per this project's own
// timezone-normalized stripTime_ -- the single source of truth for the Today's Order? badge.
// Used both at write time (batch paste, single-row apply -- guarantees the badge is correct
// the instant a row is processed, no live-formula recalculation dependency) and by the Daily
// Carryover sweep's refresh (which is what flips yesterday's TRUE back to FALSE each morning).
function isOrderDateToday_(dateVal, now) {
  if (!(dateVal instanceof Date) || isNaN(dateVal.getTime())) return false;
  return stripTime_(dateVal).getTime() === stripTime_(now || new Date()).getTime();
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
      'Run this from the sheet itself: Order Tools > Show Orders For a Date…',
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
    'Showing orders dated ' + Utilities.formatDate(targetDate, getBusinessTimeZone_(), 'd MMM yyyy') + '.',
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
  const criteria = {};
  criteria[columnIndex1Based] = { hiddenValues: ['FALSE'] };
  createFilterViewMultiCriteria_(title, criteria);
}

function createFilterViewMultiCriteria_(title, criteriaByColumn1Based) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getActiveSheet();
  const spreadsheetId = ss.getId();
  const sheetId = sheet.getSheetId();

  removeFilterViewByTitle_(title);

  const criteria = {};
  Object.keys(criteriaByColumn1Based).forEach(function(col1Based) {
    criteria[String(Number(col1Based) - 1)] = criteriaByColumn1Based[col1Based];
  });

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
    'Desktop: Data > Filter views > ' + title + '.'
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

// Silent half: property + trigger only, no alert. Reused by the public function below and by
// "Enable All Features" / Monthly Rollover, which need to install several of these back-to-back
// without a UI dialog blocking after each one.
function enableCheckupReminders_impl(sheet) {
  PropertiesService.getScriptProperties().setProperty('REMINDER_SHEET_NAME', sheet.getName());
  removeCheckupReminderTriggers_();
  ScriptApp.newTrigger('checkForUpcomingCheckups').timeBased().everyMinutes(5).create();
}

function enableCheckupReminders() {
  const sheet = SpreadsheetApp.getActiveSheet();
  enableCheckupReminders_impl(sheet);
  safeAlert(
    'Check-up reminders are ON for "' + sheet.getName() + '".\n\n' +
    "You'll get a toast about " + CONFIG.REMINDER_WINDOW_MINUTES + ' minutes before each scheduled check-up — but only while this spreadsheet is open.'
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
  try { resolveColumns_(sheet); } catch (e) { console.error(e); return; }

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

/* ---------------- DAILY CARRYOVER ---------------- */

function enableDailyCarryover_impl(sheet) {
  PropertiesService.getScriptProperties().setProperty('CARRYOVER_SHEET_NAME', sheet.getName());
  removeDailyCarryoverTriggers_();
  ScriptApp.newTrigger('runDailyCarryover')
    .timeBased()
    .atHour(0)
    .nearMinute(1)
    .everyDays(1)
    .create();
}

function enableDailyCarryover() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  enableDailyCarryover_impl(sheet);
  safeAlert(
    'Daily Carryover is ON for "' + sheet.getName() + '".\n\n' +
    'Near 12:01 AM daily, any overdue order not in ' + CARRYOVER_EXCLUDED_STATUSES.join(', ') +
    ' has its check-up moved to today and "' + CARRYOVER_NOTE_TEXT + '" prepended to its Note.\n\n' +
    'Also cleans up: any order that reached ' + CARRYOVER_RESET_STATUSES.join(', ') +
    ' but still has a leftover carryover tag gets it reset — immediately on status change, or overnight.\n\n' +
    'Also refreshes the "Today\'s Order?" badge for every row each night, so yesterday\'s orders correctly ' +
    'flip back to unchecked.\n\n' +
    'Use "Run Daily Carryover Now (test)" to see it work without waiting.'
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

function runDailyCarryoverManual() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  const result = runDailyCarryover_impl(sheet);
  const parts = [];
  if (result.carried > 0) parts.push('Carried over ' + result.carried + ' order(s) — check-up moved to today, "' + CARRYOVER_NOTE_TEXT + '" prepended to each Note.');
  if (result.reset > 0) parts.push('Reset ' + result.reset + ' resolved order(s) that still had a leftover carryover tag or Schedule Source.');
  safeAlert(parts.length > 0 ? parts.join('\n\n') : 'Nothing to do right now.');
}

function runDailyCarryover() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheetName = PropertiesService.getScriptProperties().getProperty('CARRYOVER_SHEET_NAME');
  const sheet = sheetName ? ss.getSheetByName(sheetName) : ss.getActiveSheet();
  if (!sheet) { console.error('Daily Carryover: no target sheet found.'); return; }

  try {
    const result = runDailyCarryover_impl(sheet);
    console.log('Daily Carryover: rescheduled ' + result.carried + ' order(s), reset ' + result.reset + ' resolved order(s).');
  } catch (e) {
    console.error('Daily Carryover failed: ' + e.message);
  }
}

function runDailyCarryover_impl(sheet, now) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  try { resolveColumns_(sheet); } catch (e) { console.error(e); return { carried: 0, reset: 0 }; }
  findOrCreateCalendarEventColumn_(sheet, true);
  findOrCreateScheduleSourceColumn_(sheet, true);
  findOrCreateCarryoverCountColumn_(sheet, true);

  const lastRow = Math.min(sheet.getLastRow(), CONFIG.VALIDATION_LAST_ROW);
  if (lastRow < CONFIG.FIRST_DATA_ROW) return { carried: 0, reset: 0 };
  const numRows = lastRow - CONFIG.FIRST_DATA_ROW + 1;

  const names = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.NAME_COL, numRows, 1).getValues();
  const checkups = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.CHECKUP_COL, numRows, 1).getValues();
  const actions = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.ACTION_COL, numRows, 1).getValues();
  const notes = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.NOTE_COL, numRows, 1).getValues();
  const dates = sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.DATE_COL, numRows, 1).getValues();
  const carryoverCounts = CARRYOVER_COUNT_COL
    ? sheet.getRange(CONFIG.FIRST_DATA_ROW, CARRYOVER_COUNT_COL, numRows, 1).getValues()
    : null;

  const today = stripTime_(now || new Date());

  // Today's Order? is written once, at paste time, for an immediate guaranteed-correct badge
  // (see writeComputedRowsBatched_ / isOrderDateToday_). This is the other half: a written
  // value doesn't expire on its own the way a live TODAY() formula would, so this daily pass
  // -- right after midnight -- re-checks every row in one batched call and flips yesterday's
  // TRUE badges back to FALSE.
  const todaysOrderVals = dates.map(function(r) { return [isOrderDateToday_(r[0], today)]; });
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.TODAY_ORDER_COL, numRows, 1).setValues(todaysOrderVals);

  let carriedCount = 0;
  let resetCount = 0;

  for (let i = 0; i < numRows; i++) {
    const row = CONFIG.FIRST_DATA_ROW + i;
    const name = names[i][0];
    const checkupVal = checkups[i][0];
    const action = actions[i][0];
    const currentNote = notes[i][0];

    if (!name || !String(name).trim()) continue;

    if (resetCarryoverArtifactsIfResolved_(sheet, row, action, currentNote)) resetCount++;
    if (CARRYOVER_EXCLUDED_STATUSES.indexOf(action) !== -1) continue;

    if (!(checkupVal instanceof Date)) continue;

    const checkupDateOnly = stripTime_(checkupVal);
    if (checkupDateOnly.getTime() >= today.getTime()) continue;

    const currentCarryoverCount = carryoverCounts ? (Number(carryoverCounts[i][0]) || 0) : 0;
    if (currentCarryoverCount >= CARRYOVER_MAX_TIMES) continue;

    const newCheckup = new Date(
      today.getFullYear(), today.getMonth(), today.getDate(),
      checkupVal.getHours(), checkupVal.getMinutes(), checkupVal.getSeconds()
    );

    sheet.getRange(row, CONFIG.CHECKUP_COL).setValue(newCheckup);
    sheet.getRange(row, CONFIG.NOTE_COL).setValue(buildCarryoverNote_(currentNote));
    if (SCHEDULE_SOURCE_COL) sheet.getRange(row, SCHEDULE_SOURCE_COL).setValue(AUTO_CARRIED_STATUS.AUTOMATED);
    if (CARRYOVER_COUNT_COL) sheet.getRange(row, CARRYOVER_COUNT_COL).setValue(currentCarryoverCount + 1);
    syncCalendarEvent_(sheet, row);
    carriedCount++;
  }

  return { carried: carriedCount, reset: resetCount };
}

/* ---------------- ARCHIVE OLD RESOLVED ORDERS ---------------- */

const ARCHIVE_SHEET_NAME = 'Archive';
const ARCHIVE_ELIGIBLE_STATUSES = ['Delivered', 'Cash Remitted', 'Cancelled', 'Deleted', 'Double Order'];
const ARCHIVE_AFTER_DAYS = 30;

function getOrCreateArchiveSheet_(mainSheet) {
  const ss = mainSheet.getParent();
  let archiveSheet = ss.getSheetByName(ARCHIVE_SHEET_NAME);
  if (!archiveSheet) {
    archiveSheet = ss.insertSheet(ARCHIVE_SHEET_NAME);
    const width = getManagedColumnCount_(mainSheet);
    mainSheet.getRange(CONFIG.HEADER_ROW, 1, 1, width)
      .copyTo(archiveSheet.getRange(1, 1, 1, width));
    archiveSheet.setFrozenRows(1);
  }
  return archiveSheet;
}

function archiveOldOrders() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!requireColumns_(sheet)) return;
  findOrCreateWhatsAppLinkColumn_(sheet, false);
  findOrCreateCallAttemptsColumn_(sheet, false);
  findOrCreateCalendarEventColumn_(sheet, false);

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
    if (!name || !String(name).trim()) continue;
    if (ARCHIVE_ELIGIBLE_STATUSES.indexOf(action) === -1) continue;
    if (!(dateVal instanceof Date)) continue;
    if (stripTime_(dateVal).getTime() > cutoffDateOnly.getTime()) continue;
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
    SpreadsheetApp.getActiveSpreadsheet().toast('Run Archive Old Resolved Orders from the sheet itself.', 'Needs the sheet open', 8);
    return;
  }

  const response = ui.alert(
    'Archive ' + eligibleRows.length + ' order(s)?',
    eligibleRows.length + ' order(s) match [' + ARCHIVE_ELIGIBLE_STATUSES.join(', ') + '] and are older than ' +
    ARCHIVE_AFTER_DAYS + ' days. They\'ll be copied to "' + ARCHIVE_SHEET_NAME + '", then cleared and ' +
    're-validated in this sheet — ready for a new order. Rows are NOT deleted.',
    ui.ButtonSet.YES_NO
  );
  if (response !== ui.Button.YES) return;

  const archiveSheet = getOrCreateArchiveSheet_(sheet);
  const width = getManagedColumnCount_(sheet);
  let destRow = archiveSheet.getLastRow() + 1;

  eligibleRows.forEach(function(row) {
    sheet.getRange(row, 1, 1, width).copyTo(archiveSheet.getRange(destRow, 1, 1, width));
    destRow++;
  });

  eligibleRows.forEach(function(row) {
    if (CALENDAR_EVENT_COL) {
      const eventId = String(sheet.getRange(row, CALENDAR_EVENT_COL).getValue() || '').trim();
      if (eventId) deleteCalendarEventSafe_(eventId);
    }
    sheet.getRange(row, 1, 1, width).clearContent();
    applyRowValidation(sheet, row);
    invalidateReportRowCache_(sheet, row);
  });

  safeAlert(
    'Archived ' + eligibleRows.length + ' order(s) to the "' + ARCHIVE_SHEET_NAME + '" tab. ' +
    'Those rows in this sheet are now cleared and ready for new orders.'
  );
}

/* ---------------- CORE PROCESSING ---------------- */

function processRow(sheet, row, rawText) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  if (!row) {
    safeAlert('processRow needs a row — run this from the sheet (paste into column A) rather than the ▶ Run button.');
    return;
  }
  const blocks = splitIntoBlocks(rawText);
  if (blocks.length === 0) return;

  const scanData = loadPasteScanData_(sheet);

  const anchorHadExistingOrder = scanData.rows.some(function(r) { return r.row === row && (r.name || r.phone); });

  const computed = [];
  let haltInfo = null;

  for (let i = 0; i < blocks.length; i++) {
    let result = null, lastErr = null, succeeded = false;
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        result = computeRowData_(blocks[i], scanData, scanData.maxSn + 1);
        succeeded = true;
        break;
      } catch (e) {
        lastErr = e;
        console.error('processRow: order ' + (i + 1) + '/' + blocks.length + ', attempt ' + attempt + ' failed: ' + e.message);
      }
    }
    if (!succeeded) {
      haltInfo = { orderNumber: i + 1, snippet: String(blocks[i]).slice(0, 60), error: lastErr };
      break;
    }
    scanData.maxSn = result.sn;
    result.scanEntry.row = row + computed.length;
    scanData.rows.push(result.scanEntry);
    computed.push(result);
  }

  if (computed.length === 0) {
    reportPasteOutcome_(0, blocks.length, haltInfo, row, null);
    return;
  }

  const numNewRows = computed.length - 1;
  if (numNewRows > 0) {
    sheet.insertRowsAfter(row, numNewRows);
    applyRowValidationBatch_(sheet, row + 1, numNewRows);
  }

  writeComputedRowsBatched_(sheet, row, computed, anchorHadExistingOrder);

  const numRows = computed.length;
  sheet.getRange(row, CONFIG.PHONE_COL, numRows, 1).clearDataValidations().setNumberFormat('@');
  sheet.getRange(row, CONFIG.WHATSAPP_COL, numRows, 1).clearDataValidations().setNumberFormat('@');
  sheet.getRange(row, CONFIG.PRICE_COL, numRows, 1).setNumberFormat('"₦"#,##0');
  sheet.getRange(row, CONFIG.DATE_COL, numRows, 1).setNumberFormat(CONFIG.ORDER_DATE_FORMAT);
  // CLIP across the whole written row width, not just RAW_COL -- any column still in WRAP
  // mode (Note/Product/Address/etc., however that got set -- template default, a prior manual
  // edit) forces Sheets to grow the row to fit its content no matter what height we set next,
  // which is what was silently undoing the reset below.
  sheet.getRange(row, 1, numRows, getManagedColumnCount_(sheet)).setWrapStrategy(SpreadsheetApp.WrapStrategy.CLIP);
  // setRowHeights() only sets a height -- Sheets can still auto-resize the row later because
  // its row-height MODE stays "Fit to data". setRowHeightsForced() is the API that actually
  // flips that mode to "Specify height", which is what stops future recalculation from
  // growing the row again.
  sheet.setRowHeightsForced(row, numRows, CONFIG.DEFAULT_ROW_HEIGHT);

  markDuplicateRows_(sheet, computed.reduce(function(acc, r) { return acc.concat(r.dupMarkRows); }, []));

  reportPasteOutcome_(computed.length, blocks.length, haltInfo, row, row + computed.length - 1);
  checkRowCapWarning(sheet);
  refreshRecentOrdersNav_(sheet);
}

function reportPasteOutcome_(savedCount, totalBlocks, haltInfo, startRow, lastGoodRow) {
  if (!haltInfo) return;

  const message =
    'Order ' + haltInfo.orderNumber + ' of ' + totalBlocks + ' failed 3 times in a row: ' +
    haltInfo.error.message + '\n\n' +
    'Text: "' + haltInfo.snippet + (haltInfo.snippet.length >= 60 ? '…' : '') + '"\n\n' +
    'Processing stopped at that point — nothing after order ' + haltInfo.orderNumber + ' was attempted.\n\n' +
    (savedCount > 0
      ? '✅ Order(s) 1–' + savedCount + ' are safely saved (rows ' + startRow + '–' + lastGoodRow + ').\n\n'
      : '⬜ Nothing before it was saved either.\n\n') +
    'Fix order ' + haltInfo.orderNumber + ' onward and paste just that remainder back in — safe to paste ' +
    'starting at row ' + (savedCount > 0 ? (lastGoodRow + 1) : startRow) + '.';

  try {
    SpreadsheetApp.getUi().alert('Paste stopped partway through', message, SpreadsheetApp.getUi().ButtonSet.OK);
  } catch (uiErr) {
    SpreadsheetApp.getActiveSpreadsheet().toast(message, 'Paste stopped', 15);
  }
}

function applyRowValidation(sheet, row) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
  if (!row) {
    safeAlert('applyRowValidation needs a row — runs automatically during order processing.');
    return;
  }
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
  sheet.getRange(row, CONFIG.TODAY_ORDER_COL).setValue(false);

  if (WHATSAPP_LINK_COL) sheet.getRange(row, WHATSAPP_LINK_COL).setFormula(whatsappLinkFormula(row));
  if (CALL_ATTEMPTS_COL) sheet.getRange(row, CALL_ATTEMPTS_COL).setNumberFormat('0');
  if (SCHEDULE_SOURCE_COL) applyAutoCarriedValidationSingleRow_(sheet, row, SCHEDULE_SOURCE_COL).setValue(AUTO_CARRIED_STATUS.BLANK);

  if (STATUS_SINCE_COL) sheet.getRange(row, STATUS_SINCE_COL).setNumberFormat('yyyy-mm-dd hh:mm');
  if (STALL_ALERT_STARTED_COL) sheet.getRange(row, STALL_ALERT_STARTED_COL).setNumberFormat('yyyy-mm-dd hh:mm');

  // Same fixed-height treatment as every other row-(re)initialization path, so a row cleared
  // and reused after archiving doesn't silently drift back to "Fit to data".
  sheet.getRange(row, 1, 1, getManagedColumnCount_(sheet)).setWrapStrategy(SpreadsheetApp.WrapStrategy.CLIP);
  sheet.setRowHeightsForced(row, 1, CONFIG.DEFAULT_ROW_HEIGHT);
}

function applyRowValidationBatch_(sheet, startRow, numRows) {
  if (numRows <= 0) return;

  const statusRule = SpreadsheetApp.newDataValidation()
    .requireValueInList(STATUS_LIST, true)
    .setAllowInvalid(false)
    .build();
  sheet.getRange(startRow, CONFIG.ACTION_COL, numRows, 1).setDataValidation(statusRule);

  const dateRule = SpreadsheetApp.newDataValidation().requireDate().setAllowInvalid(true).build();
  const checkupRange = sheet.getRange(startRow, CONFIG.CHECKUP_COL, numRows, 1);
  checkupRange.setDataValidation(dateRule);
  checkupRange.setNumberFormat('yyyy-mm-dd hh:mm');

  sheet.getRange(startRow, CONFIG.DUPLICATE_COL, numRows, 1).insertCheckboxes();
  sheet.getRange(startRow, CONFIG.INVALID_COL, numRows, 1).insertCheckboxes();

  sheet.getRange(startRow, CONFIG.PHONE_COL, numRows, 1).clearDataValidations().setNumberFormat('@');
  sheet.getRange(startRow, CONFIG.WHATSAPP_COL, numRows, 1).clearDataValidations().setNumberFormat('@');
  sheet.getRange(startRow, CONFIG.PRICE_COL, numRows, 1).setNumberFormat('"₦"#,##0');
  sheet.getRange(startRow, CONFIG.DATE_COL, numRows, 1).setNumberFormat(CONFIG.ORDER_DATE_FORMAT);

  sheet.getRange(startRow, CONFIG.TODAY_COL, numRows, 1).insertCheckboxes();
  sheet.getRange(startRow, CONFIG.FLAGGED_COL, numRows, 1).insertCheckboxes();
  sheet.getRange(startRow, CONFIG.OVERDUE_COL, numRows, 1).insertCheckboxes();
  sheet.getRange(startRow, CONFIG.TODAY_ORDER_COL, numRows, 1).insertCheckboxes();

  if (CALL_ATTEMPTS_COL) sheet.getRange(startRow, CALL_ATTEMPTS_COL, numRows, 1).setNumberFormat('0');

  if (SCHEDULE_SOURCE_COL) {
    const scheduleRange = sheet.getRange(startRow, SCHEDULE_SOURCE_COL, numRows, 1);
    scheduleRange.clearDataValidations();
    scheduleRange.setDataValidation(buildAutoCarriedStatusRule_());
    scheduleRange.setValue(AUTO_CARRIED_STATUS.BLANK);
  }

  if (STATUS_SINCE_COL) sheet.getRange(startRow, STATUS_SINCE_COL, numRows, 1).setNumberFormat('yyyy-mm-dd hh:mm');
  if (STALL_ALERT_STARTED_COL) sheet.getRange(startRow, STALL_ALERT_STARTED_COL, numRows, 1).setNumberFormat('yyyy-mm-dd hh:mm');
}

function filteredSeqFormula(row) {
  const snLetter = columnLetter_(CONFIG.SN_COL);
  return '=IF($' + snLetter + row + '="","",SUBTOTAL(103,$' + snLetter + '$2:$' + snLetter + row + '))';
}

function computeRowData_(blockText, scanData, sn) {
  const fields = parseBlock(blockText);
  const phone = normalizePhone(fields.phone);
  const whatsapp = normalizePhone(fields.whatsapp);
  const priceInfo = resolvePrice(fields.product, fields.price);
  const priceNumber = extractPriceNumber(priceInfo.priceText);
  const productText = priceInfo.priceText ? `${priceInfo.product} — ₦${priceInfo.priceText}` : priceInfo.product;
  const orderDate = parseOrderDate(fields.date) || stripTime_(new Date());

  const isInvalid = !isValidNigerianNumber(phone) || (!!whatsapp && !isValidNigerianNumber(whatsapp));

  const targetPhoneNorm = normalizeText(phone);
  const targetNameNorm = normalizeText(fields.name);
  const targetProductNorm = normalizeText(productText);
  const targetAddressNorm = normalizeText(fields.address);
  let isDuplicate = false;
  const dupMarkRows = [];
  if (targetPhoneNorm) {
    scanData.rows.forEach(function(r) {
      if (r.phone !== targetPhoneNorm) return;
      const nameMatches = !!targetNameNorm && r.name === targetNameNorm;
      const productAddressMatches = r.product === targetProductNorm && r.address === targetAddressNorm;
      if (nameMatches || productAddressMatches) {
        isDuplicate = true;
        dupMarkRows.push(r.row);
      }
    });
  }

  return {
    sn: sn,
    rawText: blockText.trim(),
    fields: fields,
    phone: phone,
    whatsapp: whatsapp,
    productText: productText,
    priceNumber: priceNumber,
    orderDate: orderDate,
    isInvalid: isInvalid,
    isDuplicate: isDuplicate,
    dupMarkRows: dupMarkRows,
    scanEntry: { row: null, phone: targetPhoneNorm, name: targetNameNorm, product: targetProductNorm, address: targetAddressNorm }
  };
}

function writeComputedRowsBatched_(sheet, startRow, computed, anchorHadExistingOrder) {
  const n = computed.length;
  const range = function(col) { return sheet.getRange(startRow, col, n, 1); };

  const snVals = [], nameVals = [], phoneVals = [], waVals = [], noteVals = [], dateVals = [],
    catVals = [], productVals = [], priceVals = [], addrVals = [], delivVals = [], questionVals = [],
    invalidVals = [], dupVals = [], rawVals = [], todaysOrderVals = [];
  const filteredF = [], todayF = [], flaggedF = [], overdueF = [], waLinkF = [];
  const statusSinceRows = [];

  computed.forEach(function(d, i) {
    const r = startRow + i;
    const f = d.fields;
    snVals.push([d.sn]);
    nameVals.push([f.name]);
    phoneVals.push([d.phone]);
    waVals.push([d.whatsapp]);
    noteVals.push(['']);
    dateVals.push([d.orderDate]);
    catVals.push([f.category]);
    productVals.push([d.productText]);
    priceVals.push([d.priceNumber === '' ? '' : d.priceNumber]);
    addrVals.push([f.address]);
    delivVals.push([f.delivery]);
    questionVals.push([f.question]);
    invalidVals.push([d.isInvalid]);
    dupVals.push([d.isDuplicate]);
    rawVals.push([d.rawText]);
    // Written directly from the same orderDate value being written to Date -- guaranteed
    // TRUE the instant this batch is processed, never dependent on Sheets recalculating a
    // live TODAY() formula (and immune to any spreadsheet-vs-script timezone mismatch, since
    // both values come from this one script execution).
    todaysOrderVals.push([isOrderDateToday_(d.orderDate)]);
    filteredF.push([filteredSeqFormula(r)]);
    todayF.push([todayCheckupFormula(r)]);
    flaggedF.push([flaggedFormula(r)]);
    overdueF.push([overdueFormula(r)]);
    if (WHATSAPP_LINK_COL) waLinkF.push([whatsappLinkFormula(r)]);
    const isNewRow = (i > 0) || !anchorHadExistingOrder;
    if (STATUS_SINCE_COL && isNewRow) statusSinceRows.push(r);
  });

  range(CONFIG.SN_COL).setValues(snVals);
  range(CONFIG.NAME_COL).setValues(nameVals);
  range(CONFIG.PHONE_COL).setValues(phoneVals);
  range(CONFIG.WHATSAPP_COL).setValues(waVals);
  range(CONFIG.NOTE_COL).setValues(noteVals);
  range(CONFIG.DATE_COL).setValues(dateVals);
  range(CONFIG.CATEGORY_COL).setValues(catVals);
  range(CONFIG.PRODUCT_COL).setValues(productVals);
  range(CONFIG.PRICE_COL).setValues(priceVals);
  range(CONFIG.ADDRESS_COL).setValues(addrVals);
  range(CONFIG.DELIVERY_COL).setValues(delivVals);
  range(CONFIG.QUESTION_COL).setValues(questionVals);
  range(CONFIG.INVALID_COL).setValues(invalidVals);
  range(CONFIG.DUPLICATE_COL).setValues(dupVals);
  range(CONFIG.RAW_COL).setValues(rawVals);
  range(CONFIG.FILTERED_COL).setFormulas(filteredF);
  range(CONFIG.TODAY_COL).setFormulas(todayF);
  range(CONFIG.FLAGGED_COL).setFormulas(flaggedF);
  range(CONFIG.OVERDUE_COL).setFormulas(overdueF);
  range(CONFIG.TODAY_ORDER_COL).setValues(todaysOrderVals);
  if (WHATSAPP_LINK_COL) range(WHATSAPP_LINK_COL).setFormulas(waLinkF);

  if (STATUS_SINCE_COL && statusSinceRows.length > 0) {
    const now = new Date();
    if (statusSinceRows.length === n) {
      range(STATUS_SINCE_COL).setValues(computed.map(function() { return [now]; }));
    } else {
      sheet.getRange(startRow + 1, STATUS_SINCE_COL, n - 1, 1).setValues(
        computed.slice(1).map(function() { return [now]; })
      );
    }
  };

  if (isReportRowCacheEnabled_()) {
    computed.forEach(function(d, i) {
      writeReportRowCache_(sheet, startRow + i, {
        product: d.productText,
        price: d.priceNumber === '' ? '' : d.priceNumber,
        name: d.fields.name,
        address: d.fields.address,
        phone: d.phone,
        whatsapp: d.whatsapp
      });
    });
  }
}

function markDuplicateRows_(sheet, markRows) {
  if (!markRows || markRows.length === 0) return;
  const minRow = Math.min.apply(null, markRows);
  const maxRow = Math.max.apply(null, markRows);
  const numRows = maxRow - minRow + 1;
  const range = sheet.getRange(minRow, CONFIG.DUPLICATE_COL, numRows, 1);
  const values = range.getValues();
  const markSet = {};
  markRows.forEach(function(r) { markSet[r] = true; });
  for (let i = 0; i < numRows; i++) {
    if (markSet[minRow + i]) values[i][0] = true;
  }
  range.setValues(values);
}

function loadPasteScanData_(sheet) {
  sheet = sheet || SpreadsheetApp.getActiveSheet();
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

/* ---------------- PARSING ---------------- */

const WHATSAPP_EXPORT_PREFIX_REGEX = /^\[\d{1,2}\/\d{1,2}(?:\/\d{2,4})?,\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?\]\s*[^:\n]+:\s?/;
const WHATSAPP_EXPORT_SPLIT_REGEX = /\n(?=\[\d{1,2}\/\d{1,2}(?:\/\d{2,4})?,\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?\]\s*[^:\n]+:)/;

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

const LOOSE_LABEL_PATTERNS = [
  { key: 'whatsapp', regex: /^whatsapp\w*(?:\s+(?:number|num|no))?/i },
  { key: 'phone',    regex: /^(?:phone|mobile|tel(?:ephone)?)\w*(?:\s+(?:number|num|no))?/i },
  { key: 'address',  regex: /^(?:address|location)\w*/i },
  { key: 'delivery', regex: /^deliver\w*(?:\s+date)?/i },
  { key: 'question', regex: /^(?:question|query)\w*/i },
  { key: 'price',    regex: /^(?:price|amount|cost)\w*/i },
  { key: 'product',  regex: /^(?:product|package|item)\w*/i },
  { key: 'name',     regex: /^name\w*/i }
];

const LOOSE_LABEL_SEPARATOR_REGEX = /^[\s:\-=_.,|–—~]+/;

const PURE_SEPARATOR_LINE_REGEX = /^[\s:\-=_.,|–—~*#•>]+$/;

function matchLooseLabelLine_(line) {
  const trimmed = String(line || '').trim();
  if (!trimmed) return null;
  for (let i = 0; i < LOOSE_LABEL_PATTERNS.length; i++) {
    const pattern = LOOSE_LABEL_PATTERNS[i];
    const m = trimmed.match(pattern.regex);
    if (!m || m[0].length === 0) continue;
    let rest = trimmed.slice(m[0].length);
    rest = rest.replace(LOOSE_LABEL_SEPARATOR_REGEX, '').replace(/\s+/g, ' ').trim();
    if (!rest) return null;
    return { key: pattern.key, value: rest };
  }
  return null;
}

const UNLABELED_PHONE_LINE_REGEX = /^\+?[\d(][\d\s\-.()]{5,20}\d$/;
function looksLikePhoneLine_(line) {
  const trimmed = String(line || '').trim();
  if (!UNLABELED_PHONE_LINE_REGEX.test(trimmed)) return false;
  const digitsOnly = trimmed.replace(/[^\d]/g, '');
  return digitsOnly.length >= 7 && digitsOnly.length <= 15;
}

const UNLABELED_PRICE_LINE_REGEX = /^(?:[#₦$€£]|N(?=\d))?\s*[\d,]+(?:\.\d+)?\s*(k)?\s*(naira|ngn|usd|dollars?)?$/i;
function looksLikePriceLine_(line) {
  const trimmed = String(line || '').trim();
  if (!UNLABELED_PRICE_LINE_REGEX.test(trimmed)) return false;
  const hasCurrencySignal = /^[#₦$€£]/.test(trimmed) || /^N(?=\d)/i.test(trimmed) || /(k|naira|ngn|usd|dollars?)\s*$/i.test(trimmed);
  const digitsOnly = trimmed.replace(/[^\d]/g, '');
  if (!digitsOnly) return false;
  return hasCurrencySignal ? digitsOnly.length >= 1 : digitsOnly.length >= 3;
}

const PRODUCT_LINE_KEYWORDS = [
  'sadoer','combo','collagen','lotion','cream','serum','set','package','kit','sachet','bottle','pack',
  'soap','toner','oil','gel','powder','spray','shampoo','conditioner','mask','scrub','wash','balm',
  'bundle','tube','jar','capsule','tablet','syrup','perfume','cologne','deodorant','sunscreen',
  'bleaching','whitening','brightening','glow','herbal','extract','vitamin','retinol','niacinamide',
  'hyaluronic','kojic','moisturizer','exfoliator','cleanser'
];
function looksLikeProductLine_(line) {
  const trimmed = String(line || '').trim();
  if (!trimmed) return false;
  const lower = trimmed.toLowerCase();
  return PRODUCT_LINE_KEYWORDS.some(function(k) { return new RegExp('\\b' + k + '\\b').test(lower); });
}

const ADDRESS_HINT_KEYWORDS = [
  'street','str','road','rd','avenue','ave','close','cres','crescent','estate','junction','opposite',
  'behind','near','state','lga','area','zone','plaza','market','gra','phase','block','flat','plot',
  'drive','lane','way','off','by',
  'lagos','abuja','ikeja','lekki','ajah','ikorodu','yaba','surulere','warri','delta','ughelli','asaba',
  'enugu','onitsha','aba','owerri','imo','anambra','port harcourt','portharcourt','rivers','kano',
  'kaduna','ibadan','oyo','ogun','abeokuta','benin','edo','uyo','calabar','cross river','jos','plateau',
  'kwara','ilorin','osun','osogbo','ekiti','ado ekiti','sokoto','katsina','borno','maiduguri','bauchi',
  'gombe','yola','adamawa','niger state','minna','kogi','lokoja','nasarawa','bayelsa','yenagoa',
  'ebonyi','abakaliki','ondo','akure'
];
function looksLikeAddressContinuation_(line) {
  const lower = String(line || '').toLowerCase();
  return ADDRESS_HINT_KEYWORDS.some(function(k) { return new RegExp('\\b' + k + '\\b').test(lower); });
}

function parseBlock(blockText) {
  blockText = blockText || '';
  const fields = {
    date: '', category: '', product: '', price: '', name: '',
    phone: '', whatsapp: '', address: '', delivery: '', question: ''
  };

  const lines = blockText.split('\n')
    .map(l => l.replace(/\t/g, ' ').trim())
    .filter(l => l.length > 0 && !/^#{2,}$/.test(l) && !PURE_SEPARATOR_LINE_REGEX.test(l));

  let currentKey = null;
  let sawFirstLabel = false;

  for (const line of lines) {
    if (/^from\s+.+/i.test(line) && sawFirstLabel) {
      currentKey = null;
      continue;
    }

    const colonIdx = line.indexOf(':');

    if (colonIdx === -1 && !BOLD_HEADING_LINE_REGEX.test(line)) {
      const loose = matchLooseLabelLine_(line);
      if (loose) {
        sawFirstLabel = true;
        currentKey = loose.key;
        fields[loose.key] = fields[loose.key] ? fields[loose.key] + ' ' + loose.value : loose.value;
        continue;
      }
    }

    const labelPart = colonIdx > -1 ? line.substring(0, colonIdx) : line;
    const restPart = colonIdx > -1 ? line.substring(colonIdx + 1).trim().replace(LOOSE_LABEL_SEPARATOR_REGEX, '').trim() : '';
    const normalized = normalizeLabel(labelPart);

    const labelWordCount = labelPart.trim().split(/\s+/).filter(Boolean).length;

    const PRICE_LABEL_KEYWORDS_ = ['price', 'amount', 'cost'];

    const looksLikeLabel = labelPart.length <= MAX_LABEL_CHARS &&
      labelWordCount <= MAX_LABEL_WORDS &&
      !BOLD_HEADING_LINE_REGEX.test(line);
    const matchedKey = looksLikeLabel ? matchLabel(normalized) : null;

    if (matchedKey) {
      sawFirstLabel = true;
      currentKey = matchedKey;
      let valueText = restPart;

      if (!valueText && colonIdx === -1 && matchedKey === 'price') {
        const words = labelPart.trim().split(/\s+/);
        const kwIdx = words.findIndex(w => PRICE_LABEL_KEYWORDS_.some(k => normalizeLabel(w).indexOf(k) === 0));
        if (kwIdx > -1) {
          const after = words.slice(kwIdx + 1).join(' ').trim();
          if (after && /\d/.test(after)) valueText = after;
        }
      }

      if (valueText) {
        fields[matchedKey] = fields[matchedKey] ? fields[matchedKey] + ' ' + valueText : valueText;
      }
      continue;
    }

    if (looksLikePhoneLine_(line)) {
      if (!fields.phone) {
        fields.phone = line;
      } else if (!fields.whatsapp) {
        fields.whatsapp = line;
      }
      continue;
    }

    if (!fields.price && looksLikePriceLine_(line)) {
      fields.price = line;
      continue;
    }

    if (BOLD_HEADING_LINE_REGEX.test(line)) {
      // Capture bold product headings like *Tiktok Body lotion* as product names
      // Skip order dates like *1st October*
      const boldMatch = line.match(/^\*(.+)\*\.?$/);
      if (boldMatch) {
        const productName = boldMatch[1].trim();
        if (productName && !/^\d{1,2}(st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*$/i.test(productName)) {
          fields.product = fields.product ? fields.product + ' ' + productName : productName;
          currentKey = 'product';
        }
      }
      continue;
    }

    if ((!currentKey || currentKey === 'product') && looksLikeProductLine_(line)) {
      currentKey = 'product';
      fields.product = fields.product ? fields.product + ' ' + line : line;
      continue;
    }

    if (currentKey === 'address' && fields.address) {
      if (looksLikeAddressContinuation_(line)) {
        fields.address = fields.address + ' ' + line;
      } else {
        currentKey = 'product';
        fields.product = fields.product ? fields.product + ' ' + line : line;
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

let _businessTimeZoneCache = null;
function getBusinessTimeZone_() {
  if (_businessTimeZoneCache === null) {
    _businessTimeZoneCache = Session.getScriptTimeZone();
  }
  return _businessTimeZoneCache;
}

function stripTime_(d) {
  const parts = Utilities.formatDate(d, getBusinessTimeZone_(), 'yyyy-MM-dd').split('-');
  return new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10));
}

function resolveOrderYear_(day, monthIndex, now) {
  const year = parseInt(Utilities.formatDate(now, getBusinessTimeZone_(), 'yyyy'), 10);
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

/* ---------------- LEGACY ONE-TIME MIGRATION ---------------- */

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