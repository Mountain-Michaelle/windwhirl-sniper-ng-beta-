const fs = require('fs');
const content = fs.readFileSync('c:/Users/Chinedu/Documents/Automation/windwhirl-sniper-ng-beta-/apps/sniper/test_order_parser.js', 'utf8');
const startMarker = 'const TEST_ORDERS = `';
const endMarker = '`;';
const startIdx = content.indexOf(startMarker);
const endIdx = content.indexOf(endMarker, startIdx);
if (startIdx >= 0 && endIdx >= 0) {
    const ordersText = content.substring(startIdx + startMarker.length, endIdx);
    
    // Test splitIntoBlocks logic
    const WHATSAPP_EXPORT_PREFIX_REGEX = /^\[\d{1,2}\/\d{1,2}(?:\/\d{2,4})?,\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?]\s*[^\n]+:\s?/;
    const WHATSAPP_EXPORT_SPLIT_REGEX = /\n(?=\[\d{1,2}\/\d{1,2}(?:\/\d{2,4})?,\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?]\s*[^\n]+:)/;
    
    console.log('Prefix regex test:', WHATSAPP_EXPORT_PREFIX_REGEX.test(ordersText));
    const chunks = ordersText.split(WHATSAPP_EXPORT_SPLIT_REGEX);
    console.log('Number of chunks: ' + chunks.length);
    chunks.forEach(function(c, i) { 
        const firstLine = c.split('\n')[0];
        console.log('Chunk ' + i + ' (' + c.length + ' chars) starts with: ' + firstLine.substring(0, 60)); 
    });
    
    // Check each order boundary
    const orderLines = ordersText.split('\n').filter(function(l) { return /^\[\d{1,2}\/\d{1,2}/.test(l); });
    console.log('\nOrder header lines found: ' + orderLines.length);
    orderLines.forEach(function(l, i) { console.log('  Order ' + (i+1) + ': ' + l.substring(0, 50)); });
} else {
    console.log('Could not find boundaries. startIdx=' + startIdx + ' endIdx=' + endIdx);
}