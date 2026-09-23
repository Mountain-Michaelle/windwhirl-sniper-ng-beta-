// ==============================================================
// WINDWHIRL OMS — GOOGLE SHEETS APPS SCRIPT
// PATH: windwhirl/apps_script/sync_trigger.gs
// ==============================================================
//
// COLUMN LAYOUT (must match google_provider.py ALL_COLUMNS exactly).
// Worker-editable fields come first so the daily-use columns are
// always the ones on screen; reference/system fields trail behind.
// Actual protection (only the service account may write outside the
// worker-editable columns) is applied server-side by Python's
// GoogleSheetsProvider.ensure_field_protection() — this script only
// adds the dropdowns/pickers and visual layout.
//
// ── WORKER-EDITABLE (A–H) ─────────────────────────────────────
//  A  customer_name
//  B  phone_number
//  C  whatsapp_number
//  D  comments             ← free text
//  E  sniper_action        ← dropdown, must match allowed list
//  F  scheduled_date       ← required only when E = "Scheduled"
//  G  scheduled_time       ← required only when E = "Scheduled"
//  H  _action              ← type DELETE to soft-archive this row
//
// ── REFERENCE (I–T) — protected, read-only for workers ────────
//  I  database_id   J  campaign          K  package
//  L  delivery_address                   M  delivery_request
//  N  order_date     O  customer_question
//  P  assigned_worker                    Q  assignment_status
//  R  duplicate_status                   S  quality_score
//  T  is_valid
//
// ── SYNC METADATA (U–X) — protected ────────────────────────────
//  U  sync_status   V  created_at   W  updated_at   X  google_row_id
//
// ── CONTROL (Y–Z) — protected, Y is also hidden ────────────────
//  Y  _row_key       (hidden + protected, the true match key)
//  Z  _sync_note      (protected — sync engine writes status here)
//
// ── TRIGGER CELLS (outside the data range) ─────────────────────
//  AA1  REQUESTED / IDLE   ← "Sync Now" button writes here
//  AA2  ISO timestamp      ← onEdit() writes here on every edit
// ==============================================================

var COL = {
  CUSTOMER_NAME:     1,   // A
  PHONE_NUMBER:      2,   // B
  WHATSAPP_NUMBER:   3,   // C
  COMMENTS:          4,   // D
  SNIPER_ACTION:     5,   // E
  SCHED_DATE:        6,   // F
  SCHED_TIME:        7,   // G
  ACTION:            8,   // H

  DATABASE_ID:       9,   // I
  CAMPAIGN:          10,  // J
  PACKAGE:           11,  // K
  DELIVERY_ADDRESS:  12,  // L
  DELIVERY_REQUEST:  13,  // M
  ORDER_DATE:        14,  // N
  CUSTOMER_QUESTION: 15,  // O
  ASSIGNED_WORKER:   16,  // P
  ASSIGNMENT_STATUS: 17,  // Q
  DUPLICATE_STATUS:  18,  // R
  QUALITY_SCORE:     19,  // S
  IS_VALID:          20,  // T

  SYNC_STATUS:       21,  // U
  CREATED_AT:        22,  // V
  UPDATED_AT:        23,  // W
  GOOGLE_ROW_ID:     24,  // X

  ROW_KEY:           25,  // Y  — hidden + protected
  SYNC_NOTE:         26,  // Z  — protected
};

var TRIGGER_CELL   = 'AA1';
var LAST_EDIT_CELL = 'AA2';

var SNIPER_ACTION_STATUSES = [
  'Pending', 'Confirmed', 'Awaiting', 'Delivered',
  'Commitment Fee Requested', 'Not Picking Calls', 'Switched Off',
  'Shipped', 'Scheduled', 'Failed', 'Cancelled', 'Returned',
  'Cash Remitted', 'After-Sale Call', 'Deleted', 'Banned',
];


// ==============================================================
// MENU
// ==============================================================

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('OMS Sync')
    .addItem('Sync Now', 'requestSync')
    .addSeparator()
    .addItem('Setup Sheet (run once)', 'runAllSetup')
    .addToUi();
}


// ==============================================================
// SYNC TRIGGER
// ==============================================================

function requestSync() {
  var sheet = SpreadsheetApp.getActiveSheet();
  sheet.getRange(TRIGGER_CELL).setValue('REQUESTED');
  SpreadsheetApp.getUi().alert(
    'Sync requested.\\n' +
    'It will run once your edits settle (about 60 seconds).'
  );
}


// ==============================================================
// onEdit — stamps last-edit time + manages Scheduled date/time
// ==============================================================

function onEdit(e) {
  var sheet       = e.range.getSheet();
  var editedRange = e.range;

  // Always stamp the last-edit time so the Python side can debounce
  // and schedule the 30-min-after-last-edit auto-sync correctly.
  // AA2 sits outside the data area, safe to touch on every edit.
  sheet.getRange(LAST_EDIT_CELL).setValue(new Date().toISOString());

  // Only handle single-cell edits in real data rows below this point.
  if (editedRange.getNumRows() !== 1 || editedRange.getNumColumns() !== 1) return;
  var row = editedRange.getRow();
  var col = editedRange.getColumn();
  if (row < 2) return;  // ignore header row

  if (col !== COL.SNIPER_ACTION) return;

  var newValue = editedRange.getValue();
  var dateCell = sheet.getRange(row, COL.SCHED_DATE);
  var timeCell = sheet.getRange(row, COL.SCHED_TIME);

  if (newValue === 'Scheduled') {
    var dateRule = SpreadsheetApp.newDataValidation()
      .requireDate()
      .setAllowInvalid(false)
      .setHelpText('Pick the scheduled delivery date.')
      .build();
    dateCell.setDataValidation(dateRule);
    dateCell.setNumberFormat('dd/mm/yyyy');

    // Sheets has no dedicated "time" validation rule, so this still
    // uses requireDate() (times are stored as date fractions), but we
    // format the cell as a time so it displays and enters like one.
    var timeRule = SpreadsheetApp.newDataValidation()
      .requireDate()
      .setAllowInvalid(false)
      .setHelpText('Pick the scheduled delivery time.')
      .build();
    timeCell.setDataValidation(timeRule);
    timeCell.setNumberFormat('hh:mm');

    SpreadsheetApp.getActive().toast(
      'Fill in Scheduled Date (F) and Time (G) for this row — required for "Scheduled" to be accepted.',
      'Scheduled Delivery',
      6
    );
  } else {
    // Status changed away from "Scheduled" → clear stale date/time so
    // an old schedule can never linger under a different status.
    dateCell.clearContent().clearDataValidations();
    timeCell.clearContent().clearDataValidations();
  }
}


// ==============================================================
// ONE-TIME SETUP — Extensions → Apps Script → select runAllSetup → Run
// ==============================================================

function runAllSetup() {
  setupSniperActionDropdown();
  setupActionDropdown();
  setupScheduledColumnFormatting();
  applyColumnWidths();
  applyHeaderFormatting();
  hideRowKeyColumn();
  SpreadsheetApp.getUi().alert(
    'Setup complete.\\n\\n' +
    '• Sniper Action dropdown active (column E)\\n' +
    '• Delete action dropdown active (column H)\\n' +
    '• Scheduled Date / Time formatting ready (F, G)\\n' +
    '• Column widths + header colours applied\\n' +
    '• _row_key (Y) hidden\\n\\n' +
    'Note: locking columns I–Z to system-only edits is handled by the ' +
    'backend (ensure_field_protection) the first time it connects.'
  );
}

function setupSniperActionDropdown() {
  var sheet = SpreadsheetApp.getActiveSheet();
  var range = sheet.getRange(2, COL.SNIPER_ACTION, sheet.getMaxRows() - 1, 1);
  var rule  = SpreadsheetApp.newDataValidation()
    .requireValueInList(SNIPER_ACTION_STATUSES, true)  // suggestion dropdown while typing
    .setAllowInvalid(false)                             // off-list values rejected client-side too
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

function setupScheduledColumnFormatting() {
  var sheet = SpreadsheetApp.getActiveSheet();
  sheet.getRange(1, COL.SCHED_DATE).setValue('Scheduled Date');
  sheet.getRange(1, COL.SCHED_TIME).setValue('Scheduled Time');
}


// ==============================================================
// COLUMN WIDTHS — grouped, readable, single source of truth (COL.*)
// ==============================================================

function applyColumnWidths() {
  var sheet  = SpreadsheetApp.getActiveSheet();
  var widths = {};

  // Worker-editable — the columns used every day, generous widths
  widths[COL.CUSTOMER_NAME]     = 150;
  widths[COL.PHONE_NUMBER]      = 130;
  widths[COL.WHATSAPP_NUMBER]   = 130;
  widths[COL.COMMENTS]          = 220;
  widths[COL.SNIPER_ACTION]     = 170;
  widths[COL.SCHED_DATE]        = 120;
  widths[COL.SCHED_TIME]        = 100;
  widths[COL.ACTION]            = 90;

  // Reference — read-only, keep tighter
  widths[COL.DATABASE_ID]       = 110;
  widths[COL.CAMPAIGN]          = 110;
  widths[COL.PACKAGE]           = 150;
  widths[COL.DELIVERY_ADDRESS]  = 200;
  widths[COL.DELIVERY_REQUEST]  = 120;
  widths[COL.ORDER_DATE]        = 100;
  widths[COL.CUSTOMER_QUESTION] = 180;
  widths[COL.ASSIGNED_WORKER]   = 120;
  widths[COL.ASSIGNMENT_STATUS] = 120;
  widths[COL.DUPLICATE_STATUS]  = 120;
  widths[COL.QUALITY_SCORE]     = 90;
  widths[COL.IS_VALID]          = 70;

  // Sync metadata — narrow
  widths[COL.SYNC_STATUS]       = 100;
  widths[COL.CREATED_AT]        = 130;
  widths[COL.UPDATED_AT]        = 130;
  widths[COL.GOOGLE_ROW_ID]     = 90;

  // Control
  widths[COL.ROW_KEY]           = 100;
  widths[COL.SYNC_NOTE]         = 220;

  for (var col in widths) {
    sheet.setColumnWidth(parseInt(col), widths[col]);
  }
}


// ==============================================================
// HEADER FORMATTING — colour-coded groups
// ==============================================================

function applyHeaderFormatting() {
  var sheet  = SpreadsheetApp.getActiveSheet();
  var header = sheet.getRange(1, 1, 1, COL.SYNC_NOTE);  // A1:Z1

  header
    .setFontWeight('bold')
    .setFontSize(10)
    .setFontColor('#FFFFFF')
    .setVerticalAlignment('middle')
    .setWrapStrategy(SpreadsheetApp.WrapStrategy.WRAP);
  sheet.setRowHeight(1, 40);

  var groups = [
    // [startCol, endCol, hex background]
    [COL.CUSTOMER_NAME,     COL.ACTION,            '#1E6B3C'],  // Worker-editable — green
    [COL.DATABASE_ID,       COL.IS_VALID,           '#1B4F8A'],  // Reference — blue
    [COL.SYNC_STATUS,       COL.GOOGLE_ROW_ID,      '#4A4A4A'],  // Sync metadata — grey
    [COL.ROW_KEY,           COL.SYNC_NOTE,          '#7A0000'],  // Control — dark red
  ];

  groups.forEach(function(g) {
    sheet.getRange(1, g[0], 1, g[1] - g[0] + 1).setBackground(g[2]);
  });

  sheet.setFrozenRows(1);
  sheet.setFrozenColumns(3);  // keep customer_name/phone/whatsapp visible while scrolling
}


// ==============================================================
// HIDE THE TRUE MATCH KEY
// _sync_note (Z) stays visible so workers can see sync results;
// only _row_key (Y) is hidden — it's meaningless to a worker and
// its only job is letting the backend match rows safely.
// ==============================================================

function hideRowKeyColumn() {
  var sheet = SpreadsheetApp.getActiveSheet();
  sheet.hideColumns(COL.ROW_KEY);
}
