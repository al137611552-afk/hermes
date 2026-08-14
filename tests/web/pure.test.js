// 前端纯逻辑单测（node:test，零依赖）。运行：node --test tests/web/
// 这是 hermes 前端的第一组自动化测试——以后 pure.js 加纯函数就在这里补用例。
const test = require("node:test");
const assert = require("node:assert");
const {
  summarize, escapeHtml, sessionRowClasses, isBusyState, composerState,
  computeTaskProgress, sessionTitleMatches, matchSlashCommands, parseSlashInput,
  needsKeySetup, validateModelProfile,
  resolveTheme, normFontSize, isHelpKey, foldToolOutput, appendStreamBuffer,
  accumulateUsage, estimateCostUsd,
  findMentionQuery, matchFileMentions, flattenTreeFiles, clampWidth, formatQuote,
  formatEval,
  reviewGateLabel, decisionsByStatus, decisionNeedsUser,
  DEBATE_ROLES, DEBATE_ROLE_LABELS, DEBATE_MAIN, DEBATE_MAIN_LABEL, debateMainRoundLabel,
  splitVerdictProse, verdictTally, debateConvergedText,
  shouldShowUpdate, updateBannerHtml,
} = require("../../web/pure.js");

test("shouldShowUpdate 仅在有新版且 ok 时为真", () => {
  assert.equal(shouldShowUpdate({ ok: true, newer: true, latest: "3.51.3" }), true);
  assert.equal(shouldShowUpdate({ ok: true, newer: false, latest: "3.51.2" }), false);
  assert.equal(shouldShowUpdate({ ok: false, error: "x" }), false);  // 网络失败 → 不弹
  assert.equal(shouldShowUpdate(null), false);
  assert.equal(shouldShowUpdate({ ok: true, newer: true }), false);   // 缺 latest → 不弹
});

test("updateBannerHtml 含版本号与按钮，且转义版本串防注入", () => {
  const html = updateBannerHtml({ current: "3.51.2", latest: "3.51.3" });
  assert.match(html, /v3\.51\.3/);
  assert.match(html, /当前 v3\.51\.2/);
  assert.match(html, /id="upd-apply"/);
  assert.match(html, /id="upd-later"/);
  const evil = updateBannerHtml({ current: "1.0.0", latest: "<img src=x>" });
  assert.ok(!evil.includes("<img src=x>"), "版本串应被转义");
  assert.match(evil, /&lt;img/);
});

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

test("hub-and-spoke：主模型（hub）为保留 role 'main'，有中文标签与逐轮标题", () => {
  assert.equal(DEBATE_MAIN, "main");                 // 与引擎 MAIN seam 名一致
  assert.ok(DEBATE_MAIN_LABEL.includes("主模型"));
  assert.ok(!DEBATE_ROLES.includes("main"));         // main 不是评审员列、单独整宽区
  assert.ok(debateMainRoundLabel(2).includes("第 2 轮"));
  assert.ok(debateMainRoundLabel(2).includes("主模型"));
  assert.ok(debateMainRoundLabel().includes("第 1 轮"));  // 缺省第 1 轮
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
  // 形状随 ADR 0025 扩了 cacheWrite/estimated/model（写缓存单独计价、估算要可识别）
  const z = { cacheWrite: 0, estimated: false, model: "" };
  const a = accumulateUsage(null, { input: 100, output: 50, cache_read: 20 });
  assert.deepEqual(a, { input: 100, output: 50, cacheRead: 20, turns: 1, ...z });
  const b = accumulateUsage(a, { input: 10, output: 5 });
  assert.deepEqual(b, { input: 110, output: 55, cacheRead: 20, turns: 2, ...z });
  assert.deepEqual(a, { input: 100, output: 50, cacheRead: 20, turns: 1, ...z }); // 原对象不变
  // 缺字段/非数字安全
  assert.deepEqual(accumulateUsage(null, {}), { input: 0, output: 0, cacheRead: 0, turns: 1, ...z });
});

test("estimateCostUsd：按 model_id 前缀匹配，未知模型返回 null", () => {
  // claude-sonnet: in 3 / out 15（每百万）
  const c = estimateCostUsd("claude-sonnet-4-6", { input: 1e6, output: 1e6, cacheRead: 0 });
  assert.ok(Math.abs(c - 18) < 1e-9);
  // 缓存读按输入价 10%：kimi in 0.15
  const k = estimateCostUsd("kimi-k2", { input: 0, output: 0, cacheRead: 1e6 });
  assert.ok(Math.abs(k - 0.015) < 1e-9);
  // 写缓存不假装便宜：按输入价全额算
  const w = estimateCostUsd("kimi-k2", { input: 0, output: 0, cacheWrite: 1e6 });
  assert.ok(Math.abs(w - 0.15) < 1e-9);
  // 最长前缀优先：gpt-4o-mini 命中自己（0.15），不被更短的 gpt-4o（2.5）抢走
  const mini = estimateCostUsd("gpt-4o-mini-2026", { input: 1e6 });
  assert.ok(Math.abs(mini - 0.15) < 1e-9);
  // **有意变更（ADR 0025 决策 4）**：改前缀匹配、且传的是真实 model_id 而非档名。
  // 旧行为是拿档名做子串匹配——"ark-kimi" 这种档名会命中 kimi，而 "我的主力" 永远命中不了；
  // 更糟的是 "opus" 会命中任何含该词的自定义档名。下面两条钉住新语义。
  assert.equal(estimateCostUsd("ark-kimi", { input: 1e6 }), null, "档名不再参与匹配");
  assert.equal(estimateCostUsd("my-opus-tuned", { input: 1e6 }), null, "子串不再算命中");
  // 未知模型 -> null
  assert.equal(estimateCostUsd("some-random-model", { input: 1e6, output: 1e6 }), null);
  assert.equal(estimateCostUsd("", { input: 1 }), null);
});

test("accumulateUsage：写缓存单独累计，估算标记一路带下去", () => {
  let acc = accumulateUsage(null, { input: 10, output: 2, cache_read: 5, cache_write: 3,
                                    measured: true, model: "m-1" });
  assert.equal(acc.cacheWrite, 3);
  assert.equal(acc.estimated, false);
  assert.equal(acc.model, "m-1");
  // 只要有一轮是估的，整段累计就带上标记（别让估算值冒充实测）
  acc = accumulateUsage(acc, { input: 1, output: 1, measured: false });
  assert.equal(acc.estimated, true);
  assert.equal(acc.turns, 2);
  // 后续实测轮不会把标记洗掉
  acc = accumulateUsage(acc, { input: 1, output: 1, measured: true });
  assert.equal(acc.estimated, true);
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

test("appendStreamBuffer 拼接并按尾部截断", () => {
  assert.equal(appendStreamBuffer("", "abc"), "abc");
  assert.equal(appendStreamBuffer("ab", "cd"), "abcd");
  assert.equal(appendStreamBuffer(null, null), "");
  const big = appendStreamBuffer("x".repeat(19999), "yyy", 20000);
  assert.ok(big.length <= 20000 + 20, "应保留尾部约 maxChars");
  assert.ok(big.includes("上文已省略"), "截断时应标注省略");
  assert.ok(big.endsWith("yyy"), "应保留最新增量在尾部");
});

// ---- 🧩 技能面板纯逻辑（FR-13.S2）---------------------------------------
const {
  skillGradeBadge, installConfirmLevel, filterMarketEntries,
  groupSkillsBySource, SOURCE_LABELS, SKILL_GRADES,
} = require("../../web/pure.js");

test("skillGradeBadge：三档各有图标与文案，未知档不炸", () => {
  assert.equal(skillGradeBadge("clean").cls, "sg-clean");
  assert.equal(skillGradeBadge("review").cls, "sg-review");
  assert.equal(skillGradeBadge("warn").cls, "sg-warn");
  assert.equal(skillGradeBadge("nope").cls, "sg-unknown");
  assert.equal(skillGradeBadge(undefined).label, "未扫描");
});

test("skillGradeBadge：措辞不承诺'安全'（扫描是启发式的）", () => {
  for (const g of Object.values(SKILL_GRADES)) {
    assert.ok(!g.label.includes("安全"), `档位文案不该出现"安全"：${g.label}`);
  }
});

test("installConfirmLevel：绿档直接装 / 黄档确认 / 红档危险确认", () => {
  assert.equal(installConfirmLevel("clean").needConfirm, false);
  assert.equal(installConfirmLevel("review").needConfirm, true);
  assert.equal(installConfirmLevel("review").danger, false);
  const warn = installConfirmLevel("warn");
  assert.equal(warn.needConfirm, true);
  assert.equal(warn.danger, true);
  assert.equal(warn.text, "仍要安装"); // 红档措辞要让人意识到在冒险
  // 未知分级按最保守处理（不能因为分级缺失就直接装）
  assert.equal(installConfirmLevel(undefined).needConfirm, false);
});

test("filterMarketEntries：按名称/描述/分类/关键词匹配，空查询全返回", () => {
  const entries = [
    { name: "pr-review", description: "评审 PR", category: "development", keywords: ["git"] },
    { name: "seo-writer", description: "写 SEO 文案", category: "marketing", keywords: ["content"] },
  ];
  assert.equal(filterMarketEntries(entries, "").length, 2);
  assert.equal(filterMarketEntries(entries, "  ").length, 2);
  assert.equal(filterMarketEntries(entries, "pr")[0].name, "pr-review");
  assert.equal(filterMarketEntries(entries, "评审")[0].name, "pr-review");
  assert.equal(filterMarketEntries(entries, "marketing")[0].name, "seo-writer");
  assert.equal(filterMarketEntries(entries, "content")[0].name, "seo-writer");
  assert.equal(filterMarketEntries(entries, "PR").length, 1); // 大小写不敏感
  assert.equal(filterMarketEntries(entries, "zzz").length, 0);
  assert.deepEqual(filterMarketEntries(null, "x"), []);
});

test("groupSkillsBySource：按来源分组，未知来源自建一组不丢", () => {
  const g = groupSkillsBySource([
    { name: "a", source: "builtin" }, { name: "b", source: "project" },
    { name: "c", source: "builtin" }, { name: "d", source: "weird" },
  ]);
  assert.deepEqual(g.builtin.map((s) => s.name), ["a", "c"]);
  assert.deepEqual(g.project.map((s) => s.name), ["b"]);
  assert.deepEqual(g.weird.map((s) => s.name), ["d"]);
  assert.deepEqual(g.global, []);
  assert.ok(SOURCE_LABELS.builtin && SOURCE_LABELS.project);
});

const { skillCountLabel } = require("../../web/pure.js");

test("skillCountLabel：未深扫不显示 / 有技能显数量 / 零技能标明", () => {
  assert.equal(skillCountLabel(null), "");
  assert.equal(skillCountLabel(undefined), "");
  assert.equal(skillCountLabel(3), "3 个技能");
  assert.equal(skillCountLabel(0), "不含技能");
});

test("filterMarketEntries：深扫后滤掉不含技能的条目（实测官方市场 13 个里只有 4 个含技能）", () => {
  const entries = [
    { name: "has-skills", description: "", skill_count: 2 },
    { name: "commands-only", description: "", skill_count: 0 },
    { name: "not-scanned", description: "", skill_count: null },
  ];
  // 未深扫（hideEmpty=false）：全都列出来
  assert.equal(filterMarketEntries(entries, "", false).length, 3);
  // 深扫后：滤掉 0 技能的，未数过的保留（宁可多列也不误删）
  const kept = filterMarketEntries(entries, "", true).map((e) => e.name);
  assert.deepEqual(kept, ["has-skills", "not-scanned"]);
  // 过滤与搜索可叠加
  assert.deepEqual(filterMarketEntries(entries, "has", true).map((e) => e.name), ["has-skills"]);
});

const { updateStatusLabel, summarizeUpdateCheck } = require("../../web/pure.js");

test("updateStatusLabel：四种状态各有文案，未知状态不炸", () => {
  assert.equal(updateStatusLabel("update").text, "有新版本");
  assert.equal(updateStatusLabel("current").text, "已是最新");
  assert.equal(updateStatusLabel("no_source").text, "无来源记录");
  assert.equal(updateStatusLabel("gone").text, "上游已移除");
  assert.equal(updateStatusLabel("zzz").text, "");
  assert.equal(updateStatusLabel(undefined).text, "");
});

test("summarizeUpdateCheck：如实分类计数，不把查不了的算成'已是最新'", () => {
  const results = [
    { status: "update" }, { status: "update" },
    { status: "current" },
    { status: "no_source" },
    { status: "gone" },
  ];
  const s = summarizeUpdateCheck(results);
  assert.ok(s.includes("2 个有新版本"));
  assert.ok(s.includes("1 个已是最新"));
  // 关键：手动放进来的技能查不了，必须说出来，不能含糊成"都是最新"
  assert.ok(s.includes("1 个无来源记录（查不了）"), s);
  assert.ok(s.includes("1 个上游已移除"), s);
  assert.equal(summarizeUpdateCheck([]), "没有可检查的已装技能");
  assert.equal(summarizeUpdateCheck(null), "没有可检查的已装技能");
  // 全部最新时也不该出现误导性的绝对说法
  assert.equal(summarizeUpdateCheck([{ status: "current" }]), "1 个已是最新");
});

// ===== 设置面板导航：分组 + 状态徽标 =====
const {
  buildSettingsNav, mcpNavBadge, browserNavBadge, skillsNavBadge, hooksNavBadge,
} = require("../../web/pure.js");

test("buildSettingsNav：provider 归模型服务，固定面板按组分到扩展能力/通用", () => {
  const groups = buildSettingsNav(
    [{ key: "ark", label: "火山方舟", enabled: true }, { key: "ds", label: "DeepSeek", enabled: false }],
    {},
  );
  assert.deepEqual(groups.map((g) => g.id), ["models", "capabilities", "general"]);
  const models = groups[0];
  assert.equal(models.title, "模型服务");
  assert.deepEqual(models.items.map((i) => i.key), ["ark", "ds"]);
  assert.deepEqual(models.items.map((i) => i.dot), [true, false]);   // 亮灭点＝enabled
  assert.deepEqual(groups[1].items.map((i) => i.key),
    ["__browser__", "__mcp__", "__hooks__", "__skills__", "__commands__"]);
  assert.deepEqual(groups[2].items.map((i) => i.key),
    ["__appearance__", "__features__", "__permissions__", "__limits__"]);
  assert.ok(groups[1].items.every((i) => i.kind === "pane"));
});

test("buildSettingsNav：徽标按 key 挂到对应面板；没有 provider 时模型服务组仍保留（要能加自定义）", () => {
  const groups = buildSettingsNav([], { __mcp__: { text: "3 工具", tone: "ok" } });
  assert.equal(groups[0].id, "models");
  assert.deepEqual(groups[0].items, []);
  const mcp = groups[1].items.find((i) => i.key === "__mcp__");
  assert.deepEqual(mcp.badge, { text: "3 工具", tone: "ok" });
  const hooks = groups[1].items.find((i) => i.key === "__hooks__");
  assert.equal(hooks.badge, null);   // 没给徽标就是不显示，不拿 0 占位
});

test("mcpNavBadge：全连上报工具数，有掉线优先报未连上，停用的不算", () => {
  assert.equal(mcpNavBadge(null), null);
  assert.equal(mcpNavBadge({ servers: {} }), null);
  // 只有停用的 server → 不显示徽标
  assert.equal(mcpNavBadge({ servers: { fs: { enabled: false } }, connected: {} }), null);
  assert.deepEqual(
    mcpNavBadge({ servers: { fs: { enabled: true }, git: { enabled: true } },
      connected: { fs: ["fs__a", "fs__b"], git: ["git__c"] } }),
    { text: "3 工具", tone: "ok" });
  // 一个没连上 → 报问题而不是报还剩多少工具（问题优先）
  assert.deepEqual(
    mcpNavBadge({ servers: { fs: { enabled: true }, git: { enabled: true } },
      connected: { fs: ["fs__a"] } }),
    { text: "1 未连上", tone: "warn" });
});

test("browserNavBadge：未启用不显示；启用未连上区分缺 Node 与装配中", () => {
  assert.equal(browserNavBadge(null), null);
  assert.equal(browserNavBadge({ enabled: false, node: true }), null);
  assert.deepEqual(browserNavBadge({ enabled: true, connected: true, tools: 23 }),
    { text: "已连上", tone: "ok" });
  assert.deepEqual(browserNavBadge({ enabled: true, connected: false, node: true }),
    { text: "装配中", tone: "warn" });
  assert.deepEqual(browserNavBadge({ enabled: true, connected: false, node: false }),
    { text: "缺 Node", tone: "warn" });
});

test("skillsNavBadge / hooksNavBadge：计数徽标，零个不显示；停用的 hook 不计", () => {
  assert.equal(skillsNavBadge([]), null);
  assert.equal(skillsNavBadge(null), null);
  assert.deepEqual(skillsNavBadge([{ name: "a" }, { name: "b" }]), { text: "2", tone: "muted" });
  assert.equal(hooksNavBadge([]), null);
  assert.deepEqual(hooksNavBadge([{ enabled: true }, { enabled: false }, {}]),
    { text: "2", tone: "muted" });
});

const { wrapFocusIndex } = require("../../web/pure.js");

test("wrapFocusIndex：Tab 到末尾绕回开头，Shift+Tab 反向；焦点在浮层外时从两端进", () => {
  assert.equal(wrapFocusIndex(3, 0, false), 1);
  assert.equal(wrapFocusIndex(3, 2, false), 0);    // 末尾 → 开头
  assert.equal(wrapFocusIndex(3, 0, true), 2);     // 开头 + Shift → 末尾
  assert.equal(wrapFocusIndex(3, -1, false), 0);   // 焦点不在浮层内
  assert.equal(wrapFocusIndex(3, -1, true), 2);
  assert.equal(wrapFocusIndex(0, -1, false), -1);  // 浮层里没有可聚焦元素
});

// ===== 自定义斜杠命令：合并与查找 =====
const { mergeSlashCommands, findCustomCommand } = require("../../web/pure.js");

const BUILTIN = [
  { cmd: "/add-dir", arg: "<目录>", desc: "授权目录" },
  { cmd: "/crazy", arg: "<目标>", desc: "自主模式" },
  { cmd: "/help", arg: "", desc: "列出命令" },
];
const CUSTOM = [
  { name: "盯盘", slash: "/盯盘", description: "查期货盯盘数据", mode: "prompt",
    argument_hint: "[动量|热点]", source: "project" },
  { name: "动量", slash: "/动量", description: "直接跑动量排名", mode: "exec",
    argument_hint: "", source: "global" },
];

test("mergeSlashCommands：内置在前、自定义在后，标出 custom 与 exec", () => {
  const m = mergeSlashCommands(BUILTIN, CUSTOM);
  assert.deepEqual(m.map((c) => c.cmd), ["/add-dir", "/crazy", "/help", "/盯盘", "/动量"]);
  assert.equal(m[0].custom, false);
  const dp = m.find((c) => c.cmd === "/盯盘");
  assert.equal(dp.custom, true);
  assert.equal(dp.arg, "[动量|热点]");
  assert.equal(dp.mode, "prompt");
  // exec 模式在菜单里要能一眼看出来——它不过模型、直接跑命令
  assert.ok(m.find((c) => c.cmd === "/动量").desc.includes("直接执行"));
});

test("mergeSlashCommands：自定义不得顶掉同名内置命令（/crazy 是免确认入口）", () => {
  const m = mergeSlashCommands(BUILTIN, [
    { name: "crazy", slash: "/crazy", description: "冒充的", mode: "exec" },
    ...CUSTOM,
  ]);
  assert.equal(m.filter((c) => c.cmd === "/crazy").length, 1);
  assert.equal(m.find((c) => c.cmd === "/crazy").desc, "自主模式");   // 还是内置那条
  assert.equal(m.find((c) => c.cmd === "/crazy").custom, false);
});

test("mergeSlashCommands：空输入与缺字段不炸", () => {
  assert.deepEqual(mergeSlashCommands(null, null), []);
  assert.deepEqual(mergeSlashCommands([], [{ description: "没名字" }]), []);
  const m = mergeSlashCommands([], [{ name: "x" }]);
  assert.equal(m[0].cmd, "/x");
  assert.equal(m[0].desc, "自定义命令");   // 没写 description 也有兜底文案
});

test("findCustomCommand：按名字对到命令，找不到回 null", () => {
  assert.equal(findCustomCommand(CUSTOM, "/盯盘").name, "盯盘");
  assert.equal(findCustomCommand(CUSTOM, "盯盘").name, "盯盘");
  assert.equal(findCustomCommand(CUSTOM, "/没有的"), null);
  assert.equal(findCustomCommand(CUSTOM, "/"), null);
  assert.equal(findCustomCommand(null, "/盯盘"), null);
});

const { commandsNavBadge } = require("../../web/pure.js");

test("commandsNavBadge：坏文件优先报问题，其次报条数，零个不显示", () => {
  assert.equal(commandsNavBadge([], []), null);
  assert.equal(commandsNavBadge(null, null), null);
  assert.deepEqual(commandsNavBadge([{ name: "盯盘" }, { name: "动量" }], []),
    { text: "2", tone: "muted" });
  // 存了却加载不了的命令必须显眼——比少显示个数字严重
  assert.deepEqual(commandsNavBadge([{ name: "盯盘" }], ["坏的.md：exec 模式必须写 command"]),
    { text: "1 个没加载", tone: "warn" });
});

const { permissionsNavBadge } = require("../../web/pure.js");

test("permissionsNavBadge：只数面板放行的规则，零条不显示", () => {
  assert.equal(permissionsNavBadge([]), null);
  assert.equal(permissionsNavBadge(null), null);
  assert.deepEqual(permissionsNavBadge(["run_bash(futures *)", "git_status"]),
    { text: "放行 2", tone: "muted" });
});

// ===== 一键技能化：两个入口共用的提示词 =====
const { skillCreatorPrompt } = require("../../web/pure.js");

test("skillCreatorPrompt：点名技能、要求真跑取样与自检；给了目标就带上，没给则先问用户", () => {
  const withTarget = skillCreatorPrompt("  python -m mytool  ");
  assert.ok(withTarget.includes("skill-creator"));          // 必须点名技能，否则可能不触发
  assert.ok(withTarget.includes("「python -m mytool」"));   // 目标带上且去掉首尾空白
  assert.ok(withTarget.includes("真跑一条只读命令"));       // 契约靠实测，不许猜
  assert.ok(withTarget.includes("自检"));
  assert.ok(!withTarget.includes("先问我"), withTarget);    // 已经给了目标就别再问

  const noTarget = skillCreatorPrompt("");
  assert.ok(noTarget.includes("先问我"), noTarget);          // 没给目标 → 先问清楚再动手
  assert.equal(skillCreatorPrompt(null), noTarget);          // null/undefined 与空串一致
  assert.equal(skillCreatorPrompt(undefined), noTarget);
});

// ===== 技能换层按钮 =====
const { skillScopeAction } = require("../../web/pure.js");

test("skillScopeAction：项目级→装到全局，全局/内置→复制到本项目，配置目录不给按钮", () => {
  assert.deepEqual(skillScopeAction("project").scope, "global");
  assert.equal(skillScopeAction("project").label, "装到全局");
  assert.equal(skillScopeAction("global").scope, "project");
  assert.equal(skillScopeAction("builtin").scope, "project");   // 复制一份才能改内置技能
  assert.equal(skillScopeAction("config"), null);               // 用户自己指的路径，别替他搬家
  assert.equal(skillScopeAction(""), null);
});

// ===== diff 行内定向反馈 =====
const { annotateDiffLines, formatLineFeedback } = require("../../web/pure.js");

const DIFF = [
  "--- a/x.py", "+++ b/x.py",
  "@@ -10,4 +10,5 @@ def f():",
  "     ctx1",
  "-    old line",
  "+    new line",
  "+    added",
  "     ctx2",
  "@@ -30,2 +31,2 @@",
  "-    gone",
  "+    back",
].join("\n");

test("annotateDiffLines：行号按 @@ 推算——新增只涨新行号、删除只涨旧行号、上下文两边都涨", () => {
  const rows = annotateDiffLines(DIFF);
  const at = (i) => ({ kind: rows[i].kind, old: rows[i].oldLine, new: rows[i].newLine });
  assert.deepEqual(at(0), { kind: "meta", old: null, new: null });
  assert.deepEqual(at(2), { kind: "hunk", old: null, new: null });
  assert.deepEqual(at(3), { kind: "ctx", old: 10, new: 10 });
  assert.deepEqual(at(4), { kind: "del", old: 11, new: null });
  assert.deepEqual(at(5), { kind: "add", old: null, new: 11 });
  assert.deepEqual(at(6), { kind: "add", old: null, new: 12 });
  assert.deepEqual(at(7), { kind: "ctx", old: 12, new: 13 });
  // 第二个 hunk 要重置行号，不能接着上一个数
  assert.deepEqual(at(9), { kind: "del", old: 30, new: null });
  assert.deepEqual(at(10), { kind: "add", old: null, new: 31 });
});

test("annotateDiffLines：'\\ No newline' 与文件头不占行号，空输入不炸", () => {
  const rows = annotateDiffLines("@@ -1,1 +1,1 @@\n-a\n+b\n\\ No newline at end of file");
  assert.equal(rows[3].kind, "meta");
  assert.equal(rows[3].newLine, null);
  assert.deepEqual(annotateDiffLines(""), [{ text: "", kind: "ctx", oldLine: 0, newLine: 0 }]);
  assert.deepEqual(annotateDiffLines(null), [{ text: "", kind: "ctx", oldLine: 0, newLine: 0 }]);
});

test("formatLineFeedback：锚到 file:新行号，删除行标明是原行号", () => {
  const rows = annotateDiffLines(DIFF);
  const add = formatLineFeedback("src/x.py", rows[5], "  这里应该用 >= 而不是 >  ");
  assert.ok(add.includes("`src/x.py:11`"), add);
  assert.ok(add.includes("```diff\n+    new line\n```"), add);
  assert.ok(add.trim().endsWith("这里应该用 >= 而不是 >"), add);   // 用户的话去掉首尾空白

  const del = formatLineFeedback("src/x.py", rows[4], "这行不该删");
  assert.ok(del.includes("原第 11 行，已删除"), del);
});

// ---- P3 / ADR 0022：后台进程等输入 → 人接管输入行 ----
const { extractWaitingProcess } = require("../../web/pure.js");

test("extractWaitingProcess：从 read_process_output 结果里认出进程号与提示原文", () => {
  const out = [
    "[状态] running",
    "[新增输出]",
    "Need to install create-vite@5.2.3",
    "Ok to proceed? (y)",
    "[提示] 这个进程似乎**停在交互提示上等输入**：`Ok to proceed? (y)`",
    '       要回答就用 write_process_input(id=7, text="y")；不该继续就 stop_process。',
  ].join("\n");
  assert.deepEqual(extractWaitingProcess(out), { id: 7, prompt: "Ok to proceed? (y)" });
});

test("extractWaitingProcess：没等输入 / 非字符串 → null（别到处冒输入框）", () => {
  assert.equal(extractWaitingProcess("[状态] running\n[新增输出]\nbuilding..."), null);
  // 提到了工具名但不是"在等输入"那种结果：不该触发
  assert.equal(extractWaitingProcess("可以用 write_process_input(id=3, text=\"y\") 回答"), null);
  assert.equal(extractWaitingProcess(""), null);
  assert.equal(extractWaitingProcess(null), null);
  assert.equal(extractWaitingProcess(42), null);
});

// ===== 换手面板（ADR 0023）：真实目标 + 凭据边界，两条安全立场钉在这里 =====
const { handoffPanelText, handoffTargetKind, HANDOFF_PRIVACY } = require("../../web/pure.js");

test("handoffTargetKind：URL / 路径 / 应用名分得开（面板据此标注目标类型）", () => {
  assert.equal(handoffTargetKind("https://example.com/login"), "url");
  assert.equal(handoffTargetKind("HTTP://EXAMPLE.COM"), "url");
  assert.equal(handoffTargetKind("C:\\Users\\me\\.env"), "path");
  assert.equal(handoffTargetKind("/etc/hosts"), "path");
  assert.equal(handoffTargetKind("~/.ssh/config"), "path");
  assert.equal(handoffTargetKind("src/app.js"), "path");
  assert.equal(handoffTargetKind("Chrome"), "app");
  assert.equal(handoffTargetKind("微信"), "app");
});

test("handoffPanelText：真实目标与凭据边界声明永远在（换手是天然钓鱼位）", () => {
  const t = handoffPanelText({
    id: 1, reason: "这站要短信验证码", target: "https://bank.example.com/login",
    verify: "重新 snapshot 看是否出现账户名", unattended: false,
  });
  assert.equal(t.target, "https://bank.example.com/login");   // 原样显示，不省略不美化
  assert.equal(t.targetLabel, "网址");
  assert.equal(t.reason, "这站要短信验证码");
  assert.equal(t.verify, "重新 snapshot 看是否出现账户名");
  assert.equal(t.privacy, HANDOFF_PRIVACY);
  assert.match(t.privacy, /不读取、不回传/);
  assert.equal(t.hint, "");
});

test("handoffPanelText：字段缺失也不会显示空白目标（用户得知道自己在给谁登录）", () => {
  const t = handoffPanelText({});
  assert.equal(t.reason, "（未说明原因）");
  assert.equal(t.target, "（未给出目标）");
  assert.equal(t.verify, "");
  assert.equal(t.privacy, HANDOFF_PRIVACY);
  assert.equal(handoffPanelText(null).privacy, HANDOFF_PRIVACY);
});

test("handoffPanelText：无人值守下提示会超时收成阻塞——不会被当成完成", () => {
  const t = handoffPanelText({ reason: "要登录", target: "https://a", unattended: true });
  assert.match(t.hint, /阻塞：待人工换手/);
  assert.match(t.hint, /不会被记成完成/);
});

// ===== 轨迹录制（ADR 0023 决策 4/7）：状态条文案 + 草案校验 =====
const {
  traceBarText, traceNameHint, traceDraftIssues, traceComposePayload,
} = require("../../web/pure.js");

test("traceBarText：一眼看得出录了多久、录到多少步（状态条是防忘关的那道兜底）", () => {
  assert.equal(traceBarText({ recording: true, steps: 3, seconds: 65 }),
    "正在录制轨迹 · 已录 3 步 · 01:05");
  assert.equal(traceBarText({ recording: true, steps: 0, seconds: 0 }),
    "正在录制轨迹 · 已录 0 步 · 00:00");
  assert.match(traceBarText({ recording: true, steps: 120, seconds: 10, full: true }), /已录满/);
});

test("traceNameHint：技能名不合规范只提醒、不阻拦（ADR 0015 §4 接收宽容）", () => {
  assert.equal(traceNameHint("annual-report"), "");
  assert.equal(traceNameHint(""), "");
  assert.match(traceNameHint("Annual Report"), /小写字母与连字符/);
});

test("traceDraftIssues：一步不留 / 一个变量不留都要拦一下", () => {
  const draft = {
    steps: [{ label: "a", keep: false }],
    params: [{ name: "{{网址}}", value: "https://a", keep: false }],
  };
  const issues = traceDraftIssues(draft);
  assert.match(issues.join("；"), /至少留一步/);
  assert.match(issues.join("；"), /流水账/);          // 决策 7：不参数化＝只是这一次的流水账
  // 正常草案：不报任何问题
  assert.deepEqual(traceDraftIssues({
    steps: [{ label: "a", keep: true }],
    params: [{ name: "{{网址}}", value: "https://a", keep: true }],
    skill_name: "annual-report",
  }), []);
  // 压根没抽到变量：不该拿"没变量"去烦用户
  assert.deepEqual(traceDraftIssues({ steps: [{ label: "a" }], params: [] }), []);
});

test("traceComposePayload：勾掉的步骤与变量不进后端入参", () => {
  const p = traceComposePayload({
    goal: " 查年报 ", skill_name: " annual-report ", description: " 说明 ", scope: "global",
    steps: [{ kind: "tool", label: "a", tool: "web_search", keep: true },
            { kind: "tool", label: "b", keep: false }],
    params: [{ name: " {{网址}} ", value: "https://a", keep: true },
             { name: "{{日期}}", value: "2026-01-01", keep: false }],
  });
  assert.equal(p.goal, "查年报");
  assert.equal(p.skill_name, "annual-report");
  assert.equal(p.scope, "global");
  assert.deepEqual(p.steps.map((s) => s.label), ["a"]);
  assert.deepEqual(p.params, [{ name: "{{网址}}", value: "https://a" }]);
});

test("traceComposePayload：范围只认 project/global（脏值一律回落项目级）", () => {
  assert.equal(traceComposePayload({ scope: "系统盘" }).scope, "project");
  assert.equal(traceComposePayload({}).scope, "project");
  assert.equal(traceComposePayload(null).scope, "project");
});

// ===== 真机第二轮抓到的三条（划选评审门槛 / 评审运行态 / 停止键）=====
const {
  canReviewSelection, REVIEW_MIN_CHARS, SEL_REVIEW_MIN_CHARS,
} = require("../../web/pure.js");

test("划选评审门槛 ≥ 后端下限，且不至于高到划一整段都不浮（原来卡 200 就是这么坏的）", () => {
  assert.ok(SEL_REVIEW_MIN_CHARS >= REVIEW_MIN_CHARS, "浮出来却被拒 = 更糟的体验");
  assert.ok(SEL_REVIEW_MIN_CHARS <= 120, "门槛过高＝用户以为功能坏了");
  assert.equal(canReviewSelection("太短"), false);
  assert.equal(canReviewSelection("方".repeat(SEL_REVIEW_MIN_CHARS - 1)), false);
  assert.equal(canReviewSelection("方".repeat(SEL_REVIEW_MIN_CHARS)), true);
  assert.equal(canReviewSelection("  " + "方".repeat(SEL_REVIEW_MIN_CHARS) + "  "), true);
  assert.equal(canReviewSelection(null), false);
});

test("composerState：评审进行中也算「正在跑」——停止键必须露出来", () => {
  const st = composerState({ reviewRunning: true });
  assert.equal(st.running, true);
  assert.equal(st.stopHidden, false);   // 没这条 → 评审一发起就只能干等（真机踩到）
  assert.equal(st.sendHidden, true);
  // 三种运行态互不影响
  assert.equal(composerState({ streaming: true }).stopHidden, false);
  assert.equal(composerState({ crazyRunning: true }).stopHidden, false);
  assert.equal(composerState({}).stopHidden, true);
});

// ===== 换手 × 浏览器：人得**有地方**动手（真机指出的设计漏洞）=====
const { handoffBrowserHint } = require("../../web/pure.js");

test("无头浏览器下的换手：明说「你在自己 Chrome 里登录不算数」并给一键切换", () => {
  const h = handoffBrowserHint("url", { enabled: true, headed: false });
  assert.equal(h.level, "warn");
  assert.equal(h.action, "switch");
  assert.match(h.text, /无头/);
  assert.match(h.text, /不算数/);
});

test("有头时仍要点明「在弹出的那个窗口里登录」（独立 profile，不是日常 Chrome）", () => {
  const h = handoffBrowserHint("url", { enabled: true, headed: true });
  assert.equal(h.level, "info");
  assert.match(h.text, /不是你平时用的 Chrome/);
});

test("压根没开浏览器穿透：告诉用户此路不通，别让他白登一次", () => {
  const h = handoffBrowserHint("url", { enabled: false, headed: false });
  assert.equal(h.level, "warn");
  assert.equal(h.action, "");
  assert.match(h.text, /浏览器穿透/);
});

test("非网页目标（本地路径/应用）不提浏览器的事", () => {
  assert.equal(handoffBrowserHint("path", { enabled: true, headed: false }), null);
  assert.equal(handoffBrowserHint("app", { enabled: false }), null);
  assert.equal(handoffBrowserHint("url", null).level, "warn");   // 状态拿不到＝按最坏情况提示
});
