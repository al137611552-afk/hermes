"""评审「开始编码」按 cid 路由自测。

回归 bug：点开始编码后主模型重排耗时里用户切到别的对话，开工指令/落规划本会发到
当前活动对话（新对话），而不是发起时的原对话。修复：Api 的这几个终态动作接受可选 cid，
按 cid 路由到发起对话（未给/失效退回活动对话，兼容），仿 resolve_permission 同类修复。

运行：python tests/test_review_cid_routing.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.bridge.api import Api  # noqa: E402


class FakeConv:
    def __init__(self, cid: int):
        self.cid = cid
        self.calls: list = []

    def enqueue(self, text, attachments=None):
        self.calls.append(("enqueue", text)); return {"ok": True, "cid": self.cid}

    def apply_review_to_plan(self):
        self.calls.append(("apply",)); return {"ok": True, "cid": self.cid}

    def set_plan_mode(self, on):
        self.calls.append(("plan", on)); return bool(on)

    def can_start_coding(self):
        self.calls.append(("gate",)); return True


def _fake_api(active_cid=1):
    """构造一个只带 conversations/active 的假 self，够跑这几个路由方法。"""
    a = FakeConv(active_cid)
    b = FakeConv(active_cid + 1)
    self = SimpleNamespace(active=a, conversations={a.cid: a, b.cid: b})
    self._conv_by_cid = lambda cid, _s=self: Api._conv_by_cid(_s, cid)  # 供 wrapper 方法调用
    return self, a, b


def test_conv_by_cid_none_returns_active():
    self, a, b = _fake_api()
    assert Api._conv_by_cid(self, None) is a


def test_conv_by_cid_routes_to_target():
    self, a, b = _fake_api()
    assert Api._conv_by_cid(self, b.cid) is b


def test_conv_by_cid_stale_falls_back_to_active():
    self, a, b = _fake_api()
    assert Api._conv_by_cid(self, 999) is a   # 已失效的 cid → 退回活动，不崩


def test_send_message_routes_to_origin_not_active():
    self, a, b = _fake_api()
    # 活动是 a，但发起对话是 b：带 b.cid 应发到 b，不发到 a
    Api.send_message(self, "开工", None, b.cid)
    assert ("enqueue", "开工") in b.calls and a.calls == []


def test_apply_review_to_plan_routes_by_cid():
    self, a, b = _fake_api()
    Api.apply_review_to_plan(self, b.cid)
    assert ("apply",) in b.calls and a.calls == []


def test_set_plan_mode_routes_by_cid_and_reports_that_cid():
    self, a, b = _fake_api()
    out = Api.set_plan_mode(self, False, b.cid)
    assert out["cid"] == b.cid and ("plan", False) in b.calls and a.calls == []


def test_can_start_coding_routes_by_cid():
    self, a, b = _fake_api()
    out = Api.can_start_coding(self, b.cid)
    assert out["cid"] == b.cid and ("gate",) in b.calls and a.calls == []


def test_default_cid_still_hits_active():
    self, a, b = _fake_api()
    Api.send_message(self, "x")           # 不给 cid
    Api.apply_review_to_plan(self)
    assert ("enqueue", "x") in a.calls and ("apply",) in a.calls and b.calls == []


def _run_all():
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    for name, fn in fns:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(fns)}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
