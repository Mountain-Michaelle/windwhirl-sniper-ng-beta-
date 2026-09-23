/**
 * WhatsApp Order Normalizer
 * ---------------------------------------------------------------
 * Paste raw WhatsApp order text into Column A (any row, from row 2 down).
 * The script parses it and fills the rest of the row with structured,
 * ready-to-use data. Multiple orders pasted together in one go
 * (separated by "####", or just back-to-back) are split into their
 * own rows automatically.
 *
 * COLUMN LAYOUT (most-used columns placed right up front):
 *  A: Raw Message        <- paste here
 *  B: S/N
 *  C: Customer Name
 *  D: Phone Number
 *  E: WhatsApp Number
 *  F: Note               <- your comments, always left blank by the script
 *  G: Action             <- dropdown, restricted to the fixed status list
 *  H: Check-up Date&Time <- pick a date/time here when Action = Pending
 *  I: Date               (order date, e.g. "13th July")
 *  J: Category           (e.g. "Tiktok Body lotion")
 *  K: Product
 *  L: Address
 *  M: Delivery Date
 *  N: Question
 *  O: Agent / Source
 *
 * SETUP:
 *  1. Extensions > Apps Script, replace all code with this file, Save.
 *  2. Reload the spreadsheet.
 *  3. Menu "Order Tools" > "Initialize Sheet" (run once — builds headers,
 *     the Action dropdown, and the Check-up date picker, for rows 2-1000).
 *  4. Approve permissions the first time you're asked (one-time only).
 *  5. Click a single cell in column A and paste a WhatsApp order message.
 *
 * DAILY USE:
 *  - Column G (Action) is a dropdown — click the cell, pick a status.
 *  - Pick "Pending" and the script jumps you straight to column H so you
 *    can set the check-up date/time (native Sheets calendar picker —
 *    click the cell, a calendar pops up; type the time after the date
 *    if you need one, e.g. 2026-07-14 10:00).
 *  - To see all of today's check-ups: select column H, Data > Create a
 *    filter, then filter that column by condition "Date is" -> "today".
 *    That's a native Sheets feature, so it stays accurate every day with
 *    no extra setup.
 *  - If data lands in column A without a normal paste (e.g. an import),
 *    run Order Tools > Reprocess Column A to catch it up.
 *
 * PASTE NOTE: click ONE cell in column A before pasting (not a range),
 * so the multi-line message stays inside that single cell.
 */

const CONFIG = {
  RAW_COL: 1,       // A
  SN_COL: 2,        // B
  NAME_COL: 3,      // C
  PHONE_COL: 4,     // D
  WHATSAPP_COL: 5,  // E
  NOTE_COL: 6,      // F
  ACTION_COL: 7,    // G
  CHECKUP_COL: 8,   // H
  DATE_COL: 9,        // I
  CATEGORY_COL: 10,   // J
  PRODUCT_COL: 11,    // K
  ADDRESS_COL: 12,    // L
  DELIVERY_COL: 13,   // M
  QUESTION_COL: 14,   // N
  AGENT_COL: 15,      // O

  HEADER_ROW: 1,
  FIRST_DATA_ROW: 2,
  VALIDATION_LAST_ROW: 1000, // how many rows to pre-apply dropdown/date validation to

  HEADERS: ['Raw Message','S/N','Customer Name','Phone Number','WhatsApp Number',
             'Note','Action','Check-up Date & Time','Date','Category','Product',
             'Address','Delivery Date','Question','Agent / Source']
};

const STATUS_LIST = [
  "Pending", "Confirmed", "Awaiting", "Delivered",
  "Commitment Fee Requested", "Not Picking Calls", "Switched Off",
  "Shipped", "Scheduled", "Failed", "Cancelled", "Returned",
  "Cash Remitted", "After-Sale Call", "Deleted", "Banned"
];

// Known field labels from the WhatsApp form, normalized (lowercase, no punctuation/spaces).
// .indexOf(...) === 0 allows variant labels to still match (e.g. trailing "?", inline ":").
const LABELS = [
  { key: 'product',  test: n => n === 'selectyourpackage' },
  { key: 'price',    test: n => n === 'price' },
  { key: 'name',     test: n => n === 'inputyourfullname' },
  { key: 'phone',    test: n => n === 'inputphonenumber' },
  { key: 'whatsapp', test: n => n === 'inputwhatsappnumber' },
  { key: 'address',  test: n => n.indexOf('inputfulladdress') === 0 },
  { key: 'delivery', test: n => n.indexOf('whendoyouwantustodeliver') === 0 },
  { key: 'question', test: n => n.indexOf('doyouhaveanyquestion') === 0 }
];

// Matches things like "13th July", "7th July", "13/07/2026"
const DATE_REGEX = /^\d{1,2}(st|nd|rd|th)?\s+[A-Za-z]+\.?$|^\d{1,2}[\/\-]\d{1,2}([\/\-]\d{2,4})?$/;

/* ---------------- TRIGGERS & MENU ---------------- */

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Order Tools')
    .addItem('Initialize Sheet', 'initializeSheet')
    .addItem('Reprocess Column A', 'reprocessAll')
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

    // Case 2: Action dropdown changed to "Pending" -> jump to check-up date/time
    if (col === CONFIG.ACTION_COL && row >= CONFIG.FIRST_DATA_ROW) {
      const value = e.range.getValue();
      if (value === 'Pending') {
        const checkupCell = sheet.getRange(row, CONFIG.CHECKUP_COL);
        checkupCell.activate();
        SpreadsheetApp.getActiveSpreadsheet().toast('Pick the check-up date & time.', 'Pending order', 5);
      }
    }
  } catch (err) {
    console.error(err);
  }
}

/* ---------------- MENU ACTIONS ---------------- */

function initializeSheet() {
  const sheet = SpreadsheetApp.getActiveSheet();

  const headerRange = sheet.getRange(CONFIG.HEADER_ROW, 1, 1, CONFIG.HEADERS.length);
  headerRange.setValues([CONFIG.HEADERS]);
  headerRange.setFontWeight('bold').setBackground('#4a86e8').setFontColor('#ffffff');
  sheet.setFrozenRows(1);

  const numRows = CONFIG.VALIDATION_LAST_ROW - CONFIG.FIRST_DATA_ROW + 1;

  // Phone / WhatsApp columns stay as plain text (protects leading + / 0)
  sheet.getRange(CONFIG.FIRST_DATA_ROW, CONFIG.PHONE_COL, numRows, 2).setNumberFormat('@');

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

  sheet.autoResizeColumns(1, CONFIG.HEADERS.length);
  SpreadsheetApp.getUi().alert('Sheet initialized. Paste WhatsApp messages into column A, starting row 2.');
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
  SpreadsheetApp.getUi().alert('Reprocessing complete.');
}

/* ---------------- CORE PROCESSING ---------------- */

function processRow(sheet, row, rawText) {
  const blocks = splitIntoBlocks(rawText);
  if (blocks.length === 0) return;

  writeRecord(sheet, row, blocks[0]);

  for (let i = 1; i < blocks.length; i++) {
    sheet.insertRowAfter(row + i - 1);
    writeRecord(sheet, row + i, blocks[i]);
    applyRowValidation(sheet, row + i); // new rows need their own dropdown/date validation
  }
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

  sheet.getRange(row, CONFIG.PHONE_COL, 1, 2).setNumberFormat('@');
}

function writeRecord(sheet, row, blockText) {
  const fields = parseBlock(blockText);
  const sn = nextSerialNumber(sheet, row);

  sheet.getRange(row, CONFIG.RAW_COL).setValue(blockText.trim());
  sheet.getRange(row, CONFIG.SN_COL).setValue(sn);
  sheet.getRange(row, CONFIG.NAME_COL).setValue(fields.name);
  sheet.getRange(row, CONFIG.PHONE_COL).setValue(normalizePhone(fields.phone));
  sheet.getRange(row, CONFIG.WHATSAPP_COL).setValue(normalizePhone(fields.whatsapp));
  sheet.getRange(row, CONFIG.NOTE_COL).setValue(''); // always blank, yours to fill
  // Action (col G) intentionally left untouched if already set by you; default blank on new rows.
  sheet.getRange(row, CONFIG.DATE_COL).setValue(fields.date);
  sheet.getRange(row, CONFIG.CATEGORY_COL).setValue(fields.category);
  sheet.getRange(row, CONFIG.PRODUCT_COL).setValue(fields.price ? `${fields.product} — ₦${fields.price}` : fields.product);
  sheet.getRange(row, CONFIG.ADDRESS_COL).setValue(fields.address);
  sheet.getRange(row, CONFIG.DELIVERY_COL).setValue(fields.delivery);
  sheet.getRange(row, CONFIG.QUESTION_COL).setValue(fields.question);
  sheet.getRange(row, CONFIG.AGENT_COL).setValue(fields.agent);

  sheet.getRange(row, CONFIG.PHONE_COL, 1, 2).setNumberFormat('@');
}

function nextSerialNumber(sheet, currentRow) {
  let max = 0;
  const lastRow = sheet.getLastRow();
  for (let r = CONFIG.FIRST_DATA_ROW; r <= lastRow; r++) {
    if (r === currentRow) continue;
    const v = sheet.getRange(r, CONFIG.SN_COL).getValue();
    if (typeof v === 'number' && v > max) max = v;
  }
  return max + 1;
}

/* ---------------- PARSING ---------------- */

function splitIntoBlocks(rawText) {
  let text = rawText.replace(/\r\n/g, '\n').trim();

  // Primary split: explicit "####" separator lines
  let chunks = text.split(/\n\s*#{3,}\s*\n/);

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
  return str.toLowerCase().replace(/[^a-z0-9]+/g, '');
}

function matchLabel(normalized) {
  for (const l of LABELS) {
    if (l.test(normalized)) return l.key;
  }
  return null;
}

function parseBlock(blockText) {
  const fields = {
    date: '', category: '', product: '', price: '', name: '',
    phone: '', whatsapp: '', address: '', delivery: '', question: '', agent: ''
  };

  const lines = blockText.split('\n')
    .map(l => l.replace(/\t/g, ' ').trim())
    .filter(l => l.length > 0 && !/^#{2,}$/.test(l));

  let currentKey = null;
  let sawFirstLabel = false;

  for (const line of lines) {
    // Trailing meta like "From Michael Follow-up"
    if (/^from\s+.+/i.test(line) && sawFirstLabel) {
      const val = line.replace(/^from\s+/i, '').trim();
      fields.agent = fields.agent ? fields.agent + ' ' + val : val;
      currentKey = 'agent';
      continue;
    }

    const colonIdx = line.indexOf(':');
    const labelPart = colonIdx > -1 ? line.substring(0, colonIdx) : line;
    const restPart = colonIdx > -1 ? line.substring(colonIdx + 1).trim() : '';
    const normalized = normalizeLabel(labelPart);
    const matchedKey = matchLabel(normalized);

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

function normalizePhone(raw) {
  if (!raw) return '';
  let digits = raw.replace(/[^\d+]/g, '');
  if (digits.startsWith('+234')) return digits;
  if (digits.startsWith('234')) return '+' + digits;
  if (digits.startsWith('0')) return '+234' + digits.substring(1);
  return digits;
}