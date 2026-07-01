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

  return {
    summarize, escapeHtml, sessionRowClasses, isBusyState, composerState,
    computeTaskProgress, sessionTitleMatches, matchSlashCommands, parseSlashInput,
    needsKeySetup, validateModelProfile,
    THEME_PREFS, FONT_SIZES, resolveTheme, normFontSize, isHelpKey, foldToolOutput,
    accumulateUsage, estimateCostUsd, MODEL_PRICING,
    findMentionQuery, matchFileMentions, flattenTreeFiles, clampWidth, formatQuote,
    formatEval,
    REVIEW_STATUSES, REVIEW_LABELS, reviewGateLabel, decisionsByStatus, decisionNeedsUser,
    planSessionList,
    WS_TAB_KEYS, wsTabVisible, resolveWorkspaceTabs,
  };
});
