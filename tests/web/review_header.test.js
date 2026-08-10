// 方案评审标题的诚实性（ADR 0019）。运行：node --test tests/web/
const test = require("node:test");
const assert = require("node:assert");
const { debateHeader } = require("../../web/pure.js");

test("真异构：标题叫多模型讨论，副标题列出各角色用的模型", () => {
  const h = debateHeader({ product: "openai/gpt-4o", technical: "volcengine-ark/kimi-k2.6",
                           main: "volcengine-ark/kimi-k2.6" }, true);
  assert.equal(h.warn, false);
  assert.match(h.title, /多模型讨论/);
  assert.match(h.sub, /gpt-4o/);
  assert.match(h.sub, /kimi-k2\.6/);
});

test("同构：不许再叫多模型讨论，且要说清为什么、怎么改", () => {
  const h = debateHeader({ product: "ark/kimi", technical: "ark/kimi", main: "ark/kimi" }, false);
  assert.equal(h.warn, true);
  assert.match(h.title, /单模型自审/);
  assert.doesNotMatch(h.title, /多模型/);
  assert.match(h.sub, /错误高度相关/);        // 说清为什么打折
  assert.match(h.sub, /第二个 provider/);      // 给出可操作的改进路径
});

test("老会话/没带分配信息：中性措辞，不吹也不误伤", () => {
  for (const d of [undefined, null, {}, { product: "a" }]) {
    const h = debateHeader(d, false);
    assert.equal(h.warn, false);
    assert.doesNotMatch(h.title, /单模型自审/);
  }
});
