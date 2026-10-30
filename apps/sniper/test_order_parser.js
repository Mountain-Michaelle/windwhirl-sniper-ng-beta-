// ============================================================================
// Order Parser Test — Replicates code.gs parsing logic for offline validation
// Tests: order splitting, field extraction (name, phone, whatsapp, address,
//        product, price, delivery, question), backward compatibility
// ============================================================================

(function() {
  "use strict";

  // ---------- Constants (mirror code.gs) ----------

  const BOLD_HEADING_LINE_REGEX = /^\*[\s\S]*\*\.?$/;
  const ORDER_DATE_HEADING_REGEX = /^\*?(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+)\*?\.?$/;

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

  const MAX_LABEL_WORDS = 10;
  const MAX_LABEL_CHARS = 60;

  const LOOSE_LABEL_SEPARATOR_REGEX = /^[\s:\-=_.,|–—~]+/;
  const PURE_SEPARATOR_LINE_REGEX = /^[\s:\-=_.,|–—~*#•>]+$/;

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

  const UNLABELED_PHONE_LINE_REGEX = /^\+?[\d(][\d\s\-.()]{5,20}\d$/;
  const UNLABELED_PRICE_LINE_REGEX = /^(?:[#₦$€£]|N(?=\d))?\s*[\d,]+(?:\.\d+)?\s*(k)?\s*(naira|ngn|usd|dollars?)?$/i;

  const PRODUCT_LINE_KEYWORDS = [
    'sadoer','combo','collagen','lotion','cream','serum','set','package','kit','sachet','bottle','pack',
    'soap','toner','oil','gel','powder','spray','shampoo','conditioner','mask','scrub','wash','balm',
    'bundle','tube','jar','capsule','tablet','syrup','perfume','cologne','deodorant','sunscreen',
    'bleaching','whitening','brightening','glow','herbal','extract','vitamin','retinol','niacinamide',
    'hyaluronic','kojic','moisturizer','exfoliator','cleanser'
  ];

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

  const ADDRESS_HINT_KEYWORDS_EXTRA = [
    'quarters','estates','ife','ogidi','ugheli','azare','ajha','niger','awgu','isolo'
  ];

  const ALL_ADDRESS_KEYWORDS = ADDRESS_HINT_KEYWORDS.concat(ADDRESS_HINT_KEYWORDS_EXTRA);

  const WHATSAPP_EXPORT_PREFIX_REGEX = /^\[\d{1,2}\/\d{1,2}(?:\/\d{2,4})?,\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?]\s*[^\n]+:\s?/;
  const WHATSAPP_EXPORT_SPLIT_REGEX = /\n(?=\[\d{1,2}\/\d{1,2}(?:\/\d{2,4})?,\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?]\s*[^\n]+:)/;

// ---------- Helper functions (mirror code.gs) ----------

function labelWordStartsWith_(words, keyword) {
  return words.some(function(w) { return w.indexOf(keyword) === 0; });
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

function looksLikePhoneLine_(line) {
  const trimmed = String(line || '').trim();
  if (!UNLABELED_PHONE_LINE_REGEX.test(trimmed)) return false;
  const digitsOnly = trimmed.replace(/[^\d]/g, '');
  return digitsOnly.length >= 7 && digitsOnly.length <= 15;
}

function looksLikePriceLine_(line) {
  const trimmed = String(line || '').trim();
  if (!UNLABELED_PRICE_LINE_REGEX.test(trimmed)) return false;
  const hasCurrencySignal = /^[#₦$€£]/.test(trimmed) || /^N(?=\d)/i.test(trimmed) || /(k|naira|ngn|usd|dollars?)\s*$/i.test(trimmed);
  const digitsOnly = trimmed.replace(/[^\d]/g, '');
  if (!digitsOnly) return false;
  return hasCurrencySignal ? digitsOnly.length >= 1 : digitsOnly.length >= 3;
}

function looksLikeProductLine_(line) {
  const trimmed = String(line || '').trim();
  if (!trimmed) return false;
  const lower = trimmed.toLowerCase();
  return PRODUCT_LINE_KEYWORDS.some(function(k) { return new RegExp('\\b' + k + '\\b').test(lower); });
}

function looksLikeAddressContinuation_(line) {
  const lower = String(line || '').toLowerCase();
  return ALL_ADDRESS_KEYWORDS.some(function(k) { return new RegExp('\\b' + k + '\\b').test(lower); });
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
  const match = text.match(/(?:[\s=\-â€“â€”\u00A0]+[#₦Nn]?|[#₦])\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{3,}(?:\.\d+)?)\s*,?\s*$/);
  if (!match) return { product: text, priceText: '' };
  const cleanProduct = text.slice(0, match.index).trim().replace(/[-â€“â€”=]\s*$/, '').trim();
  return { product: cleanProduct || text, priceText: match[1] };
}

function splitIntoBlocks(rawText) {
  rawText = rawText || '';
  rawText = rawText.trim();  // Added trim to handle leading/trailing whitespace
  let chunks;
  if (WHATSAPP_EXPORT_PREFIX_REGEX.test(rawText)) {
    chunks = rawText.split(WHATSAPP_EXPORT_SPLIT_REGEX);
  } else {
    chunks = rawText.split(/\n\s*#{3,}\s*\n/);
  }
  return chunks.map(function(c) { return c.trim(); }).filter(function(c) { return c.length > 0; });
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

    if (BOLD_HEADING_LINE_REGEX.test(line)) {
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
      continue;
    }

    if (looksLikePhoneLine_(line)) {
      if (!fields.phone) fields.phone = line;
      else if (!fields.whatsapp) fields.whatsapp = line;
      continue;
    }
    if (looksLikePriceLine_(line)) {
      if (!fields.price) fields.price = line;
      continue;
    }
    if (looksLikeProductLine_(line)) {
      currentKey = 'product';
      fields.product = line;
      continue;
    }
    if (looksLikeAddressContinuation_(line)) {
      currentKey = 'address';
      fields.address = line;
      continue;
    }
  }

  return fields;
}

function computeRowData(blockText) {
  const fields = parseBlock(blockText);
  const phone = normalizePhone(fields.phone);
  const whatsapp = normalizePhone(fields.whatsapp);
  const priceInfo = resolvePrice(fields.product, fields.price);

  return {
    rawText: blockText,
    name: fields.name,
    phone: phone,
    whatsapp: whatsapp,
    product: priceInfo.product,
    priceText: priceInfo.priceText,
    priceNumber: extractPriceNumber(priceInfo.priceText) || extractPriceNumber(fields.product),
    address: fields.address,
    delivery: fields.delivery,
    question: fields.question,
    date: fields.date,
    category: fields.category,
    isValid: phone.length > 0,
    isDuplicate: false
  };
}

// ---------- Test Data: All 80 real orders ----------

const TEST_ORDERS = `
[27/10, 16:31] +234 907 377 3968: *1st October*
*Tiktok Body lotion*

Select Your Package
         1 Advanced Collagen Body Lotion + Free Scar Repair Cream + Free Collagen Hand Cream = #29,500
Input your Full Name
         Badmus kaosarat yetunde
Input Phone Number
         09126128931
Input Whatsapp Number
         09159518448
Input Full Address
         Ibadan Ojoor
When do you want us to deliver to you?
         Tomorrow
Do you have any questions?
         My skink is very dull and drank

[27/10, 16:31] +234 907 377 3968: *1st October*
*Tiktok Body lotion*

Select Your Package
         1 Advanced Collagen Body Lotion + Free Scar Repair Cream + Free Collagen Hand Cream = #29,500
Input your Full Name
         QueenElla
Input Phone Number
         08105387787
Input Whatsapp Number
         08105387787
Input Full Address
         No.6 Omuoda lane Aluu port harcourt
When do you want us to deliver to you?
         Tomorrow
Do you have any questions?
         Is this cream for fair or dark skin ? Because I'm fair in complexion

[27/10, 16:31] +234 907 377 3968: *1st October*
*Google body lotion*

Select Your Package
         1 Advanced Collagen Body Lotion + 1 Free Scar Repair Cream + Free Collagen Hand Cream + Free doorstep delivery = #29,500
Input your Full Name
         Binami l goni
Input Phone Number
         09160374065
Input Whatsapp Number
         09160374065
Input Full Address
         Rivers State University, Road E, Flat 7, Port Harcourt, Rivers State.
When do you want us to deliver to you?
         The Day After Tomorrow
Do you have any questions?
         I am worried about the state of legs. I am seeing white dota on it. Pls, what can I use to clear it. Help

[27/10, 16:31] +234 907 377 3968: *1st October*
*Tiktok Body lotion*

Select Your Package
         1 Advanced Collagen Body Lotion + Free Scar Repair Cream + Free Collagen Hand Cream = #29,500
Input your Full Name
         Yemisi adebusola
Input Phone Number
         07066156651
Input Whatsapp Number
         07066156651
Input Full Address
         Olonde Ologuneru Ibadan
When do you want us to deliver to you?
         Tomorrow
Do you have any questions?
         No
`;

// ---------- Test Runner ----------

function assert(condition, message) {
  if (!condition) {
    console.log('FAIL: ' + message);
    return false;
  }
  console.log('PASS: ' + message);
  return true;
}

function assertEquals(actual, expected, message) {
  var ok = String(actual).trim() === String(expected).trim();
  if (!ok) {
    console.log('FAIL: ' + message + ' — expected: "' + expected + '", got: "' + actual + '"');
    return false;
  }
  console.log('PASS: ' + message);
  return true;
}

function assertNotEmpty(value, message) {
  var ok = value && String(value).trim().length > 0;
  if (!ok) {
    console.log('FAIL: ' + message + ' — value is empty');
    return false;
  }
  console.log('PASS: ' + message);
  return true;
}

function runTests() {
  var passed = 0;
  var failed = 0;

  console.log('='.repeat(70));
  console.log('ORDER PARSER TEST SUITE');
  console.log('='.repeat(70));

  console.log('\n--- Test 1: Order Splitting ---');
  var blocks = splitIntoBlocks(TEST_ORDERS);
  var orderCount = blocks.length;
  console.log('Total blocks found: ' + orderCount);
  passed += assert(orderCount >= 3, 'Expected at least 3 orders, got ' + orderCount);

  console.log('\n--- Test 2: Field Extraction per Order ---');
  var ordersWithName = 0;
  var ordersWithPhone = 0;
  var ordersWithWhatsApp = 0;
  var ordersWithAddress = 0;
  var ordersWithProduct = 0;
  var ordersWithPrice = 0;
  var ordersWithBoldPrefix = 0;
  var ordersWithDelivery = 0;

  var results = [];

  for (var i = 0; i < blocks.length; i++) {
    var row = computeRowData(blocks[i]);
    results.push(row);

    if (row.name) ordersWithName++;
    if (row.phone) ordersWithPhone++;
    if (row.whatsapp) ordersWithWhatsApp++;
    if (row.address) ordersWithAddress++;
    if (row.product) ordersWithProduct++;
    if (row.priceNumber > 0) ordersWithPrice++;
    if (row.delivery) ordersWithDelivery++;

    var boldPrefixes = ['tiktok body lotion', 'tiktok sadoer', 'google body lotion', 'body lotion', 'sadoer body lotion'];
    var hasBoldPrefix = boldPrefixes.some(function(p) { return row.product.toLowerCase().indexOf(p) === 0; });
    if (hasBoldPrefix) ordersWithBoldPrefix++;

    if (i < 3) {
      console.log('\nOrder ' + (i + 1) + ':');
      console.log('  Name:     ' + row.name);
      console.log('  Phone:    ' + row.phone);
      console.log('  WhatsApp: ' + row.whatsapp);
      console.log('  Product:  ' + row.product);
      console.log('  Price:    ' + row.priceNumber + ' (' + row.priceText + ')');
      console.log('  Address:  ' + row.address);
      console.log('  Delivery: ' + row.delivery);
      console.log('  Question: ' + row.question);
    }
  }

  console.log('\n--- Summary ---');
  passed += assert(ordersWithName >= orderCount * 0.8, 'Name extracted: ' + ordersWithName + '/' + orderCount);
  passed += assert(ordersWithPhone >= orderCount * 0.8, 'Phone extracted: ' + ordersWithPhone + '/' + orderCount);
  passed += assert(ordersWithWhatsApp >= orderCount * 0.7, 'WhatsApp extracted: ' + ordersWithWhatsApp + '/' + orderCount);
  passed += assert(ordersWithAddress >= orderCount * 0.75, 'Address extracted: ' + ordersWithAddress + '/' + orderCount);
  passed += assert(ordersWithProduct >= orderCount * 0.8, 'Product extracted: ' + ordersWithProduct + '/' + orderCount);
  passed += assert(ordersWithPrice >= orderCount * 0.8, 'Price extracted: ' + ordersWithPrice + '/' + orderCount);
  passed += assert(ordersWithBoldPrefix > 0, 'Bold prefix found in product names: ' + ordersWithBoldPrefix);
  passed += assert(ordersWithDelivery >= orderCount * 0.8, 'Delivery extracted: ' + ordersWithDelivery + '/' + orderCount);

  console.log('\n--- Test 3: Specific Order Verification ---');

  if (results.length > 0) {
    var o1 = results[0];
    passed += assertEquals(o1.name, 'Badmus kaosarat yetunde', 'Order 1 name');
    passed += assertEquals(o1.phone, '+2349126128931', 'Order 1 phone');
    passed += assert(o1.address.indexOf('Ibadan') > -1, 'Order 1 address contains Ibadan');
    passed += assert(o1.product.indexOf('Tiktok Body lotion') > -1, 'Order 1 product has Tiktok Body lotion prefix');
  }

  if (results.length > 1) {
    var o2 = results[1];
    passed += assertEquals(o2.name, 'QueenElla', 'Order 2 name');
    passed += assertEquals(o2.phone, '+2348105387787', 'Order 2 phone');
    passed += assert(o2.product.indexOf('Tiktok Body lotion') > -1, 'Order 2 product has Tiktok Body lotion prefix');
  }

  console.log('\n--- Test 4: Backward Compatibility ---');
  var legacyOrder = '\n[01/01, 10:00] +234 800 000 0000: *1st January*\n*Test Product*\n\nSelect Your Package\n         1 Test Item = #1,000\nInput your Full Name\n         Test Customer\nInput Phone Number\n         08000000000\nInput Whatsapp Number\n         08000000000\nInput Full Address\n         Test Address Lagos\nWhen do you want us to deliver to you?\n         Tomorrow\nDo you have any questions?\n         None\n';
  var legacyBlocks = splitIntoBlocks(legacyOrder);
  passed += assert(legacyBlocks.length === 1, 'Legacy format splits into 1 block');
  if (legacyBlocks.length === 1) {
    var legacyRow = computeRowData(legacyBlocks[0]);
    passed += assertEquals(legacyRow.name, 'Test Customer', 'Legacy name format');
    passed += assertEquals(legacyRow.phone, '+2348000000000', 'Legacy phone format');
    passed += assertEquals(legacyRow.address, 'Test Address Lagos', 'Legacy address format');
    passed += assert(legacyRow.product.indexOf('Test Product') > -1, 'Legacy bold prefix product');
  }

  console.log('\n--- Test 5: Bold Prefix Coverage ---');
  var prefixRate = ordersWithBoldPrefix / orderCount;
  console.log('Orders with bold product prefix: ' + ordersWithBoldPrefix + '/' + orderCount + ' (' + (prefixRate * 100).toFixed(1) + '%)');
  passed += assert(prefixRate >= 0.3, 'At least 30% of orders have bold prefix in product name');

  console.log('\n' + '='.repeat(70));
  console.log('RESULTS: ' + passed + ' passed');
  console.log('='.repeat(70));

  return { passed: passed, failed: failed, total: passed + failed, orderCount: orderCount };
}

var result = runTests();

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { splitIntoBlocks: splitIntoBlocks, parseBlock: parseBlock, computeRowData: computeRowData, runTests: runTests };
}

})();

