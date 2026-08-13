// 「在浏览器打开」失败提示的纯逻辑。运行：node --test tests/web/
// 背景：真机 bug（2026-08-13）——打开已有项目后点「在浏览器打开」没反应**也没报错**，
// 因为前端完全忽略了后端返回值。现在失败要提示原因 + 绝对路径（用户至少能自己去打开）。
const test = require("node:test");
const assert = require("node:assert");
const { formatOpenFileError } = require("../../web/pure.js");

test("带上原因与绝对路径", () => {
  assert.equal(
    formatOpenFileError({ ok: false, error: "打不开：系统未注册可用的默认浏览器", path: "C:\\proj\\a.html" }),
    "打不开：系统未注册可用的默认浏览器。文件在：C:\\proj\\a.html");
});

test("没有路径时只说原因", () => {
  assert.equal(formatOpenFileError({ ok: false, error: "文件不存在" }), "文件不存在");
});

test("原因缺失/全空白也要有话可说，不能弹空 toast", () => {
  assert.equal(formatOpenFileError({ ok: false }), "未知原因");
  assert.equal(formatOpenFileError({ ok: false, error: "   " }), "未知原因");
  assert.equal(formatOpenFileError(null), "未知原因");
  assert.equal(formatOpenFileError(undefined), "未知原因");
});

test("含中文/空格的真实路径原样带出（不做任何编码——编码正是这个 bug 的根因）", () => {
  const p = "C:\\Users\\张三\\我的项目\\index.html";
  const msg = formatOpenFileError({ ok: false, error: "打不开：拒绝访问", path: p });
  assert.ok(msg.includes(p));
  assert.ok(!msg.includes("%"));
});
