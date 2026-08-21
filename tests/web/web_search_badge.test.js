// 「🔍 联网检索」导航徽标的纯逻辑。运行：node --test tests/web/
//
// 这个徽标报的是**实际生效档位**而不是配置里写的那个：
// "配了 primary 但没 key，于是搜索一直走 bing" 是真实踩过的坑（2026-08-21），
// 左栏直接喊「缺 key」才省得点进去逐项对账。
const test = require("node:test");
const assert = require("node:assert");
const { webSearchNavBadge, SETTINGS_PANES, buildSettingsNav } = require("../../web/pure.js");

test("后端没回 / 回失败 → 不显示徽标", () => {
  assert.equal(webSearchNavBadge(null), null);
  assert.equal(webSearchNavBadge(undefined), null);
  assert.equal(webSearchNavBadge({ ok: false }), null);
});

test("off 档＝默认态，不用喊", () => {
  assert.equal(webSearchNavBadge({ ok: true, mode: "off", key_set: false }), null);
  assert.equal(webSearchNavBadge({ ok: true, mode: "off", key_set: true }), null);
});

test("开了档位却没 key → 警告「缺 key」（本次踩坑的正主）", () => {
  assert.deepEqual(webSearchNavBadge({ ok: true, mode: "primary", key_set: false }),
    { text: "缺 key", tone: "warn" });
  assert.deepEqual(webSearchNavBadge({ ok: true, mode: "always", key_set: false }),
    { text: "缺 key", tone: "warn" });
});

test("配额用尽 → 警告，且压过「已生效」", () => {
  assert.deepEqual(
    webSearchNavBadge({ ok: true, mode: "primary", key_set: true, quota_exhausted: "HTTP 402" }),
    { text: "配额用尽", tone: "warn" });
});

test("key 与档位都齐 → 绿灯显示当前档位名", () => {
  assert.deepEqual(webSearchNavBadge({ ok: true, mode: "primary", key_set: true }),
    { text: "primary", tone: "ok" });
  assert.deepEqual(webSearchNavBadge({ ok: true, mode: "fallback", key_set: true, quota_exhausted: "" }),
    { text: "fallback", tone: "ok" });
});

test("面板注册进「扩展能力」组，且徽标能挂上去", () => {
  const pane = SETTINGS_PANES.find((p) => p.key === "__websearch__");
  assert.ok(pane, "SETTINGS_PANES 里应有 __websearch__");
  assert.equal(pane.group, "capabilities");
  const nav = buildSettingsNav([], { __websearch__: { text: "缺 key", tone: "warn" } });
  const cap = nav.find((g) => g.id === "capabilities");
  const row = cap.items.find((i) => i.key === "__websearch__");
  assert.deepEqual(row.badge, { text: "缺 key", tone: "warn" });
  assert.equal(row.kind, "pane");
});
