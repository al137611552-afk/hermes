// 分批评审的分屏渲染纯逻辑。运行：node --test tests/web/
// 背景：分批后**同一轮里同一角色会说多次**，而发言块原本按 (轮, 角色) 索引——第 2 批的流式文本
// 会追加进第 1 批的气泡、且第 1 批的结论被覆盖（挤成一团）。这几个纯函数是修法的地基。
const test = require("node:test");
const assert = require("node:assert");
const { debateBatchSuffix, debateBatchSepText, debateTurnKey } = require("../../web/pure.js");

test("单批不加后缀——日常方案界面零变化", () => {
  assert.equal(debateBatchSuffix(1, 1), "");
  assert.equal(debateBatchSuffix(0, 0), "");
  assert.equal(debateBatchSuffix(undefined, undefined), "");
  assert.equal(debateBatchSuffix(1, undefined), "");
});

test("多批时标签说清第几批", () => {
  assert.equal(debateBatchSuffix(1, 3), " · 第 1/3 批");
  assert.equal(debateBatchSuffix(3, 3), " · 第 3/3 批");
});

test("发言块索引键：分批后 (轮,角色) 不再唯一", () => {
  // 第 1 批沿用原键——存量单批会话的 DOM 键不变
  assert.equal(debateTurnKey("product", 1), "product");
  assert.equal(debateTurnKey("product", 0), "product");
  assert.equal(debateTurnKey("product", undefined), "product");
  // 第 2 批起必须是**不同的键**，否则会写进第 1 批那块
  assert.equal(debateTurnKey("product", 2), "product#2");
  assert.notEqual(debateTurnKey("product", 2), debateTurnKey("product", 1));
  assert.notEqual(debateTurnKey("technical", 2), debateTurnKey("product", 2));
});

test("批次分隔条写出本批评了哪几条决策", () => {
  assert.equal(debateBatchSepText(1, 2, ["d1", "d2", "d3"]), "第 1/2 批：d1、d2、d3");
  assert.equal(debateBatchSepText(2, 2, []), "第 2/2 批");
  assert.equal(debateBatchSepText(2, 2, null), "第 2/2 批");
  assert.equal(debateBatchSepText(undefined, undefined, undefined), "第 1/1 批");
  assert.equal(debateBatchSepText(1, 2, ["d1", null, "", "d2"]), "第 1/2 批：d1、d2");
});
