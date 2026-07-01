// 工作区标签页纯逻辑单测（node:test，零依赖）。运行：node --test tests/web/
// 对齐 Figma 重设计：改动/评审「发生才出现」，文件/预览常驻；覆盖一次真实工程任务的标签生命周期。
const test = require("node:test");
const assert = require("node:assert");
const { WS_TAB_KEYS, wsTabVisible, resolveWorkspaceTabs } = require("../../web/pure.js");

const NONE = { hasChanges: false, hasCheckpoints: false, hasReview: false };

test("wsTabVisible：文件/预览常驻，改动看改动或检查点，评审看评审", () => {
  assert.equal(wsTabVisible("files", NONE), true);
  assert.equal(wsTabVisible("preview", NONE), true);
  assert.equal(wsTabVisible("changes", NONE), false);
  assert.equal(wsTabVisible("review", NONE), false);
  // 只有检查点没改动，改动标签也应出现（改动/检查点共用一个标签）
  assert.equal(wsTabVisible("changes", { ...NONE, hasCheckpoints: true }), true);
  assert.equal(wsTabVisible("changes", { ...NONE, hasChanges: true }), true);
  assert.equal(wsTabVisible("review", { ...NONE, hasReview: true }), true);
  assert.equal(wsTabVisible("unknown", NONE), true); // 未知键当常驻，绝不吞标签
});

test("resolveWorkspaceTabs：空工程只剩文件/预览，且不显示标签条", () => {
  const r = resolveWorkspaceTabs(NONE, "files");
  assert.deepEqual(r.tabs, ["files", "preview"]);
  assert.equal(r.active, "files");
  assert.equal(r.showStrip, true); // 文件+预览=2 个 → 显示（>1）
});

test("resolveWorkspaceTabs：期望标签不可见时回退到文件（不会卡在空标签）", () => {
  // 用户上次停在「评审」，但本工程还没评审 → 回文件
  assert.equal(resolveWorkspaceTabs(NONE, "review").active, "files");
  // 有评审时保持评审
  assert.equal(resolveWorkspaceTabs({ ...NONE, hasReview: true }, "review").active, "review");
});

test("标签顺序始终是 改动→文件→预览→评审（对齐 Figma），不受出现次序影响", () => {
  const all = resolveWorkspaceTabs(
    { hasChanges: true, hasCheckpoints: true, hasReview: true }, "changes");
  assert.deepEqual(all.tabs, WS_TAB_KEYS); // changes,files,preview,review
});

// —— 一次真实工程任务的标签生命周期（模拟：开工→改文件→存检查点→评审→切回文件）——
test("生命周期：一个真实工程任务跑一遍，标签按事件增减、激活项合理", () => {
  let avail = { ...NONE };
  let want = "files";
  const step = (patch, wantTab) => {
    if (patch) avail = { ...avail, ...patch };
    if (wantTab) want = wantTab;
    const r = resolveWorkspaceTabs(avail, want);
    want = r.active; // 模拟 setWorkspaceTab 把激活项写回
    return r;
  };

  // 1) 刚打开工程：无改动/检查点/评审 → 只有 文件+预览
  let r = step(null, "files");
  assert.deepEqual(r.tabs, ["files", "preview"]);
  assert.equal(r.active, "files");

  // 2) 浏览一个源文件 → 切到「预览」标签（常驻，可用）
  r = step(null, "preview");
  assert.equal(r.active, "preview");

  // 3) Agent 改了几个文件 → 「改动」标签出现
  r = step({ hasChanges: true }, null);
  assert.ok(r.tabs.includes("changes"));
  assert.deepEqual(r.tabs, ["changes", "files", "preview"]);
  assert.equal(r.active, "preview"); // 出现新标签不该抢走当前焦点

  // 4) 存了检查点 → 改动标签仍在（现在改动+检查点都在）
  r = step({ hasCheckpoints: true }, null);
  assert.ok(r.tabs.includes("changes"));

  // 5) 进入评审 → 「评审」标签出现，自动切到评审
  r = step({ hasReview: true }, "review");
  assert.deepEqual(r.tabs, WS_TAB_KEYS);
  assert.equal(r.active, "review");

  // 6) 评审通过、开始编码后清掉评审面板 → 评审标签消失，激活项回退（不卡在空评审）
  r = step({ hasReview: false }, null);
  assert.ok(!r.tabs.includes("review"));
  assert.notEqual(r.active, "review");
  assert.equal(r.active, "files"); // 纯逻辑回退到常驻的文件

  // 7) 用户全部回退、清空改动与检查点 → 又只剩 文件+预览
  r = step({ hasChanges: false, hasCheckpoints: false }, null);
  assert.deepEqual(r.tabs, ["files", "preview"]);
  assert.equal(r.active, "files");
});
