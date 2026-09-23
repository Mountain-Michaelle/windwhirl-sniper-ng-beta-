// -- Menu: adds a "Sync" menu with a "Sync Now" button --
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Sync')
    .addItem('Sync Now', 'requestSync')
    .addToUi();
}

function requestSync() {
  var sheet = SpreadsheetApp.getActiveSheet();
  sheet.getRange('Z1').setValue('REQUESTED');
  SpreadsheetApp.getUi().alert('Sync requested — it will run once your edits settle.');
}

// -- onEdit: stamps the last-edit time on ANY edit. This is what
//    lets the Python side debounce (wait for edits to settle) and
//    schedule the 30-min-after-last-edit auto-sync. --
function onEdit(e) {
  var sheet = e.range.getSheet();
  sheet.getRange('Z2').setValue(new Date().toISOString());
}

// -- One-time setup: run manually once from the Apps Script editor.
//    Adds a dropdown to the _action column so users pick "DELETE"
//    from a list instead of free-typing it (avoids typos silently
//    failing to trigger, and avoids accidental case mismatches). --
function setupActionDropdown() {
  var sheet = SpreadsheetApp.getActiveSheet();
  var actionColIndex = sheet.getRange('W1:W1').getColumn(); // "_action" column
  var range = sheet.getRange(2, actionColIndex, sheet.getMaxRows() - 1, 1);
  var rule = SpreadsheetApp.newDataValidation()
    .requireValueInList(['', 'DELETE'], true)
    .setAllowInvalid(false)
    .build();
  range.setDataValidation(rule);
}

// -- One-time setup: run manually once. Adds a searchable dropdown to
//    the "Sniper Action" column — as the worker types, Sheets narrows
//    the suggestion list to matching statuses. setAllowInvalid(false)
//    means only a listed status (or blank) can actually be entered,
//    matching the Python-side normalize_sniper_action() allow-list. --
function setupSniperActionDropdown() {
  var sheet = SpreadsheetApp.getActiveSheet();
  var statuses = [
    'Pending', 'Confirmed', 'Awaiting', 'Delivered',
    'Commitment Fee Requested', 'Not Picking Calls', 'Switched Off',
    'Shipped', 'Scheduled', 'Failed', 'Cancelled', 'Returned',
    'Cash Remitted', 'After-Sale Call', 'Deleted', 'Banned',
  ];
  var sniperActionColIndex = sheet.getRange('T1:T1').getColumn(); // "Sniper Action" column
  var range = sheet.getRange(2, sniperActionColIndex, sheet.getMaxRows() - 1, 1);
  var rule = SpreadsheetApp.newDataValidation()
    .requireValueInList(statuses, true)   // true = show as suggestion dropdown while typing
    .setAllowInvalid(false)               // blank is still allowed; anything off-list is rejected
    .setHelpText('Pick a status — free typing outside this list is rejected.')
    .build();
  range.setDataValidation(rule);
}
