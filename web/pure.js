// pure.js —— 前端里**可脱离 DOM 的纯逻辑**集中地。
//
// 为什么单独一个文件：app.js 是浏览器全局脚本、整段和 DOM 强耦合，没法在 Node 里单测；
// 而事件路由、状态判断、字符串处理这类纯逻辑出过 bug（如 cid 路由 / 排队竞态）。把它们抽到这里，
// 用 UMD 包一层——浏览器里 pure.js 先于 app.js 加载、把这些函数挂成全局供 app.js 直接用；
// Node 里 module.exports 出来供 tests/web 单测。**以后新增纯逻辑就写这里 + 配 tests/web 单测，
// 别再埋进 DOM 渲染函数里测不了。**
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api; // Node：供 tests/web 单测
  } else {
    for (const k in api) root[k] = api[k]; // 浏览器：挂全局，app.js 直接用同名函数
  }
})(typeof self !== "undefined" ? self : this, function () {
  // 把任意值压成一行简短预览（工具入参摘要等），超 80 字省略
  function summarize(input) {
    try {
      const s = JSON.stringify(input);
      return s.length > 80 ? s.slice(0, 80) + "…" : s;
    } catch (e) {
      return "";
    }
  }

  // HTML 转义（防注入）。与 app.js 历史行为一致：只转 & < >
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // 会话行该亮哪些状态 class。出过 bug 的点：awaiting/unread 优先级、running 含 queued、
  // 活动会话不算未读。判定收敛在这里，DOM 侧只负责按结果 toggle。
  function sessionRowClasses(status, unread, active) {
    return {
      running: status === "running" || status === "queued",
      awaiting: status === "awaiting",
      unread: !!unread && !active,
    };
  }

  // 这些后端状态都算「忙」：占住 streaming，新消息走 steering/排队而非另起一轮。
  function isBusyState(state) {
    return state === "running" || state === "queued" || state === "awaiting";
  }

  // 输入区按钮该长啥样：运行中只留「停止」（发送隐藏，Enter 仍可走 steering）；
  // 规划模式发送键文案变「规划」。v 可能为 null（无活动会话）。
  function composerState(v) {
    const running = !!(v && (v.streaming || v.crazyRunning));
    const planMode = !!(v && v.planMode);
    return {
      running,
      sendHidden: running,
      stopHidden: !running,
      sendText: planMode ? "规划" : "发送",
      planActive: planMode,
    };
  }

  // 任务清单进度：完成数 / 总数（顶部进度条用）。容忍 null。
  function computeTaskProgress(tasks) {
    const list = tasks || [];
    const done = list.filter((t) => t && t.status === "completed").length;
    return { done, total: list.length, text: `${done}/${list.length}` };
  }

  // 会话搜索：标题是否命中查询（空查询命中全部，大小写不敏感，子串匹配）。
  function sessionTitleMatches(title, query) {
    const q = (query || "").trim().toLowerCase();
    return !q || (title || "").toLowerCase().includes(q);
  }

  // slash 命令菜单：输入以 / 开头、且还在打命令名（无空格）时，返回前缀匹配的命令；
  // 否则返回空数组（= 不弹菜单）。
  function matchSlashCommands(commands, inputValue) {
    const val = inputValue || "";
    if (!val.startsWith("/") || /\s/.test(val)) return [];
    const q = val.slice(1).toLowerCase();
    return (commands || []).filter((c) => c.cmd.slice(1).toLowerCase().startsWith(q));
  }

  // 引用回复（P4）：把一段文本转成 Markdown 引用块（每行前缀 "> "），末尾留空行供续写。
  // 空文本返回空串。超长截断（避免把整条超长回答灌进输入框），截断处加省略标记。
  function formatQuote(text, maxChars) {
    const s = (text == null ? "" : String(text)).trim();
    if (!s) return "";
    const max = maxChars || 2000;
    let body = s.length > max ? s.slice(0, max).trimEnd() + " …（引用已截断）" : s;
    const quoted = body.split("\n").map((l) => "> " + l).join("\n");
    return quoted + "\n\n";
  }

  // 面板宽度夹取（P3）：把拖拽算出的像素宽限制在 [min,max]，非数字回落到 fallback。
  function clampWidth(px, min, max, fallback) {
    const n = Number(px);
    if (!Number.isFinite(n)) return fallback != null ? fallback : min;
    return Math.max(min, Math.min(max, n));
  }

  // @ 文件引用（P3）：判断光标处是否正在打一个 @ 文件名 token。
  // 规则：从光标往前找最近的 @；@ 与光标之间不能有空白；@ 前必须是行首或空白（避免 a@b 邮箱误触发）。
  function findMentionQuery(text, caret) {
    const s = text || "";
    const pos = caret == null ? s.length : caret;
    const upto = s.slice(0, pos);
    const at = upto.lastIndexOf("@");
    if (at === -1) return { active: false, query: "", start: -1 };
    const between = upto.slice(at + 1);
    if (/\s/.test(between)) return { active: false, query: "", start: -1 };
    const before = at === 0 ? "" : upto[at - 1];
    if (before && !/\s/.test(before)) return { active: false, query: "", start: -1 };
    return { active: true, query: between, start: at };
  }

  // 按查询过滤候选文件路径（子串匹配、大小写不敏感、限量）。
  function matchFileMentions(files, query, limit) {
    const q = (query || "").toLowerCase();
    const lim = limit || 8;
    return (files || []).filter((f) => f.toLowerCase().includes(q)).slice(0, lim);
  }

  // 把工作区目录树拍平成「文件相对路径」数组（供 @ 补全用；只收文件、不收目录）。
  function flattenTreeFiles(node, out) {
    const acc = out || [];
    if (!node) return acc;
    if (node.type === "file" && node.path) acc.push(node.path);
    (node.children || []).forEach((c) => flattenTreeFiles(c, acc));
    return acc;
  }

  // 把一行 slash 输入拆成命令名（小写）+ 参数（去首尾空白）。
  function parseSlashInput(text) {
    const s = text || "";
    const sp = s.indexOf(" ");
    return {
      cmd: (sp === -1 ? s : s.slice(0, sp)).toLowerCase(),
      arg: sp === -1 ? "" : s.slice(sp + 1).trim(),
    };
  }

  // 首次引导判断：所有 key 都未配置 = 全新用户，启动时自动打开设置面板。
  // 只要已配置任意一个就不强制弹（老用户/已填过的不被打扰）。
  function needsKeySetup(keys) {
    const list = keys || [];
    return list.length > 0 && list.every((k) => !k.set);
  }

  // 模型档案表单校验：通过返回 null，否则返回一句错误提示（前端保存前用，后端会再校验一次）。
  function validateModelProfile(p) {
    const f = p || {};
    if (!(f.name || "").trim()) return "请填写档案名";
    if (!["anthropic", "openai"].includes(f.provider)) return "provider 必须是 anthropic 或 openai";
    if (!(f.model || "").trim()) return "请填写 model";
    if (!(f.api_key_env || "").trim()) return "请填写 api_key_env（对应 .env 里的 key 名）";
    const mt = Number(f.max_tokens);
    if (!Number.isInteger(mt) || mt <= 0) return "max_tokens 必须是正整数";
    return null;
  }

  // ---- 外观设置（P2：浅色主题 + 字号）纯逻辑 ----
  const THEME_PREFS = ["system", "dark", "light"]; // 用户偏好（system=跟随系统）
  const FONT_SIZES = ["sm", "md", "lg"];           // 字号档位

  // 把用户主题偏好 + 系统是否暗色，解析成实际生效主题（"dark"|"light"）。
  // 非法 pref 当作 system；data-theme 用它写到 <html>。
  function resolveTheme(pref, prefersDark) {
    const p = THEME_PREFS.includes(pref) ? pref : "system";
    if (p === "system") return prefersDark ? "dark" : "light";
    return p;
  }

  // 归一字号档位：非法值回落到 md（中）。
  function normFontSize(f) {
    return FONT_SIZES.includes(f) ? f : "md";
  }

  // 是否该弹出快捷键帮助面板：? 键，或 Ctrl/⌘+/。（"打字中不触发 ?" 的判断在 app.js 侧，
  // 因为要看事件目标是不是输入框——这里只判按键组合本身。）
  function isHelpKey(key, ctrlOrMeta) {
    return key === "?" || (!!ctrlOrMeta && key === "/");
  }

  // ---- 会话累计用量（P2）----
  // 把一次 usage 事件累加进会话累计；acc 可为空。返回新累计（不改原对象）。
  function accumulateUsage(acc, ev) {
    const a = acc || { input: 0, output: 0, cacheRead: 0, turns: 0 };
    const e = ev || {};
    return {
      input: a.input + (Number(e.input) || 0),
      output: a.output + (Number(e.output) || 0),
      cacheRead: a.cacheRead + (Number(e.cache_read) || 0),
      turns: a.turns + 1,
    };
  }

  // 成本估算价目表：USD / 每百万 token，[模型名子串(小写), 输入价, 输出价]。
  // ⚠ 公开列表价粗估、会随官方调价过时；要校准就改这里。匹配不到则不显示成本。
  const MODEL_PRICING = [
    ["claude-opus", 15, 75], ["opus", 15, 75],
    ["claude-sonnet", 3, 15], ["sonnet", 3, 15],
    ["claude-haiku", 0.8, 4], ["haiku", 0.8, 4],
    ["gpt-4o-mini", 0.15, 0.6], ["gpt-4o", 2.5, 10],
    ["deepseek", 0.27, 1.1],
    ["kimi", 0.15, 2.5], ["moonshot", 0.15, 2.5], ["k2", 0.15, 2.5],
  ];

  // 估算累计成本（USD）。命中价目表才算，否则返回 null（UI 据此只显 token、不显 $）。
  // 缓存读按输入价的 10% 计（多数厂商缓存命中显著便宜，粗略折算）。
  function estimateCostUsd(model, usage) {
    const m = String(model || "").toLowerCase();
    const u = usage || {};
    const hit = MODEL_PRICING.find(([pat]) => m.includes(pat));
    if (!hit) return null;
    const [, inP, outP] = hit;
    const cost = ((u.input || 0) * inP + (u.cacheRead || 0) * inP * 0.1 + (u.output || 0) * outP) / 1e6;
    return cost;
  }

  // 工具输出折叠判定（P2）：超过行数/字符阈值时默认只展示前若干行，给「展开」入口。
  // 返回 folded=false 表示短、原样全显；folded=true 时 preview 是截断预览、full 是全文。
  // 前台实时流输出：把新增量拼到已有 live 缓冲，并只保留尾部 maxChars（长跑命令不撑爆 DOM）。
  // 截断时前缀 "…（上文已省略）\n"，让用户知道看到的是尾部。返回新缓冲字符串。
  function appendStreamBuffer(prev, delta, maxChars) {
    const mc = maxChars || 20000;
    let buf = (prev == null ? "" : String(prev)) + (delta == null ? "" : String(delta));
    if (buf.length > mc) {
      buf = "…（上文已省略）\n" + buf.slice(buf.length - mc);
    }
    return buf;
  }

  function foldToolOutput(text, maxLines, maxChars) {
    const ml = maxLines || 20, mc = maxChars || 2000;
    const s = text == null ? "" : String(text);
    const lines = s.split("\n");
    const tooMany = lines.length > ml || s.length > mc;
    if (!tooMany) return { folded: false, preview: s, full: s, total: lines.length, hidden: 0 };
    let preview = lines.slice(0, ml).join("\n");
    if (preview.length > mc) preview = preview.slice(0, mc);
    return { folded: true, preview, full: s, total: lines.length, hidden: Math.max(0, lines.length - ml) };
  }

  // 方案评审的标题/副标题（ADR 0019）：**按真实模型分配说话**。
  // 三个角色落在同一个模型上时不许再叫「多模型讨论」——同模型的错误高度相关，
  // 它挑不出自己看不见的问题，管这个叫多模型讨论是骗自己。
  function debateHeader(models, heterogeneous) {
    const m = models || {};
    const short = (p) => String(p || "").split("/").pop() || "?";
    if (!m.product || !m.technical) {                    // 老会话/没带分配信息：保持中性措辞
      return { title: "🔬 方案评审 · 多轮讨论", warn: false,
               sub: "产品镜头 → 技术镜头 → 主模型回复 · 逐轮收敛" };
    }
    if (heterogeneous) {
      return { title: "🔬 方案评审 · 多模型讨论", warn: false,
               sub: `产品镜头 · ${short(m.product)} → 技术镜头 · ${short(m.technical)}`
                    + ` → 主模型回复 · ${short(m.main)} · 逐轮收敛` };
    }
    return { title: "🔬 方案评审 · 单模型自审", warn: true,
             sub: `⚠ 三个角色都是同一个模型（${short(m.product)}）：同模型的错误高度相关，`
                  + `对冲价值有限。在设置里配第二个 provider 才是真正的多模型讨论。` };
  }

  // 工具产物句柄（ADR 0021）：从工具结果文本里认出产物路径，前端把它渲染成可点开的文件。
  // 认**路径**而不是认提示语——三处接入点（前台 shell / web_fetch / 后台进程）措辞各不相同，
  // 但路径形态是统一的，将来改文案也不会把这里改坏。按出现顺序去重。
  const ARTIFACT_RE = /(?:^|[^\w/\\.])((?:\.hermes)\/artifacts\/(art_\d+)\.(?:txt|log))/g;

  function extractArtifacts(text) {
    if (!text || typeof text !== "string") return [];
    const out = [];
    const seen = new Set();
    let m;
    ARTIFACT_RE.lastIndex = 0;
    while ((m = ARTIFACT_RE.exec(text)) !== null) {
      const path = m[1];
      if (seen.has(path)) continue;
      seen.add(path);
      out.push({ id: m[2], path });
    }
    return out;
  }

  // 工具结果的结构化评估（块B 事实层，见 docs/adr/0014）→ 一行人读摘要。
  // eval = {metrics, signals, issues, confidence, score}。有 issues=有问题(warn)，
  // 否则按是否有 signals 给 ok/中性。返回 null 表示无可展示事实（不渲染）。
  function formatEval(ev) {
    if (!ev || typeof ev !== "object") return null;
    const metrics = ev.metrics || {};
    const signals = ev.signals || [];
    const issues = ev.issues || [];
    const parts = [];
    // 测试类：N/total 通过最有信息量
    if (metrics.total != null && metrics.passed != null) {
      parts.push(`${metrics.passed}/${metrics.total} 通过`);
    } else if (metrics.hits != null) {
      parts.push(`命中 ${metrics.hits} 条`);
    } else if (metrics.exit_code != null) {
      parts.push(`退出码 ${metrics.exit_code}`);
    }
    // 信号补充（最多两条，避免刷屏）
    signals.slice(0, 2).forEach((s) => { if (!parts.includes(s)) parts.push(s); });
    if (!parts.length && !issues.length) return null;
    const level = issues.length ? "warn" : "ok";
    // 块C：失败时把错误分类标签缀在末尾（如 [transient_io]），给人快速根因感
    const classes = (ev.error_classes || []).filter(Boolean);
    const tag = classes.length ? ` [${classes.join("/")}]` : "";
    const text = (issues.length ? "⚠ " : "") + parts.join(" · ") +
                 (issues.length ? `（${issues.join("；")}）` : "") + tag;
    return { level, text, score: typeof ev.score === "number" ? ev.score : null };
  }

  // ── ADR 0019 方案评审面板：纯逻辑（DOM 由 app.js 渲染）─────────────────
  const REVIEW_STATUSES = ["Accepted", "Rejected", "Deferred", "NeedUser", "Open"];
  const REVIEW_LABELS = {
    Accepted: "Accepted（采纳）", Rejected: "Rejected（否决）",
    Deferred: "Deferred（后置）", NeedUser: "Need User Decision（待你拍板）",
    Open: "Open（仍在评审）",
  };

  // 把 gate 状态译成"开工按钮"的 UI 态：能否点 + 文案。**绝不出现百分比**（守 ADR 0014/0019）。
  function reviewGateLabel(gate) {
    if (!gate) return { enabled: false, text: "尚未评审" };
    if (gate.can_start) return { enabled: true, text: "开始编码" };
    const n = gate.blocking_count || 0;
    return { enabled: false,
             text: n > 0 ? `还有 ${n} 个未决问题` : "等待签字确认" };
  }

  // 决策按四态分组（Open 垫底），供面板分区渲染。
  function decisionsByStatus(decisions) {
    const groups = {};
    REVIEW_STATUSES.forEach((s) => { groups[s] = []; });
    (decisions || []).forEach((d) => {
      const s = REVIEW_STATUSES.includes(d.status) ? d.status : "Open";
      groups[s].push(d);
    });
    return groups;
  }

  // 一个决策是否还"挂着未决"（NeedUser 或带未澄清 blocking）→ 面板高亮提示用户拍板。
  function decisionNeedsUser(d) {
    return !!d && (d.status === "NeedUser" ||
                   (Array.isArray(d.blocking) && d.blocking.length > 0));
  }

  // ── ADR 0019 v4 分屏辩论：纯逻辑（DOM 由 app.js 逐 token 渲染）───────────
  // 异构双镜头：产品（市场/路线图/价值）⟷ 技术（选型/架构/风险），主模型收敛=第三视角。
  const DEBATE_ROLES = ["product", "technical"];
  const DEBATE_ROLE_LABELS = {
    product: "产品镜头 · 市场/路线图/价值",
    technical: "技术镜头 · 选型/架构/风险",
  };
  // v5 hub-and-spoke：主模型（hub）逐轮回复——两评审员只进言，采纳/反驳/收敛由主模型逐条回复决定。
  const DEBATE_MAIN = "main";
  const DEBATE_MAIN_LABEL = "主模型回复 · 逐条采纳/反驳/收敛";
  // 主模型逐轮回复的轮标：给整宽回复区一个"第 N 轮 · 主模型回复"小标题。
  function debateMainRoundLabel(round) {
    return `第 ${round || 1} 轮 · ${DEBATE_MAIN_LABEL}`;
  }
  const DEBATE_STATUS_ZH = {
    Accepted: "采纳", Rejected: "否决", Deferred: "后置", NeedUser: "待拍板",
  };
  // reviewer 输出「散文在前、```json 结论在末」：拆成给人看的散文 + 机器结论，分屏只显散文。
  function splitVerdictProse(text) {
    const s = text || "";
    const i = s.lastIndexOf("```json");
    if (i < 0) return { prose: s.trim(), json: "" };
    const rest = s.slice(i + "```json".length);
    const end = rest.indexOf("```");
    return { prose: s.slice(0, i).trim(), json: (end < 0 ? rest : rest.slice(0, end)).trim() };
  }
  // 把一位评审员的结论 JSON（数组）归纳成一句状态计数，**绝不出现百分比**（守 ADR 0014/0019）。
  function verdictTally(jsonText) {
    let arr;
    try { arr = JSON.parse(jsonText); } catch (e) { return ""; }
    if (!Array.isArray(arr)) return "";
    const cnt = {};
    arr.forEach((d) => { const s = d && d.status; if (s) cnt[s] = (cnt[s] || 0) + 1; });
    return ["Accepted", "Rejected", "Deferred", "NeedUser"]
      .filter((s) => cnt[s]).map((s) => `${DEBATE_STATUS_ZH[s]}×${cnt[s]}`).join(" · ");
  }
  // 收敛横幅文案：停因 + 轮数，人话表述（引擎已保证停因可数，无分数）。
  function debateConvergedText(payload) {
    const reasons = {
      no_new_blocking: "无新增未决问题",
      wording_only: "两轮仅措辞微调",
      max_rounds: "达到最大轮数",
    };
    const p = payload || {};
    const why = reasons[p.stop_reason] || p.stop_reason || "已收敛";
    return `✅ 讨论收敛（${p.rounds || 0} 轮 · ${why}），下面由主模型汇总共识`;
  }

  // 会话列表分组渲染计划（对齐 Figma：已置顶 / 最近）。返回有序渲染项：
  // {type:"group",label} 或 {type:"item",session}。无置顶时不加分组标题（保持扁平列表）。
  function planSessionList(sessions) {
    const list = sessions || [];
    const pinned = list.filter((s) => s && s.pinned);
    const recent = list.filter((s) => s && !s.pinned);
    const plan = [];
    if (pinned.length) {
      plan.push({ type: "group", label: "已置顶" });
      pinned.forEach((s) => plan.push({ type: "item", session: s }));
      if (recent.length) plan.push({ type: "group", label: "最近" }); // 有置顶才需「最近」分隔
    }
    recent.forEach((s) => plan.push({ type: "item", session: s }));
    return plan;
  }

  // 工作区标签页可见性（对齐 Figma 重设计）：改动/评审「发生才出现」，文件/预览常驻。
  // avail = { hasChanges, hasCheckpoints, hasReview }（都是布尔）。纯逻辑，DOM 只负责喂状态+渲染。
  const WS_TAB_KEYS = ["changes", "files", "preview", "review"];
  function wsTabVisible(key, avail) {
    avail = avail || {};
    if (key === "changes") return !!(avail.hasChanges || avail.hasCheckpoints);
    if (key === "review") return !!avail.hasReview;
    return true; // 文件 / 预览 常驻
  }
  // 给定可见性 + 期望激活标签，算出：可见标签序列、实际激活标签（消失则回"文件"）、是否显示标签条。
  function resolveWorkspaceTabs(avail, wantActive) {
    const tabs = WS_TAB_KEYS.filter((k) => wsTabVisible(k, avail));
    let active = wantActive;
    if (!tabs.includes(active)) active = "files"; // 期望标签不可见→回文件
    return { tabs, active, showStrip: tabs.length > 1 };
  }

  // 应用内更新条幅（ADR 0020）：由 check_update 结果决定是否显示 + 显示什么。纯逻辑，DOM 侧只按结果渲染。
  function shouldShowUpdate(info) {
    return !!(info && info.ok && info.newer && info.latest);
  }
  function updateBannerHtml(info) {
    // 前置条件由 shouldShowUpdate 把关；这里只拼展示。版本号转义防注入。
    const cur = escapeHtml((info && info.current) || "");
    const latest = escapeHtml((info && info.latest) || "");
    return (
      '<span class="upd-text">发现新版本 <b>v' + latest + "</b>" +
      (cur ? ' <span class="upd-cur">（当前 v' + cur + "）</span>" : "") + "</span>" +
      '<span class="upd-actions">' +
      '<button id="upd-apply" class="upd-btn upd-btn-primary">立即更新</button>' +
      '<button id="upd-later" class="upd-btn">稍后</button>' +
      "</span>"
    );
  }

  // 技能安全分级 → 徽章展示（FR-13.S2）。分级名沿用社区注册表的 clean/review/warn。
  // 措辞刻意不说"安全"——扫描是启发式的，只说"未发现可疑信号"。
  const SKILL_GRADES = {
    clean: { icon: "✓", label: "未发现可疑信号", cls: "sg-clean" },
    review: { icon: "⚠", label: "建议过目", cls: "sg-review" },
    warn: { icon: "⛔", label: "高风险", cls: "sg-warn" },
  };
  function skillGradeBadge(grade) {
    return SKILL_GRADES[grade] || { icon: "?", label: "未扫描", cls: "sg-unknown" };
  }

  // 安装按钮的确认强度：绿档直接装；黄档需先看一眼（一次确认）；红档二次确认且默认不装。
  function installConfirmLevel(grade) {
    if (grade === "warn") return { needConfirm: true, danger: true, text: "仍要安装" };
    if (grade === "review") return { needConfirm: true, danger: false, text: "确认安装" };
    return { needConfirm: false, danger: false, text: "安装" };
  }

  // 市场条目筛选：按名字/描述/分类/关键词子串匹配（空查询返回全部）。
  // 深扫过（skill_count 已知）后，不含技能的条目直接滤掉——插件可以只有 commands/agents/hooks，
  // 那些 hermes 装不了，列出来只会让人点进去白等一次下载。
  function filterMarketEntries(entries, query, hideEmpty) {
    const q = (query || "").trim().toLowerCase();
    let list = entries || [];
    if (hideEmpty) list = list.filter((e) => e.skill_count === null
      || e.skill_count === undefined || e.skill_count > 0);
    if (!q) return list;
    return list.filter((e) => {
      const hay = [e.name, e.description, e.category, ...(e.keywords || [])]
        .filter(Boolean).join(" ").toLowerCase();
      return hay.includes(q);
    });
  }

  // 条目上的技能数标记：未深扫时不显示，深扫后显示「N 个技能」或标明不含技能。
  function skillCountLabel(count) {
    if (count === null || count === undefined) return "";
    return count > 0 ? `${count} 个技能` : "不含技能";
  }

  // 检查更新的结果 → 卡片上的一行状态（FR-13.S3）。
  const UPDATE_STATUS = {
    update: { text: "有新版本", cls: "su-update" },
    current: { text: "已是最新", cls: "su-current" },
    no_source: { text: "无来源记录", cls: "su-muted" },
    gone: { text: "上游已移除", cls: "su-muted" },
    error: { text: "检查失败", cls: "su-muted" },
  };
  function updateStatusLabel(status) {
    return UPDATE_STATUS[status] || { text: "", cls: "" };
  }

  // 检查更新后的总结文案：有更新说几个，没更新也要说清「检查了什么、什么没法检查」，
  // 不能只说"全是最新"——手动放进来的技能压根没法检查，含糊其辞会让人误以为都盯着了。
  function summarizeUpdateCheck(results) {
    const list = results || [];
    const n = (s) => list.filter((r) => r.status === s).length;
    const up = n("update");
    const parts = [];
    if (up) parts.push(`${up} 个有新版本`);
    if (n("current")) parts.push(`${n("current")} 个已是最新`);
    if (n("no_source")) parts.push(`${n("no_source")} 个无来源记录（查不了）`);
    if (n("gone")) parts.push(`${n("gone")} 个上游已移除`);
    if (n("error")) parts.push(`${n("error")} 个检查失败`);
    if (!parts.length) return "没有可检查的已装技能";
    return parts.join("，");
  }

  // 已装技能按来源分组（内置/全局/项目级），供面板分区展示。
  function groupSkillsBySource(skills) {
    const groups = { builtin: [], global: [], config: [], project: [] };
    (skills || []).forEach((s) => {
      (groups[s.source] || (groups[s.source] = [])).push(s);
    });
    return groups;
  }

  const SOURCE_LABELS = {
    builtin: "内置（随程序分发）", global: "已安装（全局）",
    config: "配置目录", project: "本项目（.hermes/skills）",
  };

  // ---- 自定义斜杠命令（FR-13.C1）：内置 + 用户自定义合并 ----

  // 合并成补全菜单用的清单。内置在前（顺序稳定、肌肉记忆不变），自定义在后。
  // 同名以内置为准——后端 discover_commands 已挡掉同名文件，这里再兜一层，
  // 因为被顶掉的若是 /crazy 这种免确认入口，代价太大。
  function mergeSlashCommands(builtin, custom) {
    const out = (builtin || []).map((c) => ({ ...c, custom: false }));
    const taken = new Set(out.map((c) => c.cmd));
    (custom || []).forEach((c) => {
      const cmd = c.slash || ("/" + (c.name || ""));
      if (!c || !c.name || taken.has(cmd)) return;
      taken.add(cmd);
      out.push({
        cmd,
        arg: c.argument_hint || "",
        desc: (c.description || "自定义命令") + (c.mode === "exec" ? "　·直接执行" : ""),
        custom: true,
        mode: c.mode || "prompt",
        source: c.source || "",
      });
    });
    return out;
  }

  // 把用户敲的 `/盯盘` 对到具体自定义命令上（找不到返回 null，由调用方提示"未知命令"）。
  function findCustomCommand(custom, cmdText) {
    const name = String(cmdText || "").replace(/^\//, "").trim();
    if (!name) return null;
    return (custom || []).find((c) => c && c.name === name) || null;
  }

  // 焦点环绕（浮层 focus trap 用）：Tab 到末尾回到开头，Shift+Tab 到开头绕到末尾。
  // 当前焦点不在浮层内时（cur<0）：Tab 进第一个、Shift+Tab 进最后一个。
  function wrapFocusIndex(len, cur, shift) {
    if (!len) return -1;
    if (cur < 0) return shift ? len - 1 : 0;
    return shift ? (cur - 1 + len) % len : (cur + 1) % len;
  }

  // ---- diff 行内定向反馈（UX Tier2-③）----
  // 把 unified diff 逐行标注上「这行在新/旧文件里是第几行」。行号靠 @@ -a,b +c,d @@ 推算——
  // 反馈要能定位到 file:line，行号错了整条反馈就是错的，所以这段必须单测。
  function annotateDiffLines(diff) {
    const out = [];
    let oldNo = 0, newNo = 0;
    String(diff || "").split("\n").forEach((text) => {
      const hunk = /^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/.exec(text);
      if (hunk) {
        oldNo = parseInt(hunk[1], 10);
        newNo = parseInt(hunk[2], 10);
        out.push({ text, kind: "hunk", oldLine: null, newLine: null });
        return;
      }
      if (text.startsWith("+++") || text.startsWith("---") || text.startsWith("diff ") ||
          text.startsWith("index ") || text.startsWith("new file") || text.startsWith("deleted file")) {
        out.push({ text, kind: "meta", oldLine: null, newLine: null });
        return;
      }
      if (text.startsWith("\\")) {                 // "\ No newline at end of file"
        out.push({ text, kind: "meta", oldLine: null, newLine: null });
        return;
      }
      if (text.startsWith("+")) {
        out.push({ text, kind: "add", oldLine: null, newLine: newNo++ });
      } else if (text.startsWith("-")) {
        out.push({ text, kind: "del", oldLine: oldNo++, newLine: null });
      } else {
        // 上下文行（含 diff 末尾的空串）
        out.push({ text, kind: "ctx", oldLine: oldNo++, newLine: newNo++ });
      }
    });
    return out;
  }

  // 一条行内反馈组装成发给模型的消息。锚点用「新文件行号」，删除行只有旧行号则标明。
  function formatLineFeedback(path, entry, note) {
    const e = entry || {};
    const n = String(note || "").trim();
    const anchor = e.newLine != null
      ? `${path}:${e.newLine}`
      : `${path}（原第 ${e.oldLine} 行，已删除）`;
    const code = String(e.text || "").replace(/\n$/, "");
    return `关于 \`${anchor}\` 这一行：\n\n\`\`\`diff\n${code}\n\`\`\`\n\n${n}`;
  }

  // 技能卡片上的"换个层"按钮：项目级 → 装到全局（换项目也能用）；
  // 全局/内置 → 复制到本项目（只在这个项目里改一份，不动原来那份）。
  // 配置目录来源的不给按钮——那是用户自己在 config 里指的路径，别替他搬家。
  function skillScopeAction(source) {
    if (source === "project") {
      return { scope: "global", label: "装到全局", title: "复制到全局技能目录，所有项目都能用" };
    }
    if (source === "global" || source === "builtin") {
      return { scope: "project", label: "复制到本项目",
               title: "在本项目复制一份，可单独修改（同名时项目级优先）" };
    }
    return null;
  }

  // ---- 一键技能化（P2）：/技能化 与 🧩 技能页按钮共用同一段提示词 ----
  // 放这里而不是各写一份：两个入口给的指令必须一模一样，否则同一个功能两种行为。
  function skillCreatorPrompt(target) {
    const t = String(target || "").trim();
    return "（使用 `skill-creator` 技能）把" + (t ? `「${t}」` : "我的一个程序") +
      "做成 hermes 技能：先摸清接口（--help / 读源码），**真跑一条只读命令**拿真实输出，" +
      "据此写 SKILL.md，然后跑技能自检脚本改到通过。" +
      (t ? "" : "开始前先问我：程序入口怎么调、哪些子命令是只读的、技能名叫什么。") +
      "做完告诉我生成了什么、取样跑的哪条命令、自检结果，并问我要不要顺手绑一个斜杠命令。";
  }

  // ---- 设置面板导航：分组 + 状态徽标（对标主流设置：分区标题 + 行内状态，不必点进去才知道）----

  // 固定面板（非 provider）。顺序即展示顺序；group 决定归到哪一区。
  const SETTINGS_PANES = [
    { key: "__browser__", label: "🌐 浏览器穿透", group: "capabilities" },
    { key: "__mcp__", label: "🔌 MCP 扩展", group: "capabilities" },
    { key: "__hooks__", label: "🪝 Hooks", group: "capabilities" },
    { key: "__skills__", label: "🧩 技能", group: "capabilities" },
    { key: "__commands__", label: "⌨ 命令", group: "capabilities" },
    { key: "__appearance__", label: "🎨 外观", group: "general" },
    { key: "__features__", label: "🛠 功能开关", group: "general" },
    { key: "__permissions__", label: "🔐 权限", group: "general" },
    { key: "__limits__", label: "📊 限额与预算", group: "general" },
  ];
  const SETTINGS_GROUPS = [
    { id: "models", title: "模型服务" },
    { id: "capabilities", title: "扩展能力" },
    { id: "general", title: "通用" },
  ];

  // 各面板的导航徽标。约定：没什么可说时回 null（不显示），别拿「0」占位制造噪音。
  // tone: ok=已连通(绿) / warn=有问题(黄) / muted=只是计数(灰)。

  function mcpNavBadge(r) {
    const servers = (r && r.servers) || {};
    const connected = (r && r.connected) || {};
    const names = Object.keys(servers).filter((n) => servers[n] && servers[n].enabled);
    if (!names.length) return null;
    let tools = 0, down = 0;
    names.forEach((n) => {
      const k = (connected[n] || []).length;
      tools += k;
      if (!k) down += 1;
    });
    if (down) return { text: `${down} 未连上`, tone: "warn" };
    return { text: `${tools} 工具`, tone: "ok" };
  }

  function browserNavBadge(s) {
    if (!s || !s.enabled) return null;          // 未启用＝默认态，不用喊
    if (s.connected) return { text: "已连上", tone: "ok" };
    return { text: s.node ? "装配中" : "缺 Node", tone: "warn" };
  }

  function skillsNavBadge(skills) {
    const n = (skills || []).length;
    return n ? { text: String(n), tone: "muted" } : null;
  }

  function commandsNavBadge(commands, errors) {
    // 有加载失败的命令文件时优先报问题——一条存了却用不了的命令，比少显示个数字严重
    if ((errors || []).length) return { text: `${errors.length} 个没加载`, tone: "warn" };
    const n = (commands || []).length;
    return n ? { text: String(n), tone: "muted" } : null;
  }

  // 权限徽标：只数"用户自己放行的"——config.yaml 手编的规则不归面板管，数进来会误导
  function permissionsNavBadge(userAllow) {
    const n = (userAllow || []).length;
    return n ? { text: `放行 ${n}`, tone: "muted" } : null;
  }

  function hooksNavBadge(hooks) {
    const n = ((hooks || []).filter((h) => !h || h.enabled !== false)).length;
    return n ? { text: String(n), tone: "muted" } : null;
  }

  // 把 provider 列表 + 固定面板拼成分组导航。badges: {面板key: {text,tone}}；空组自动丢掉。
  function buildSettingsNav(providers, badges) {
    const b = badges || {};
    const items = {
      models: (providers || []).map((p) => ({
        key: p.key, label: p.label, kind: "provider", dot: !!p.enabled, badge: null,
      })),
      capabilities: [], general: [],
    };
    SETTINGS_PANES.forEach((p) => {
      (items[p.group] || (items[p.group] = [])).push({
        key: p.key, label: p.label, kind: "pane", dot: null, badge: b[p.key] || null,
      });
    });
    return SETTINGS_GROUPS
      .map((g) => ({ id: g.id, title: g.title, items: items[g.id] || [] }))
      // 空组不占位；但「模型服务」永远留着——它挂着「+ 自定义服务」入口，一个 provider 都没有时更要能加
      .filter((g) => g.items.length || g.id === "models");
  }

  return {
    SETTINGS_PANES, SETTINGS_GROUPS, buildSettingsNav, wrapFocusIndex,
    mergeSlashCommands, findCustomCommand, skillCreatorPrompt, skillScopeAction,
    annotateDiffLines, formatLineFeedback,
    mcpNavBadge, browserNavBadge, skillsNavBadge, hooksNavBadge, commandsNavBadge,
    permissionsNavBadge,
    SKILL_GRADES, skillGradeBadge, installConfirmLevel, filterMarketEntries,
    groupSkillsBySource, SOURCE_LABELS, skillCountLabel,
    UPDATE_STATUS, updateStatusLabel, summarizeUpdateCheck,
    shouldShowUpdate, updateBannerHtml,
    summarize, escapeHtml, sessionRowClasses, isBusyState, composerState,
    computeTaskProgress, sessionTitleMatches, matchSlashCommands, parseSlashInput,
    needsKeySetup, validateModelProfile,
    THEME_PREFS, FONT_SIZES, resolveTheme, normFontSize, isHelpKey, foldToolOutput, appendStreamBuffer,
    accumulateUsage, estimateCostUsd, MODEL_PRICING,
    findMentionQuery, matchFileMentions, flattenTreeFiles, clampWidth, formatQuote,
    formatEval, extractArtifacts, debateHeader,
    REVIEW_STATUSES, REVIEW_LABELS, reviewGateLabel, decisionsByStatus, decisionNeedsUser,
    DEBATE_ROLES, DEBATE_ROLE_LABELS, DEBATE_MAIN, DEBATE_MAIN_LABEL, debateMainRoundLabel,
    DEBATE_STATUS_ZH, splitVerdictProse, verdictTally, debateConvergedText,
    planSessionList,
    WS_TAB_KEYS, wsTabVisible, resolveWorkspaceTabs,
  };
});
