// 将 intelligence.json 完整数据嵌入 index.html 的 embedded-data 脚本标签
// 作为 fetch 失败时的离线兜底数据
// 注意：必须用正则精确定位 embedded-data 标签，绝不能全局替换 FALLBACK_PLACEHOLDER
// （JS 代码中有一处 `!== 'FALLBACK_PLACEHOLDER'` 判断字符串，全局替换会误伤）
const fs = require('fs');
const path = require('path');

const dir = __dirname;
const htmlPath = path.join(dir, 'index.html');
const jsonPath = path.join(dir, 'data', 'intelligence.json');

let html = fs.readFileSync(htmlPath, 'utf8');
const json = fs.readFileSync(jsonPath, 'utf8');

// 校验 JSON 有效性
let data;
try {
  data = JSON.parse(json);
} catch (e) {
  console.error('ERROR: intelligence.json is not valid JSON:', e.message);
  process.exit(1);
}

// 序列化并防止 </script 提前闭合标签（JSON.parse 会正确处理 \/ 转义）
let embedded = JSON.stringify(data).replace(/<\//g, '<\\/');

// 用正则精确替换 embedded-data 标签内容（无论内容是占位符还是旧数据）
const re = /(<script id="embedded-data" type="application\/json">)([\s\S]*?)(<\/script>)/;
if (!re.test(html)) {
  console.error('ERROR: embedded-data script tag not found in index.html');
  process.exit(1);
}
html = html.replace(re, '$1' + embedded + '$3');

fs.writeFileSync(htmlPath, html, 'utf8');
console.log('Embedded data written to index.html');
console.log('  HTML size:', fs.statSync(htmlPath).size, 'bytes');
console.log('  Embedded JSON size:', embedded.length, 'bytes');

// 验证：从 HTML 中提取嵌入数据并解析
const m = html.match(/<script id="embedded-data" type="application\/json">([\s\S]*?)<\/script>/);
if (!m) {
  console.error('ERROR: embedded script tag not found after write');
  process.exit(1);
}
const parsed = JSON.parse(m[1]);
console.log('Verification OK:');
console.log('  lastUpdated:', parsed.lastUpdated);
console.log('  companies:', parsed.companies.length);
console.log('  currentPrices:', parsed.rareEarth.currentPrices.length);
console.log('  priceHistory:', parsed.rareEarth.priceHistory.length);
console.log('  indexHistory:', parsed.rareEarth.indexHistory.length);
console.log('  activities:', parsed.activities.length);
console.log('  news:', parsed.news.length);
console.log('  JS placeholder check intact:', html.includes("!== 'FALLBACK_PLACEHOLDER'"));
