"use strict";

// 与 Python 桥约定的事件名（集中管理，遵循 CONVENTIONS §5）
const EV = {
  CHUNK: "chunk",
  THINKING: "thinking",
  TOOL_USE: "tool_use",
  TOOL_RESULT: "tool_result",
  PERMISSION: "permission_request",
  ASK_USER: "ask_user",
  CRAZY_START: "crazy_start",
  CRAZY_ROUND: "crazy_round",
  CRAZY_REPLAN: "crazy_replan",
  CRAZY_DONE: "crazy_done",
  VISION_START: "vision_start",
  VISION_DONE: "vision_done",
  CONTEXT_COMPRESSED: "context_compressed",
  SESSION_CREATED: "session_created",
  MEMORY_CAPTURED: "memory_captured",
  WORKSPACE_CHANGED: "workspace_changed",
  CONVENTIONS_GENERATED: "conventions_generated",
  STATE: "state",
  DONE: "done",
  STOPPED: "stopped",
  TASKS_UPDATED: "tasks_updated",
  CHECKPOINT_CREATED: "checkpoint_created",
  ENQUEUED: "enqueued",
  USAGE: "usage",
  STEP_WARNING: "step_warning",
  SUBAGENT_START: "subagent_start",
  SUBAGENT_EVENT: "subagent_event",
  SUBAGENT_DONE: "subagent_done",
  AUTO_TEST: "auto_test",
  ERROR: "error",
};

const chat = document.getElementById("chat");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const stopBtn = document.getElementById("stop");
const modelSelect = document.getElementById("model-select");
const subagentSelect = document.getElementById("subagent-select");
const newSessionBtn = document.getElementById("new-session");
const attachBtn = document.getElementById("attach-btn");
const fileInput = document.getElementById("file-input");
const attachmentsBar = document.getElementById("attachments");
const sessionList = document.getElementById("session-list");
const sessionSearch = document.getElementById("session-search");

// 桥未就绪前禁用输入，避免点击发送/下拉时卡住（pywebviewready 后再开放）
input.disabled = true;
sendBtn.disabled = true;

// ---- 多对话视图（FR-8.2b）---------------------------------------------
// 每个对话(cid)有独立的 chat 容器与渲染状态；只有活动对话挂载在 #chat 里，
// 后台对话的 DOM 离屏保留、事件照常渲染进去，切回时挂载即"续看"。
const views = new Map();          // cid -> view
let activeCid = null;             // 当前挂载的对话 cid
let activeSessionId = null;       // 当前会话 id（会话列表高亮用）
const sessionIdToCid = new Map(); // session_id -> cid（由 session_created 建立）

let pendingAttachments = [];      // 待发送附件 [{name, mime, data(base64), dataUrl, isImage}]

function makeView(cid) {
  const el = document.createElement("div");
  el.className = "chat-view";
  return {
    cid, el,
    sessionId: null,
    streaming: false,        // 是否正在接收回复
    status: "idle",          // idle / queued / running / awaiting / error
    unread: false,           // 非活动时来了新内容
    tasks: [],               // 任务清单（FR-9.1）[{content, status}]
    subBlocks: {},           // 子任务块（FR-9.3）sub_id -> {stream,streamText,activity,status,details}
    currentBubble: null,     // 当前 assistant 文本气泡 DOM
    currentText: "",         // 当前 assistant 累积文本
    userTurns: 0,            // 已出现的用户消息数（= 用户轮次序号，供重新生成/编辑重发定位）
    toolBlocks: {},          // tool_use_id -> 工具块 DOM
    visionBlock: null,       // 当前视觉预处理提示块 DOM
    thinkingEl: null,        // 思考过程块 DOM
    thinkingText: "",
    workingEl: null, workTimer: null, workStart: 0,  // 工作指示器
    usage: null,             // 本会话累计用量（P2）{input,output,cacheRead,turns}；本次打开以来累计
    draft: "",               // 未发送的输入草稿（P3）：切会话保留、切回还原（本次打开以来，内存态）
  };
}

function getView(cid) {
  let v = views.get(cid);
  if (!v) { v = makeView(cid); views.set(cid, v); }
  return v;
}
function activeView() { return activeCid != null ? getView(activeCid) : null; }
function isActive(v) { return !!v && v.cid === activeCid; }

function mountView(cid) {
  // 切走前把当前输入存为旧会话草稿（P3）；切回来能还原，不同会话草稿互不串
  const prev = activeCid != null ? views.get(activeCid) : null;
  if (prev) prev.draft = input.value;
  const v = getView(cid);
  chat.innerHTML = "";
  chat.appendChild(v.el);
  activeCid = cid;
  v.unread = false;
  input.value = v.draft || ""; autoResize();   // 还原该会话未发送的草稿（P3）
  rebuildChatIndex();
  scrollChatForce();   // 切换对话：到底看最新、恢复粘底
  renderTaskBar();   // 先按已有 v.tasks 渲染
  refreshTasks();    // 再从后端拉当前会话权威清单
  updateComposerButtons();  // 同步规划模式/发送按钮到该会话状态（FR-11.5）
  updateUsageChip();        // 刷新顶部累计用量芯片到该会话（P2）
  if (typeof refreshReview === "function") refreshReview();  // 评审面板按会话独立（无则自动隐藏）
}

// ---- 任务清单面板（FR-9.1，对话区顶部可折叠条）-------------------------
const taskBar = document.getElementById("task-bar");
const taskList = document.getElementById("task-list");
const taskProgress = document.getElementById("task-progress");
const TASK_MARK = { pending: "⬜", in_progress: "🔄", delegated: "🤖", completed: "✅" };

function renderTaskBar() {
  const v = activeView();
  const tasks = (v && v.tasks) || [];
  if (!tasks.length) { taskBar.hidden = true; return; }
  taskBar.hidden = false;
  taskProgress.textContent = computeTaskProgress(tasks).text;
  taskBar.classList.toggle("collapsed", localStorage.getItem("tasksCollapsed") === "1");
  taskList.innerHTML = "";
  tasks.forEach((t) => {
    const li = document.createElement("li");
    li.className = "task-item " + (t.status || "pending");
    const mark = document.createElement("span");
    mark.className = "task-mark";
    mark.textContent = TASK_MARK[t.status] || "⬜";
    const text = document.createElement("span");
    text.className = "task-text";
    text.textContent = t.content;
    li.appendChild(mark);
    li.appendChild(text);
    taskList.appendChild(li);
  });
}

async function refreshTasks() {
  if (!window.pywebview) return;
  try {
    const r = await window.pywebview.api.get_tasks();
    const v = activeView();
    if (v && r && r.cid === v.cid) { v.tasks = r.tasks || []; renderTaskBar(); }
  } catch (e) { /* ignore */ }
}

// 智能粘底（对标主流）：只在用户已在底部时才跟随流式输出；往上翻看历史就不强制拽回
let stickBottom = true;
const scrollBtn = document.getElementById("scroll-bottom-btn");
function atBottom() { return chat.scrollHeight - chat.scrollTop - chat.clientHeight < 80; }
function updateScrollBtn() { if (scrollBtn) scrollBtn.classList.toggle("show", !atBottom()); }
chat.addEventListener("scroll", () => { stickBottom = atBottom(); updateScrollBtn(); });
function scrollChat() { if (stickBottom) chat.scrollTop = chat.scrollHeight; }
function scrollChatForce() { stickBottom = true; chat.scrollTop = chat.scrollHeight; }  // 主动发送/切换：强制到底+恢复粘底
function scrollView(v) { if (isActive(v)) { scrollChat(); updateScrollBtn(); } }  // 内容增长时同步「回到底部」按钮显隐
if (scrollBtn) scrollBtn.addEventListener("click", () => { scrollChatForce(); updateScrollBtn(); });

// 非活动对话来了新内容：标记未读 + 更新会话行
function markActivity(v) {
  if (!isActive(v)) { v.unread = true; updateSessionRow(v); }
}

// Mermaid 懒加载：mermaid.min.js 约 3MB，多数消息用不到，故不在启动时加载，
// 仅当真的出现 ```mermaid 代码块时才动态注入并初始化（只做一次）。
let _mermaidReady = null;
function ensureMermaid() {
  if (_mermaidReady) return _mermaidReady;
  _mermaidReady = new Promise((resolve) => {
    if (window.mermaid) { resolve(window.mermaid); return; }
    const s = document.createElement("script");
    s.src = "vendor/mermaid.min.js";
    s.onload = () => {
      try {
        window.mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict",
          suppressErrorRendering: true });  // 语法错时别把"报错炸弹图"注入页面破坏布局（bug 修复）
      } catch (e) { /* 初始化失败也不致命 */ }
      resolve(window.mermaid || null);
    };
    s.onerror = () => resolve(null); // 加载失败 -> 降级为保留代码块
    document.head.appendChild(s);
  });
  return _mermaidReady;
}

// ---- markdown 渲染（带离线降级） ---------------------------------------
function renderMarkdown(el, text) {
  if (window.marked) {
    try {
      el.innerHTML = window.marked.parse(text);   // 畸形/流式半截 markdown 抛错也不破坏气泡（降级纯文本）
    } catch (e) {
      el.textContent = text;
      return;
    }
    if (window.hljs) {
      el.querySelectorAll("pre code").forEach((b) => {
        if (b.classList.contains("language-mermaid")) return; // mermaid 交给 renderMermaidIn
        try { window.hljs.highlightElement(b); } catch (e) { /* 单块高亮失败不影响其它块/整体渲染 */ }
      });
    }
    // GFM 任务项打 class（替代 CSS :has，避免选择器在某些 WebView 上的行为差异）
    el.querySelectorAll('li > input[type="checkbox"]').forEach((cb) => {
      cb.parentElement.classList.add("task-list-item");
    });
    // 宽表格横向滚动：用外层 div 包一层（保持 table 正常 display，避免 display:block 触发 WebView2 异常重排/滚动塌陷）
    el.querySelectorAll("table").forEach((t) => {
      if (t.parentElement && t.parentElement.classList.contains("table-wrap")) return;
      const w = document.createElement("div");
      w.className = "table-wrap";
      t.replaceWith(w);
      w.appendChild(t);
    });
  } else {
    el.textContent = text; // 离线：纯文本
  }
}

// ---- 复制 / 图片预览（对标主流 agent 的易用性） ------------------------
const COPY_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const QUOTE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 14 4 9 9 4"/><path d="M20 20v-7a4 4 0 0 0-4-4H4"/></svg>';

async function copyText(text, btn) {
  text = text || "";
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; }
  catch (e) {
    try {  // WebView2 偶发拒绝 clipboard API 时降级
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      ok = document.execCommand("copy"); ta.remove();
    } catch (_) {}
  }
  if (btn) flashCopied(btn, ok);
}

function flashCopied(btn, ok) {
  const span = btn.querySelector("span");
  const done = ok === false ? "复制失败" : "已复制";
  if (span) {
    const prev = span.textContent;
    span.textContent = done; btn.classList.add("copied");
    setTimeout(() => { span.textContent = prev; btn.classList.remove("copied"); }, 1200);
  } else {
    const prev = btn.textContent;
    btn.textContent = done; btn.classList.add("copied");
    setTimeout(() => { btn.textContent = prev; btn.classList.remove("copied"); }, 1200);
  }
}

// 给代码块加「复制」按钮（mermaid 是图，跳过）
function addCodeCopy(pre) {
  if (pre.querySelector(".code-copy")) return;
  if (pre.querySelector("code.language-mermaid")) return;
  const btn = document.createElement("button");
  btn.className = "code-copy"; btn.type = "button"; btn.textContent = "复制";
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const code = pre.querySelector("code");
    copyText(code ? code.innerText : pre.innerText, btn);
  });
  pre.classList.add("has-copy");
  pre.appendChild(btn);
}

// 定稿后的 assistant 气泡：挂「复制整条」动作 + 代码块复制按钮（只挂一次）
function decorateAssistant(bubble, raw) {
  if (!bubble || bubble.dataset.decorated) return;
  bubble.dataset.decorated = "1";
  if (raw != null) bubble.dataset.raw = raw;
  bubble.querySelectorAll("pre").forEach(addCodeCopy);
  const act = document.createElement("div");
  act.className = "msg-actions";
  const cp = document.createElement("button");
  cp.className = "msg-copy"; cp.type = "button"; cp.title = "复制整条回答";
  cp.innerHTML = COPY_ICON + "<span>复制</span>";
  cp.addEventListener("click", () => copyText(bubble.dataset.raw || bubble.innerText, cp));
  act.appendChild(cp);
  const rg = document.createElement("button");
  rg.className = "msg-copy msg-regen"; rg.type = "button"; rg.title = "重新生成（丢弃此回答及其后对话）";
  rg.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/></svg><span>重新生成</span>';
  rg.addEventListener("click", () => doRegenerate(bubble.closest(".msg")));
  act.appendChild(rg);
  const qt = document.createElement("button");
  qt.className = "msg-copy msg-quote"; qt.type = "button"; qt.title = "引用这条回答到输入框";
  qt.innerHTML = QUOTE_ICON + "<span>引用</span>";
  qt.addEventListener("click", () => quoteToComposer(bubble.dataset.raw || bubble.innerText));
  act.appendChild(qt);
  bubble.appendChild(act);
}

// 引用回复（P4）：把文本以 Markdown 引用块填进输入框（已有内容则追加），聚焦续写
function quoteToComposer(text) {
  const q = formatQuote(text);
  if (!q) return;
  const cur = input.value;
  input.value = cur ? (cur.replace(/\s*$/, "") + "\n\n" + q) : q;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  autoResize();
}

// 图片灯箱：点击放大 + 下载到本地
function openLightbox(src) {
  let lb = document.getElementById("lightbox");
  if (!lb) {
    lb = document.createElement("div");
    lb.id = "lightbox"; lb.className = "lightbox";
    lb.innerHTML =
      '<div class="lb-bar">' +
      '<button class="lb-dl" type="button" title="下载到本地">下载</button>' +
      '<button class="lb-close" type="button" title="关闭">✕</button></div>' +
      '<img class="lb-img" alt="预览">';
    lb.addEventListener("click", (e) => {
      if (e.target === lb || e.target.closest(".lb-close")) lb.classList.remove("show");
    });
    lb.querySelector(".lb-dl").addEventListener("click", () => downloadImage(lb.querySelector(".lb-img").src));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && lb.classList.contains("show")) lb.classList.remove("show");
    });
    document.body.appendChild(lb);
  }
  lb.querySelector(".lb-img").src = src;
  lb.classList.add("show");
}

function downloadImage(src) {
  const ext = ((src || "").match(/^data:image\/([a-zA-Z0-9]+)/) || [, "png"])[1];
  const a = document.createElement("a");
  a.href = src; a.download = "hermes-image-" + Date.now() + "." + ext;
  document.body.appendChild(a); a.click(); a.remove();
}

// 让图片可点开预览（统一入口；带 .zoomable 的 img 走灯箱）
document.addEventListener("click", (e) => {
  const img = e.target.closest && e.target.closest("img.zoomable");
  if (img) openLightbox(img.src);
});

function downloadText(filename, text) {
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

// ---- 导出当前对话为 Markdown（从已渲染的气泡重建，assistant 用 dataset.raw 原文）----
function activeSessionTitle() {
  const t = document.querySelector(".session-item.active .session-title");
  const label = (t && t.textContent.trim()) || (document.getElementById("app-title") || {}).textContent || "对话";
  return label.replace(/\s+/g, " ").trim() || "对话";
}

function buildExportMarkdown(v, titleOverride) {
  const title = titleOverride || activeSessionTitle();
  const out = [`# ${title}`, "", `> 导出时间：${new Date().toLocaleString()}`, ""];
  v.el.querySelectorAll(".msg").forEach((msg) => {
    if (msg.classList.contains("sent-atts")) {
      const imgs = msg.querySelectorAll("img").length;
      const docs = Array.from(msg.querySelectorAll(".sent-doc")).map((d) => d.textContent.trim());
      const parts = [];
      if (imgs) parts.push(`${imgs} 张图片`);
      docs.forEach((d) => parts.push(d));
      if (parts.length) out.push(`> 📎 附件：${parts.join("、")}`, "");
      return;
    }
    if (msg.classList.contains("working")) return;
    const bubble = msg.querySelector(":scope > .bubble");
    if (!bubble) return;                       // 工具块/确认条没有 .bubble，跳过
    if (msg.classList.contains("user")) {
      const text = bubble.textContent.trim();
      if (text) out.push("## 你", "", text, "");
    } else if (msg.classList.contains("assistant")) {
      const raw = bubble.dataset.raw;          // 只有真正的回答气泡有 raw（通知/错误没有）
      if (raw == null) return;
      const text = raw.trim();
      if (text) out.push("## AI", "", text, "");
    }
  });
  return out.join("\n").replace(/\n{3,}/g, "\n\n") + "\n";
}

async function exportConversation(titleOverride) {
  const v = activeView();
  if (!v) return;
  const title = titleOverride || activeSessionTitle();
  const md = buildExportMarkdown(v, title);
  const safe = title.replace(/[\\/:*?"<>|]/g, "_").slice(0, 40);
  const filename = `${safe}-${Date.now()}.md`;
  // 优先用系统「保存为」对话框（让用户自己选位置、并明确告知存到哪）；不可用再回退浏览器下载
  try {
    const res = await window.pywebview.api.export_markdown(filename, md);
    if (res && res.ok) { showToast("📄 已保存：" + res.path); return; }
    if (res && res.cancelled) return;  // 用户取消，不回退、不提示
  } catch (e) { /* 没有 pywebview（如浏览器预览）：走下载 */ }
  downloadText(filename, md);
  showToast("📄 已导出到「下载」文件夹：" + filename);
}

// 导出入口已移到左侧会话行（每会话一个 ⬇ 按钮，见 makeSessionItem）；exportConversation 复用。

// ---- 会话内查找（Ctrl+F）：高亮匹配 + 上/下定位 -----------------------
const findBar = document.getElementById("find-bar");
const findInput = document.getElementById("find-input");
const findCount = document.getElementById("find-count");
let findHits = [];
let findIdx = -1;

function clearFindMarks() {
  const v = activeView();
  if (v) v.el.querySelectorAll("mark.find-hit").forEach((m) => {
    const t = document.createTextNode(m.textContent);
    m.replaceWith(t);
  });
  // 合并被拆开的文本节点
  if (v) v.el.normalize();
  findHits = []; findIdx = -1;
}

function runFind(q) {
  clearFindMarks();
  const v = activeView();
  if (!v || !q) { updateFindCount(); return; }
  const needle = q.toLowerCase();
  const walker = document.createTreeWalker(v.el, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue || !node.nodeValue.toLowerCase().includes(needle)) return NodeFilter.FILTER_REJECT;
      const p = node.parentElement;
      if (!p || p.closest("script,style,mark.find-hit")) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const targets = [];
  let n;
  while ((n = walker.nextNode())) targets.push(n);
  targets.forEach((node) => {
    const text = node.nodeValue;
    const low = text.toLowerCase();
    const frag = document.createDocumentFragment();
    let i = 0, pos;
    while ((pos = low.indexOf(needle, i)) !== -1) {
      if (pos > i) frag.appendChild(document.createTextNode(text.slice(i, pos)));
      const mark = document.createElement("mark");
      mark.className = "find-hit";
      mark.textContent = text.slice(pos, pos + needle.length);
      frag.appendChild(mark);
      findHits.push(mark);
      i = pos + needle.length;
    }
    if (i < text.length) frag.appendChild(document.createTextNode(text.slice(i)));
    node.parentNode.replaceChild(frag, node);
  });
  if (findHits.length) gotoFindHit(0);
  updateFindCount();
}

function gotoFindHit(idx) {
  if (!findHits.length) return;
  if (findHits[findIdx]) findHits[findIdx].classList.remove("cur");
  findIdx = (idx + findHits.length) % findHits.length;
  const cur = findHits[findIdx];
  cur.classList.add("cur");
  cur.scrollIntoView({ block: "center", behavior: "smooth" });
  updateFindCount();
}

function updateFindCount() {
  if (findCount) findCount.textContent = `${findHits.length ? findIdx + 1 : 0}/${findHits.length}`;
}

function openFind() {
  if (!findBar) return;
  findBar.hidden = false;
  findInput.focus(); findInput.select();
  if (findInput.value) runFind(findInput.value.trim());
}
function closeFind() {
  if (!findBar) return;
  clearFindMarks(); updateFindCount();
  findBar.hidden = true;
}

if (findInput) {
  findInput.addEventListener("input", () => runFind(findInput.value.trim()));
  findInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); gotoFindHit(findIdx + (e.shiftKey ? -1 : 1)); }
    else if (e.key === "Escape") { e.preventDefault(); closeFind(); }
  });
  document.getElementById("find-next").addEventListener("click", () => gotoFindHit(findIdx + 1));
  document.getElementById("find-prev").addEventListener("click", () => gotoFindHit(findIdx - 1));
  document.getElementById("find-close").addEventListener("click", closeFind);
}

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && (e.key === "f" || e.key === "F")) {
    e.preventDefault();
    openFind();
  } else if (e.key === "Escape" && findBar && !findBar.hidden) {
    e.preventDefault();
    closeFind();
  }
});

// ---- 重新生成 / 编辑重发（覆盖式：丢弃目标轮次之后的对话，后端截断重跑）-------
// 删除某锚点之后（含/不含锚点本身）的所有 .msg / 工具行，让重跑的流式内容接着追加
function removeAfter(anchor, includeAnchor) {
  let n = includeAnchor ? anchor : anchor.nextSibling;
  while (n) { const next = n.nextSibling; n.remove(); n = next; }
}
// 目标轮次之后是否还有更晚的用户轮次（有则提醒会丢弃后续对话）
function hasLaterTurns(v, turn) {
  return Array.from(v.el.children).some(
    (c) => c.classList && c.classList.contains("msg") && c.classList.contains("user")
      && Number(c.dataset.turn) > turn);
}
// 重跑前的公共收尾：断开流式引用、清待发指示器
function resetStreamRefs(v) {
  v.currentBubble = null; v.currentText = "";
  v.thinkingEl = null; v.thinkingText = ""; v.toolBlocks = {}; v.subBlocks = {};
  hideWorking(v);
}

async function doRegenerate(msgEl) {
  if (!msgEl || !window.pywebview) return;
  const v = activeView();
  if (isActive(v) && v.status !== "idle" && v.status !== "error") {
    showToast("⚠ 运行中，先停止再重新生成"); return;
  }
  const turn = Number(msgEl.dataset.turn);
  if (hasLaterTurns(v, turn) && !confirm("重新生成会丢弃这条回答之后的所有对话，确定？")) return;
  // 找到该轮次的用户气泡作锚点，保留它、删掉其后全部
  const userEl = Array.from(v.el.children).find(
    (c) => c.classList && c.classList.contains("msg") && c.classList.contains("user")
      && Number(c.dataset.turn) === turn);
  const anchor = userEl || msgEl;
  removeAfter(anchor, false);
  v.userTurns = turn + 1;
  resetStreamRefs(v);
  rebuildChatIndex();
  const r = await window.pywebview.api.regenerate(turn);
  if (r && !r.ok) showToast("⚠ " + (r.error || "重新生成失败"));
}

function enterEditMode(v, wrap, bubble) {
  if (wrap.querySelector(".edit-box")) return;       // 已在编辑
  const turn = Number(wrap.dataset.turn);
  const orig = bubble.textContent;
  bubble.style.display = "none";
  const box = document.createElement("div");
  box.className = "edit-box";
  const ta = document.createElement("textarea");
  ta.className = "edit-ta"; ta.value = orig;
  const bar = document.createElement("div");
  bar.className = "edit-bar";
  const save = document.createElement("button");
  save.className = "edit-save"; save.textContent = "保存并重发";
  const cancel = document.createElement("button");
  cancel.className = "edit-cancel"; cancel.textContent = "取消";
  bar.append(save, cancel);
  box.append(ta, bar);
  wrap.insertBefore(box, bubble.nextSibling);
  ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
  const exit = () => { box.remove(); bubble.style.display = ""; };
  cancel.addEventListener("click", exit);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); exit(); }
    else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save.click(); }
  });
  save.addEventListener("click", () => saveEdit(v, wrap, turn, ta.value, exit));
}

async function saveEdit(v, wrap, turn, text, exit) {
  text = (text || "").trim();
  if (!text) { showToast("⚠ 内容为空"); return; }
  if (isActive(v) && v.status !== "idle" && v.status !== "error") {
    showToast("⚠ 运行中，先停止再编辑"); return;
  }
  if (hasLaterTurns(v, turn) && !confirm("编辑会丢弃这条消息之后的所有对话，确定？")) return;
  exit();
  removeAfter(wrap, true);           // 删掉旧的用户气泡及其后全部
  v.userTurns = turn;                // 让新加的用户气泡拿回同一个轮次号
  addMessage(v, "user", text);
  resetStreamRefs(v);
  rebuildChatIndex();
  const r = await window.pywebview.api.edit_and_resend(turn, text);
  if (r && !r.ok) showToast("⚠ " + (r.error || "编辑重发失败"));
}

// 把 ```mermaid 代码块渲染成图。流式途中不调用（图未写完会报错），只在气泡定稿/
// 加载历史时调用；失败则保留原始代码块，不破坏对话。SVG 由 marked 直接当 HTML 渲染，无需在此处理。
async function renderMermaidIn(el) {
  if (!el) return;
  const blocks = el.querySelectorAll("code.language-mermaid");
  if (!blocks.length) return;            // 没有 mermaid 块就不碰它（避免无谓加载）
  const mermaid = await ensureMermaid(); // 首次遇到才动态加载
  if (!mermaid) return;                  // 加载失败 -> 保留原始代码块
  for (const code of blocks) {
    const src = code.textContent || "";
    const pre = code.closest("pre") || code;
    const id = "mmd-" + Math.random().toString(36).slice(2);
    try {
      // 先校验语法：suppressErrors 不抛异常、也不会往 body 注入"报错炸弹图"（10.9.1 render() 失败的副作用，
      // 会飘在页面顶部破坏布局）。不合法就保留原始代码块、跳过，不污染 GUI。
      const ok = await window.mermaid.parse(src, { suppressErrors: true });
      if (!ok) continue;
      const { svg } = await window.mermaid.render(id, src);
      const host = document.createElement("div");
      host.className = "mermaid-diagram";
      host.innerHTML = svg;
      pre.replaceWith(host);
    } catch (e) {
      /* 图未写完 / 语法错：保留原始代码块 */
    } finally {
      // 兜底：清掉 mermaid 渲染过程可能遗留在 body 的临时/报错元素（防炸弹图残留）
      document.getElementById("d" + id)?.remove();
      const orphan = document.getElementById(id);
      if (orphan && orphan.parentElement === document.body) orphan.remove();
    }
  }
  scrollChat();
}

// ---- 消息气泡 ----------------------------------------------------------
function addMessage(v, role, text) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;

  // 轮次标记：每条用户消息占一个轮次序号；assistant 归属当前（最后）轮次
  if (role === "user") { wrap.dataset.turn = String(v.userTurns); v.userTurns++; }
  else { wrap.dataset.turn = String(Math.max(0, v.userTurns - 1)); }

  const roleEl = document.createElement("div");
  roleEl.className = "role";
  roleEl.textContent = role === "user" ? "你" : "AI";

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (role === "user") {
    bubble.textContent = text;
    addUserEditBtn(v, wrap, bubble);
  } else {
    renderMarkdown(bubble, text);
  }

  wrap.appendChild(roleEl);
  wrap.appendChild(bubble);
  v.el.appendChild(wrap);
  scrollView(v);
  return bubble;
}

// 用户消息的「编辑」按钮（悬停出现，点开就地改文本 + 重发）
function addUserEditBtn(v, wrap, bubble) {
  const btn = document.createElement("button");
  btn.className = "msg-edit"; btn.type = "button"; btn.title = "编辑并重发";
  btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
  btn.addEventListener("click", () => enterEditMode(v, wrap, bubble));
  wrap.appendChild(btn);
  // 引用按钮（悬停出现，在编辑按钮左侧）：把这条用户消息引用到输入框
  const qbtn = document.createElement("button");
  qbtn.className = "msg-edit msg-uquote"; qbtn.type = "button"; qbtn.title = "引用这条到输入框";
  qbtn.innerHTML = QUOTE_ICON;
  qbtn.addEventListener("click", () => quoteToComposer(bubble.textContent));
  wrap.appendChild(qbtn);
}

// 把一段裸 DOM（工具块/确认条）作为一行插进对话流
function appendRow(v, node) {
  const wrap = document.createElement("div");
  wrap.className = "msg assistant";
  const spacer = document.createElement("div");
  spacer.className = "role";
  spacer.textContent = "🔧";
  wrap.appendChild(spacer);
  wrap.appendChild(node);
  v.el.appendChild(wrap);
  scrollView(v);
}

// ---- 工具调用展示 ------------------------------------------------------
// summarize() / escapeHtml() 等纯逻辑已抽到 pure.js（浏览器里先加载、挂为全局；tests/web 可单测）。

function renderToolUse(v, data) {
  if (data.name === "delegate") return;  // 委派由专门的子任务块展示，不再出通用工具块
  finalizeTextBubble(v);
  const box = document.createElement("details");
  box.className = "tool-block";
  box.open = false;

  const summary = document.createElement("summary");
  summary.innerHTML = `<span class="tool-name">${data.name}</span>` +
    `<span class="tool-args">${escapeHtml(summarize(data.input))}</span>` +
    `<span class="tool-status running">运行中…</span>`;
  box.appendChild(summary);

  const result = document.createElement("pre");
  result.className = "tool-result";
  result.textContent = "";
  box.appendChild(result);

  v.toolBlocks[data.id] = box;
  appendRow(v, box);
}

function renderToolResult(v, data) {
  if (data.name === "delegate") return;  // 同上，委派结果在子任务块里看
  const box = v.toolBlocks[data.id];
  if (!box) return;
  const status = box.querySelector(".tool-status");
  status.textContent = data.ok ? "完成" : "失败";
  status.className = "tool-status " + (data.ok ? "ok" : "fail");
  const result = box.querySelector(".tool-result");
  result.textContent = "";
  const out = data.output || "(无输出)";
  const fold = foldToolOutput(out);
  const txt = document.createElement("span");
  txt.className = "tr-text";
  txt.textContent = fold.folded ? fold.preview + "\n…" : out;
  result.appendChild(txt);
  if (fold.folded) {
    // 超长输出默认折叠，给「展开/收起」开关（避免一条工具结果刷屏）。
    // 开关放在结果框之外（作为 box 的子节点），不会被结果框的滚动区盖住。
    let expanded = false;
    const btn = document.createElement("button");
    btn.className = "tr-fold-btn";
    const relabel = () => {
      btn.textContent = expanded ? "收起 ▴"
        : (fold.hidden > 0 ? `展开剩余 ${fold.hidden} 行 ▾` : `展开全部（共 ${fold.total} 行）▾`);
    };
    relabel();
    btn.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation();
      expanded = !expanded;
      txt.textContent = expanded ? fold.full : fold.preview + "\n…";
      result.classList.toggle("tr-expanded", expanded);
      relabel();
    });
    box.appendChild(btn);
  }
  if (!data.ok) result.classList.add("fail");
  if (data.eval) {
    // 块B 事实层：结构化评估摘要（测试通过数 / 命中数 / 退出码 + 问题）。
    // 纯展示——不参与任何决策（ADR 0014）。
    const ef = formatEval(data.eval);
    if (ef) {
      const chip = document.createElement("div");
      chip.className = "tr-eval " + ef.level;
      chip.textContent = ef.text;
      if (ef.score != null) chip.title = `评估分 ${ef.score}（仅展示，不参与决策）`;
      box.appendChild(chip);
    }
  }
  if (data.image) {
    // 截屏等返回图片的工具：在结果下方展示缩略图
    const img = document.createElement("img");
    img.className = "tool-image zoomable";
    img.title = "点击预览 / 下载";
    img.src = data.image;
    result.appendChild(document.createElement("br"));
    result.appendChild(img);
  }
  if (data.diff) {
    // 写/编辑工具：把本次改动的 diff 内联展示在对话流（对标 Claude Code）
    result.appendChild(renderDiffBlock(data.diff));
  }
  scrollView(v);
}

// 内联 diff 块：复用 .diff-line 配色（+绿 / -红 / @@ 蓝）
function renderDiffBlock(diff) {
  const pre = document.createElement("pre");
  pre.className = "tool-diff ws-diff";
  (diff.text || "").split("\n").forEach((line) => {
    const span = document.createElement("span");
    let cls = "diff-line";
    if (line.startsWith("+++") || line.startsWith("---")) cls += " meta";
    else if (line.startsWith("+")) cls += " add";
    else if (line.startsWith("-")) cls += " del";
    else if (line.startsWith("@@")) cls += " hunk";
    span.className = cls;
    span.textContent = line;
    pre.appendChild(span);
    pre.appendChild(document.createTextNode("\n"));
  });
  return pre;
}

// ---- 子任务块（FR-9.3，委派子 Agent 的实时折叠块）----------------------
const ROLE_LABELS = { researcher: "调研", reviewer: "评审", tester: "测试", general: "通用" };
function ensureSubBlock(v, id, task, roleLabel) {
  if (v.subBlocks[id]) return v.subBlocks[id];
  const block = document.createElement("div");
  block.className = "subagent-block";
  const details = document.createElement("details");
  details.className = "sub-details";
  details.open = true;
  const summary = document.createElement("summary");
  const head = (roleLabel && roleLabel !== "通用") ? `🤖 子任务 · ${roleLabel}` : "🤖 子任务";
  summary.innerHTML =
    `<span class="sub-head"></span>` +
    `<span class="sub-task"></span>` +
    `<span class="sub-status running">运行中…</span>`;
  summary.querySelector(".sub-head").textContent = head;
  summary.querySelector(".sub-task").textContent = task || "";
  const body = document.createElement("div");
  body.className = "sub-body";
  const activity = document.createElement("div");
  activity.className = "sub-activity";
  const stream = document.createElement("div");
  stream.className = "sub-stream bubble";
  body.appendChild(activity);
  body.appendChild(stream);
  details.appendChild(summary);
  details.appendChild(body);
  const summaryEl = document.createElement("div");
  summaryEl.className = "sub-summary";
  block.appendChild(details);
  block.appendChild(summaryEl);

  const wrap = document.createElement("div");
  wrap.className = "msg assistant";
  const spacer = document.createElement("div");
  spacer.className = "role";
  spacer.textContent = "🤖";
  wrap.appendChild(spacer);
  wrap.appendChild(block);
  v.el.appendChild(wrap);
  scrollView(v);

  const blk = { details, status: summary.querySelector(".sub-status"),
                activity, stream, streamText: "", summaryEl };
  v.subBlocks[id] = blk;
  return blk;
}

function subActivityLine(blk, text, cls) {
  const line = document.createElement("div");
  line.className = "sub-line" + (cls ? " " + cls : "");
  line.textContent = text;
  blk.activity.appendChild(line);
}

function handleSubEvent(v, id, event, data) {
  const blk = v.subBlocks[id] || ensureSubBlock(v, id, "");
  if (event === EV.CHUNK) {
    blk.streamText += data;
    renderMarkdown(blk.stream, blk.streamText);
  } else if (event === EV.TOOL_USE) {
    subActivityLine(blk, `🔧 ${data.name} ${summarize(data.input)}`);
  } else if (event === EV.TOOL_RESULT) {
    subActivityLine(blk, `↳ ${data.ok ? "完成" : "失败"}`, data.ok ? "ok" : "fail");
  } else if (event === EV.ERROR) {
    subActivityLine(blk, "⚠ " + data, "fail");
  }
  // thinking 等忽略，保持子块简洁
  scrollView(v);
}

function finishSubBlock(v, id, ok, summary) {
  const blk = v.subBlocks[id] || ensureSubBlock(v, id, "");
  blk.status.textContent = ok ? "✅ 完成" : "⚠ 失败";
  blk.status.className = "sub-status " + (ok ? "ok" : "fail");
  blk.details.open = false;           // 收起实时过程，留下摘要
  renderMarkdown(blk.summaryEl, summary || "");
  scrollView(v);
}

// ---- 视觉预处理提示 ----------------------------------------------------
function renderVisionStart(v, data) {
  finalizeTextBubble(v);
  const box = document.createElement("details");
  box.className = "tool-block vision-block";
  const summary = document.createElement("summary");
  summary.innerHTML =
    `<span class="tool-name">🔍 视觉预处理</span>` +
    `<span class="tool-args">主模型不支持图像，正在将 ${data.count} 张图转为文字描述…</span>` +
    `<span class="tool-status running">识别中…</span>`;
  box.appendChild(summary);
  const result = document.createElement("pre");
  result.className = "tool-result";
  box.appendChild(result);
  v.visionBlock = box;
  appendRow(v, box);
}

function renderVisionDone(v, data) {
  if (!v.visionBlock) return;
  const status = v.visionBlock.querySelector(".tool-status");
  status.textContent = data.ok ? "已转文字" : "失败";
  status.className = "tool-status " + (data.ok ? "ok" : "fail");
  const result = v.visionBlock.querySelector(".tool-result");
  if (data.ok) {
    result.textContent = data.summary || "(已生成描述)";
  } else {
    result.textContent = data.error || "(识别失败)";
    result.classList.add("fail");
  }
  v.visionBlock = null;
  scrollView(v);
}

// 上下文超预算被压缩时的提示（P6.2）：历史完整保存在库里，仅本次喂模型时裁剪。
function renderContextCompressed(v, data) {
  finalizeTextBubble(v);
  const note = document.createElement("div");
  note.className = "context-note";
  const how = data.dropped > 0
    ? (data.summary === "model" ? "（已压成模型生成的摘要）" : "（已压成摘要）") : "";
  note.textContent =
    `🗜 上下文较长，已压缩后再发送：省略较早 ${data.dropped} 条消息${how}` +
    `（约 ${data.before} → ${data.after} tokens，预算 ${data.budget}）。完整历史仍保存在会话中。`;
  appendRow(v, note);
}

// 本轮用量（FR-11.8）：tokens 入/出、缓存命中、步数。克制地显示在回合末。
function renderUsage(v, data) {
  finalizeTextBubble(v);
  const note = document.createElement("div");
  note.className = "usage-note";
  const cache = data.cache_read ? `，缓存命中 ${data.cache_read}` : "";
  note.textContent =
    `📊 本轮：输入 ${data.input} / 输出 ${data.output} tokens${cache}` +
    `，${data.steps} 步`;
  appendRow(v, note);
  // 累计进会话用量，并刷新顶部累计芯片（P2）
  v.usage = accumulateUsage(v.usage, data);
  if (isActive(v)) updateUsageChip();
}

// 顶部「会话累计用量」芯片：当前会话累计 token + 按公开价粗估的成本。切会话时刷新。
function updateUsageChip() {
  const chip = document.getElementById("usage-chip");
  if (!chip) return;
  const v = activeView();
  const u = v && v.usage;
  if (!u || !u.turns) { chip.hidden = true; return; }
  chip.hidden = false;
  const total = u.input + u.output + u.cacheRead;
  const cost = estimateCostUsd(modelSelect.value, u);
  const costTxt = cost != null ? `　≈ $${cost < 0.01 ? cost.toFixed(4) : cost.toFixed(2)}` : "";
  chip.textContent = `Σ ${fmtK(total)} tok${costTxt}`;
  chip.title =
    `本会话累计（本次打开以来，共 ${u.turns} 轮）\n` +
    `输入 ${u.input} / 输出 ${u.output} / 缓存 ${u.cacheRead} tokens` +
    (cost != null ? `\n成本 ≈ $${cost.toFixed(4)}（按公开列表价粗估，仅供参考）` : "\n（当前模型无价目，仅统计 token）");
}

// 数字缩写：1234 -> 1.2k，1200000 -> 1.2M（累计芯片用，省地方）
function fmtK(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}

// ---- 长期记忆抽取提示（P6.3） ------------------------------------------
// 抽取发生在「离开会话」之后，用浮层 toast（全局，不属于某个对话视图）。
function renderMemoryCaptured(data) {
  const items = (data.items || []).map((m) => `「${m.content}」`).join("、");
  showToast(`🧠 已记入长期记忆 ${data.count} 条：${items}`);
}

let toastTimer = null;

function showToast(text) {
  let el = document.getElementById("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 6000);
}

// ---- 权限确认条 --------------------------------------------------------
function renderPermission(v, req) {
  const bar = document.createElement("div");
  bar.className = "perm-bar bubble";
  bar.innerHTML =
    `<div class="perm-text">⚠ 请求执行危险操作：<b>${escapeHtml(req.tool)}</b>` +
    `<code>${escapeHtml(summarize(req.params))}</code></div>`;

  const actions = document.createElement("div");
  actions.className = "perm-actions";
  const mk = (label, decision, cls) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.className = cls;
    b.addEventListener("click", async () => {
      bar.querySelectorAll("button").forEach((x) => (x.disabled = true));
      bar.querySelector(".perm-text").insertAdjacentText("beforeend", `  → ${label}`);
      await window.pywebview.api.resolve_permission(req.id, decision, v.cid);
    });
    return b;
  };
  actions.appendChild(mk("允许", "allow", "perm-allow"));
  actions.appendChild(mk("拒绝", "deny", "perm-deny"));
  // 细粒度「记住此类」（FR-11.4）：本会话后续匹配该规则的同类操作免确认
  if (req.suggest) {
    const b = mk(`总是允许 ${req.suggest}`, "allow_rule", "perm-rule");
    b.title = "本会话内，匹配此规则的同类操作不再询问";
    actions.appendChild(b);
  }
  actions.appendChild(mk("本会话全部允许", "allow_all", "perm-all"));
  bar.appendChild(actions);
  appendRow(v, bar);
}

// ask_user：agent 请你拍板细节，给选项勾选 +「其他」自行补充（对标 Claude Code AskUserQuestion）
function renderAskUser(v, req) {
  const bar = document.createElement("div");
  bar.className = "ask-bar bubble";
  bar.innerHTML = `<div class="ask-q">🤔 ${escapeHtml(req.question)}</div>`;
  const done = (answer) => {
    bar.querySelectorAll("button,input").forEach((x) => (x.disabled = true));
    bar.querySelector(".ask-q").insertAdjacentText("beforeend", `  → ${answer}`);
    window.pywebview.api.resolve_ask_user(req.id, answer, v.cid);
  };
  const opts = document.createElement("div");
  opts.className = "ask-options";
  (req.options || []).forEach((opt, i) => {
    const b = document.createElement("button");
    b.className = "ask-opt";
    b.textContent = `${i + 1}. ${opt}`;
    b.addEventListener("click", () => done(opt));
    opts.appendChild(b);
  });
  const other = document.createElement("div");
  other.className = "ask-other";
  const inp = document.createElement("input");
  inp.type = "text";
  inp.placeholder = "其他（自己补充…）";
  const submit = () => { const t = inp.value.trim(); if (t) done(t); };
  const send = document.createElement("button");
  send.textContent = "提交";
  send.addEventListener("click", submit);
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
  other.appendChild(inp);
  other.appendChild(send);
  bar.appendChild(opts);
  bar.appendChild(other);
  appendRow(v, bar);
}

// ---- 接收来自 Python 的流式事件（按 cid 路由到对应对话视图） ------------
window.__onAgentEvent = function (msg) {
  const { event, data, cid } = msg;
  // 全局事件（无 cid）：浏览器穿透安装进度 / 完成
  if (event === "browser_mcp_progress") { onBrowserProgress(data); return; }
  if (event === "browser_mcp_done") { onBrowserDone(data); return; }
  if (event === "smart_default") {  // 情境自启②：自动开了某行为，告知用户一句（可在面板覆盖）
    if (data && data.message) showToast(data.message);
    return;
  }
  const v = getView(cid);
  if (event === EV.CHUNK) {
    hideWorking(v);
    finalizeThinking(v);
    if (!v.currentBubble) {
      v.currentBubble = addMessage(v, "assistant", "");
      v.currentText = "";
    }
    v.currentText += data;
    renderMarkdown(v.currentBubble, v.currentText);
    v.currentBubble.classList.add("cursor");
    scrollView(v);
    markActivity(v);
  } else if (event === EV.THINKING) {
    hideWorking(v);
    renderThinking(v, data);
    markActivity(v);
  } else if (event === EV.TOOL_USE) {
    hideWorking(v);
    finalizeThinking(v);
    renderToolUse(v, data);
    markActivity(v);
  } else if (event === EV.TOOL_RESULT) {
    renderToolResult(v, data);
    if (v.streaming) showWorking(v, "思考中…"); // 工具结束、下一轮模型思考前再给反馈
  } else if (event === EV.PERMISSION) {
    hideWorking(v);
    renderPermission(v, data);
    markActivity(v);
  } else if (event === EV.ASK_USER) {
    hideWorking(v);
    renderAskUser(v, data);
    markActivity(v);
  } else if (event === EV.CRAZY_START) {
    v.crazyRunning = true;
    if (isActive(v)) updateComposerButtons();   // 自主模式运行中：显示「停止」
    markActivity(v);
  } else if (event === EV.CRAZY_ROUND) {
    addSysLine(v, `🤖 自主模式 · 第 ${data.round}/${data.max} 轮…`);
    markActivity(v);
  } else if (event === EV.CRAZY_REPLAN) {
    addSysLine(v, "🤖 阶段完成 · 按本阶段所学重规划剩余阶段…");
    markActivity(v);
  } else if (event === EV.CRAZY_DONE) {
    v.crazyRunning = false;
    if (isActive(v)) updateComposerButtons();   // 自主模式结束：隐藏「停止」
    const reasons = {
      goal_reached: "✅ 目标达成", stopped: "⏹ 已停止",
      stalled: "⚠ 疑似空转已中止", time_budget: "⏳ 用尽时间预算",
      token_budget: "⏳ 用尽 token 预算", budget_exhausted: "⏳ 用尽轮数预算（可再 /crazy 续）",
    };
    addSysLine(v, `🤖 自主模式结束（共 ${data.round} 轮）：${reasons[data.reason] || data.reason}`);
    markActivity(v);
  } else if (event === EV.VISION_START) {
    hideWorking(v);
    renderVisionStart(v, data);
  } else if (event === EV.VISION_DONE) {
    renderVisionDone(v, data);
    if (v.streaming) showWorking(v, "思考中…");
  } else if (event === EV.CONTEXT_COMPRESSED) {
    renderContextCompressed(v, data);
    if (v.streaming) showWorking(v, "思考中…");
  } else if (event === EV.STATE) {
    v.status = data.state;
    if (isBusyState(data.state)) v.streaming = true;
    if (data.state === "awaiting" && !isActive(v)) {
      showToast("⚠ 后台对话需要权限确认，点开该会话处理");
    }
    updateSessionRow(v);
    if (isActive(v)) restoreInputState();
  } else if (event === EV.SESSION_CREATED) {
    v.sessionId = data.id;
    sessionIdToCid.set(data.id, cid);
    if (isActive(v)) activeSessionId = data.id;
    refreshSessions();
  } else if (event === EV.SUBAGENT_START) {
    hideWorking(v);
    finalizeThinking(v);
    finalizeTextBubble(v);
    ensureSubBlock(v, data.id, data.task, data.role_label);
    markActivity(v);
  } else if (event === EV.SUBAGENT_EVENT) {
    handleSubEvent(v, data.id, data.event, data.data);
    markActivity(v);
  } else if (event === EV.SUBAGENT_DONE) {
    finishSubBlock(v, data.id, data.ok, data.summary);
    if (v.streaming) showWorking(v, "思考中…"); // 子任务回灌后主 Agent 继续
    markActivity(v);
  } else if (event === EV.TASKS_UPDATED) {
    v.tasks = (data && data.tasks) || [];
    if (isActive(v)) renderTaskBar();
    else markActivity(v);
  } else if (event === EV.CHECKPOINT_CREATED) {
    // 自动打点（每回合改动前）静默刷新列表不弹 toast；手动存才提示
    if (isActive(v)) {
      if (!data.auto) showToast("📌 已存检查点：" + (data.label || ""));
      refreshCheckpoints();
    }
  } else if (event === EV.USAGE) {
    renderUsage(v, data);
  } else if (event === EV.STEP_WARNING) {
    if (isActive(v)) showToast(`⏳ 已用 ${data.steps}/${data.max_steps} 步，接近上限`);
  } else if (event === EV.AUTO_TEST) {
    if (isActive(v)) showToast(
      data.config_error ? `⚠ 测试命令没跑起来，检查 test_command 配置：${data.command}`
      : data.ok ? `✅ 自动测试通过（${data.command}）`
      : `❌ 自动测试未通过（${data.command}，第 ${data.iter}/${data.max} 次）` +
          (data.iter < data.max ? "，让模型修…" : ""));
  } else if (event === EV.ENQUEUED) {
    if (isActive(v)) showToast(
      data && data.steering
        ? "✏ 已追加，agent 会在下一步纳入并调整" +
            (data.pending > 1 ? `（待纳入 ${data.pending}）` : "")
        : "📨 已排队，当前任务完成后处理" +
            (data && data.pending > 1 ? `（待处理 ${data.pending}）` : ""));
  } else if (event === EV.MEMORY_CAPTURED) {
    renderMemoryCaptured(data);
  } else if (event === EV.WORKSPACE_CHANGED) {
    if (isActive(v)) { setTopTitle(data.label); refreshWorkspace(); }
  } else if (event === EV.CONVENTIONS_GENERATED) {
    if (isActive(v)) {
      showToast("🧭 已为本项目生成规范文件 " + (data.file || "hermes.md"));
      refreshWorkspace();
    }
  } else if (event === EV.DONE) {
    stopWorking(v);
    finalizeThinking(v);
    finishStreaming(v);
    v.status = "idle";
    if (!isActive(v)) v.unread = true;
    refreshSessions();
    if (isActive(v)) refreshWorkspace(); // Agent 可能写了文件，刷新工作区树
  } else if (event === EV.STOPPED) {
    stopWorking(v);
    finalizeThinking(v);
    if (v.currentBubble) v.currentBubble.classList.remove("cursor");
    addMessage(v, "assistant", "⏹ 已停止").classList.add("notice");
    finishStreaming(v);
    v.status = "idle";
    if (!isActive(v)) v.unread = true;
    refreshSessions();
    if (isActive(v)) refreshWorkspace();
  } else if (event === EV.ERROR) {
    stopWorking(v);
    finalizeThinking(v);
    if (v.currentBubble) {
      v.currentBubble.classList.remove("cursor");
      v.currentBubble.classList.add("error");
      v.currentBubble.textContent = "⚠ " + data;
    } else {
      addMessage(v, "assistant", "⚠ " + data).classList.add("error");
    }
    finishStreaming(v);
    v.status = "error";
    if (!isActive(v)) v.unread = true;
    refreshSessions();
  }
};

// ---- 工作指示器（T1：发送后立即反馈 + 已用时长；每对话视图各一份） ------
function startWorking(v) {
  v.workStart = Date.now();
  showWorking(v, "思考中…");
}

function showWorking(v, status) {
  if (!v.workingEl) {
    v.workingEl = document.createElement("div");
    v.workingEl.className = "msg assistant working";
    v.workingEl.innerHTML =
      '<div class="role">AI</div>' +
      '<div class="bubble work-bubble">' +
      '<span class="work-dots"><i></i><i></i><i></i></span>' +
      '<span class="work-status"></span>' +
      '<span class="work-time"></span></div>';
  }
  v.workingEl.querySelector(".work-status").textContent = status || "思考中…";
  v.el.appendChild(v.workingEl); // 始终移到底部
  if (!v.workTimer) v.workTimer = setInterval(() => updateWorkTime(v), 250);
  updateWorkTime(v);
  scrollView(v);
}

function updateWorkTime(v) {
  if (!v.workingEl) return;
  const s = ((Date.now() - v.workStart) / 1000).toFixed(0);
  const t = v.workingEl.querySelector(".work-time");
  if (t) t.textContent = "已用 " + s + "s";
}

function hideWorking(v) {
  if (v.workingEl && v.workingEl.parentNode) v.workingEl.parentNode.removeChild(v.workingEl);
}

function stopWorking(v) {
  hideWorking(v);
  if (v.workTimer) { clearInterval(v.workTimer); v.workTimer = null; }
}

// ---- 思考过程块（T2：模型推理实时流入淡色可折叠块） --------------------
function renderThinking(v, delta) {
  if (!v.thinkingEl) {
    v.thinkingText = "";
    v.thinkingEl = document.createElement("details");
    v.thinkingEl.className = "thinking-block";
    v.thinkingEl.open = true;
    const sum = document.createElement("summary");
    sum.textContent = "💭 思考过程";
    const body = document.createElement("div");
    body.className = "thinking-body";
    v.thinkingEl.appendChild(sum);
    v.thinkingEl.appendChild(body);
    appendRow(v, v.thinkingEl);
  }
  v.thinkingText += delta;
  v.thinkingEl.querySelector(".thinking-body").textContent = v.thinkingText;
  scrollView(v);
}

function finalizeThinking(v) {
  if (v.thinkingEl) v.thinkingEl.open = false; // 答案/工具到来后自动折叠
  v.thinkingEl = null;
  v.thinkingText = "";
}

// 工具调用前，把当前文本气泡定稿（停止光标、断开引用）
function finalizeTextBubble(v) {
  if (v.currentBubble) {
    v.currentBubble.classList.remove("cursor");
    decorateAssistant(v.currentBubble, v.currentText); // 定稿后挂复制按钮
    renderMermaidIn(v.currentBubble); // 气泡定稿后再渲染 mermaid 图
  }
  v.currentBubble = null;
  v.currentText = "";
}

function finishStreaming(v) {
  finalizeTextBubble(v);
  v.streaming = false;
  if (isActive(v)) { updateComposerButtons(); input.focus(); }
  updateSessionRow(v);
}

// 按当前活动视图是否在运行，切换「发送 / 停止」按钮（运行中显示停止）
function updateComposerButtons() {
  const st = composerState(activeView());
  // 运行中只留「停止」：发送按钮隐藏（输入框 Enter 仍可发，消息走 steering 纳入当前任务，对标主流）
  sendBtn.hidden = st.sendHidden;
  sendBtn.disabled = false;
  sendBtn.textContent = st.sendText;
  stopBtn.hidden = st.stopHidden;
  // 规划模式按钮高亮 + 顶部提示（FR-11.5）
  planBtn.classList.toggle("active", st.planActive);
  document.body.classList.toggle("plan-on", st.planActive);
}

// 规划模式开关（FR-11.5）：按对话独立，切到该会话即恢复其状态
const planBtn = document.getElementById("plan-btn");
planBtn.addEventListener("click", async () => {
  const v = activeView();
  if (!v || !window.pywebview) return;
  const next = !v.planMode;
  try {
    const res = await window.pywebview.api.set_plan_mode(next);
    v.planMode = !!(res && res.plan_mode);
  } catch (e) { return; }
  updateComposerButtons();
});

// ---- 架构评审面板（ADR 0019 Architecture Review Mode）-------------------
// 纯逻辑（reviewGateLabel / decisionsByStatus / decisionNeedsUser / REVIEW_*）在 pure.js，
// 这里只做 DOM 渲染与 api 调用。gate 永不显示百分比（守 ADR 0014/0019）。
const reviewBtn = document.getElementById("review-btn");
function reviewPanelEl() { return document.getElementById("ws-review"); }

function renderDecisionCard(d) {
  const needs = decisionNeedsUser(d);
  const blk = (d.blocking || []).map((b) => `<li>${escapeHtml(b)}</li>`).join("");
  let html = `<div class="rv-card ${needs ? "needs" : ""}" data-id="${escapeHtml(d.id)}">` +
    `<div class="rv-card-t">${escapeHtml(d.title || d.id)}</div>`;
  if (d.current_choice) html += `<div class="rv-choice">${escapeHtml(d.current_choice)}</div>`;
  if (blk) html += `<ul class="rv-blocking">${blk}</ul>`;
  if (needs) {
    const opts = ["Accepted", "Rejected", "Deferred", "NeedUser"]
      .map((s) => `<option value="${s}">${escapeHtml(REVIEW_LABELS[s] || s)}</option>`).join("");
    html += `<div class="rv-resolve">` +
      `<select class="rv-status">${opts}</select>` +
      `<input class="rv-input" type="text" placeholder="定稿选择（可选）" />` +
      `<button class="rv-btn" data-rv="resolve">拍板</button></div>`;
  }
  return html + `</div>`;
}

function renderReviewPanel(state, opts) {
  const el = reviewPanelEl();
  if (!el) return;
  if (!state || !state.ok) { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  const running = !!(opts && opts.running);
  const gate = state.gate || {};
  const lbl = reviewGateLabel(gate);          // {enabled, text}
  const groups = decisionsByStatus(state.decisions || []);
  const parts = [];
  parts.push(`<div class="rv-head"><span class="rv-title">架构评审</span>` +
    `<span class="rv-actions">` +
    `<button class="rv-btn" data-rv="rerun" title="对已拆解的决策重新跑一轮评审">↻</button>` +
    `<button class="rv-btn" data-rv="close" title="收起面板">✕</button></span></div>`);
  if (running) parts.push(`<div class="rv-running">⏳ 多角色评审进行中…（拆出 ${(state.decisions || []).length} 项决策，正在收敛）</div>`);
  parts.push(`<div class="rv-gate ${lbl.enabled ? "ok" : "blocked"}">` +
    `<button class="rv-start" data-rv="start-coding"${lbl.enabled ? "" : " disabled"}>${escapeHtml(lbl.text)}</button>` +
    (gate.reason ? `<span class="rv-reason">${escapeHtml(gate.reason)}</span>` : "") + `</div>`);
  if ((gate.blocking_count || 0) === 0 && !gate.user_signed) {
    parts.push(`<div class="rv-sign"><button class="rv-btn primary" data-rv="sign">签字确认开工</button>` +
      `<span class="rv-hint">未决已清零，签字后放行编码</span></div>`);
  } else if (gate.user_signed) {
    parts.push(`<div class="rv-sign signed">已签字 ✓</div>`);
  }
  // gate 放行后：把定稿落回规划/任务（Accepted 建议→待办，共识→notes）
  if (lbl.enabled) {
    parts.push(`<div class="rv-apply"><button class="rv-btn primary" data-rv="apply">应用到规划 / 任务</button>` +
      `<span class="rv-hint">采纳项写入待办、共识写入工作笔记（不改你原方案正文）</span></div>`);
  }
  REVIEW_STATUSES.forEach((s) => {
    const items = groups[s] || [];
    if (!items.length) return;
    parts.push(`<div class="rv-group rv-${s.toLowerCase()}">` +
      `<div class="rv-group-h">${escapeHtml(REVIEW_LABELS[s] || s)}<span class="rv-count">${items.length}</span></div>` +
      items.map(renderDecisionCard).join("") + `</div>`);
  });
  if (state.stop_reason) parts.push(`<div class="rv-stop">收敛于：${escapeHtml(state.stop_reason)}</div>`);
  el.innerHTML = parts.join("");
}

async function refreshReview() {
  if (!window.pywebview) return;
  try { renderReviewPanel(await window.pywebview.api.get_design_review()); } catch (e) { /* ignore */ }
}

if (reviewBtn) reviewBtn.addEventListener("click", async () => {
  const v = activeView();
  if (!v || !window.pywebview) return;
  if (!v.planMode) { showToast("先开规划模式产出方案，再发起架构评审"); return; }
  showToast("正在拆解方案…");
  reviewBtn.classList.add("busy");
  try {
    // 第一阶段：拆解（一次模型调用）→ 立刻把面板亮出来，决策可见
    const st = await window.pywebview.api.start_design_review();
    if (!st || !st.ok) { showToast(st && st.error ? st.error : "评审未能开始"); return; }
    renderReviewPanel(st, { running: true });
    showToast(`已拆出 ${(st.decisions || []).length} 项决策，多角色评审中…`);
    // 第二阶段：跑评审（最多 3 轮×2 角色，耗时较长）→ 回填共识
    const st2 = await window.pywebview.api.run_design_review();
    renderReviewPanel(st2 && st2.ok ? st2 : st);
    if (!st2 || !st2.ok) showToast(st2 && st2.error ? st2.error : "评审过程出错（决策已保留，可点 ↻ 重试）");
  } catch (e) { showToast("评审失败：" + (e && e.message ? e.message : e)); } finally { reviewBtn.classList.remove("busy"); }
});

(function bindReviewPanel() {
  const el = reviewPanelEl();
  if (!el) return;
  el.addEventListener("click", async (e) => {
    const t = e.target.closest("[data-rv]");
    if (!t || !window.pywebview) return;
    const act = t.getAttribute("data-rv");
    if (act === "close") { renderReviewPanel(null); return; }
    if (act === "rerun") {
      const cur = await window.pywebview.api.get_design_review();
      if (cur && cur.ok) renderReviewPanel(cur, { running: true });
      const st = await window.pywebview.api.run_design_review();
      renderReviewPanel(st && st.ok ? st : cur);
      if (!st || !st.ok) showToast(st && st.error ? st.error : "评审出错");
      return;
    }
    if (act === "sign") { renderReviewPanel(await window.pywebview.api.sign_off_design_review()); return; }
    if (act === "start-coding") {
      const r = await window.pywebview.api.can_start_coding();
      if (!r || !r.can_start) { showToast("尚未满足开工条件：未决清零并签字后再点"); return; }
      // 终态动作：把采纳项落回规划/任务（幂等）+ 退出规划模式，放行编码
      const ap = await window.pywebview.api.apply_review_to_plan();
      const v = activeView();
      try {
        const pm = await window.pywebview.api.set_plan_mode(false);
        if (v) v.planMode = !!(pm && pm.plan_mode);
        updateComposerButtons();
      } catch (e) { /* 退出规划模式失败不阻断提示 */ }
      if (ap && ap.ok) { refreshTasks(); showToast(`✅ 采纳项已落回（+${ap.tasks_added || 0} 待办）、已退出规划模式，可以开始编码`); }
      else showToast("✅ 已退出规划模式，可以开始编码");
      return;
    }
    if (act === "apply") {
      const r = await window.pywebview.api.apply_review_to_plan();
      if (r && r.ok) {
        showToast(`已落回：采纳项 +${r.tasks_added || 0} 条待办，共识已写入工作笔记`);
        refreshTasks();   // 刷新任务清单反映新待办
      } else { showToast(r && r.error ? r.error : "应用失败"); }
      return;
    }
    if (act === "resolve") {
      const card = t.closest(".rv-card");
      if (!card) return;
      const id = card.getAttribute("data-id");
      const status = card.querySelector(".rv-status").value;
      const choice = card.querySelector(".rv-input").value.trim();
      renderReviewPanel(await window.pywebview.api.resolve_decision(id, status, choice || null));
    }
  });
})();

// ---- 附件 --------------------------------------------------------------
function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

async function addAttachment(file) {
  if (!file) return;
  const dataUrl = await readFileAsDataUrl(file); // data:<mime>;base64,<data>
  const comma = dataUrl.indexOf(",");
  const meta = dataUrl.slice(5, dataUrl.indexOf(";")); // mime
  const data = dataUrl.slice(comma + 1);
  const att = {
    name: file.name || (meta.startsWith("image/") ? "粘贴图片." + meta.split("/")[1] : "附件"),
    mime: meta,
    data,
    dataUrl,
    isImage: meta.startsWith("image/"),
  };
  pendingAttachments.push(att);
  renderAttachmentChip(att);
}

function renderAttachmentChip(att) {
  const chip = document.createElement("div");
  chip.className = "att-chip";
  if (att.isImage) {
    const img = document.createElement("img");
    img.src = att.dataUrl;
    chip.appendChild(img);
  } else {
    const icon = document.createElement("span");
    icon.className = "att-icon";
    icon.textContent = "📄";
    chip.appendChild(icon);
  }
  const label = document.createElement("span");
  label.className = "att-name";
  label.textContent = att.name;
  chip.appendChild(label);

  const del = document.createElement("button");
  del.className = "att-del";
  del.textContent = "✕";
  del.title = "移除";
  del.addEventListener("click", () => {
    pendingAttachments = pendingAttachments.filter((a) => a !== att);
    chip.remove();
  });
  chip.appendChild(del);
  attachmentsBar.appendChild(chip);
}

function clearAttachments() {
  pendingAttachments = [];
  attachmentsBar.innerHTML = "";
}

// 在用户气泡上方渲染本条消息携带的附件
function renderSentAttachments(v, atts) {
  if (!atts.length) return;
  const row = document.createElement("div");
  row.className = "msg user sent-atts";
  const spacer = document.createElement("div");
  spacer.className = "role";
  spacer.textContent = "📎";
  const box = document.createElement("div");
  box.className = "sent-atts-box";
  atts.forEach((a) => {
    if (a.isImage) {
      const img = document.createElement("img");
      img.src = a.dataUrl;
      img.className = "zoomable";
      img.title = "点击预览 / 下载";
      box.appendChild(img);
    } else {
      const tag = document.createElement("span");
      tag.className = "sent-doc";
      tag.textContent = "📄 " + a.name;
      box.appendChild(tag);
    }
  });
  row.appendChild(spacer);
  row.appendChild(box);
  v.el.appendChild(row);
  scrollView(v);
}

// ---- 会话列表（P6.1） --------------------------------------------------
async function refreshSessions() {
  if (!window.pywebview) return;
  const { sessions, active, active_cid } = await window.pywebview.api.list_sessions();
  if (active != null) activeSessionId = active;
  // 初次：为当前活动对话建视图并挂载
  if (active_cid != null && activeCid == null) {
    const v = getView(active_cid);
    v.sessionId = active != null ? active : null;
    if (active != null) sessionIdToCid.set(active, active_cid);
    mountView(active_cid);
  }
  lastSessions = sessions || [];
  sessionList.innerHTML = "";
  lastSessions.forEach((s) => sessionList.appendChild(makeSessionItem(s)));
  applySessionFilter();
  updateRunningChip();
  if (ccPopover && !ccPopover.hidden) renderCommandCenter();   // 弹层开着时同步刷新
}
let lastSessions = [];
const ccPopover = document.getElementById("cc-popover");

// 「N 个会话运行中」概览（轻量指挥中心）：统计跑着的会话，点击跳到下一个非当前的运行会话。
function runningSessions() {
  const out = [];
  views.forEach((v) => {
    if ((v.status === "running" || v.status === "queued") && v.sessionId != null)
      out.push({ sid: v.sessionId, cid: v.cid });
  });
  return out;
}
function updateRunningChip() {
  const chip = document.getElementById("running-chip");
  if (!chip) return;
  const n = runningSessions().length;
  chip.hidden = n === 0;
  chip.textContent = `▶ ${n} 运行中`;
  if (n === 0) closeCommandCenter();
}
function sessionTitle(sid) {
  const s = lastSessions.find((x) => x.id === sid);
  return (s && s.title) || "新会话";
}
// 指挥中心弹层：从顶部一处管理所有运行中会话——切换 / 停止 / 改名，不必到 sidebar 找分散的按钮。
function renderCommandCenter() {
  if (!ccPopover) return;
  const running = runningSessions();
  if (!running.length) { closeCommandCenter(); return; }
  ccPopover.innerHTML = '<div class="cc-head">▶ 运行中的会话（点名称切换）</div>' +
    running.map((r) => `<div class="cc-row" data-sid="${r.sid}" data-cid="${r.cid}">` +
      `<span class="cc-name" title="切换到此会话">${escapeHtml(sessionTitle(r.sid))}` +
      `${r.sid === activeSessionId ? ' <span class="cc-cur">当前</span>' : ''}</span>` +
      '<button class="cc-btn cc-stop" type="button" title="停止该会话">⏹</button>' +
      '<button class="cc-btn cc-ren" type="button" title="重命名">✎</button></div>').join("");
  ccPopover.querySelectorAll(".cc-row").forEach((row) => {
    const sid = parseInt(row.dataset.sid, 10), cid = parseInt(row.dataset.cid, 10);
    row.querySelector(".cc-name").addEventListener("click", () => { closeCommandCenter(); selectSession(sid); });
    row.querySelector(".cc-stop").addEventListener("click", (e) => {
      e.stopPropagation(); window.pywebview.api.stop_conversation(cid); showToast("已请求停止");
    });
    row.querySelector(".cc-ren").addEventListener("click", (e) => { e.stopPropagation(); ccRename(row, sid); });
  });
  // 定位到运行计数 chip 下方
  const chip = document.getElementById("running-chip");
  if (chip) {
    const rect = chip.getBoundingClientRect();
    ccPopover.style.top = (rect.bottom + 6) + "px";
    ccPopover.style.left = Math.max(8, Math.min(rect.left, window.innerWidth - 290)) + "px";
  }
}
function ccRename(row, sid) {
  const nameEl = row.querySelector(".cc-name");
  if (!nameEl) return;
  const cur = sessionTitle(sid);
  const ipt = document.createElement("input");
  ipt.className = "cc-ren-input"; ipt.value = cur;
  nameEl.replaceWith(ipt); ipt.focus(); ipt.select();
  let done = false;
  const commit = async (save) => {
    if (done) return; done = true;
    const t = ipt.value.trim();
    if (save && t && t !== cur) await window.pywebview.api.rename_session(sid, t);
    refreshSessions();   // 重建列表 + 弹层
  };
  ipt.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(true); }
    else if (e.key === "Escape") { e.preventDefault(); commit(false); }
  });
  ipt.addEventListener("blur", () => commit(true));
}
function closeCommandCenter() { if (ccPopover) ccPopover.hidden = true; }
function toggleCommandCenter() {
  if (!ccPopover) return;
  if (!ccPopover.hidden) { closeCommandCenter(); return; }
  ccPopover.hidden = false;
  renderCommandCenter();
}
{
  const chip = document.getElementById("running-chip");
  if (chip) chip.addEventListener("click", toggleCommandCenter);
  // 点击弹层外部关闭（点 chip 本身不关——由上面的 toggle 处理）
  document.addEventListener("click", (e) => {
    if (ccPopover && !ccPopover.hidden && !ccPopover.contains(e.target) && !e.target.closest("#running-chip"))
      closeCommandCenter();
  });
}

// ⑥ 会话搜索：按标题过滤列表行（每次重渲染后重新应用）
function applySessionFilter() {
  const q = sessionSearch.value;
  sessionList.querySelectorAll("li").forEach((li) => {
    const t = li.querySelector(".session-title")?.textContent || "";
    li.style.display = sessionTitleMatches(t, q) ? "" : "none";
  });
}

// 跨会话全局搜索（P3）：输入 ≥2 字时，除按标题过滤会话外，再搜所有会话的消息内容，
// 结果列在搜索框下方，点一条跳到对应会话。
const msgSearchEl = document.getElementById("msg-search");
let _msgSearchTimer = null;
const ROLE_CN = { user: "你", assistant: "AI", tool: "工具", system: "系统" };

function renderMsgSearch(query, results) {
  if (!query || query.length < 2) { msgSearchEl.hidden = true; msgSearchEl.innerHTML = ""; return; }
  if (!results.length) {
    msgSearchEl.hidden = false;
    msgSearchEl.innerHTML = '<div class="ms-empty">消息中无匹配</div>';
    return;
  }
  const ql = query.toLowerCase();
  msgSearchEl.innerHTML = `<div class="ms-head">消息匹配 ${results.length}</div>` +
    results.map((r) => {
      const text = r.text || "";
      const i = text.toLowerCase().indexOf(ql);
      // 取命中处前后片段，高亮关键词
      let snip = text;
      if (i >= 0) {
        const from = Math.max(0, i - 24);
        snip = (from > 0 ? "…" : "") + text.slice(from, i + query.length + 40);
        snip = escapeHtml(snip).replace(escapeHtml(text.slice(i, i + query.length)),
          `<mark>${escapeHtml(text.slice(i, i + query.length))}</mark>`);
      } else { snip = escapeHtml(text.slice(0, 64)); }
      return `<button class="ms-item" data-sid="${r.session_id}">` +
        `<span class="ms-title">${escapeHtml(r.title || "新会话")}</span>` +
        `<span class="ms-snip"><span class="ms-role">${ROLE_CN[r.role] || r.role}</span>${snip}</span></button>`;
    }).join("");
  msgSearchEl.hidden = false;
  msgSearchEl.querySelectorAll(".ms-item").forEach((b) => {
    b.addEventListener("click", () => {
      const sid = Number(b.dataset.sid);
      sessionSearch.value = ""; applySessionFilter(); renderMsgSearch("", []);
      selectSession(sid);
    });
  });
}

async function runMsgSearch() {
  const q = sessionSearch.value.trim();
  if (q.length < 2) { renderMsgSearch("", []); return; }
  try {
    const r = await window.pywebview.api.search_messages(q);
    // 防抖期间用户可能又改了输入：只渲染仍匹配当前输入的结果
    if (sessionSearch.value.trim() === q) renderMsgSearch(q, (r && r.results) || []);
  } catch (e) { renderMsgSearch(q, []); }
}

sessionSearch.addEventListener("input", () => {
  applySessionFilter();
  clearTimeout(_msgSearchTimer);
  _msgSearchTimer = setTimeout(runMsgSearch, 200);  // 防抖，避免每键一次查询
});

// ⑧ 全局快捷键：Ctrl/⌘+N 新会话、Ctrl/⌘+Shift+P 规划模式、Ctrl/⌘+K 聚焦会话搜索
document.addEventListener("keydown", (e) => {
  if (!(e.ctrlKey || e.metaKey)) return;
  const k = (e.key || "").toLowerCase();
  if (k === "n") { e.preventDefault(); newSessionBtn.click(); }
  else if (k === "p" && e.shiftKey) { e.preventDefault(); planBtn.click(); }
  else if (k === "k") { e.preventDefault(); sessionSearch.focus(); }
});

// ---- 快捷键帮助面板（P2）：? 或 Ctrl/⌘+/ 打开，Esc 关闭 ----
const shortcutsOverlay = document.getElementById("shortcuts-overlay");
const shortcutsBody = document.getElementById("shortcuts-body");
const shortcutsClose = document.getElementById("shortcuts-close");
const _isMac = /Mac|iPhone|iPad/.test(navigator.platform || "");
const _mod = _isMac ? "⌘" : "Ctrl";
// 与上方各处实际绑定保持一致；改快捷键时记得同步这里
const SHORTCUT_GROUPS = [
  ["会话", [
    [`${_mod} N`, "新建会话"],
    [`${_mod} K`, "聚焦会话搜索"],
  ]],
  ["对话", [
    ["Enter", "发送消息"],
    ["Shift Enter", "换行"],
    ["/", "唤起斜杠命令菜单"],
    [`${_mod} F`, "在本对话中查找"],
    [`${_mod} Shift P`, "切换规划模式"],
  ]],
  ["查找栏", [
    ["Enter", "跳到下一个匹配"],
    ["Shift Enter", "跳到上一个匹配"],
    ["Esc", "关闭查找栏"],
  ]],
  ["编辑消息", [
    [`${_mod} Enter`, "保存并重发"],
    ["Esc", "取消编辑"],
  ]],
  ["帮助", [
    [`?　/　${_mod} /`, "打开本面板"],
    ["Esc", "关闭本面板"],
  ]],
];
function renderShortcuts() {
  shortcutsBody.innerHTML = SHORTCUT_GROUPS.map(([title, rows]) =>
    `<div class="sc-group"><div class="sc-group-title">${escapeHtml(title)}</div>` +
    rows.map(([keys, desc]) =>
      `<div class="sc-row"><span class="sc-keys">` +
      keys.split(" ").map((k) => `<kbd>${escapeHtml(k)}</kbd>`).join("") +
      `</span><span class="sc-desc">${escapeHtml(desc)}</span></div>`
    ).join("") + "</div>"
  ).join("");
}
function openShortcuts() { renderShortcuts(); shortcutsOverlay.hidden = false; }
function closeShortcuts() { shortcutsOverlay.hidden = true; }
if (shortcutsClose) shortcutsClose.addEventListener("click", closeShortcuts);
if (shortcutsOverlay) shortcutsOverlay.addEventListener("click", (e) => {
  if (e.target === shortcutsOverlay) closeShortcuts();   // 点遮罩关闭
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && shortcutsOverlay && !shortcutsOverlay.hidden) {
    e.preventDefault(); closeShortcuts(); return;
  }
  if (isHelpKey(e.key, e.ctrlKey || e.metaKey)) {
    const tag = (e.target && e.target.tagName) || "";
    const typing = tag === "INPUT" || tag === "TEXTAREA" || (e.target && e.target.isContentEditable);
    if (e.key === "?" && typing) return;   // 打字时让用户能正常输入问号
    e.preventDefault();
    shortcutsOverlay.hidden ? openShortcuts() : closeShortcuts();
  }
});

function makeSessionItem(s) {
  const li = document.createElement("li");
  li.dataset.sid = String(s.id);
  const cid = sessionIdToCid.get(s.id);
  const v = cid != null ? views.get(cid) : null;
  let cls = "session-item";
  if (s.id === activeSessionId) cls += " active";
  if (v && (v.status === "running" || v.status === "queued")) cls += " running";
  if (v && v.status === "awaiting") cls += " awaiting";
  if (v && v.unread && s.id !== activeSessionId) cls += " unread";
  if (s.pinned) cls += " pinned";
  li.className = cls;

  const name = document.createElement("span");
  name.className = "session-title";
  name.textContent = s.title || "新会话";
  name.title = s.title || "";
  name.addEventListener("click", () => selectSession(s.id));

  const pin = document.createElement("button");
  pin.className = "session-pin";
  pin.textContent = "📌";
  pin.title = s.pinned ? "取消置顶" : "置顶会话";
  pin.addEventListener("click", async (e) => {
    e.stopPropagation();
    await window.pywebview.api.set_session_pinned(s.id, !s.pinned);
    refreshSessions();
  });

  const exp = document.createElement("button");
  exp.className = "session-exp";
  exp.textContent = "⬇";
  exp.title = "导出该会话为 Markdown";
  exp.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (s.id !== activeSessionId) await selectSession(s.id);  // 先切到该会话（DOM 就绪），再按现有逻辑导出
    await exportConversation(s.title || "对话");  // 显式传标题，避开 refreshSessions 异步刷 active 的竞态
  });

  const ren = document.createElement("button");
  ren.className = "session-ren";
  ren.textContent = "✎";
  ren.title = "重命名会话";
  ren.addEventListener("click", (e) => {
    e.stopPropagation();
    beginRename(li, name, s);
  });

  const del = document.createElement("button");
  del.className = "session-del";
  del.textContent = "🗑";
  del.title = "删除会话";
  del.addEventListener("click", async (e) => {
    e.stopPropagation();
    const r = await window.pywebview.api.delete_session(s.id);
    const delCid = sessionIdToCid.get(s.id);
    if (delCid != null) { views.delete(delCid); sessionIdToCid.delete(s.id); }
    if (s.id === activeSessionId) {            // 删的是当前会话 -> 切到后端给的新草稿
      activeSessionId = null;
      if (r && r.active_cid != null) mountView(r.active_cid);
      else { chat.innerHTML = ""; activeCid = null; }
    }
    refreshSessions();
  });

  li.appendChild(name);
  // 停止运行中会话改由顶部「指挥中心」弹层统一处理（不在 sidebar 每行放按钮，避免重复/分散）
  li.appendChild(pin);
  li.appendChild(ren);
  li.appendChild(exp);   // 导出按钮放到改名之后（用户指定顺序）
  li.appendChild(del);
  return li;
}

// 仅更新某会话行的运行中/未读标记（避免每个 chunk 都重建整列表）
function updateSessionRow(v) {
  if (v.sessionId == null) return;
  const li = sessionList.querySelector(`li[data-sid="${v.sessionId}"]`);
  if (!li) return;
  const cls = sessionRowClasses(v.status, v.unread, isActive(v));
  li.classList.toggle("running", cls.running);
  li.classList.toggle("awaiting", cls.awaiting);
  li.classList.toggle("unread", cls.unread);
}

// 会话标题内联重命名：把标题换成输入框，Enter/失焦提交，Esc 取消
function beginRename(li, name, s) {
  const ipt = document.createElement("input");
  ipt.className = "session-rename-input";
  ipt.value = s.title || "";
  li.replaceChild(ipt, name);
  ipt.focus();
  ipt.select();

  let done = false;
  const commit = async (save) => {
    if (done) return;
    done = true;
    const title = ipt.value.trim();
    if (save && title && title !== s.title) {
      await window.pywebview.api.rename_session(s.id, title);
    }
    refreshSessions();
  };
  ipt.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(true); }
    else if (e.key === "Escape") { e.preventDefault(); commit(false); }
  });
  ipt.addEventListener("blur", () => commit(true));
}

async function selectSession(id) {
  // 该会话有活着的运行时（含后台运行中）-> 直接切回并挂载其视图，不重载
  const cid = sessionIdToCid.get(id);
  if (cid != null && views.has(cid)) {
    const r = await window.pywebview.api.switch_conversation(cid);
    if (r && r.ok) {
      activeSessionId = id;
      syncModelSelect(r.active_model);   // 下拉同步成该会话自己的模型
      mountView(cid);
      restoreInputState();
      refreshSessions();
      refreshWorkspace();
      return;
    }
    // 运行时已不在（被回收）-> 落到冷加载
  }
  const res = await window.pywebview.api.load_session(id);
  if (!res.ok) return;
  const ncid = res.cid;
  const v = getView(ncid);
  v.sessionId = id;
  sessionIdToCid.set(id, ncid);
  v.el.innerHTML = "";
  for (const k in v.toolBlocks) delete v.toolBlocks[k];
  renderHistory(v, res.messages || []);
  activeSessionId = id;
  syncModelSelect(res.active_model);   // 下拉同步成该会话自己的模型
  mountView(ncid);
  restoreInputState();
  refreshSessions();
  refreshWorkspace();
}

// 把模型下拉同步成某会话自己的模型（仅改显示值，不触发 set_active_model / 不动全局默认）
function syncModelSelect(model) {
  if (!model || !modelSelect) return;
  if ([...modelSelect.options].some((o) => o.value === model)) modelSelect.value = model;
}

// 切换/挂载后，按当前活动视图是否在流式中决定「发送 / 停止」按钮
function restoreInputState() {
  updateComposerButtons();
}

// 重渲染持久化的历史消息（text / image / tool 做静态展示）
function renderHistory(v, messages) {
  v.subBlocks = {};  // 重渲染前清空子任务块映射，避免残留
  v.userTurns = 0;   // 轮次计数随历史重渲染归零
  messages.forEach((m) => {
    const c = m.content;
    if (typeof c === "string") {
      const b = addMessage(v, m.role, c);
      if (m.role !== "user") { decorateAssistant(b, c); renderMermaidIn(b); }
      return;
    }
    if (!Array.isArray(c)) return;
    const atts = [];
    let textBuf = "";
    for (const b of c) {
      if (b.type === "text") {
        textBuf += (textBuf ? "\n\n" : "") + (b.text || "");
      } else if (b.type === "image") {
        const src = b.source || {};
        atts.push({ isImage: true, dataUrl: `data:${src.media_type};base64,${src.data}` });
      } else if (b.type === "tool_use") {
        if (b.name === "delegate") {
          const inp = b.input || {};
          ensureSubBlock(v, b.id, inp.task || "", ROLE_LABELS[inp.role]);  // 历史委派：静态子任务块
        } else {
          renderToolUse(v, { id: b.id, name: b.name, input: b.input || {} });
        }
      } else if (b.type === "tool_result") {
        if (v.subBlocks[b.tool_use_id]) {
          finishSubBlock(v, b.tool_use_id, true, b.content || "");    // 回填委派摘要
        } else {
          renderToolResult(v, { id: b.tool_use_id, name: "", ok: true, output: b.content || "" });
        }
      }
    }
    if (atts.length) renderSentAttachments(v, atts);
    if (textBuf) {
      const b = addMessage(v, m.role, textBuf);
      if (m.role !== "user") { decorateAssistant(b, textBuf); renderMermaidIn(b); }
    }
  });
  rebuildChatIndex();
  scrollChat();
}

// ---- 发送 --------------------------------------------------------------
// ---- 斜杠命令（低频操作走 /命令，不占常驻按钮，对标 Claude Code）-------
const SLASH_COMMANDS = [
  { cmd: "/add-dir", arg: "<目录路径>", desc: "授权一个工作区外的目录，文件工具之后可读其中文件" },
  { cmd: "/crazy", arg: "<目标>", desc: "🤖 自主模式：无人值守，AI 自己写目标+循环干到底（免确认，慎用）" },
  { cmd: "/help", arg: "", desc: "列出所有可用命令" },
];
const slashMenu = document.getElementById("slash-menu");
let slashSel = -1;

function hideSlashMenu() { slashMenu.hidden = true; slashMenu._matches = null; slashSel = -1; }

function renderSlashMenu(matches) {
  slashMenu.innerHTML = "";
  matches.forEach((c, i) => {
    const item = document.createElement("div");
    item.className = "slash-item" + (i === slashSel ? " sel" : "");
    item.innerHTML = `<span class="slash-cmd">${c.cmd}</span>` +
      (c.arg ? ` <span class="slash-arg">${c.arg}</span>` : "") +
      `<span class="slash-desc">${c.desc}</span>`;
    item.addEventListener("mousedown", (e) => { e.preventDefault(); applySlash(c); });
    slashMenu.appendChild(item);
  });
  slashMenu._matches = matches;
  slashMenu.hidden = false;
}

// 输入以 / 开头、且还在打命令名（无空格）时，浮出匹配命令
function updateSlashMenu() {
  const matches = matchSlashCommands(SLASH_COMMANDS, input.value);
  if (!matches.length) { hideSlashMenu(); return; }
  if (slashSel >= matches.length) slashSel = -1;
  renderSlashMenu(matches);
}

function applySlash(c) {
  if (!c) return;
  input.value = c.cmd + (c.arg ? " " : "");
  hideSlashMenu();
  input.focus();
  autoResize();
}

// ---- @ 文件引用（P3）：输入 @ 弹工作区文件补全，选中插入 @相对路径（agent 用 read_file 读）----
const mentionMenu = document.getElementById("mention-menu");
let mentionSel = -1;
let mentionFiles = [];        // 当前会话工作区的扁平文件路径列表（懒加载、按会话缓存）
let mentionFilesCid = null;   // mentionFiles 属于哪个 cid（切会话失效）

async function ensureMentionFiles() {
  if (mentionFilesCid === activeCid && mentionFiles.length) return;
  try {
    const tree = await window.pywebview.api.get_workspace_tree();
    mentionFiles = flattenTreeFiles(tree && tree.tree ? tree.tree : tree);
    mentionFilesCid = activeCid;
  } catch (e) { mentionFiles = []; }
}

function hideMentionMenu() { mentionMenu.hidden = true; mentionMenu._matches = null; mentionSel = -1; }

function renderMentionMenu(matches) {
  mentionMenu.innerHTML = "";
  matches.forEach((path, i) => {
    const item = document.createElement("div");
    item.className = "slash-item" + (i === mentionSel ? " sel" : "");
    const slash = path.lastIndexOf("/");
    const name = slash === -1 ? path : path.slice(slash + 1);
    const dir = slash === -1 ? "" : path.slice(0, slash + 1);
    item.innerHTML = `<span class="slash-cmd">@${escapeHtml(name)}</span>` +
      (dir ? `<span class="slash-desc">${escapeHtml(dir)}</span>` : "");
    item.addEventListener("mousedown", (e) => { e.preventDefault(); applyMention(path); });
    mentionMenu.appendChild(item);
  });
  mentionMenu._matches = matches;
  mentionMenu.hidden = false;
}

async function updateMentionMenu() {
  const m = findMentionQuery(input.value, input.selectionStart);
  if (!m.active) { hideMentionMenu(); return; }
  await ensureMentionFiles();
  const matches = matchFileMentions(mentionFiles, m.query);
  if (!matches.length) { hideMentionMenu(); return; }
  if (mentionSel >= matches.length) mentionSel = -1;
  mentionMenu._start = m.start;
  renderMentionMenu(matches);
}

// 把光标前的 @query token 替换成 @path（后跟空格），保留两侧文本
function applyMention(path) {
  const start = mentionMenu._start;
  const caret = input.selectionStart;
  if (start == null || start < 0) { hideMentionMenu(); return; }
  const before = input.value.slice(0, start);
  const after = input.value.slice(caret);
  const insert = "@" + path + " ";
  input.value = before + insert + after;
  const pos = before.length + insert.length;
  hideMentionMenu();
  input.focus();
  input.setSelectionRange(pos, pos);
  autoResize();
}

// 命令结果以"系统提示行"显示在对话流（区别于 你/AI 气泡）
function addSysLine(v, text) {
  const el = document.createElement("div");
  el.className = "sys-line";
  el.textContent = text;
  appendRow(v, el);
  scrollChatForce();
}

async function handleSlashCommand(v, text) {
  const { cmd, arg } = parseSlashInput(text);
  if (cmd === "/add-dir") {
    if (!arg) { addSysLine(v, "用法：/add-dir <目录路径>　例如：/add-dir D:\\其它项目"); return; }
    let r;
    try { r = await window.pywebview.api.add_dir(arg, v.cid); }
    catch (e) { addSysLine(v, "⚠ 授权失败：" + e); return; }
    if (r && r.ok) addSysLine(v, `✅ 已授权目录（本会话共 ${(r.dirs || []).length} 个）：\n${(r.dirs || []).map((d) => "  · " + d).join("\n")}\n文件工具（read_file / list_dir 等）现在即可读其中文件。`);
    else addSysLine(v, "⚠ " + ((r && r.error) || "授权失败"));
  } else if (cmd === "/crazy") {
    if (!arg) { addSysLine(v, "用法：/crazy <你的高层目标>　例如：/crazy 做一个待办命令行小工具并配测试"); return; }
    addSysLine(v, `🤖 自主模式启动：${arg}\n无人值守、免确认、AI 自己写目标循环干到底；随时点「停止」中止。`);
    try { await window.pywebview.api.start_autonomous(arg, 0); }
    catch (e) { addSysLine(v, "⚠ 启动失败：" + e); }
  } else if (cmd === "/help") {
    addSysLine(v, "可用命令：\n" + SLASH_COMMANDS.map((c) => `  ${c.cmd}${c.arg ? " " + c.arg : ""}　—　${c.desc}`).join("\n"));
  } else {
    addSysLine(v, `未知命令 ${cmd}　输入 /help 查看可用命令`);
  }
}

async function send() {
  const v = activeView();
  if (!v) return;
  const text = input.value.trim();
  if (!text && !pendingAttachments.length) return;

  // 斜杠命令：本地处理，不发给模型
  if (text.startsWith("/") && !pendingAttachments.length) {
    hideSlashMenu();
    input.value = ""; autoResize();
    await handleSlashCommand(v, text);
    return;
  }

  const atts = pendingAttachments.slice();
  const queued = v.streaming;   // 运行中发送 = 排队（不打断当前回合，对标 Claude Code）

  renderSentAttachments(v, atts);
  if (text) { addMessage(v, "user", text); rebuildChatIndex(); }
  scrollChatForce();   // 主动发送：强制到底并恢复粘底（哪怕之前往上翻看了历史）
  input.value = "";
  autoResize();
  clearAttachments();

  if (!queued) {
    // 首次发送：正常启动流式
    v.streaming = true;
    updateComposerButtons();
    v.currentBubble = null;
    v.currentText = "";
    v.thinkingEl = null;
    v.thinkingText = "";
    startWorking(v); // 立即给"思考中…"反馈，消除全白等待
  }
  // 运行中：用户消息已加进对话（即排队的可视反馈），后端 enqueue 串行处理；不重置当前流式

  // 只把后端需要的字段传过去（去掉 dataUrl/isImage）
  const payload = atts.map((a) => ({ name: a.name, mime: a.mime, data: a.data }));
  try {
    await window.pywebview.api.send_message(text, payload);
  } catch (e) {
    window.__onAgentEvent({ event: EV.ERROR, data: String(e), cid: v.cid });
  }
}

// ---- 输入框交互 --------------------------------------------------------
function autoResize() {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 200) + "px";
}
input.addEventListener("input", () => { autoResize(); updateSlashMenu(); updateMentionMenu(); });
input.addEventListener("keydown", (e) => {
  if (!slashMenu.hidden) {
    const m = slashMenu._matches || [];
    if (e.key === "ArrowDown") { e.preventDefault(); slashSel = (slashSel + 1) % m.length; renderSlashMenu(m); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); slashSel = (slashSel - 1 + m.length) % m.length; renderSlashMenu(m); return; }
    if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); applySlash(m[slashSel >= 0 ? slashSel : 0]); return; }
    if (e.key === "Escape") { e.preventDefault(); hideSlashMenu(); return; }
  }
  if (!mentionMenu.hidden) {
    const m = mentionMenu._matches || [];
    if (e.key === "ArrowDown") { e.preventDefault(); mentionSel = (mentionSel + 1) % m.length; renderMentionMenu(m); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); mentionSel = (mentionSel - 1 + m.length) % m.length; renderMentionMenu(m); return; }
    if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); applyMention(m[mentionSel >= 0 ? mentionSel : 0]); return; }
    if (e.key === "Escape") { e.preventDefault(); hideMentionMenu(); return; }
  }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {  // !isComposing：输入法确认候选词的回车不当发送
    e.preventDefault();
    send();
  }
});
document.getElementById("task-toggle").addEventListener("click", () => {
  const collapsed = localStorage.getItem("tasksCollapsed") === "1";
  localStorage.setItem("tasksCollapsed", collapsed ? "0" : "1");
  renderTaskBar();
});

sendBtn.addEventListener("click", send);
stopBtn.addEventListener("click", async () => {
  const v = activeView();
  if (!v || (!v.streaming && !v.crazyRunning)) return;
  stopBtn.disabled = true;
  try {
    await window.pywebview.api.stop_conversation(v.cid);
  } finally {
    stopBtn.disabled = false; // 实际收尾由 stopped/done 事件驱动
  }
});

// ---- 附件入口：选文件 / 粘贴 / 拖拽 ------------------------------------
attachBtn.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", async () => {
  for (const f of fileInput.files) await addAttachment(f);
  fileInput.value = ""; // 允许重复选同一文件
});

input.addEventListener("paste", async (e) => {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  for (const it of items) {
    if (it.kind === "file") {
      const f = it.getAsFile();
      if (f) await addAttachment(f);
    }
  }
});

// 拖拽到整个窗口都可放下
document.addEventListener("dragover", (e) => {
  e.preventDefault();
  document.body.classList.add("dragging");
});
document.addEventListener("dragleave", (e) => {
  if (e.relatedTarget === null) document.body.classList.remove("dragging");
});
document.addEventListener("drop", async (e) => {
  e.preventDefault();
  document.body.classList.remove("dragging");
  const files = e.dataTransfer && e.dataTransfer.files;
  if (files) for (const f of files) await addAttachment(f);
});

newSessionBtn.addEventListener("click", async () => {
  const r = await window.pywebview.api.new_session();
  clearAttachments();
  if (r && r.cid != null) {
    const v = getView(r.cid);
    v.el.innerHTML = "";
    for (const k in v.toolBlocks) delete v.toolBlocks[k];
    v.sessionId = null;
    activeSessionId = null;
    mountView(r.cid);
    restoreInputState();
  }
  refreshSessions();
  input.focus();
});

document.getElementById("open-project").addEventListener("click", async () => {
  const res = await window.pywebview.api.open_project();
  if (!res || !res.ok) {           // 取消或失败：不动当前会话
    if (res && res.error) showToast("⚠ " + res.error);
    return;
  }
  clearAttachments();
  if (res.cid != null) {
    const v = getView(res.cid);
    v.el.innerHTML = "";
    for (const k in v.toolBlocks) delete v.toolBlocks[k];
    v.sessionId = null;
    activeSessionId = null;
    mountView(res.cid);
    restoreInputState();
  }
  refreshSessions();
  refreshWorkspace();              // 展示所选项目的文件
  input.focus();
});

modelSelect.addEventListener("change", async () => {
  await window.pywebview.api.set_active_model(modelSelect.value);
});

subagentSelect.addEventListener("change", async () => {
  await window.pywebview.api.set_subagent_model(subagentSelect.value);
  showToast(subagentSelect.value ? "委派模型：" + subagentSelect.value : "委派模型：跟随主模型");
});

// ---- 外观设置（P2：浅色主题 + 字号）：纯客户端，存 localStorage ----
// themePref: "system"|"dark"|"light"（默认跟随系统）；fontSize: "sm"|"md"|"lg"（默认中）
const _mqlDark = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
function getThemePref() { return localStorage.getItem("themePref") || "system"; }
function getFontSize() { return normFontSize(localStorage.getItem("fontSize")); }
function applyAppearance() {
  const el = document.documentElement;
  el.dataset.theme = resolveTheme(getThemePref(), _mqlDark ? _mqlDark.matches : true);
  el.dataset.font = getFontSize();
}
applyAppearance(); // 立即应用，避免渲染后才换肤的闪烁
// 跟随系统时，系统明暗变化要实时反映
if (_mqlDark) {
  const onSysTheme = () => { if (getThemePref() === "system") applyAppearance(); };
  if (_mqlDark.addEventListener) _mqlDark.addEventListener("change", onSysTheme);
  else if (_mqlDark.addListener) _mqlDark.addListener(onSysTheme); // 旧 WebView 兜底
}

// ---- 设置面板：Provider 中心（产品化③：provider 配一次 key/url/格式、下挂多个模型）----
const settingsBtn = document.getElementById("settings-btn");
const settingsOverlay = document.getElementById("settings-overlay");
const settingsClose = document.getElementById("settings-close");
const provListEl = document.getElementById("prov-list");
const provDetailEl = document.getElementById("prov-detail");
let provData = [];
let provSelected = null;

async function openSettings() { await loadProviders(); settingsOverlay.hidden = false; }
function closeSettings() { settingsOverlay.hidden = true; }

async function loadProviders() {
  const r = await window.pywebview.api.get_providers();
  provData = (r && r.providers) || [];
  if (!provSelected || !provData.find((p) => p.key === provSelected)) {
    provSelected = (provData[0] && provData[0].key) || null;
  }
  renderProviderList();
  renderProviderDetail();
}

function renderProviderList() {
  provListEl.innerHTML = "";
  provData.forEach((p) => {
    const row = document.createElement("button");
    row.className = "prov-item" + (p.key === provSelected ? " active" : "");
    row.innerHTML = `<span class="prov-name">${escapeHtml(p.label)}</span>` +
      `<span class="prov-dot ${p.enabled ? "on" : ""}"></span>`;
    row.addEventListener("click", () => { provSelected = p.key; renderProviderList(); renderProviderDetail(); });
    provListEl.appendChild(row);
  });
  // 浏览器穿透（特殊项：一键开关，深度调研用）
  const br = document.createElement("button");
  br.className = "prov-item prov-special" + (provSelected === "__browser__" ? " active" : "");
  br.innerHTML = '<span class="prov-name">🌐 浏览器穿透</span>';
  br.addEventListener("click", () => { provSelected = "__browser__"; renderProviderList(); renderProviderDetail(); });
  provListEl.appendChild(br);
  // MCP 扩展（特殊项：增删改外部 MCP server，不必手编 config.yaml）
  const mc = document.createElement("button");
  mc.className = "prov-item prov-special" + (provSelected === "__mcp__" ? " active" : "");
  mc.innerHTML = '<span class="prov-name">🔌 MCP 扩展</span>';
  mc.addEventListener("click", () => { provSelected = "__mcp__"; renderProviderList(); renderProviderDetail(); });
  provListEl.appendChild(mc);
  // Hooks（特殊项：工具调用前后跑自定义命令，守卫/动作）
  const hk = document.createElement("button");
  hk.className = "prov-item prov-special" + (provSelected === "__hooks__" ? " active" : "");
  hk.innerHTML = '<span class="prov-name">🪝 Hooks</span>';
  hk.addEventListener("click", () => { provSelected = "__hooks__"; renderProviderList(); renderProviderDetail(); });
  provListEl.appendChild(hk);
  // 外观（特殊项：浅色主题 + 字号，纯客户端）
  const ap = document.createElement("button");
  ap.className = "prov-item prov-special" + (provSelected === "__appearance__" ? " active" : "");
  ap.innerHTML = '<span class="prov-name">🎨 外观</span>';
  ap.addEventListener("click", () => { provSelected = "__appearance__"; renderProviderList(); renderProviderDetail(); });
  provListEl.appendChild(ap);
  // 功能开关（特殊项：默认关的进阶 agent 能力，即点即生效 + 持久化）
  const fp = document.createElement("button");
  fp.className = "prov-item prov-special" + (provSelected === "__features__" ? " active" : "");
  fp.innerHTML = '<span class="prov-name">🛠 功能开关</span>';
  fp.addEventListener("click", () => { provSelected = "__features__"; renderProviderList(); renderProviderDetail(); });
  provListEl.appendChild(fp);
  const add = document.createElement("button");
  add.className = "prov-add";
  add.textContent = "+ 自定义服务";
  add.addEventListener("click", addCustomProvider);
  provListEl.appendChild(add);
}

async function renderBrowserPane() {
  const s = (await window.pywebview.api.get_browser_mcp_status()) || {};
  const resuming = s.enabled && !s.connected && s.node;   // 已启用但没连上 → 会自动续装
  const statusTxt = s.enabled
    ? (s.connected ? `已启用 · 已连上（${s.tools} 个工具）` : (resuming ? "已启用 · 正在继续安装…" : "已启用 · 未连上"))
    : "未启用";
  // 手动安装说明（点「启用」自动下载 Chrome 约 150MB，网络不好易超时 → 给可一键复制的手动方案）
  const MANUAL_CMDS = [
    "npx -y playwright@latest install chrome",
    "npx -y @playwright/mcp@latest --version",
  ];
  const cmdRow = (i, note) =>
    `<div class="br-cmd-note">${escapeHtml(note)}</div>` +
    `<div class="br-cmd"><code>${escapeHtml(MANUAL_CMDS[i])}</code>` +
    `<button class="br-cmd-copy" type="button" data-i="${i}"><span>复制</span></button></div>`;
  const manualHtml =
    '<details class="br-manual"><summary>⬇ 下载老是超时 / 想自己手动装？点这里看说明</summary>' +
    '<div class="br-manual-body">' +
    '<p class="settings-hint">点「启用」会自动下载 Google Chrome（约 150MB），网络不稳时容易超时失败。' +
    '你可以改成<b>自己手动装</b>——装好后回来点「启用浏览器穿透」会<b>直接秒连、不再下载</b>。</p>' +
    '<p class="settings-hint"><b>① 前提：装 Node.js</b>（已装可跳过）。没有就去官网 <b>nodejs.org</b> 下 LTS 版装上，' +
    '装完<b>重启本应用</b>让它检测到。</p>' +
    '<p class="settings-hint"><b>② 最省事：直接装一个 Google Chrome 浏览器</b>（官网 <b>google.cn/chrome</b>）。' +
    '装了系统 Chrome 后，Playwright 会直接复用它，启用时<b>完全不用再下载那 150MB</b>。</p>' +
    '<p class="settings-hint"><b>③ 或在终端手动跑命令预装</b>（Windows 用 PowerShell；能看到进度、失败可直接重试）：</p>' +
    cmdRow(0, "下载 / 检测 Chrome —— 已装系统 Chrome 则秒过，否则下载约 150MB") +
    cmdRow(1, "预热 Playwright MCP server 包 —— 免首次连接时再拉包超时") +
    '<p class="settings-hint">两条都成功后，回到此页点「启用浏览器穿透」即可连上。' +
    '若公司网 / 校园网下载特别慢，建议优先用 ② 直接装系统 Chrome。</p>' +
    '</div></details>';
  provDetailEl.innerHTML =
    '<div class="prov-d-head"><span class="prov-d-title">🌐 浏览器穿透（深度调研）</span></div>' +
    '<p class="settings-hint">让 Agent 真实开浏览器在站内导航 / 点击 / 翻页 / 抽取，突破「只能搜 + 读一级页面」' +
    '——搜索被污染、深层链接 404 时能像人一样逐层点进去。底层走 Playwright 驱动你本机的 <b>Google Chrome</b>，' +
    '需本机有 <b>Node.js</b>；首次启用会自动下载 / 检测 Chrome（约 150MB，已装系统 Chrome 则秒过）。</p>' +
    `<div class="prov-field"><div class="prov-label">状态</div><div class="prov-fmt">${escapeHtml(statusTxt)}　·　Node：${s.node ? "已检测到" : "未检测到 ⚠"}</div></div>` +
    `<label class="feat-row" style="margin-top:4px"><input type="checkbox" class="feat-ck" id="br-headed"${s.headed ? " checked" : ""}>` +
    '<span class="feat-text"><span class="feat-title">有头·登录态模式</span>' +
    '<span class="feat-desc">弹出<b>可见浏览器</b>：碰到要登录的网站 / 滑块验证，你手动登录·划一次，登录态会<b>保留复用</b>，' +
    '之后 hermes 就以你的已登录身份类人查询——这是绕过反爬的正解（别跟滑块硬刚）。关掉=无头后台跑（快但易撞反爬）。</span></label>' +
    `<div class="model-ops"><button class="br-toggle btn-primary">${s.enabled ? "关闭" : "启用浏览器穿透"}</button></div>` +
    '<div class="br-busy" hidden>⏳ 安装中…（首次约 150MB，请稍候，别关窗口）</div>' +
    manualHtml;
  // 手动安装命令的一键复制（复用 copyText + flashCopied，带 WebView2 降级）
  provDetailEl.querySelectorAll(".br-cmd-copy").forEach((b) => {
    b.addEventListener("click", () => copyText(MANUAL_CMDS[+b.dataset.i], b));
  });
  const headedCk = provDetailEl.querySelector("#br-headed");
  if (headedCk) headedCk.addEventListener("change", async () => {
    const r = await window.pywebview.api.set_browser_headed(headedCk.checked);
    showToast(headedCk.checked
      ? "已切到有头登录态：下次浏览会弹出可见浏览器，去登录/划滑块即可"
      : "已切回无头后台模式");
    if (r && r.ok) renderBrowserPane();
  });
  const btn = provDetailEl.querySelector(".br-toggle");
  const busy = provDetailEl.querySelector(".br-busy");
  btn.addEventListener("click", async () => {
    const turnOn = !s.enabled;
    if (turnOn && !s.node) { showToast("⚠ 未检测到 Node.js，请先安装 Node 再启用"); return; }
    btn.disabled = true;
    if (turnOn) {
      busy.hidden = false; busy.textContent = "⏳ 准备中…";
      const res = await window.pywebview.api.set_browser_mcp(true);
      if (res && res.ok && res.status === "installing") {
        showToast("开始安装浏览器（后台进行，可关此窗口，装好会提示）");  // 进度/完成走事件
      } else if (res && res.ok) {
        busy.hidden = true; btn.disabled = false; showToast(`✅ 已启用，连上 ${res.tools} 个工具`); renderBrowserPane();
      } else {
        busy.hidden = true; btn.disabled = false; showToast("⚠ " + ((res && res.error) || "启用失败"));
      }
    } else {
      const res = await window.pywebview.api.set_browser_mcp(false);
      btn.disabled = false;
      if (res && res.ok) { showToast("已关闭浏览器穿透"); renderBrowserPane(); }
      else showToast("⚠ " + ((res && res.error) || "关闭失败"));
    }
  });
  // 打开面板时若发现「已启用但没连上」（上次中断）→ 自动续装，不必用户再点
  if (resuming && !window.__brResuming) {
    window.__brResuming = true;
    if (busy) { busy.hidden = false; busy.textContent = "⏳ 上次没装完，正在继续…"; }
    window.pywebview.api.set_browser_mcp(true).finally(() => { window.__brResuming = false; });
  }
}

// 浏览器穿透安装进度 / 完成（后台线程推来的全局事件，无 cid）
function onBrowserProgress(data) {
  const busy = provDetailEl && provDetailEl.querySelector(".br-busy");
  if (busy) { busy.hidden = false; busy.textContent = "⏳ " + ((data && data.text) || "安装中…"); }
}
function onBrowserDone(data) {
  if (data && data.ok) showToast(`✅ 浏览器穿透已启用，连上 ${data.tools} 个工具`);
  else showToast("⚠ 浏览器穿透启用失败：" + ((data && data.error) || ""));
  if (provSelected === "__browser__" && settingsOverlay && !settingsOverlay.hidden) renderBrowserPane();
}

function renderAppearancePane() {
  const themePref = getThemePref();
  const fontSize = getFontSize();
  const themeOpts = [["system", "跟随系统"], ["dark", "深色"], ["light", "浅色"]];
  const fontOpts = [["sm", "小"], ["md", "中"], ["lg", "大"]];
  const seg = (name, opts, cur) => opts.map(([v, label]) =>
    `<button class="ap-seg${v === cur ? " active" : ""}" data-group="${name}" data-val="${v}">${label}</button>`
  ).join("");
  provDetailEl.innerHTML =
    '<div class="prov-d-head"><span class="prov-d-title">🎨 外观</span></div>' +
    '<p class="settings-hint">主题与字号仅影响本机界面显示，立即生效、自动记住。</p>' +
    `<div class="prov-field"><div class="prov-label">主题</div><div class="ap-segs" data-name="theme">${seg("theme", themeOpts, themePref)}</div></div>` +
    `<div class="prov-field"><div class="prov-label">字号</div><div class="ap-segs" data-name="font">${seg("font", fontOpts, fontSize)}</div></div>`;
  provDetailEl.querySelectorAll(".ap-seg").forEach((b) => {
    b.addEventListener("click", () => {
      const group = b.dataset.group, val = b.dataset.val;
      if (group === "theme") localStorage.setItem("themePref", val);
      else localStorage.setItem("fontSize", val);
      applyAppearance();
      renderAppearancePane(); // 刷新高亮
    });
  });
}

async function renderMcpPane() {
  // 🔌 MCP 扩展：列出/增删改用户加的外部 MCP server，改动即时重连生效。
  const r = (await window.pywebview.api.get_mcp_servers()) || {};
  const servers = r.servers || {};
  const connected = r.connected || {};
  const errors = r.errors || {};
  const esc = escapeHtml;
  const names = Object.keys(servers);

  let rows;
  if (!names.length) {
    rows = '<p class="settings-hint">还没有自定义 MCP server。下面添加一个（如官方 filesystem / git server）。</p>';
  } else {
    rows = names.map((name) => {
      const s = servers[name] || {};
      const ntools = (connected[name] || []).length;
      const status = !s.enabled
        ? '<span class="mcp-off">○ 已停用</span>'
        : (ntools ? `<span class="mcp-ok">● ${ntools} 工具</span>` : '<span class="mcp-warn">● 未连上</span>');
      const err = (s.enabled && !ntools && errors[name])
        ? `<div class="mcp-err">连接失败：${esc(errors[name])}</div>` : "";
      return `<div class="mcp-row" data-name="${esc(name)}">
        <div class="mcp-main">
          <div class="mcp-name">${esc(name)}${s.trust ? ' <span class="mcp-trust">免确认</span>' : ''} ${status}</div>
          <div class="mcp-cmd">${esc(s.command || "")} ${esc((s.args || []).join(" "))}</div>
          ${err}
        </div>
        <label class="mcp-toggle" title="启用 / 停用"><input type="checkbox" class="mcp-en"${s.enabled ? " checked" : ""}></label>
        <button class="ws-btn mcp-edit" type="button">编辑</button>
        <button class="ws-btn mcp-del" type="button">删除</button>
      </div>`;
    }).join("");
  }

  provDetailEl.innerHTML =
    '<div class="prov-d-head"><span class="prov-d-title">🔌 MCP 扩展</span></div>' +
    '<p class="settings-hint">接入外部 MCP server，给 Agent 加工具（文件系统 / Git / 浏览器等）。需本机有对应运行环境（npx 要装 Node、uvx 要装 uv）。改动即时重连生效，不必手编 config.yaml。</p>' +
    `<div class="mcp-list">${rows}</div>` +
    '<div class="mcp-form">' +
      '<div class="prov-d-title" style="margin-top:10px;font-size:13px">添加 / 编辑 server</div>' +
      '<div class="mcp-presets"><span class="mcp-presets-tip">套用模板（点一下自动填好，再改目录即可）：</span>' +
        '<button class="ws-btn mcp-preset" data-p="filesystem" type="button">📁 文件系统</button>' +
        '<button class="ws-btn mcp-preset" data-p="git" type="button">🔧 Git</button>' +
        '<button class="ws-btn mcp-preset" data-p="fetch" type="button">🌐 网页抓取</button></div>' +
      '<input class="feat-input" id="mcp-f-name" placeholder="名称（如 filesystem）">' +
      '<input class="feat-input" id="mcp-f-cmd" placeholder="启动命令（如 npx / uvx / python）">' +
      '<textarea class="feat-input mcp-ta" id="mcp-f-args" rows="3" placeholder="参数：每行一个（不要带任何说明文字！）&#10;-y&#10;@modelcontextprotocol/server-filesystem&#10;D:\\你的目录"></textarea>' +
      '<button class="ws-btn mcp-pickdir" id="mcp-pickdir" type="button">📁 选择文件夹填入目录</button>' +
      '<textarea class="feat-input mcp-ta" id="mcp-f-env" rows="2" placeholder="环境变量（可选）：每行 KEY=VALUE"></textarea>' +
      '<label class="feat-row"><input type="checkbox" id="mcp-f-trust"><span class="feat-text"><span class="feat-title">免确认（trust）</span><span class="feat-desc">该 server 的工具免逐次权限确认——只对你信任的 server 开。</span></span></label>' +
      '<button class="prov-save" id="mcp-save" type="button">保存并连接</button>' +
    '</div>';

  // 一键模板：点了自动填好命令+参数结构，用户只改目录行（避免手敲结构填错）
  const MCP_PRESETS = {
    filesystem: { name: "filesystem", cmd: "npx",
      args: ["-y", "@modelcontextprotocol/server-filesystem", "改成你的目录，如 D:\\项目"], trust: true },
    git: { name: "git", cmd: "uvx",
      args: ["mcp-server-git", "--repository", "改成你的git仓库目录"], trust: true },
    fetch: { name: "fetch", cmd: "uvx", args: ["mcp-server-fetch"], trust: true },
  };
  provDetailEl.querySelectorAll(".mcp-preset").forEach((b) => {
    b.addEventListener("click", () => {
      const p = MCP_PRESETS[b.dataset.p];
      if (!p) return;
      const args = p.args.slice();
      // filesystem / git 的目录默认填「当前工作区」——多数情况点一下直接能存、免得手填目录填错
      if ((b.dataset.p === "filesystem" || b.dataset.p === "git") && wsRoot) {
        args[args.length - 1] = wsRoot;
      }
      provDetailEl.querySelector("#mcp-f-name").value = p.name;
      provDetailEl.querySelector("#mcp-f-cmd").value = p.cmd;
      provDetailEl.querySelector("#mcp-f-args").value = args.join("\n");
      provDetailEl.querySelector("#mcp-f-env").value = "";
      provDetailEl.querySelector("#mcp-f-trust").checked = !!p.trust;
      provDetailEl.querySelector("#mcp-f-args").focus();
      showToast(wsRoot ? "已套用模板（目录=当前工作区），可直接保存" : "已套用模板——把最后一行改成你的真实目录再保存");
    });
  });

  // 选文件夹：弹系统对话框，把路径填进 args 的目录行（最后一行；占位行直接替换）
  provDetailEl.querySelector("#mcp-pickdir").addEventListener("click", async () => {
    const res = await window.pywebview.api.pick_directory().catch(() => null);
    if (!res || !res.ok) { if (res && !res.cancelled) showToast((res && res.error) || "选择失败"); return; }
    const ta = provDetailEl.querySelector("#mcp-f-args");
    const lines = ta.value.split("\n");
    // 替换最后一个非空行（通常就是目录/占位）；若没有就追加
    let i = lines.length - 1;
    while (i >= 0 && !lines[i].trim()) i--;
    if (i >= 0) lines[i] = res.path; else lines.push(res.path);
    ta.value = lines.join("\n");
    showToast("已填入目录：" + res.path);
  });

  provDetailEl.querySelectorAll(".mcp-row").forEach((row) => {
    const name = row.dataset.name;
    row.querySelector(".mcp-en").addEventListener("change", async (e) => {
      const res = await window.pywebview.api.toggle_mcp_server(name, e.target.checked);
      showToast(!res || !res.ok ? ((res && res.error) || "操作失败")
        : (e.target.checked ? `已启用，连上 ${res.tools} 个工具` : "已停用"));
      renderMcpPane();
    });
    row.querySelector(".mcp-del").addEventListener("click", async () => {
      await window.pywebview.api.delete_mcp_server(name);
      showToast("🗑 已删除"); renderMcpPane();
    });
    row.querySelector(".mcp-edit").addEventListener("click", () => {
      const s = servers[name] || {};
      provDetailEl.querySelector("#mcp-f-name").value = name;
      provDetailEl.querySelector("#mcp-f-cmd").value = s.command || "";
      provDetailEl.querySelector("#mcp-f-args").value = (s.args || []).join("\n");
      provDetailEl.querySelector("#mcp-f-env").value =
        Object.entries(s.env || {}).map(([k, v]) => `${k}=${v}`).join("\n");
      provDetailEl.querySelector("#mcp-f-trust").checked = !!s.trust;
    });
  });

  provDetailEl.querySelector("#mcp-save").addEventListener("click", async () => {
    const name = provDetailEl.querySelector("#mcp-f-name").value.trim();
    const command = provDetailEl.querySelector("#mcp-f-cmd").value.trim();
    if (!name || !command) { showToast("名称和启动命令都要填"); return; }
    const args = provDetailEl.querySelector("#mcp-f-args").value
      .split("\n").map((s) => s.trim()).filter(Boolean);
    const env = {};
    provDetailEl.querySelector("#mcp-f-env").value.split("\n").forEach((line) => {
      const i = line.indexOf("=");
      if (i > 0) env[line.slice(0, i).trim()] = line.slice(i + 1).trim();
    });
    const trust = provDetailEl.querySelector("#mcp-f-trust").checked;
    showToast("连接中…（首次会下载 server 包，可能要等十几秒，请稍候）");
    const res = await window.pywebview.api.save_mcp_server(name, { command, args, env, trust, enabled: true });
    if (res && res.ok) {
      showToast(res.connect_error ? `已保存，但「${name}」未连上：${res.connect_error}` : `已保存，连上 ${res.tools} 个工具`);
      renderMcpPane();
    } else showToast((res && res.error) || "保存失败");
  });
}

async function renderHooksPane() {
  // 🪝 Hooks：工具调用前/后跑自定义命令（守卫/动作），增删改即时生效（下一轮起）。
  const r = (await window.pywebview.api.get_hooks()) || {};
  const hooks = r.hooks || [];
  const esc = escapeHtml;

  let rows;
  if (!hooks.length) {
    rows = '<p class="settings-hint">还没有 hook。下面添加一个（如「写文件前扫密钥」「编辑后跑 linter」）。</p>';
  } else {
    rows = hooks.map((h, i) => {
      const ev = h.event === "PostToolUse" ? "调用后" : "调用前";
      const tag = h.event === "PostToolUse" ? "mcp-ok" : "mcp-warn";
      const title = h.name || h.command || "";
      const match = h.matcher ? `匹配 ${esc(h.matcher)}` : "匹配全部工具";
      return `<div class="mcp-row" data-i="${i}">
        <div class="mcp-main">
          <div class="mcp-name"><span class="${tag}">${ev}</span> ${esc(title)}${!h.enabled ? ' <span class="mcp-off">已停用</span>' : ''}</div>
          <div class="mcp-cmd">${match} · ${esc(h.command || "")}</div>
        </div>
        <label class="mcp-toggle" title="启用 / 停用"><input type="checkbox" class="hk-en"${h.enabled ? " checked" : ""}></label>
        <button class="ws-btn hk-edit" type="button">编辑</button>
        <button class="ws-btn hk-del" type="button">删除</button>
      </div>`;
    }).join("");
  }

  provDetailEl.innerHTML =
    '<div class="prov-d-head"><span class="prov-d-title">🪝 Hooks</span></div>' +
    '<p class="settings-hint">在工具调用<b>前</b>（PreToolUse：退出码 2=拦截 / 1=警告 / 0=放行）或<b>后</b>（PostToolUse：stdout 追加给模型）跑你的命令。命令的 stdin 收到 <code>{event,tool,params,workspace[,result]}</code> JSON，cwd=工作区。改动下一轮起生效。<br><b>matcher 匹配的是「工具名」</b>（正则）——常见工具名：<code>write_file</code>、<code>edit_file</code>、<code>run_powershell</code>/<code>run_bash</code>、<code>read_file</code>。⚠ 想拦「所有文件写入」要注意：模型可能用 <code>write_file</code>，也可能用 <code>run_powershell</code>（Set-Content/echo&gt;）写文件——这两者工具名不同。要全覆盖用 <code>write_file|edit_file|run_</code>（但 <code>run_</code> 会命中所有命令），或在命令里读 <code>params</code> 自行判断。</p>' +
    `<div class="mcp-list">${rows}</div>` +
    '<input type="hidden" id="hk-f-index" value="-1">' +
    '<div class="mcp-form">' +
      '<div class="prov-d-title" style="margin-top:10px;font-size:13px">添加 / 编辑 hook</div>' +
      '<select class="feat-input" id="hk-f-event"><option value="PreToolUse">调用前 PreToolUse（可拦截）</option><option value="PostToolUse">调用后 PostToolUse（回灌输出）</option></select>' +
      '<input class="feat-input" id="hk-f-name" placeholder="显示名（可选，如 扫密钥）">' +
      '<input class="feat-input" id="hk-f-matcher" placeholder="匹配工具名的正则（空=全部，如 write_file|edit_file 或 run_）">' +
      '<textarea class="feat-input mcp-ta" id="hk-f-cmd" rows="3" placeholder="要跑的命令（cwd=工作区，stdin 收 JSON）&#10;如：python scripts/scan_secrets.py"></textarea>' +
      '<input class="feat-input" id="hk-f-timeout" type="number" min="1" placeholder="超时秒（默认 15）">' +
      '<button class="prov-save" id="hk-save" type="button">保存</button>' +
    '</div>';

  const resetForm = () => {
    provDetailEl.querySelector("#hk-f-index").value = "-1";
    provDetailEl.querySelector("#hk-f-event").value = "PreToolUse";
    ["hk-f-name", "hk-f-matcher", "hk-f-cmd", "hk-f-timeout"].forEach((id) => { provDetailEl.querySelector("#" + id).value = ""; });
    provDetailEl.querySelector("#hk-save").textContent = "保存";
  };

  provDetailEl.querySelectorAll(".mcp-row").forEach((row) => {
    const i = parseInt(row.dataset.i, 10);
    row.querySelector(".hk-en").addEventListener("change", async (e) => {
      await window.pywebview.api.toggle_hook(i, e.target.checked);
      showToast(e.target.checked ? "已启用" : "已停用"); renderHooksPane();
    });
    row.querySelector(".hk-del").addEventListener("click", async () => {
      await window.pywebview.api.delete_hook(i);
      showToast("🗑 已删除"); renderHooksPane();
    });
    row.querySelector(".hk-edit").addEventListener("click", () => {
      const h = hooks[i];
      provDetailEl.querySelector("#hk-f-index").value = String(i);
      provDetailEl.querySelector("#hk-f-event").value = h.event || "PreToolUse";
      provDetailEl.querySelector("#hk-f-name").value = h.name || "";
      provDetailEl.querySelector("#hk-f-matcher").value = h.matcher || "";
      provDetailEl.querySelector("#hk-f-cmd").value = h.command || "";
      provDetailEl.querySelector("#hk-f-timeout").value = h.timeout || "";
      provDetailEl.querySelector("#hk-save").textContent = "保存修改";
    });
  });

  provDetailEl.querySelector("#hk-save").addEventListener("click", async () => {
    const command = provDetailEl.querySelector("#hk-f-cmd").value.trim();
    if (!command) { showToast("命令不能为空"); return; }
    const idx = parseInt(provDetailEl.querySelector("#hk-f-index").value, 10);
    const spec = {
      event: provDetailEl.querySelector("#hk-f-event").value,
      name: provDetailEl.querySelector("#hk-f-name").value.trim(),
      matcher: provDetailEl.querySelector("#hk-f-matcher").value.trim(),
      command,
      timeout: parseInt(provDetailEl.querySelector("#hk-f-timeout").value, 10) || 15,
      enabled: true,
    };
    const res = await window.pywebview.api.save_hook(idx, spec);
    if (res && res.ok) { showToast("已保存"); resetForm(); renderHooksPane(); }
    else showToast((res && res.error) || "保存失败");
  });
}

async function renderFeaturePane() {
  const f = (await window.pywebview.api.get_feature_flags()) || {};
  const row = (key, title, desc, checked) =>
    `<label class="feat-row"><input type="checkbox" class="feat-ck" data-key="${key}"${checked ? " checked" : ""}>` +
    `<span class="feat-text"><span class="feat-title">${title}</span><span class="feat-desc">${desc}</span></span></label>`;
  provDetailEl.innerHTML =
    '<div class="prov-d-head"><span class="prov-d-title">🛠 功能开关</span></div>' +
    '<p class="settings-hint">平时默认关的进阶能力，点一下即时生效、自动记住（不必改配置文件）。</p>' +
    row("auto_approve_safe", "智能确认分级（默认开）", "明显安全的只读/检视/测试命令（ls、cat、grep、git status、pytest 等）自动放行、不再逐次弹确认；写文件、装依赖、拿不准的命令仍会确认，毁灭性命令永远拦。关掉则每个命令都确认。", f.auto_approve_safe) +
    row("auto_affected_test", "改完跑定向测试" + (f.auto_affected_test_smart ? " 🤖（已按本项目自动开启）" : ""), "写/改文件后自动找「受影响的测试」跑、失败连报错即时回灌——每改一步立刻知道有没有改坏。", f.auto_affected_test) +
    row("auto_review", "收尾自动审 diff", "一轮改过文件后自动派 reviewer 子 Agent 审这次改动（每轮多一次模型调用，重要改动时开）。", f.auto_review) +
    row("auto_test", "收尾跑整套测试", "一轮改过文件后跑下面的测试命令、失败自动迭代修（需填命令；与「改完跑定向测试」可二选一）。", f.auto_test) +
    `<div class="prov-field feat-cmd"${f.auto_test ? "" : " hidden"}><div class="prov-label">测试命令</div>` +
    `<input class="feat-input" id="feat-test-command" placeholder="如 pytest -q / npm test" value="${escapeHtml(f.test_command || "")}"></div>` +
    row("delegate_grader", "委派评分回炉", "委派给子 Agent 的子任务，完成后由主模型按验收标准评分，不达标带反馈打回重做（最多 2 轮）——重型任务质量更稳，代价是多几次模型调用。", (f.delegate_max_revisions || 0) > 0);
  provDetailEl.querySelectorAll(".feat-ck").forEach((ck) => {
    ck.addEventListener("change", async () => {
      const key = ck.dataset.key;
      // 评分回炉是个伪开关：映射成 delegate_max_revisions（开=2 轮 / 关=0）
      const payload = key === "delegate_grader"
        ? { delegate_max_revisions: ck.checked ? 2 : 0 }
        : { [key]: ck.checked };
      await window.pywebview.api.set_feature_flags(payload);
      if (key === "auto_test") {
        const cmd = provDetailEl.querySelector(".feat-cmd");
        if (cmd) cmd.hidden = !ck.checked;
      }
      showToast(ck.checked ? "已开启" : "已关闭");
    });
  });
  const cmd = provDetailEl.querySelector("#feat-test-command");
  if (cmd) cmd.addEventListener("change", async () => {
    await window.pywebview.api.set_feature_flags({ test_command: cmd.value.trim() });
    showToast("测试命令已保存");
  });
}

function renderProviderDetail() {
  if (provSelected === "__browser__") { renderBrowserPane(); return; }
  if (provSelected === "__mcp__") { renderMcpPane(); return; }
  if (provSelected === "__hooks__") { renderHooksPane(); return; }
  if (provSelected === "__appearance__") { renderAppearancePane(); return; }
  if (provSelected === "__features__") { renderFeaturePane(); return; }
  const p = provData.find((x) => x.key === provSelected);
  if (!p) { provDetailEl.innerHTML = '<div class="prov-empty">选择左侧的模型服务进行配置</div>'; return; }
  const fmt = p.provider === "openai" ? "OpenAI 兼容" : "Anthropic 兼容";
  const custom = p.custom_models || [];
  const models = (p.models || []).map((mid) => {
    const on = (p.enabled_models || []).includes(mid);
    const isCustom = custom.includes(mid);
    return `<label class="pm-row"><input type="checkbox" class="pm-ck" data-mid="${escapeHtml(mid)}"${on ? " checked" : ""}>` +
      `<span class="pm-id">${escapeHtml(mid)}</span>` +
      (isCustom ? `<button class="pm-del" data-mid="${escapeHtml(mid)}" title="移除">✕</button>` : "") + "</label>";
  }).join("");
  const keyStatus = p.key_set
    ? `<span class="key-status set">已配置 ${escapeHtml(p.key_preview)}</span>`
    : '<span class="key-status unset">未配置</span>';
  provDetailEl.innerHTML =
    `<div class="prov-d-head"><span class="prov-d-title">${escapeHtml(p.label)}</span>` +
    `<span class="prov-head-right">` +
    (p.builtin ? "" : '<button class="prov-del-svc">删除服务</button>') +
    `<label class="prov-switch"><input type="checkbox" class="prov-enable"${p.enabled ? " checked" : ""}> 启用</label></span></div>` +
    `<div class="prov-field"><div class="prov-label">API Key ${keyStatus}</div>` +
    `<div class="key-edit"><input type="password" class="prov-key" placeholder="${p.key_set ? "已配置，重填可覆盖…" : "粘贴 API Key…"}">` +
    `<button class="prov-key-save key-save">保存</button></div></div>` +
    `<div class="prov-field"><div class="prov-label">API Base URL</div>` +
    `<input class="prov-url" value="${escapeHtml(p.base_url || "")}" placeholder="留空用官方默认"></div>` +
    `<div class="prov-field"><div class="prov-label">协议格式</div>` +
    `<label class="prov-fmt-opt"><input type="radio" name="prov-fmt" value="anthropic"${p.provider === "anthropic" ? " checked" : ""}> Anthropic 兼容</label>` +
    `<label class="prov-fmt-opt"><input type="radio" name="prov-fmt" value="openai"${p.provider === "openai" ? " checked" : ""}> OpenAI 兼容</label></div>` +
    `<div class="prov-field"><button class="prov-test">测试连接</button></div>` +
    `<div class="prov-models-head"><span>可用模型</span><span class="prov-models-btns">` +
    `<button class="prov-fetch">获取模型</button>` +
    `<button class="prov-add-model btn-primary btn-sm">+ 添加模型</button></span></div>` +
    `<div class="prov-models">${models || '<div class="prov-empty-sm">点「添加模型」加一个</div>'}</div>`;
  bindProviderDetail(p);
}

function bindProviderDetail(p) {
  const q = (s) => provDetailEl.querySelector(s);
  q(".prov-enable").addEventListener("change", (e) => saveProvider(p.key, { enabled: e.target.checked }));
  const keySave = async () => {
    if (!p.api_key_env) { showToast("⚠ 该服务未定义 api_key_env"); return; }
    const res = await window.pywebview.api.set_api_key(p.api_key_env, q(".prov-key").value);
    if (res && res.ok) { showToast(`✅ 已保存 ${p.label} 的 Key`); await loadProviders(); }
    else showToast("⚠ " + ((res && res.error) || "保存失败"));
  };
  q(".prov-key-save").addEventListener("click", keySave);
  q(".prov-key").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); keySave(); } });
  q(".prov-url").addEventListener("change", (e) => saveProvider(p.key, { base_url: e.target.value.trim() }));
  provDetailEl.querySelectorAll("input[name=prov-fmt]").forEach((r) => {
    r.addEventListener("change", () => { if (r.checked) saveProvider(p.key, { provider: r.value }); });
  });
  provDetailEl.querySelectorAll(".pm-ck").forEach((ck) => {
    ck.addEventListener("change", () => {
      const en = Array.from(provDetailEl.querySelectorAll(".pm-ck")).filter((x) => x.checked).map((x) => x.dataset.mid);
      saveProvider(p.key, { models: en });
    });
  });
  provDetailEl.querySelectorAll(".pm-del").forEach((b) => {
    b.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation();   // 防点击冒泡到 label 把勾选 toggle 了
      saveProvider(p.key, {
        custom_models: (p.custom_models || []).filter((m) => m !== b.dataset.mid),
        models: (p.enabled_models || []).filter((m) => m !== b.dataset.mid),
      });
    });
  });
  q(".prov-add-model").addEventListener("click", () => {
    const mid = (prompt("输入模型 ID（如 gpt-4o-mini）：") || "").trim();
    if (!mid) return;
    saveProvider(p.key, {
      custom_models: Array.from(new Set([...(p.custom_models || []), mid])),
      models: Array.from(new Set([...(p.enabled_models || []), mid])),
    });
  });
  const testBtn = q(".prov-test");
  testBtn.addEventListener("click", async () => {
    testBtn.textContent = "测试中…"; testBtn.disabled = true;
    const res = await window.pywebview.api.test_provider(p.key);
    testBtn.textContent = "测试连接"; testBtn.disabled = false;
    showToast(res && res.ok ? `✅ 连接成功（${res.model}）` : "⚠ 连接失败：" + ((res && res.error) || ""));
  });
  const fetchBtn = q(".prov-fetch");
  fetchBtn.addEventListener("click", async () => {
    fetchBtn.textContent = "获取中…"; fetchBtn.disabled = true;
    const res = await window.pywebview.api.fetch_provider_models(p.key);
    fetchBtn.textContent = "获取模型"; fetchBtn.disabled = false;
    if (res && res.ok) {
      await saveProvider(p.key, { custom_models: Array.from(new Set([...(p.custom_models || []), ...res.models])) });
      showToast(`✅ 获取到 ${res.models.length} 个模型，下面勾选启用`);
    } else showToast("⚠ " + ((res && res.error) || "获取失败"));
  });
  const delSvc = q(".prov-del-svc");
  if (delSvc) delSvc.addEventListener("click", async () => {
    if (!confirm(`删除自定义服务「${p.label}」？`)) return;
    const res = await window.pywebview.api.delete_provider(p.key);
    if (res && res.ok) { provSelected = null; showToast("🗑 已删除服务"); await loadProviders(); await refreshModelDropdowns(); }
    else showToast("⚠ " + ((res && res.error) || "删除失败"));
  });
}

async function saveProvider(key, patch) {
  const res = await window.pywebview.api.save_provider(key, patch);
  if (res && res.ok) { await loadProviders(); await refreshModelDropdowns(); }
  else showToast("⚠ " + ((res && res.error) || "保存失败"));
}

async function addCustomProvider() {
  const key = (prompt("自定义服务标识（英文，如 my-llm）：") || "").trim();
  if (!key) return;
  provSelected = key;
  await saveProvider(key, {
    enabled: true, provider: "openai",
    api_key_env: key.toUpperCase().replace(/[^A-Z0-9]/g, "_") + "_API_KEY",
    base_url: "", models: [], custom_models: [],
  });
}

settingsBtn.addEventListener("click", openSettings);
settingsClose.addEventListener("click", closeSettings);
settingsOverlay.addEventListener("click", (e) => { if (e.target === settingsOverlay) closeSettings(); });

// 首次启动：所有 key 都未配置时自动打开设置引导
async function maybePromptKeySetup() {
  try {
    const r = await window.pywebview.api.get_api_key_status();
    if (needsKeySetup((r && r.keys) || [])) {
      openSettings();
      showToast("👋 首次使用：先选一个模型服务、填入 API Key");
    }
  } catch (e) { /* 后端未就绪，忽略 */ }
}

// 重填顶部「模型 / 委派模型」两个下拉（启用模型变化后刷新；启动时也用）
async function refreshModelDropdowns() {
  const { models, active, subagent } = await window.pywebview.api.get_models();
  const fill = (sel, selected, withFollow) => {
    sel.innerHTML = "";
    if (withFollow) {
      const f = document.createElement("option");
      f.value = ""; f.textContent = "跟随主模型";
      if (!selected) f.selected = true;
      sel.appendChild(f);
    }
    models.forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name; opt.textContent = name;
      if (name === selected) opt.selected = true;
      sel.appendChild(opt);
    });
  };
  fill(modelSelect, active, false);
  fill(subagentSelect, subagent, true);
}

// ---- 会话导航索引（右缘迷你刻度条，按用户消息跳转） --------------------
const chatIndex = document.getElementById("chat-index");
let ciLabelEl = null;

// dock 式鱼眼：鼠标在 minimap 上移动时，按到光标的纵向距离放大附近刻度（越近越大），便于点击。
// 监听挂在容器上（只挂一次、不随 rebuildChatIndex 重建丢失）。
const CI_RANGE = 60;       // 影响半径(px)
const CI_MAX_SX = 1.7;     // 最大横向放大
const CI_MAX_SY = 3.4;     // 最大纵向放大（刻度很扁，纵向多放点更明显）
function ciFisheye(e) {
  const ticks = chatIndex.querySelectorAll(".ci-tick");
  let closest = null, closestF = 0;
  for (const t of ticks) {
    const r = t.getBoundingClientRect();
    const d = Math.abs(e.clientY - (r.top + r.height / 2));
    const f = Math.max(0, 1 - d / CI_RANGE);          // 1=正下方 0=超出半径
    const ease = f * f * (3 - 2 * f);                  // smoothstep，过渡更丝滑
    t.style.transform = `scale(${(1 + ease * (CI_MAX_SX - 1)).toFixed(3)}, ${(1 + ease * (CI_MAX_SY - 1)).toFixed(3)})`;
    t.style.background = ease > 0.45 ? "var(--accent)" : "";
    if (f > closestF) { closestF = f; closest = t; }
  }
  // 把鼠标最近那条刻度的「消息文字」放大弹出——移到哪条就一眼看清是哪条
  if (closest && closestF > 0.2 && closest._label) showTickLabel(closest, closest._label);
  else hideTickLabel();
}
function ciResetFisheye() {
  for (const t of chatIndex.querySelectorAll(".ci-tick")) { t.style.transform = ""; t.style.background = ""; }
  hideTickLabel();
}
if (chatIndex) {
  chatIndex.addEventListener("mousemove", ciFisheye);
  chatIndex.addEventListener("mouseleave", ciResetFisheye);
}

function rebuildChatIndex() {
  if (!chatIndex) return;
  chatIndex.innerHTML = "";
  chat.querySelectorAll(".msg.user").forEach((row) => {
    const text = (row.querySelector(".bubble")?.textContent || "").trim();
    if (!text) return;
    const tick = document.createElement("div");
    tick.className = "ci-tick";
    tick._label = text;   // 供鱼眼 mousemove 取最近刻度的消息文字放大显示
    tick.addEventListener("click", () => {
      row.scrollIntoView({ behavior: "smooth", block: "center" });
      row.classList.add("ci-flash");
      setTimeout(() => row.classList.remove("ci-flash"), 1200);
    });
    chatIndex.appendChild(tick);
  });
}

function showTickLabel(tick, text) {
  if (!ciLabelEl) {
    ciLabelEl = document.createElement("div");
    ciLabelEl.className = "ci-label";
    document.body.appendChild(ciLabelEl);
  }
  ciLabelEl.textContent = text;
  const r = tick.getBoundingClientRect();
  ciLabelEl.style.top = r.top + r.height / 2 + "px";
  ciLabelEl.style.left = r.left - 8 + "px";
  ciLabelEl.classList.add("show");
}
function hideTickLabel() {
  if (ciLabelEl) ciLabelEl.classList.remove("show");
}

// 顶部标题：显示当前项目名（打开的真实项目用文件夹名；空白会话回退 Hermes Dev）
function setTopTitle(label) {
  const el = document.getElementById("app-title");
  if (el) el.textContent = label || "Hermes Dev";
}

// ---- 右侧工作区文件预览面板（只读） ------------------------------------
const wsPanel = document.getElementById("workspace-panel");
const wsTree = document.getElementById("ws-tree");
const wsPreview = document.getElementById("ws-preview");
const wsReopen = document.getElementById("ws-reopen");
let wsCurrentPath = null; // 当前预览的文件，刷新后尽量保持
let wsRoot = null;        // 当前工作区根；变化（切换会话）时清空预览

async function refreshWorkspace() {
  if (!window.pywebview || wsPanel.classList.contains("collapsed")) return;
  const res = await window.pywebview.api.get_workspace_tree();
  if (!res || !res.ok) return;
  if (res.root !== wsRoot) {  // 工作区变了（切换/新建会话）→ 清空上个项目的预览
    wsRoot = res.root;
    wsCurrentPath = null;
    if (previewRenderOn) { previewRenderOn = false; setPreviewToggleState(); }  // 退出实时预览态
    wsPreview.innerHTML = '<div class="ws-empty">点击文件预览</div>';
  }
  const wsPath = document.getElementById("ws-path");
  if (wsPath) { wsPath.textContent = res.root || ""; wsPath.title = res.root || ""; }
  setTopTitle(res.label);
  wsTree.innerHTML = "";
  (res.tree.children || []).forEach((n) => wsTree.appendChild(renderTreeNode(n)));
  mentionFilesCid = null;  // 工作区可能变了（切换/新文件）→ @ 文件补全缓存失效，下次用时重取
  refreshChanges();
  refreshCheckpoints();
}

// ---- 检查点（FR-11.6 + P12 自动打点）：列表 + 一键回退（图标按钮，确认）-----
const wsCheckpoints = document.getElementById("ws-checkpoints");
// 回拨箭头（history/restore）：语义＝把状态倒回到这个点
const RESTORE_ICON =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v5h5"/>' +
  '<path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l3 2"/></svg>';

async function refreshCheckpoints() {
  if (!window.pywebview) return;
  let res;
  try { res = await window.pywebview.api.get_checkpoints(); } catch (e) { return; }
  const list = (res && res.checkpoints) || [];
  wsCheckpoints.innerHTML = "";
  if (!list.length) { wsCheckpoints.hidden = true; return; }   // 无检查点不占位
  wsCheckpoints.hidden = false;

  const cpCollapsed = localStorage.getItem("cpCollapsed") !== "0";  // 默认折叠，不挤压预览区
  wsCheckpoints.classList.toggle("collapsed", cpCollapsed);
  const head = document.createElement("div");
  head.className = "ws-chg-head ws-collapsible";
  const cpFold = document.createElement("span");
  cpFold.className = "ws-fold";
  cpFold.textContent = cpCollapsed ? "▸" : "▾";
  const cpTtl = document.createElement("span");
  cpTtl.textContent = `检查点 (${list.length})`;
  head.appendChild(cpFold);
  head.appendChild(cpTtl);
  head.addEventListener("click", () => {
    const now = wsCheckpoints.classList.toggle("collapsed");
    localStorage.setItem("cpCollapsed", now ? "1" : "0");
    cpFold.textContent = now ? "▸" : "▾";
  });
  wsCheckpoints.appendChild(head);

  list.forEach((c) => {
    const row = document.createElement("div");
    row.className = "ws-chg-row";
    const name = document.createElement("span");
    name.className = "ws-chg-path";
    name.textContent = `#${c.id} ${c.label}`;
    name.title = c.label;
    const back = document.createElement("button");
    back.className = "ws-restore-btn";
    back.innerHTML = RESTORE_ICON;
    back.title = "回到此处：把文件、任务清单、工作笔记一并恢复到这个检查点";
    back.setAttribute("aria-label", "回到此处");
    back.addEventListener("click", async () => {
      if (!confirm(`回到检查点「${c.label}」？\n会把改动文件、任务清单、工作笔记恢复到当时状态` +
                   `（此后的相应改动将丢失）。`)) return;
      const r = await window.pywebview.api.restore_checkpoint(c.id);
      if (r && r.ok) {
        wsCurrentPath = null;
        wsPreview.innerHTML = '<div class="ws-empty">点击文件预览</div>';
        refreshWorkspace();
        refreshTasks();
      } else {
        alert((r && r.error) || "回退失败");
      }
    });
    row.appendChild(name);
    row.appendChild(back);
    wsCheckpoints.appendChild(row);
  });
}

// ---- 改动评审与回退（FR-9.4a）------------------------------------------
const wsChanges = document.getElementById("ws-changes");
const CHANGE_MARK = { added: "＋", modified: "✎", deleted: "🗑" };

async function refreshChanges() {
  if (!window.pywebview) return;
  let res;
  try { res = await window.pywebview.api.get_changes(); } catch (e) { return; }
  const changes = (res && res.changes) || [];
  // FR-10.1：git 工作区显示全部未提交改动（跨重启、含用户手改），回退=丢弃到最近一次提交；
  // 非 git 工作区沿用内存台账（仅本对话改动）。
  const gitMode = !!(res && res.mode === "git");
  wsChanges.innerHTML = "";
  if (!changes.length) { wsChanges.hidden = true; return; }
  wsChanges.hidden = false;

  const chgCollapsed = localStorage.getItem("chgCollapsed") !== "0";  // 默认折叠，不挤压预览区
  wsChanges.classList.toggle("collapsed", chgCollapsed);
  const head = document.createElement("div");
  head.className = "ws-chg-head ws-collapsible";
  const chgFold = document.createElement("span");
  chgFold.className = "ws-fold";
  chgFold.textContent = chgCollapsed ? "▸" : "▾";
  const title = document.createElement("span");
  title.textContent = gitMode ? `未提交改动·git (${changes.length})` : `改动 (${changes.length})`;
  const revertAll = document.createElement("button");
  revertAll.className = "ws-btn";
  revertAll.textContent = "全部回退";
  revertAll.addEventListener("click", async (e) => {
    e.stopPropagation();   // 点按钮不触发 head 折叠
    const msg = gitMode
      ? `丢弃全部 ${changes.length} 处未提交改动？文件将恢复到最近一次提交（含非本对话的改动，新增/未跟踪文件会被删除）。`
      : `回退全部 ${changes.length} 处改动？文件将恢复到本对话修改前的状态。`;
    if (!confirm(msg)) return;
    await window.pywebview.api.revert_all_changes();
    wsCurrentPath = null;
    wsPreview.innerHTML = '<div class="ws-empty">点击文件预览</div>';
    refreshWorkspace();
  });
  head.addEventListener("click", () => {
    const now = wsChanges.classList.toggle("collapsed");
    localStorage.setItem("chgCollapsed", now ? "1" : "0");
    chgFold.textContent = now ? "▸" : "▾";
  });
  head.appendChild(chgFold);
  head.appendChild(title);
  head.appendChild(revertAll);
  wsChanges.appendChild(head);

  changes.forEach((c) => {
    const row = document.createElement("div");
    row.className = "ws-chg-row " + c.status;
    const mark = document.createElement("span");
    mark.className = "ws-chg-mark";
    mark.textContent = CHANGE_MARK[c.status] || "✎";
    mark.title = c.status;
    const name = document.createElement("span");
    name.className = "ws-chg-path";
    name.textContent = c.path;
    name.title = c.path + "（点击看 diff）";
    name.addEventListener("click", () => previewDiff(c.path));
    const undo = document.createElement("button");
    undo.className = "ws-btn";
    undo.textContent = "回退";
    undo.title = gitMode ? "丢弃该文件的未提交改动（恢复到最近一次提交）" : "恢复到本对话修改前";
    undo.addEventListener("click", async (e) => {
      e.stopPropagation();
      const msg = gitMode
        ? `丢弃 ${c.path} 的未提交改动？将恢复到最近一次提交（新增文件则删除）。`
        : `回退 ${c.path} 的改动？`;
      if (!confirm(msg)) return;
      await window.pywebview.api.revert_file(c.path);
      if (wsCurrentPath === c.path) {
        wsCurrentPath = null;
        wsPreview.innerHTML = '<div class="ws-empty">点击文件预览</div>';
      }
      refreshWorkspace();
    });
    row.appendChild(mark);
    row.appendChild(name);
    row.appendChild(undo);
    wsChanges.appendChild(row);
  });
}

async function previewDiff(path) {
  if (previewRenderOn) { previewRenderOn = false; setPreviewToggleState(); }  // 看 diff 退出实时预览态
  const res = await window.pywebview.api.get_file_diff(path);
  wsPreview.innerHTML = "";
  const head = document.createElement("div");
  head.className = "ws-pv-head";
  head.innerHTML = `<span class="ws-pv-name">diff: ${escapeHtml(path)}</span>`;
  wsPreview.appendChild(head);
  if (!res || !res.ok) {
    wsPreview.appendChild(Object.assign(document.createElement("div"),
      { className: "ws-empty", textContent: (res && res.error) || "无差异" }));
    return;
  }
  const pre = document.createElement("pre");
  pre.className = "ws-pv-code ws-diff";
  res.diff.split("\n").forEach((line) => {
    const span = document.createElement("span");
    span.className = "diff-line" +
      (line.startsWith("+") && !line.startsWith("+++") ? " add" :
       line.startsWith("-") && !line.startsWith("---") ? " del" :
       line.startsWith("@@") ? " hunk" :
       (line.startsWith("+++") || line.startsWith("---")) ? " meta" : "");
    span.textContent = line + "\n";
    pre.appendChild(span);
  });
  wsPreview.appendChild(pre);
}

function renderTreeNode(node) {
  if (node.type === "dir") {
    const details = document.createElement("details");
    details.className = "ws-dir";
    const summary = document.createElement("summary");
    summary.innerHTML = `<span class="ws-icon">📁</span>${escapeHtml(node.name)}`;
    details.appendChild(summary);
    (node.children || []).forEach((c) => details.appendChild(renderTreeNode(c)));
    return details;
  }
  const el = document.createElement("div");
  el.className = "ws-file";
  if (node.path === wsCurrentPath) el.classList.add("active");
  el.innerHTML = `<span class="ws-icon">${fileIcon(node.name)}</span>` +
    `<span class="ws-fname">${escapeHtml(node.name)}</span>`;
  el.addEventListener("click", () => previewFile(node.path));
  return el;
}

function fileIcon(name) {
  const e = name.slice(name.lastIndexOf(".")).toLowerCase();
  if ([".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"].includes(e)) return "🖼️";
  if ([".html", ".htm"].includes(e)) return "🌐";
  if ([".md"].includes(e)) return "📝";
  return "📄";
}

async function previewFile(path) {
  if (previewRenderOn) { previewRenderOn = false; setPreviewToggleState(); }  // 点文件退出实时预览态
  wsCurrentPath = path;
  wsTree.querySelectorAll(".ws-file").forEach((f) => f.classList.remove("active"));
  const res = await window.pywebview.api.read_workspace_file(path);
  wsPreview.innerHTML = "";
  if (!res || !res.ok) {
    wsPreview.innerHTML = `<div class="ws-empty">无法预览：${escapeHtml((res && res.error) || "未知错误")}</div>`;
    return;
  }
  const head = document.createElement("div");
  head.className = "ws-pv-head";
  head.innerHTML = `<span class="ws-pv-name">${escapeHtml(res.name)}</span>` +
    `<span class="ws-pv-size">${fmtSize(res.size)}</span>`;
  const refresh = document.createElement("button");   // 文件预览也给个刷新按钮（与实时预览一致，不必回树里重新点）
  refresh.className = "ws-btn ws-pv-refresh"; refresh.type = "button";
  refresh.textContent = "↻"; refresh.title = "刷新（重读此文件）";
  refresh.addEventListener("click", () => previewFile(path));
  head.appendChild(refresh);
  wsPreview.appendChild(head);

  if (res.kind === "image") {
    const img = document.createElement("img");
    img.className = "ws-pv-image";
    img.src = res.dataUrl;
    wsPreview.appendChild(img);
  } else if (res.kind === "html") {
    renderHtmlPreview(res, path, head);
  } else if (res.kind === "text") {
    wsPreview.appendChild(makeCodeBlock(res.text, res.truncated));
  } else if (res.kind === "binary") {
    const d = document.createElement("div");
    d.className = "ws-empty";
    d.textContent = "二进制文件，不支持预览。";
    wsPreview.appendChild(d);
  } else {
    wsPreview.appendChild(Object.assign(document.createElement("div"),
      { className: "ws-empty", textContent: res.error || "无法预览" }));
  }
}

function renderHtmlPreview(res, path, head) {
  // HTML：默认 iframe 渲染；可切「源码」；可在浏览器打开
  const btns = document.createElement("span");
  btns.className = "ws-pv-tabs";
  const open = document.createElement("button");
  open.className = "ws-btn"; open.textContent = "在浏览器打开";
  open.addEventListener("click", () => window.pywebview.api.open_workspace_file(path));
  const toggle = document.createElement("button");
  toggle.className = "ws-btn"; toggle.textContent = "源码";
  btns.appendChild(toggle); btns.appendChild(open);
  head.appendChild(btns);

  const body = document.createElement("div");
  body.className = "ws-pv-body";
  const iframe = document.createElement("iframe");
  iframe.className = "ws-pv-iframe";
  iframe.setAttribute("sandbox", "allow-scripts"); // 可跑原型 JS，但隔离于主程序
  iframe.srcdoc = injectScrollbarCss(res.text); // 让 iframe 内滚动条也用主题色
  const code = makeCodeBlock(res.text, res.truncated);
  code.style.display = "none";
  body.appendChild(iframe);
  body.appendChild(code);
  wsPreview.appendChild(body);

  let showingCode = false;
  toggle.addEventListener("click", () => {
    showingCode = !showingCode;
    iframe.style.display = showingCode ? "none" : "block";
    code.style.display = showingCode ? "block" : "none";
    toggle.textContent = showingCode ? "预览" : "源码";
  });
}

// 把暗色滚动条样式注入 iframe 的 <head>（不放到 DOCTYPE 前，避免触发 quirks 模式影响原型渲染）
function injectScrollbarCss(html) {
  const css = "<style>*::-webkit-scrollbar{width:10px;height:10px}" +
    "*::-webkit-scrollbar-track{background:transparent}" +
    "*::-webkit-scrollbar-thumb{background:#3a3a46;border-radius:8px;" +
    "border:2px solid transparent;background-clip:padding-box}" +
    "*::-webkit-scrollbar-thumb:hover{background:#9a9aa8}</style>";
  if (/<\/head>/i.test(html)) return html.replace(/<\/head>/i, css + "</head>");
  if (/<body[^>]*>/i.test(html)) return html.replace(/<body[^>]*>/i, (m) => m + css);
  return css + html; // 兜底（无 head/body 的片段）
}

function makeCodeBlock(text, truncated) {
  const pre = document.createElement("pre");
  pre.className = "ws-pv-code";
  const code = document.createElement("code");
  code.textContent = text + (truncated ? "\n\n…（文件较大，仅预览前部分）" : "");
  pre.appendChild(code);
  if (window.hljs) { try { window.hljs.highlightElement(code); } catch (e) {} }
  return pre;
}

function fmtSize(n) {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

function setWorkspaceCollapsed(collapsed) {
  wsPanel.classList.toggle("collapsed", collapsed);
  wsReopen.hidden = !collapsed;
  if (dragRight) dragRight.classList.toggle("hidden", collapsed);  // 面板收起时右分隔条也隐藏
  localStorage.setItem("wsCollapsed", collapsed ? "1" : "0");
  if (!collapsed) refreshWorkspace();
}

document.getElementById("ws-collapse").addEventListener("click", () => setWorkspaceCollapsed(true));
wsReopen.addEventListener("click", () => setWorkspaceCollapsed(false));

// ---- 实时预览面板（UX Tier1-②）：在 ws-preview 里 iframe 渲染运行中的 dev server / 页面 ----
// 自动对准当前会话后台 dev server 的本地 URL（从进程输出识别）；遇到禁止内嵌的站点（如 Django
// 默认 X-Frame-Options:DENY）用「在浏览器打开」兜底。点文件 / 切换会话会自动退出预览态。
let previewRenderOn = false;
let lastPreviewUrl = localStorage.getItem("lastPreviewUrl") || "";

function setPreviewToggleState() {
  const btn = document.getElementById("ws-preview-toggle");
  if (btn) btn.classList.toggle("active", previewRenderOn);
  wsPreview.classList.toggle("previewing", previewRenderOn);  // 集中管理预览态布局类
}

async function enterLivePreview() {
  previewRenderOn = true;
  setPreviewToggleState();
  let targets = [];
  try {
    const r = await window.pywebview.api.get_preview_urls();
    targets = (r && r.targets) || [];
  } catch (e) { /* 无后台 server 也没关系，手动填 URL */ }
  const def = lastPreviewUrl || (targets[0] && targets[0].url) || "http://localhost:3000";

  wsPreview.innerHTML = "";
  const bar = document.createElement("div");
  bar.className = "preview-bar";

  const frame = document.createElement("iframe");
  frame.className = "preview-frame";
  frame.setAttribute("sandbox", "allow-scripts allow-forms allow-same-origin allow-popups allow-modals");

  const input = document.createElement("input");
  input.className = "preview-url";
  input.type = "text";
  input.placeholder = "http://localhost:3000";
  input.value = def;

  const load = () => {
    const u = input.value.trim();
    if (!u) return;
    lastPreviewUrl = u;
    localStorage.setItem("lastPreviewUrl", u);
    frame.src = u;
  };

  // 检测到的 dev server：用 <select> 列出**全部**（datalist 下拉会按输入框内容过滤、把其它端口藏掉，故不用）
  if (targets.length) {
    const pick = document.createElement("select");
    pick.className = "preview-pick";
    pick.title = "检测到的后台 dev server，选一个预览";
    targets.forEach((t) => {
      const o = document.createElement("option");
      o.value = t.url;
      o.textContent = t.url.replace(/^https?:\/\//, "") + (t.command ? "  ·  " + t.command : "");
      pick.appendChild(o);
    });
    const custom = document.createElement("option");
    custom.value = "__custom__"; custom.textContent = "自定义 URL…";
    pick.appendChild(custom);
    pick.value = targets.some((t) => t.url === def) ? def : "__custom__";
    pick.addEventListener("change", () => {
      if (pick.value === "__custom__") { input.focus(); return; }
      input.value = pick.value; load();
    });
    bar.appendChild(pick);   // CSS 给它整行宽
  }

  const mkBtn = (label, title, fn) => {
    const b = document.createElement("button");
    b.className = "preview-btn"; b.type = "button"; b.textContent = label; b.title = title;
    b.addEventListener("click", fn); return b;
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") load(); });

  bar.appendChild(input);
  bar.appendChild(mkBtn("↻", "刷新预览", load));
  bar.appendChild(mkBtn("⤴", "在系统浏览器打开（遇到禁止内嵌的站点用它）", () => {
    const u = input.value.trim();
    if (u) window.pywebview.api.open_external(u);
  }));
  bar.appendChild(mkBtn("✕", "关闭预览，回到文件视图", exitLivePreview));
  wsPreview.appendChild(bar);
  wsPreview.appendChild(frame);
  load();
}

function exitLivePreview() {
  previewRenderOn = false;
  setPreviewToggleState();
  wsPreview.innerHTML = '<div class="ws-empty">点击文件预览</div>';
}

document.getElementById("ws-preview-toggle").addEventListener("click", () => {
  if (previewRenderOn) exitLivePreview(); else enterLivePreview();
});

// ---- 三栏宽度可拖拽（P3）：拖分隔条改 flex-basis + localStorage 记忆 + clamp 上下限 ----
const sidebarEl = document.querySelector(".sidebar");
const dragLeft = document.getElementById("drag-left");
const dragRight = document.getElementById("drag-right");
const SIDEBAR_MIN = 180, SIDEBAR_MAX = 460;
const WORKSPACE_MIN = 220, WORKSPACE_MAX = 620;

function applyPanelWidths() {
  const sw = localStorage.getItem("sidebarW");
  if (sw) sidebarEl.style.flexBasis = clampWidth(sw, SIDEBAR_MIN, SIDEBAR_MAX) + "px";
  const ww = localStorage.getItem("workspaceW");
  if (ww) wsPanel.style.flexBasis = clampWidth(ww, WORKSPACE_MIN, WORKSPACE_MAX) + "px";
}

function makeDraggable(handle, compute, apply, storeKey) {
  if (!handle) return;
  handle.addEventListener("mousedown", (e) => {
    e.preventDefault();
    handle.classList.add("dragging");
    document.body.classList.add("col-resizing");
    const onMove = (ev) => {
      const w = compute(ev.clientX);
      apply(w);
      localStorage.setItem(storeKey, String(w));
    };
    const onUp = () => {
      handle.classList.remove("dragging");
      document.body.classList.remove("col-resizing");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}
makeDraggable(dragLeft,
  (x) => clampWidth(x, SIDEBAR_MIN, SIDEBAR_MAX),
  (w) => { sidebarEl.style.flexBasis = w + "px"; }, "sidebarW");
makeDraggable(dragRight,
  (x) => clampWidth(window.innerWidth - x, WORKSPACE_MIN, WORKSPACE_MAX),
  (w) => { wsPanel.style.flexBasis = w + "px"; }, "workspaceW");
applyPanelWidths();

// ---- 外链拦截（FR-11.1 反馈修复）----------------------------------------
// 对话/面板里的 <a> 链接若直接点击，WebView 会把整个应用窗口导航到外站且无返回。
// 统一拦截：http(s) 链接交给系统默认浏览器打开（窗口不动）；其余非锚点链接一律阻止。
document.addEventListener("click", (e) => {
  const a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
  if (!a) return;
  const href = a.getAttribute("href") || "";
  if (/^https?:\/\//i.test(href)) {
    e.preventDefault();
    if (window.pywebview) window.pywebview.api.open_external(href);
  } else if (!href.startsWith("#")) {
    e.preventDefault();  // javascript:/file:/相对路径等：一律不让 WebView 导航
  }
});

// ---- 初始化（等 pywebview 就绪后拉模型列表） ----------------------------
window.addEventListener("pywebviewready", async () => {
  const api = window.pywebview.api;
  const clog = (m) => { try { api.client_log(m); } catch (e) { /* ignore */ } };
  // performance.now() = 自页面导航开始的毫秒数：含 HTML 加载+脚本解析+WebView2 建桥
  clog(`导航开始→pywebviewready: ${Math.round(performance.now())}ms`);

  let t = performance.now();
  await refreshModelDropdowns();
  clog(`get_models + 填下拉: ${Math.round(performance.now() - t)}ms`);

  t = performance.now();
  await refreshSessions();   // 内部会为初始活动对话建视图并挂载
  clog(`refreshSessions: ${Math.round(performance.now() - t)}ms`);
  setWorkspaceCollapsed(localStorage.getItem("wsCollapsed") === "1");
  refreshWorkspace();

  // 桥就绪：开放输入
  input.disabled = false;
  restoreInputState();
  input.focus();
  maybePromptKeySetup();  // 首次无 key 自动引导去设置面板
  maybeResumeBrowserInstall();  // 浏览器穿透上次关窗中断 → 开机静默续装（不必再点、不从零重下）
  clog(`初始化完成（总计自导航 ${Math.round(performance.now())}ms）`);
});

// 浏览器穿透：若「已启用但没连上」（多半是上次安装被关窗中断），后台续装。
// install-browser 幂等：已装的秒过、没装完的续上——不会从零重下，也不丢启用状态。
async function maybeResumeBrowserInstall() {
  try {
    const s = await window.pywebview.api.get_browser_mcp_status();
    if (s && s.enabled && !s.connected && s.node && !window.__brResuming) {
      window.__brResuming = true;
      showToast("浏览器穿透上次没装完，正在后台继续…");
      window.pywebview.api.set_browser_mcp(true).finally(() => { window.__brResuming = false; });
    }
  } catch (e) { /* ignore */ }
}
