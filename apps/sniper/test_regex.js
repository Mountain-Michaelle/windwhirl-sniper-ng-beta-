const WHATSAPP_EXPORT_PREFIX_REGEX = /^\[\d{1,2}\/\d{1,2}(?:\/\d{2,4})?,\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?]\s*[^\n]+:\s?/;
const WHATSAPP_EXPORT_SPLIT_REGEX = /\n(?=\[\d{1,2}\/\d{1,2}(?:\/\d{2,4})?,\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?]\s*[^\n]+:)/;

const testText = `[27/10, 16:31] +234 907 377 3968: *1st October*
*Tiktok Body lotion*

Select Your Package
         1 Advanced Collagen Body Lotion = #29,500
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
         1 Advanced Collagen Body Lotion = #29,500
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
         Is this cream for fair or dark skin ?`;

console.log('Prefix regex test:', WHATSAPP_EXPORT_PREFIX_REGEX.test(testText));
const chunks = testText.split(WHATSAPP_EXPORT_SPLIT_REGEX);
console.log('Number of chunks:', chunks.length);
chunks.forEach(function(c, i) { console.log('Chunk ' + i + ' starts with:', c.substring(0, 50)); });