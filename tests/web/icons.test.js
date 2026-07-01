// 图标模块单测（node:test，零依赖）。运行：node --test tests/web/
// 保证：图标来自统一来源、属性一致、app.js 引用的名字都存在（防手滑写错名字导致空图标）。
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { LUCIDE, icon } = require("../../web/icons.js");

test("icon() 产出统一属性的 svg（viewBox24 / stroke-width2 / 圆角），尺寸随参数", () => {
  const svg = icon("plus", 15);
  assert.match(svg, /viewBox="0 0 24 24"/);
  assert.match(svg, /stroke-width="2"/);
  assert.match(svg, /stroke-linecap="round"/);
  assert.match(svg, /width="15" height="15"/);
  assert.ok(svg.startsWith("<svg") && svg.endsWith("</svg>"));
});

test("默认尺寸 16、未知图标名返回空内容但仍是合法 svg（不抛错）", () => {
  assert.match(icon("plus"), /width="16" height="16"/);
  const unknown = icon("no-such-icon", 12);
  assert.match(unknown, /^<svg[\s\S]*><\/svg>$/);
});

test("每个矢量都非空、且不含外层 <svg>（只存内部元素）", () => {
  for (const [name, inner] of Object.entries(LUCIDE)) {
    assert.ok(inner && inner.length > 0, `${name} 矢量为空`);
    assert.ok(!/<svg/i.test(inner), `${name} 不应包含外层 svg`);
  }
});

test("app.js 里引用的图标名都在 LUCIDE 中（防拼写错导致空图标）", () => {
  const app = fs.readFileSync(path.join(__dirname, "../../web/app.js"), "utf8");
  const names = new Set();
  for (const m of app.matchAll(/\bsvgIcon\(["']([\w-]+)["']\)/g)) names.add(m[1]);
  for (const m of app.matchAll(/\bicon\(["']([\w-]+)["']/g)) names.add(m[1]);
  // 动态名（三元里的 sun/moon）单独补充
  names.add("sun"); names.add("moon");
  const missing = [...names].filter((n) => !(n in LUCIDE));
  assert.deepEqual(missing, [], "这些图标名不在 icons.js：" + missing.join(", "));
});

test("index.html 里的 data-icon 占位名都在 LUCIDE 中（注水后不会出空图标）", () => {
  const html = fs.readFileSync(path.join(__dirname, "../../web/index.html"), "utf8");
  const names = [...html.matchAll(/data-icon="([\w-]+)"/g)].map((m) => m[1]);
  assert.ok(names.length >= 16, "index.html 里的静态图标占位应有 16 个，实际 " + names.length);
  const missing = names.filter((n) => !(n in LUCIDE));
  assert.deepEqual(missing, [], "这些 data-icon 不在 icons.js：" + missing.join(", "));
});

test("index.html 内联 <svg> 只剩品牌 logo（图标全走 icons.js 注水，logo 是唯一例外）", () => {
  const html = fs.readFileSync(path.join(__dirname, "../../web/index.html"), "utf8");
  // 唯一允许的内联 svg 是 class="brand-logo"（带渐变的品牌徽章，非可复用图标）
  const stray = html.match(/<svg(?![^>]*class="brand-logo")/g) || [];
  assert.equal(stray.length, 0, "index.html 出现了非 logo 的内联 svg，应改走 icons.js");
  assert.equal((html.match(/class="brand-logo"/g) || []).length, 1, "应恰好有 1 个品牌 logo");
});
