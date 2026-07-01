// 会话列表分组渲染计划纯逻辑单测（node:test，零依赖）。运行：node --test tests/web/
// 对齐 Figma 侧栏：置顶归「已置顶」、其余归「最近」；无置顶时保持扁平列表（不加分组标题）。
const test = require("node:test");
const assert = require("node:assert");
const { planSessionList } = require("../../web/pure.js");

const S = (id, pinned) => ({ id, title: "会话" + id, pinned: !!pinned });
const labels = (plan) => plan.filter((r) => r.type === "group").map((r) => r.label);
const itemIds = (plan) => plan.filter((r) => r.type === "item").map((r) => r.session.id);

test("空列表 → 空计划", () => {
  assert.deepEqual(planSessionList([]), []);
  assert.deepEqual(planSessionList(null), []);
});

test("无置顶 → 扁平列表、不加任何分组标题", () => {
  const plan = planSessionList([S(1), S(2), S(3)]);
  assert.deepEqual(labels(plan), []);            // 无标题
  assert.deepEqual(itemIds(plan), [1, 2, 3]);    // 原序
});

test("有置顶 → 已置顶 + 最近 两段，顺序正确", () => {
  const plan = planSessionList([S(1), S(2, true), S(3), S(4, true)]);
  assert.deepEqual(labels(plan), ["已置顶", "最近"]);
  // 计划顺序：[已置顶][2][4][最近][1][3]
  assert.deepEqual(plan.map((r) => r.type === "group" ? r.label : r.session.id),
    ["已置顶", 2, 4, "最近", 1, 3]);
});

test("全部置顶 → 只有「已置顶」标题、无「最近」", () => {
  const plan = planSessionList([S(1, true), S(2, true)]);
  assert.deepEqual(labels(plan), ["已置顶"]);     // 无「最近」空标题
  assert.deepEqual(itemIds(plan), [1, 2]);
});

// —— 一段真实使用流程：新建 → 置顶 → 再建 → 取消置顶 ——
test("使用流程：置顶态变化时分组标题按需增减，不留空标题", () => {
  let sessions = [S(1)];
  // 1) 单个会话、未置顶：扁平，无标题
  assert.deepEqual(labels(planSessionList(sessions)), []);

  // 2) 置顶它：只剩「已置顶」（无其它会话 → 无「最近」）
  sessions = [S(1, true)];
  assert.deepEqual(labels(planSessionList(sessions)), ["已置顶"]);

  // 3) 新建一个会话：出现「已置顶」+「最近」
  sessions = [S(1, true), S(2)];
  assert.deepEqual(labels(planSessionList(sessions)), ["已置顶", "最近"]);
  assert.deepEqual(planSessionList(sessions).map((r) => r.type === "group" ? r.label : r.session.id),
    ["已置顶", 1, "最近", 2]);

  // 4) 取消置顶 1：回到扁平、无标题
  sessions = [S(1), S(2)];
  assert.deepEqual(labels(planSessionList(sessions)), []);
  assert.deepEqual(itemIds(planSessionList(sessions)), [1, 2]);
});
