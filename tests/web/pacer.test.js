// FR-12.3 流式节流器：不变量是**一个字都不能少**（丢字比卡顿严重得多）。
const test = require("node:test");
const assert = require("node:assert");
const { createStreamPacer } = require("../../web/pure.js");

test("首片立刻出：开头不能让人觉得没反应", () => {
  const p = createStreamPacer(80);
  p.push("你");
  assert.equal(p.take(1000), "你");        // 从没渲染过 -> 不等
});

test("间隔内的增量攒着不出，到点一次性交出全部（顺序不变）", () => {
  const p = createStreamPacer(80);
  p.push("a"); p.take(1000);               // 首片出去，lastAt=1000
  p.push("b"); p.push("c");
  assert.equal(p.take(1050), null);        // 才过 50ms，不到点
  assert.equal(p.take(1079), null);
  assert.equal(p.take(1080), "bc");        // 到点：攒的两片一次交出，顺序不乱
});

test("没有新增量时不触发渲染（别做空转）", () => {
  const p = createStreamPacer(80);
  p.push("a"); p.take(1000);
  assert.equal(p.take(9999), null);
});

test("drain 不看时间，把最后一截吐干净", () => {
  const p = createStreamPacer(80);
  p.push("a"); p.take(1000);
  p.push("尾巴");
  assert.equal(p.take(1001), null);        // 没到点
  assert.equal(p.drain(1001), "尾巴");      // 定稿必须拿到
  assert.equal(p.drain(1002), "");         // 已经空了
});

test("不变量：所有 push 的字 = 若干 take + 最后 drain 拼起来，一字不差", () => {
  const p = createStreamPacer(50);
  let src = "", got = "", now = 0;
  for (let i = 0; i < 500; i++) {
    const tok = "t" + (i % 10);
    src += tok; p.push(tok);
    now += 7;                              // 约 140 token/s，远快于 50ms 节流
    const out = p.take(now);
    if (out !== null) got += out;
  }
  got += p.drain(now + 1);
  assert.equal(got, src);
});

test("节流真的降低了渲染次数（否则这次改动毫无意义）", () => {
  const p = createStreamPacer(80);
  let renders = 0, now = 0;
  for (let i = 0; i < 400; i++) {          // 400 token @ 25ms/token = 40 token/s，典型出字速度
    p.push("x"); now += 25;
    if (p.take(now) !== null) renders++;
  }
  p.drain(now);
  assert.ok(renders < 400 / 3, `渲染 ${renders} 次，应远少于 400`);
});
