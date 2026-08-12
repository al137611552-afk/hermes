// FR-17 并发可观测性的纯逻辑单测（node:test，零依赖）。运行：node --test tests/web/
//
// 这些用例守的是一条具体教训：并发机制早就有，但"等你"从不冒泡——ask_user / request_handoff
// 只 emit 事件不改状态，而顶部计数连已有的 awaiting 都排除在外。于是换手挂起时，
// 用户不切进那个会话就完全看不见。下面每条都钉住"看得见"的某一面。
const test = require("node:test");
const assert = require("node:assert");
const {
  waitLabel, WAIT_LABELS, summarizeConcurrency, concurrencyChipText, activityLine,
  toolActivityLabel, windowBadgeTitle, unreadDoneCount,
} = require("../../web/pure.js");

const row = (sid, status, extra) => Object.assign({ sid, cid: sid, status }, extra || {});

test("summarizeConcurrency 收 awaiting——原先只收 running|queued 是本 FR 的根因", () => {
  const s = summarizeConcurrency([
    row(1, "running"), row(2, "awaiting", { waitReason: "handoff" }), row(3, "queued"),
  ]);
  assert.equal(s.counts.running, 2);   // running + queued
  assert.equal(s.counts.waiting, 1);
  assert.equal(s.ordered.length, 3);
});

test("等待的会话排在运行的前面——有人在等比还在跑更该被看见", () => {
  const s = summarizeConcurrency([
    row(1, "running"), row(2, "running"), row(3, "awaiting", { waitReason: "ask" }),
  ]);
  assert.equal(s.ordered[0].sid, 3);
  assert.deepEqual(s.ordered.map((r) => r.sid), [3, 1, 2]);
});

test("idle/error 不算进行中；没有 sid 的行丢弃（还没建会话，点了也跳不过去）", () => {
  const s = summarizeConcurrency([
    row(1, "idle"), row(2, "error"), { cid: 9, status: "running" }, null,
  ]);
  assert.equal(s.ordered.length, 0);
  assert.equal(concurrencyChipText(s.counts), "");   // 空串 → chip 整体隐藏
});

test("chip 文案：等待段在前，任一为 0 就不显示那段", () => {
  assert.equal(concurrencyChipText({ running: 2, waiting: 1 }), "✋ 1 等你 · 2 运行中");
  assert.equal(concurrencyChipText({ running: 3, waiting: 0 }), "3 运行中");
  assert.equal(concurrencyChipText({ running: 0, waiting: 2 }), "✋ 2 等你");
  assert.equal(concurrencyChipText({ running: 0, waiting: 0 }), "");
  assert.equal(concurrencyChipText(undefined), "");
});

test("三种等待各自的说法不同——换手卡着不处理会一直卡，得说清是哪一种", () => {
  assert.equal(waitLabel("permission"), "等确认");
  assert.equal(waitLabel("ask"), "等回答");
  assert.equal(waitLabel("handoff"), "等接管");
  assert.equal(waitLabel("something-new"), "等你");   // 未知原因兜底，不显示成空白
  assert.equal(Object.keys(WAIT_LABELS).length, 3);
});

test("activityLine：等待态报等什么、运行态报在干什么、都没有则留空不占位", () => {
  assert.equal(activityLine(row(1, "awaiting", { waitReason: "handoff", activity: "读页面" })),
    "等接管");   // 等待态优先：这行的作用是催人，不是报进度
  assert.equal(activityLine(row(2, "running", { activity: "run_bash" })), "run_bash");
  assert.equal(activityLine(row(3, "running", { activity: "   " })), "");
  assert.equal(activityLine(undefined), "");
});

// ---- T2：在干什么 --------------------------------------------------------

test("toolActivityLabel 保留原始工具名——UI 别处都用原名，另造叫法会对不上", () => {
  assert.equal(toolActivityLabel("read_file", { path: "src/a.py" }), "read_file src/a.py");
  assert.equal(toolActivityLabel("run_bash", { command: "pytest -q" }), "run_bash pytest -q");
  assert.equal(toolActivityLabel("web_search", { query: "显卡 价格" }), "web_search 显卡 价格");
});

test("toolActivityLabel：没有可用入参就只报工具名，不硬凑", () => {
  assert.equal(toolActivityLabel("git_status", {}), "git_status");
  assert.equal(toolActivityLabel("git_status", null), "git_status");
  assert.equal(toolActivityLabel("git_status", { extra: 42 }), "git_status");
  assert.equal(toolActivityLabel("", { path: "x" }), "");   // 没工具名就整体空
});

test("toolActivityLabel：长入参截断且不撑破一行，换行压成空格", () => {
  const long = toolActivityLabel("read_file", { path: "a/".repeat(80) + "z.py" });
  assert.ok(long.length <= 42, long);
  assert.ok(long.endsWith("…"), long);
  assert.equal(toolActivityLabel("run_bash", { command: "a\n  b\tc" }), "run_bash a b c");
});

test("activityLine 接上 toolActivityLabel 的产物：运行态报在干什么", () => {
  const row = { sid: 1, status: "running", activity: toolActivityLabel("web_fetch", { url: "https://x" }) };
  assert.equal(activityLine(row), "web_fetch https://x");
});

// ---- T3：标题角标 --------------------------------------------------------

test("windowBadgeTitle：等你优先于跑完——等你的会一直卡着不动", () => {
  assert.equal(windowBadgeTitle({ waiting: 2, unread: 3 }), "(2 等你) Hermes");
  assert.equal(windowBadgeTitle({ waiting: 0, unread: 3 }), "(3 完成) Hermes");
});

test("windowBadgeTitle：没什么要你管就是干净标题，不留残角标", () => {
  assert.equal(windowBadgeTitle({ waiting: 0, unread: 0 }), "Hermes");
  assert.equal(windowBadgeTitle({}), "Hermes");
  assert.equal(windowBadgeTitle(undefined), "Hermes");
  assert.equal(windowBadgeTitle({ waiting: -1, unread: -2 }), "Hermes");   // 脏计数不写进标题
});

test("unreadDoneCount 排掉还在忙的——运行中的会话也会未读，标成「完成」就是撒谎", () => {
  const rows = [
    { unread: true, status: "idle" },       // 跑完了没看 → 算
    { unread: true, status: "error" },      // 出错了没看 → 也算（都要你去看）
    { unread: true, status: "running" },    // 还在跑，只是有新输出 → 不算
    { unread: true, status: "awaiting" },   // 在等你，归"等你"那段，不重复计
    { unread: false, status: "idle" },      // 看过了 → 不算
  ];
  assert.equal(unreadDoneCount(rows), 2);
  assert.equal(unreadDoneCount([]), 0);
  assert.equal(unreadDoneCount(undefined), 0);
});
