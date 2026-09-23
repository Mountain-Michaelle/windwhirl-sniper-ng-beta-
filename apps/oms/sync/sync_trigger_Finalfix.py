cat > /home/claude/sync_trigger_final.gs << 'ENDOFFILE'
// ==============================================================
// WINDWHIRL OMS — GOOGLE SHEETS APPS SCRIPT
// PATH: windwhirl/apps_script/sync_trigger.gs
// ==============================================================
//
// COLUMN LAYOUT (must match google_provider.py ALL_COLUMNS exactly):
//
// ── IDENTITY ──────────────────────────────────────────────────
//  A  database_id       (cosmetic — real key is _row_key)
//  B  campaign
//
// ── CUSTOMER ──────────────────────────────────────────────────
//  C  customer_name
//  D  phone_number
//  E  whatsapp_number
//  F  delivery_address
//  G  delivery_request
//  H  order_date
//  I  customer_question
//
// ── ORDER DETAILS ─────────────────────────────────────────────
//  J  package
//  K  quality_score
//  L  is_valid
//
// ── ASSIGNMENT & STATUS ───────────────────────────────────────
//  M  assigned_worker
//  N  assignment_status
//  O  duplicate_status
//  P  sniper_action       ← worker editable, dropdown enforced
//  Q  comments            ← worker editable, free text
//
// ── SYNC METADATA ─────────────────────────────────────────────
//  R  sync_status
//  S  created_at
//  T  updated_at
//  U  google_row_id
//
// ── CONTROL COLUMNS (system use — protected/hidden) ───────────
//  V  _row_key            ← hidden + protected, never edit
//  W  _action             ← type DELETE to soft-delete
//  X  _sync_note          ← sync engine writes status here
//
// ── TRIGGER CELLS (outside data range) ───────────────────────
//  Z1  REQUESTED / IDLE   ← "Sync Now" button writes here
//  Z2  ISO timestamp      ← onEdit() writes here on every edit
// ==============================================================

// Fixed column indices (1-based, matching layout above)
var COL = {
  DATABASE_ID:       1,   // A
  CAMPAIGN:          2,   // B
  CUSTOMER_NAME:     3,   // C
  PHONE_NUMBER:      4,   // D
  WHATSAPP_NUMBER:   5,   // E
  DELIVERY_ADDRESS:  6,   // F
  DELIVERY_REQUEST:  7,   // G
  ORDER_DATE:        8,   // H
  CUSTOMER_QUESTION: 9,   // I
  PACKAGE:           10,  // J
  QUALITY_SCORE:     11,  // K
  IS_VALID:          12,  // L
  ASSIGNED_WORKER:   13,  // M
  ASSIGNMENT_STATUS: 14,  // N
  DUPLICATE_STATUS:  15,  // O
  SNIPER_ACTION:     16,  // P
  COMMENTS:          17,  // Q
  SYNC_STATUS:       18,  // R
  CREATED_AT:        19,  // S
  UPDATED_AT:        20,  // T
  GOOGLE_ROW_ID:     21,  // U
  ROW_KEY:           22,  // V  — hidden + protected
  ACTION:            23,  // W  — DELETE to soft-delete
  SYNC_NOTE:         24,  // X  — sync engine writes here
};

// Scheduled date/time columns — added only when Sniper Action = "Scheduled"
var SCHED_DATE_COL = 25;  // Y
var SCHED_TIME_COL = 26;  // Z  (note: Z1 and Z2 are trigger cells; data rows start at Z2+ only if needed)

// Because Z1 and Z2 are used as trigger cells, push Scheduled columns
// to AA and AB to avoid collision.
var SCHED_DATE_COL = 27;  // AA
var SCHED_TIME_COL = 28;  // AB


// ==============================================================
// MENU
// ==============================================================

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('OMS Sync')
    .addItem('Sync Now', 'requestSync')
    .addSeparator()
    .addItem('Setup Dropdowns (run once)', 'runAllSetup')
    .addToUi();
}


// ==============================================================
// SYNC TRIGGER
// ==============================================================

function requestSync() {
  var sheet = SpreadsheetApp.getActiveSheet();
  sheet.getRange('Z1').setValue('REQUESTED');
  SpreadsheetApp.getUi().alert(
    'Sync requested.\n' +
    'It will run once your edits settle (about 60 seconds).'
  );
}


// ==============================================================
// onEdit — stamps last-edit time + handles Scheduled date/time
// ==============================================================

function onEdit(e) {
  var sheet        = e.range.getSheet();
  var editedRange  = e.range;
  var col          = editedRange.getColumn();
  var row          = editedRange.getRow();

  // Always stamp the last-edit time so Python can debounce correctly.
  // Z2 is outside the data area — safe to write on every edit.
  sheet.getRange('Z2').setValue(new Date().toISOString());

  // Only handle single-cell edits in data rows.
  if (editedRange.getNumRows() !== 1 || editedRange.getNumColumns() !== 1) return;
  if (row < 2) return;  // ignore header row

  // When Sniper Action column (P = col 16) changes:
  if (col === COL.SNIPER_ACTION) {
    var newValue = editedRange.getValue();
    var dateCell = sheet.getRange(row, SCHED_DATE_COL);
    var timeCell = sheet.getRange(row, SCHED_TIME_COL);

    if (newValue === 'Scheduled') {
      // Add date/time pickers in AA and AB for this row.
      var dateRule = SpreadsheetApp.newDataValidation()
        .requireDate()
        .setAllowInvalid(false)
        .setHelpText('Pick the scheduled delivery date (AA)')
        .build();
      var timeRule = SpreadsheetApp.newDataValidation()
        .requireDate()
        .setAllowInvalid(false)
        .setHelpText('Pick the scheduled delivery time (AB)')
        .build();
      dateCell.setDataValidation(dateRule);
      timeCell.setDataValidation(timeRule);
      SpreadsheetApp.getActive().toast(
        'Fill in Scheduled Date (AA) and Time (AB) for this row.',
        'Scheduled Delivery',
        6
      );
    } else {
      // Clear date/time pickers when status changes away from Scheduled.
      dateCell.clearContent().clearDataValidations();
      timeCell.clearContent().clearDataValidations();
    }
  }
}


// ==============================================================
// ONE-TIME SETUP — run manually once after the sheet is created.
// Extensions → Apps Script → select function → Run
// ==============================================================

function runAllSetup() {
  setupSniperActionDropdown();
  setupActionDropdown();
  setupScheduledColumns();
  applyColumnWidths();
  applyHeaderFormatting();
  SpreadsheetApp.getUi().alert(
    'Setup complete.\n\n' +
    '• Sniper Action dropdown active (column P)\n' +
    '• Delete action dropdown active (column W)\n' +
    '• Scheduled Date / Time columns ready (AA, AB)\n' +
    '• Column widths applied\n' +
    '• Header row formatted'
  );
}

function setupSniperActionDropdown() {
  var sheet    = SpreadsheetApp.getActiveSheet();
  var statuses = [
    'Pending',
    'Confirmed',
    'Awaiting',
    'Delivered',
    'Commitment Fee Requested',
    'Not Picking Calls',
    'Switched Off',
    'Shipped',
    'Scheduled',
    'Failed',
    'Cancelled',
    'Returned',
    'Cash Remitted',
    'After-Sale Call',
    'Deleted',
    'Banned',
  ];
  var range = sheet.getRange(2, COL.SNIPER_ACTION, sheet.getMaxRows() - 1, 1);
  var rule  = SpreadsheetApp.newDataValidation()
    .requireValueInList(statuses, true)
    .setAllowInvalid(false)
    .setHelpText('Pick a status — values outside this list are rejected.')
    .build();
  range.setDataValidation(rule);
}

function setupActionDropdown() {
  var sheet = SpreadsheetApp.getActiveSheet();
  var range = sheet.getRange(2, COL.ACTION, sheet.getMaxRows() - 1, 1);
  var rule  = SpreadsheetApp.newDataValidation()
    .requireValueInList(['', 'DELETE'], true)
    .setAllowInvalid(false)
    .setHelpText('Set to DELETE to soft-archive this order.')
    .build();
  range.setDataValidation(rule);
}

function setupScheduledColumns() {
  var sheet = SpreadsheetApp.getActiveSheet();
  sheet.getRange(1, SCHED_DATE_COL).setValue('Scheduled Date');
  sheet.getRange(1, SCHED_TIME_COL).setValue('Scheduled Time');
  sheet.setColumnWidth(SCHED_DATE_COL, 130);
  sheet.setColumnWidth(SCHED_TIME_COL, 110);
}


// ==============================================================
// COLUMN WIDTHS
// Sets each group of columns to a consistent, readable width.
// Adjust numbers here — everything else updates automatically
// because COL.* constants are the single source of truth.
// ==============================================================

function applyColumnWidths() {
  var sheet = SpreadsheetApp.getActiveSheet();

  var widths = {};

  // Identity
  widths[COL.DATABASE_ID]       = 120;
  widths[COL.CAMPAIGN]          = 120;

  // Customer — slightly wider for address/question
  widths[COL.CUSTOMER_NAME]     = 150;
  widths[COL.PHONE_NUMBER]      = 130;
  widths[COL.WHATSAPP_NUMBER]   = 130;
  widths[COL.DELIVERY_ADDRESS]  = 200;
  widths[COL.DELIVERY_REQUEST]  = 120;
  widths[COL.ORDER_DATE]        = 100;
  widths[COL.CUSTOMER_QUESTION] = 180;

  // Order details
  widths[COL.PACKAGE]           = 160;
  widths[COL.QUALITY_SCORE]     = 90;
  widths[COL.IS_VALID]          = 70;

  // Assignment & status — keep these tight so workers can scan
  widths[COL.ASSIGNED_WORKER]   = 130;
  widths[COL.ASSIGNMENT_STATUS] = 130;
  widths[COL.DUPLICATE_STATUS]  = 130;
  widths[COL.SNIPER_ACTION]     = 150;
  widths[COL.COMMENTS]          = 200;

  // Sync metadata — narrower, less critical for daily use
  widths[COL.SYNC_STATUS]       = 100;
  widths[COL.CREATED_AT]        = 130;
  widths[COL.UPDATED_AT]        = 130;
  widths[COL.GOOGLE_ROW_ID]     = 90;

  // Control columns
  widths[COL.ROW_KEY]           = 100;
  widths[COL.ACTION]            = 80;
  widths[COL.SYNC_NOTE]         = 160;

  for (var col in widths) {
    sheet.setColumnWidth(parseInt(col), widths[col]);
  }
}


// ==============================================================
// HEADER FORMATTING
// Colour-coded groups so related fields are visually obvious.
// ==============================================================

function applyHeaderFormatting() {
  var sheet  = SpreadsheetApp.getActiveSheet();
  var header = sheet.getRange(1, 1, 1, 28);  // A1:AB1

  // Base style for all headers
  header
    .setFontWeight('bold')
    .setFontSize(10)
    .setFontColor('#FFFFFF')
    .setVerticalAlignment('middle')
    .setWrapStrategy(SpreadsheetApp.WrapStrategy.WRAP);
  sheet.setRowHeight(1, 40);

  // Colour groups — each group gets its own background
  var groups = [
    // [startCol, endCol, hex background]
    [COL.DATABASE_ID,       COL.CAMPAIGN,          '#1B4F8A'],  // Identity — dark blue
    [COL.CUSTOMER_NAME,     COL.CUSTOMER_QUESTION, '#1E6B3C'],  // Customer — dark green
    [COL.PACKAGE,           COL.IS_VALID,          '#5B4A00'],  // Order details — dark gold
    [COL.ASSIGNED_WORKER,   COL.COMMENTS,          '#4A1A6B'],  // Assignment/status — dark purple
    [COL.SYNC_STATUS,       COL.GOOGLE_ROW_ID,     '#4A4A4A'],  // Sync metadata — dark grey
    [COL.ROW_KEY,           COL.SYNC_NOTE,         '#7A0000'],  // Control — dark red
    [SCHED_DATE_COL,        SCHED_TIME_COL,        '#1A5C5C'],  // Scheduled — dark teal
  ];

  groups.forEach(function(g) {
    sheet.getRange(1, g[0], 1, g[1] - g[0] + 1).setBackground(g[2]);
  });

  // Freeze header row and identity columns so they stay visible while scrolling
  sheet.setFrozenRows(1);
  sheet.setFrozenColumns(2);  // Freeze A and B (database_id + campaign)
}


// ==============================================================
// HIDE CONTROL COLUMNS
// Run once after setup to hide _row_key (V), keep _action (W)
// and _sync_note (X) visible since workers interact with them.
// ==============================================================

function hideSystemColumns() {
  var sheet = SpreadsheetApp.getActiveSheet();
  // Hide _row_key (column V = index 22)
  sheet.hideColumns(COL.ROW_KEY);
  SpreadsheetApp.getUi().alert(
    'Column V (_row_key) is now hidden.\n' +
    'Columns W (_action) and X (_sync_note) remain visible.'
  );
}
ENDOFFILE
echo "Lines: $(wc -l < /home/claude/sync_trigger_final.gs)"