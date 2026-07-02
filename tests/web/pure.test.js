// 前端纯逻辑单测（node:test，零依赖）。运行：node --test tests/web/
// 这是 hermes 前端的第一组自动化测试——以后 pure.js 加纯函数就在这里补用例。
const test = require("node:test");
const assert = require("node:assert");
const {
  summarize, escapeHtml, sessionRowClasses, isBusyState, composerState,
  computeTaskProgress, sessionTitleMatches, matchSlashCommands, parseSlashInput,
  needsKeySetup, validateModelProfile,
  resolveTheme, normFontSize, isHelpKey, foldToolOutput,
  accumulateUsage, estimateCostUsd,
  findMentionQuery, matchFileMentions, flattenTreeFiles, clampWidth, formatQuote,
  formatEval,
  reviewGateLabel, decisionsByStatus, decisionNeedsUser,
  DEBATE_ROLES, DEBATE_ROLE_LABELS, splitVerdictProse, verdictTally, debateConvergedText,
} = require("../../web/pure.js");

test("reviewGateLabel：可数文案、绝不百分比（守 ADR 0019 禁 score）", () => {
  assert.deepEqual(reviewGateLabel(null), { enabled: false, text: "尚未评审" });
  assert.deepEqual(reviewGateLabel({ can_start: true }), { enabled: true, text: "开始编码" });
  const locked = reviewGateLabel({ can_start: false, blocking_count: 3 });
  assert.equal(locked.enabled, false);
  assert.equal(locked.text, "还有 3 个未决问题");
  assert.ok(!locked.text.includes("%"));
  assert.equal(reviewGateLabel({ can_start: false, blocking_count: 0 }).text, "等待签字确认");
});

test("decisionsByStatus：四态分组、非法 status 归 Open", () => {
  const g = decisionsByStatus([
    { id: "a", status: "Accepted" }, { id: "b", status: "NeedUser" },
    { id: "c", status: "魔幻" },
  ]);
  assert.equal(g.Accepted.length, 1);
  assert.equal(g.NeedUser.length, 1);
  assert.equal(g.Open.length, 1);          // 非法 status 落 Open
  assert.equal(g.Rejected.length, 0);
});

test("decisionNeedsUser：NeedUser 或带 blocking → 需用户拍板", () => {
  assert.equal(decisionNeedsUser({ status: "NeedUser" }), true);
  assert.equal(decisionNeedsUser({ status: "Accepted", blocking: ["x"] }), true);
  assert.equal(decisionNeedsUser({ status: "Accepted", blocking: [] }), false);
  assert.equal(decisionNeedsUser(null), false);
});

test("分屏辩论：异构双镜头角色固定为 product/technical，各有中文标签", () => {
  assert.deepEqual(DEBATE_ROLES, ["product", "technical"]);
  assert.ok(DEBATE_ROLE_LABELS.product.includes("产品"));
  assert.ok(DEBATE_ROLE_LABELS.technical.includes("技术"));
});

test("splitVerdictProse：散文在前、```json 结论在末，正确切分", () => {
  const raw = "我认为 d1 该采纳，理由是……\n```json\n[{\"id\":\"d1\",\"status\":\"Accepted\"}]\n```";
  const r = splitVerdictProse(raw);
  assert.equal(r.prose, "我认为 d1 该采纳，理由是……");
  assert.ok(r.json.includes("Accepted"));
});

test("splitVerdictProse：无 fence 时全当散文、json 为空", () => {
  const r = splitVerdictProse("纯散文没有结论块");
  assert.equal(r.prose, "纯散文没有结论块");
  assert.equal(r.json, "");
  assert.deepEqual(splitVerdictProse(null), { prose: "", json: "" });  // null 安全
});

test("verdictTally：按四态计数、中文短标签，绝不百分比（守 ADR 0019）", () => {
  const t = verdictTally('[{"id":"d1","status":"Accepted"},{"id":"d2","status":"NeedUser"},{"id":"d3","status":"Accepted"}]');
  assert.equal(t, "采纳×2 · 待拍板×1");
  assert.ok(!t.includes("%"));
  assert.equal(verdictTally("不是 JSON"), "");     // 解析失败→空串、不抛
  assert.equal(verdictTally("{}"), "");            // 非数组→空串
});

test("debateConvergedText：停因译人话、带轮数，无分数", () => {
  assert.ok(debateConvergedText({ stop_reason: "no_new_blocking", rounds: 2 }).includes("2 轮"));
  assert.ok(debateConvergedText({ stop_reason: "no_new_blocking", rounds: 2 }).includes("无新增未决问题"));
  assert.ok(debateConvergedText({ stop_reason: "怪停因", rounds: 3 }).includes("怪停因"));  // 未知停因原样带出
  assert.ok(!debateConvergedText({ rounds: 1 }).includes("%"));
});

test("formatEval：测试通过 → ok 级、N/total 摘要", () => {
  const r = formatEval({ metrics: { passed: 3, total: 3 }, signals: ["测试全过"], issues: [], score: 1 });
  assert.equal(r.level, "ok");
  assert.ok(r.text.includes("3/3 通过"));
  assert.equal(r.score, 1);
});

test("formatEval：有 issues → warn 级、带 ⚠ 与问题说明", () => {
  const r = formatEval({ metrics: { passed: 2, total: 3 }, signals: ["测试失败 1 项"],
                         issues: ["测试未全过=blocker"], score: 0.2 });
  assert.equal(r.level, "warn");
  assert.ok(r.text.startsWith("⚠"));
  assert.ok(r.text.includes("测试未全过=blocker"));
});

test("formatEval：检索命中数 / shell 退出码", () => {
  assert.ok(formatEval({ metrics: { hits: 5 }, signals: [], issues: [] }).text.includes("命中 5 条"));
  assert.ok(formatEval({ metrics: { exit_code: 0 }, signals: ["退出码 0"], issues: [] }).text.includes("退出码 0"));
});

test("formatEval：无事实 → null（不渲染）；非对象 → null", () => {
  assert.equal(formatEval({ metrics: {}, signals: [], issues: [] }), null);
  assert.equal(formatEval(null), null);
  assert.equal(formatEval(undefined), null);
});

test("formatEval：失败附错误分类标签（块C）", () => {
  const r = formatEval({ metrics: { exit_code: 1 }, signals: ["退出码 1"],
                         issues: ["退出码非零=失败"], error_classes: ["transient_io"], score: 0.2 });
  assert.ok(r.text.includes("[transient_io]"));
});

test("formatEval：无错误分类时不加标签", () => {
  const r = formatEval({ metrics: { passed: 3, total: 3 }, signals: ["测试全过"], issues: [] });
  assert.ok(!r.text.includes("["));
});

test("formatEval：signals 最多取两条，避免刷屏", () => {
  const r = formatEval({ metrics: {}, signals: ["a", "b", "c", "d"], issues: [] });
  assert.ok(r.text.includes("a") && r.text.includes("b"));
  assert.ok(!r.text.includes("c") && !r.text.includes("d"));
});

test("summarize：短值原样 JSON，超 80 字截断加省略号", () => {
  assert.equal(summarize({ a: 1 }), '{"a":1}');
  assert.equal(summarize("hi"), '"hi"');
  const long = summarize({ s: "x".repeat(200) });
  assert.equal(long.length, 81); // 80 + 省略号
  assert.ok(long.endsWith("…"));
});

test("summarize：不可序列化（循环引用）返回空串、不抛", () => {
  const circular = {};
  circular.self = circular;
  assert.equal(summarize(circular), "");
});

test("escapeHtml：转义 & < >，其它原样；非字符串先 String()", () => {
  assert.equal(escapeHtml("<a> & </a>"), "&lt;a&gt; &amp; &lt;/a&gt;");
  assert.equal(escapeHtml("plain"), "plain");
  assert.equal(escapeHtml(42), "42");
  // & 要先转，避免把已转义的 &lt; 再转成 &amp;lt;
  assert.equal(escapeHtml("a&<b"), "a&amp;&lt;b");
});

test("sessionRowClasses：running 含 queued、awaiting 独立、活动会话不算未读", () => {
  assert.deepEqual(sessionRowClasses("running", false, false),
    { running: true, awaiting: false, unread: false });
  assert.deepEqual(sessionRowClasses("queued", false, false),
    { running: true, awaiting: false, unread: false });
  assert.deepEqual(sessionRowClasses("awaiting", true, false),
    { running: false, awaiting: true, unread: true });
  // 活动会话即使有新内容也不标未读（出过 bug 的点）
  assert.equal(sessionRowClasses("idle", true, true).unread, false);
  assert.equal(sessionRowClasses("idle", true, false).unread, true);
});

test("isBusyState：running/queued/awaiting 为忙，idle/error/未知 不忙", () => {
  for (const s of ["running", "queued", "awaiting"]) assert.equal(isBusyState(s), true, s);
  for (const s of ["idle", "error", undefined]) assert.equal(isBusyState(s), false, String(s));
});

test("composerState：运行中只留停止、规划模式文案变规划、null 安全", () => {
  assert.deepEqual(composerState(null),
    { running: false, sendHidden: false, stopHidden: true, sendText: "发送", planActive: false });
  assert.deepEqual(composerState({ streaming: true }),
    { running: true, sendHidden: true, stopHidden: false, sendText: "发送", planActive: false });
  assert.equal(composerState({ crazyRunning: true }).running, true);
  const plan = composerState({ planMode: true });
  assert.equal(plan.sendText, "规划");
  assert.equal(plan.planActive, true);
});

test("computeTaskProgress：完成数/总数，容忍 null", () => {
  assert.deepEqual(computeTaskProgress([]), { done: 0, total: 0, text: "0/0" });
  const ts = [{ status: "completed" }, { status: "pending" }, { status: "completed" }];
  assert.deepEqual(computeTaskProgress(ts), { done: 2, total: 3, text: "2/3" });
  assert.equal(computeTaskProgress(null).text, "0/0");
});

test("sessionTitleMatches：空查询全中、大小写不敏感、子串、null 标题", () => {
  assert.equal(sessionTitleMatches("Hello World", ""), true);
  assert.equal(sessionTitleMatches("Hello World", "  "), true); // 纯空白=空查询
  assert.equal(sessionTitleMatches("Hello World", "WORLD"), true);
  assert.equal(sessionTitleMatches("Hello", "xyz"), false);
  assert.equal(sessionTitleMatches(null, "a"), false);
});

test("matchSlashCommands：/ 开头无空格前缀匹配，否则空", () => {
  const cmds = [{ cmd: "/add-dir" }, { cmd: "/crazy" }, { cmd: "/help" }];
  assert.deepEqual(matchSlashCommands(cmds, "/c").map((c) => c.cmd), ["/crazy"]);
  assert.equal(matchSlashCommands(cmds, "/").length, 3);
  assert.equal(matchSlashCommands(cmds, "hello").length, 0); // 不以 / 开头
  assert.equal(matchSlashCommands(cmds, "/add ").length, 0); // 有空格=已在打参数
  assert.equal(matchSlashCommands(cmds, "/zzz").length, 0);  // 无匹配
});

test("parseSlashInput：拆命令名(小写)+参数(去首尾空白)", () => {
  assert.deepEqual(parseSlashInput("/add-dir D:\\proj"), { cmd: "/add-dir", arg: "D:\\proj" });
  assert.deepEqual(parseSlashInput("/HELP"), { cmd: "/help", arg: "" });
  assert.deepEqual(parseSlashInput("/crazy   做个网站  "), { cmd: "/crazy", arg: "做个网站" });
});

test("needsKeySetup：全未配置才引导；有任一已配置 / 空列表都不弹", () => {
  assert.equal(needsKeySetup([{ set: false }, { set: false }]), true);
  assert.equal(needsKeySetup([{ set: true }, { set: false }]), false);
  assert.equal(needsKeySetup([]), false);   // 没有需要的 key（理论上）不弹
  assert.equal(needsKeySetup(null), false);
});

test("validateModelProfile：合法返回 null，各种缺漏返回提示", () => {
  const ok = { name: "my", provider: "openai", model: "gpt-x", api_key_env: "OPENAI_API_KEY", max_tokens: 8192 };
  assert.equal(validateModelProfile(ok), null);
  assert.match(validateModelProfile({ ...ok, name: "" }), /档案名/);
  assert.match(validateModelProfile({ ...ok, provider: "xx" }), /provider/);
  assert.match(validateModelProfile({ ...ok, model: "" }), /model/);
  assert.match(validateModelProfile({ ...ok, api_key_env: "" }), /api_key_env/);
  assert.match(validateModelProfile({ ...ok, max_tokens: 0 }), /max_tokens/);
  assert.match(validateModelProfile({ ...ok, max_tokens: "abc" }), /max_tokens/);
});

test("resolveTheme：system 按系统明暗解析，显式偏好原样，非法回落 system", () => {
  assert.equal(resolveTheme("system", true), "dark");
  assert.equal(resolveTheme("system", false), "light");
  assert.equal(resolveTheme("dark", false), "dark");   // 显式深色无视系统
  assert.equal(resolveTheme("light", true), "light");  // 显式浅色无视系统
  assert.equal(resolveTheme("bogus", false), "light"); // 非法 → 当 system → 系统浅色
  assert.equal(resolveTheme(undefined, true), "dark");
});

test("normFontSize：合法档位原样，非法回落 md", () => {
  assert.equal(normFontSize("sm"), "sm");
  assert.equal(normFontSize("md"), "md");
  assert.equal(normFontSize("lg"), "lg");
  assert.equal(normFontSize("huge"), "md");
  assert.equal(normFontSize(null), "md");
});

test("isHelpKey：? 或 Ctrl/⌘+/ 触发帮助，其它不触发", () => {
  assert.equal(isHelpKey("?", false), true);   // 直接问号
  assert.equal(isHelpKey("/", true), true);    // Ctrl/⌘+/
  assert.equal(isHelpKey("/", false), false);  // 单独 / 是斜杠命令，不弹帮助
  assert.equal(isHelpKey("?", true), true);    // Ctrl+? 也算
  assert.equal(isHelpKey("n", true), false);
  assert.equal(isHelpKey("a", false), false);
});

test("foldToolOutput：短输出不折叠，超行数/字符折叠并给预览", () => {
  const short = foldToolOutput("a\nb\nc");
  assert.equal(short.folded, false);
  assert.equal(short.preview, "a\nb\nc");

  const many = Array.from({ length: 50 }, (_, i) => "line" + i).join("\n");
  const f = foldToolOutput(many, 20);
  assert.equal(f.folded, true);
  assert.equal(f.total, 50);
  assert.equal(f.hidden, 30);
  assert.equal(f.preview.split("\n").length, 20); // 只留前 20 行
  assert.equal(f.full, many);

  // 行数不多但超字符阈值：也折叠（hidden 可能为 0）
  const longLine = "x".repeat(3000);
  const c = foldToolOutput(longLine, 20, 2000);
  assert.equal(c.folded, true);
  assert.equal(c.preview.length, 2000);
  assert.equal(c.hidden, 0);

  // 边界：null/空安全
  assert.equal(foldToolOutput(null).folded, false);
  assert.equal(foldToolOutput(null).preview, "");
});

test("accumulateUsage：从空起累加 input/output/cache，turns 计数；不改原对象", () => {
  const a = accumulateUsage(null, { input: 100, output: 50, cache_read: 20 });
  assert.deepEqual(a, { input: 100, output: 50, cacheRead: 20, turns: 1 });
  const b = accumulateUsage(a, { input: 10, output: 5 });
  assert.deepEqual(b, { input: 110, output: 55, cacheRead: 20, turns: 2 });
  assert.deepEqual(a, { input: 100, output: 50, cacheRead: 20, turns: 1 }); // 原对象不变
  // 缺字段/非数字安全
  assert.deepEqual(accumulateUsage(null, {}), { input: 0, output: 0, cacheRead: 0, turns: 1 });
});

test("estimateCostUsd：命中价目表按量算，未知模型返回 null", () => {
  // claude-sonnet: in 3 / out 15（每百万）
  const c = estimateCostUsd("claude-sonnet-4-6", { input: 1e6, output: 1e6, cacheRead: 0 });
  assert.ok(Math.abs(c - 18) < 1e-9);
  // 缓存读按输入价 10%：kimi in 0.15
  const k = estimateCostUsd("ark-kimi", { input: 0, output: 0, cacheRead: 1e6 });
  assert.ok(Math.abs(k - 0.015) < 1e-9);
  // 未知模型 -> null
  assert.equal(estimateCostUsd("some-random-model", { input: 1e6, output: 1e6 }), null);
  assert.equal(estimateCostUsd("", { input: 1 }), null);
});

test("findMentionQuery：光标前的连续 @token 才激活，邮箱/含空格不激活", () => {
  assert.deepEqual(findMentionQuery("@", 1), { active: true, query: "", start: 0 });
  assert.deepEqual(findMentionQuery("看下 @src/a", 9), { active: true, query: "src/a", start: 3 });
  // @ 前是非空白（邮箱）→ 不激活
  assert.equal(findMentionQuery("mail a@b.com", 12).active, false);
  // @ 后到光标有空格 → 不激活（已经选完了）
  assert.equal(findMentionQuery("@src/a 改一下", 9).active, false);
  // 没有 @
  assert.equal(findMentionQuery("普通消息", 4).active, false);
  // 光标在更早位置：只看光标前
  assert.deepEqual(findMentionQuery("@ab cd", 2), { active: true, query: "a", start: 0 });
});

test("matchFileMentions：子串匹配、大小写不敏感、限量", () => {
  const files = ["src/app.js", "src/Pure.js", "web/style.css", "README.md"];
  assert.deepEqual(matchFileMentions(files, "pure"), ["src/Pure.js"]);
  assert.deepEqual(matchFileMentions(files, "src/"), ["src/app.js", "src/Pure.js"]);
  assert.deepEqual(matchFileMentions(files, ""), files); // 空查询返回全部（受限量）
  assert.equal(matchFileMentions(files, "x").length, 0);
  assert.equal(matchFileMentions(["a", "b", "c"], "", 2).length, 2); // 限量
});

test("flattenTreeFiles：递归收集文件路径，跳过目录节点", () => {
  const tree = {
    type: "dir", path: "", children: [
      { type: "dir", path: "src", children: [
        { type: "file", path: "src/app.js" },
        { type: "file", path: "src/pure.js" },
      ] },
      { type: "file", path: "README.md" },
    ],
  };
  assert.deepEqual(flattenTreeFiles(tree), ["src/app.js", "src/pure.js", "README.md"]);
  assert.deepEqual(flattenTreeFiles(null), []);
});

test("formatQuote：逐行加 > 前缀、末尾留空行；空文本空串；超长截断", () => {
  assert.equal(formatQuote("一行"), "> 一行\n\n");
  assert.equal(formatQuote("第一行\n第二行"), "> 第一行\n> 第二行\n\n");
  assert.equal(formatQuote("  "), "");           // 纯空白 -> 空串
  assert.equal(formatQuote(null), "");
  const long = formatQuote("x".repeat(3000), 2000);
  assert.ok(long.startsWith("> "));
  assert.ok(long.includes("引用已截断"));
});

test("clampWidth：夹在[min,max]，非数字回落 fallback", () => {
  assert.equal(clampWidth(300, 180, 460), 300);
  assert.equal(clampWidth(100, 180, 460), 180);   // 低于下限
  assert.equal(clampWidth(900, 180, 460), 460);   // 超过上限
  assert.equal(clampWidth("250", 180, 460), 250);  // 字符串数字
  assert.equal(clampWidth("abc", 180, 460, 230), 230); // 非数字 -> fallback
  assert.equal(clampWidth(null, 180, 460), 180);   // 无 fallback -> min
});
