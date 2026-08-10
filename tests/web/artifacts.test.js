// 工具产物句柄的前端纯逻辑（ADR 0021 块4）。运行：node --test tests/web/
const test = require("node:test");
const assert = require("node:assert");
const { extractArtifacts } = require("../../web/pure.js");

test("认出前台 shell 的句柄（摘要 + 句柄格式）", () => {
  const out = "[产物 art_0007 · 原始 417,203 字符 / 6,280 行 · 已落盘 .hermes/artifacts/art_0007.log]\n" +
    "line1\n…（中间省略 6,180 行）\nFAILED 3\n" +
    "[提示] 上面是摘要（头 60 行 + 尾 40 行）。完整内容在 .hermes/artifacts/art_0007.log，需要细节就 grep_search 它。";
  assert.deepEqual(extractArtifacts(out), [{ id: "art_0007", path: ".hermes/artifacts/art_0007.log" }]);
});

test("认出 web_fetch 的句柄（.txt）", () => {
  const out = "正文…\n[产物 art_0001] 本页完整正文（46,000 字符）已存 .hermes/artifacts/art_0001.txt——要被截掉的部分就 grep 它。";
  assert.deepEqual(extractArtifacts(out), [{ id: "art_0001", path: ".hermes/artifacts/art_0001.txt" }]);
});

test("认出后台进程的句柄", () => {
  const out = "[状态] running\n[提示] 输出过多，最旧部分已被丢弃——但**完整日志已落产物 art_0002**：.hermes/artifacts/art_0002.log（grep_search 它）";
  assert.deepEqual(extractArtifacts(out), [{ id: "art_0002", path: ".hermes/artifacts/art_0002.log" }]);
});

test("同一产物在文本里出现多次只给一个入口", () => {
  const out = ".hermes/artifacts/art_0003.log 前面提过，后面又说 .hermes/artifacts/art_0003.log";
  assert.equal(extractArtifacts(out).length, 1);
});

test("一条结果里的多个产物按出现顺序都给", () => {
  const out = "stdout 见 .hermes/artifacts/art_0001.log；stderr 见 .hermes/artifacts/art_0002.log";
  assert.deepEqual(extractArtifacts(out).map((a) => a.id), ["art_0001", "art_0002"]);
});

test("普通输出不误报", () => {
  assert.deepEqual(extractArtifacts("跑完了，3 个测试通过"), []);
  assert.deepEqual(extractArtifacts(".hermes/skills/foo/SKILL.md"), []);   // 技能目录不是产物
  assert.deepEqual(extractArtifacts("artifacts/art_0001.log"), []);        // 不在 .hermes 下不算
  assert.deepEqual(extractArtifacts("my.hermes/artifacts/art_0001.log"), []); // 相似前缀不算
  assert.deepEqual(extractArtifacts(null), []);
  assert.deepEqual(extractArtifacts(undefined), []);
});

test("可重复调用（正则 lastIndex 不残留）", () => {
  const out = "见 .hermes/artifacts/art_0009.log";
  assert.equal(extractArtifacts(out).length, 1);
  assert.equal(extractArtifacts(out).length, 1);
});
