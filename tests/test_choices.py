"""枚举型设置（GUI 下拉 → choices.json）自检 + 「联网检索」面板后端。

不联网、不用 key。这一层要守住的是**面板显示的和实际生效的必须一致**：
"配了 primary 却没 key、于是一直走 bing 还查不出原因"是真实踩过的坑（2026-08-21），
所以 get_web_search 必须回 `effective`（没 key/配额用尽一律 off），且这里有测试钉住。

运行：python tests/test_choices.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentcore.config import (  # noqa: E402
    CHOICES_SPEC, _CHOICES_BY_KEY, _coerce_choice, merge_choices, read_choices, set_choices,
)
from agentcore.tools.web import FIRECRAWL_KEY_ENV, FIRECRAWL_MODES  # noqa: E402


def _tmp():
    return Path(tempfile.mkdtemp()) / "choices.json"


# ---- 规格自身 ----------------------------------------------------------------

def test_spec_shape():
    """每条 spec 都要能驱动前端渲染：key/label/hint/options 齐全，选项有 value+label+desc。"""
    for s in CHOICES_SPEC:
        assert "." in s["key"], s["key"]           # section.field 点分路径
        assert s["label"] and s["hint"], s["key"]
        assert s["options"], s["key"]
        for o in s["options"]:
            assert o["value"] and o["label"] and o["desc"], (s["key"], o)


def test_firecrawl_options_match_backend():
    """下拉候选必须与 web.py 的 FIRECRAWL_MODES 一一对应——
    面板给得出、后端不认（或反之）就是个只在真机才现形的坑。"""
    vals = [o["value"] for o in _CHOICES_BY_KEY["web.firecrawl"]["options"]]
    assert tuple(vals) == FIRECRAWL_MODES, (vals, FIRECRAWL_MODES)


# ---- 读 / 写 / 合并 ----------------------------------------------------------

def test_read_missing_and_corrupt():
    p = _tmp()
    assert read_choices(p) == {}          # 不存在
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json", encoding="utf-8")
    assert read_choices(p) == {}          # 坏档不炸，回空
    p.write_text('["a"]', encoding="utf-8")
    assert read_choices(p) == {}          # 类型不对也回空


def test_set_valid_and_roundtrip():
    p = _tmp()
    assert set_choices({"web.firecrawl": "always"}, p) == {"web.firecrawl": "always"}
    assert read_choices(p) == {"web.firecrawl": "always"}
    assert json.loads(p.read_text(encoding="utf-8")) == {"web.firecrawl": "always"}


def test_set_rejects_bad_value_and_unknown_key():
    """脏值绝不落盘：非法档位与白名单外的 key 都跳过，已有值保持不变。"""
    p = _tmp()
    set_choices({"web.firecrawl": "primary"}, p)
    assert set_choices({"web.firecrawl": "nonsense"}, p) == {"web.firecrawl": "primary"}
    assert set_choices({"web.bogus": "x"}, p) == {"web.firecrawl": "primary"}
    assert "web.bogus" not in read_choices(p)


def test_coerce_normalizes_case_and_space():
    assert _coerce_choice(_CHOICES_BY_KEY["web.firecrawl"], "  PRIMARY ") == "primary"
    assert _coerce_choice(_CHOICES_BY_KEY["web.firecrawl"], "") is None
    assert _coerce_choice(_CHOICES_BY_KEY["web.firecrawl"], None) is None


def test_merge_overrides_only_target_field():
    """覆盖到对应段，但同段其它字段（timeout 等）原样保留。"""
    p = _tmp()
    set_choices({"web.firecrawl": "off"}, p)
    data = merge_choices({"web": {"firecrawl": "primary", "timeout": 20}, "agent": {"x": 1}}, p)
    assert data["web"] == {"firecrawl": "off", "timeout": 20}
    assert data["agent"] == {"x": 1}


def test_merge_noop_without_file():
    p = _tmp()
    data = {"web": {"firecrawl": "primary"}}
    assert merge_choices(dict(data), p) == data


def test_merge_skips_corrupt_value():
    """手改坏了 choices.json 里的值 → 忽略它、回落 config.yaml，别把脏值灌进 config。"""
    p = _tmp()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"web.firecrawl": "nonsense", "nope": "x"}), encoding="utf-8")
    assert merge_choices({"web": {"firecrawl": "primary"}}, p) == {"web": {"firecrawl": "primary"}}


# ---- 「联网检索」面板后端（不起 GUI，直接调 Api 的两个方法）--------------------

class _FakeWeb:
    def __init__(self, mode="primary"):
        self.firecrawl = mode


class _FakeApi:
    """只装配 get_web_search / set_web_search 真正用到的那点状态，不起整个 Api。"""

    def __init__(self, mode="primary", choices_path=None):
        from agentcore.bridge.api import Api
        self.config = type("C", (), {"web": _FakeWeb(mode)})()
        self.get_web_search = Api.get_web_search.__get__(self)
        self.set_web_search = Api.set_web_search.__get__(self)
        self.rebuilt = 0

    def _rebuild_registries(self):
        self.rebuilt += 1


class _NoKey:
    """临时清掉环境里的 key（本机 .env 里有真 key，测试绝不能依赖它）。"""

    def __enter__(self):
        self._old = os.environ.pop(FIRECRAWL_KEY_ENV, None)
        return self

    def __exit__(self, *a):
        if self._old is not None:
            os.environ[FIRECRAWL_KEY_ENV] = self._old
        else:
            os.environ.pop(FIRECRAWL_KEY_ENV, None)


def test_pane_effective_off_without_key():
    """本次踩坑的正主：档位写着 primary、但没 key → effective 必须是 off。"""
    with _NoKey():
        s = _FakeApi("primary").get_web_search()
    assert s["ok"] and s["mode"] == "primary"
    assert s["effective"] == "off"
    assert s["key_set"] is False and s["preview"] == ""
    assert s["env"] == FIRECRAWL_KEY_ENV
    assert [o["value"] for o in s["options"]] == list(FIRECRAWL_MODES)


def test_pane_never_leaks_plaintext_key():
    """面板只回掩码，绝不回明文——设置面板的既有立场，这里也守住。"""
    os.environ[FIRECRAWL_KEY_ENV] = "fc-supersecrettoken"
    try:
        s = _FakeApi("primary").get_web_search()
    finally:
        os.environ.pop(FIRECRAWL_KEY_ENV, None)
    assert s["key_set"] is True and s["effective"] == "primary"
    assert "supersecret" not in json.dumps(s, ensure_ascii=False)
    assert s["preview"] == "fc-s…oken"


def test_pane_effective_off_when_quota_exhausted():
    """配额用尽已退回免 key 链路，面板不该还显示「primary 生效中」。"""
    from agentcore.tools.web import mark_firecrawl_exhausted, reset_firecrawl_quota
    os.environ[FIRECRAWL_KEY_ENV] = "fc-x"
    mark_firecrawl_exhausted("HTTP 402")
    try:
        s = _FakeApi("primary").get_web_search()
    finally:
        reset_firecrawl_quota()
        os.environ.pop(FIRECRAWL_KEY_ENV, None)
    assert s["quota_exhausted"] and s["effective"] == "off"


def test_set_mode_persists_and_rebuilds():
    """改档位：落盘 + 改活动 config + 重建工具注册表（不重建的话下一次搜索还是老档）。"""
    import agentcore.config as cfg
    old_app = cfg.APP_DIR
    cfg.APP_DIR = Path(tempfile.mkdtemp())
    try:
        api = _FakeApi("primary")
        with _NoKey():
            r = api.set_web_search({"mode": "off"})
        assert r["ok"] and r["mode"] == "off"
        assert api.config.web.firecrawl == "off"      # 活动 config 即时生效
        assert api.rebuilt == 1                       # 注册表重建过
        assert read_choices(cfg.APP_DIR / "choices.json") == {"web.firecrawl": "off"}
    finally:
        cfg.APP_DIR = old_app


def test_set_mode_rejects_unknown():
    api = _FakeApi("primary")
    r = api.set_web_search({"mode": "turbo"})
    assert r["ok"] is False and "turbo" in r["error"]
    assert api.config.web.firecrawl == "primary"      # 没被改坏
    assert api.rebuilt == 0                           # 也没白重建


def test_set_key_writes_env_and_resets_quota():
    """填 key：写进 .env + 即时生效 + 清掉「配额用尽」粘滞标记（换 key 正是来这一页的主因之一）。"""
    import agentcore.config as cfg
    import agentcore.bridge.api as api_mod
    from agentcore.bridge.api import Api
    from agentcore.tools.web import firecrawl_quota_exhausted, mark_firecrawl_exhausted
    tmp = Path(tempfile.mkdtemp())
    (tmp / ".env").write_text("DEEPSEEK_API_KEY=sk-keep\n", encoding="utf-8")
    old_app, old_api_app = cfg.APP_DIR, api_mod.APP_DIR
    cfg.APP_DIR = api_mod.APP_DIR = tmp
    old_key = os.environ.pop(FIRECRAWL_KEY_ENV, None)
    mark_firecrawl_exhausted("HTTP 402")
    try:
        api = _FakeApi("primary")
        api.set_api_key = Api.set_api_key.__get__(api)
        r = api.set_web_search({"key": "fc-newkey12345"})
        assert r["ok"] and r["key_set"] is True and r["effective"] == "primary"
        assert not firecrawl_quota_exhausted()        # 粘滞标记已清
        text = (tmp / ".env").read_text(encoding="utf-8")
        assert "FIRECRAWL_API_KEY=fc-newkey12345" in text
        assert "DEEPSEEK_API_KEY=sk-keep" in text     # 别的 key 原样保留
        assert os.environ[FIRECRAWL_KEY_ENV] == "fc-newkey12345"   # 即时生效，不用重启
    finally:
        cfg.APP_DIR, api_mod.APP_DIR = old_app, old_api_app
        os.environ.pop(FIRECRAWL_KEY_ENV, None)
        if old_key is not None:
            os.environ[FIRECRAWL_KEY_ENV] = old_key


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
