"""探针：把「模型 API 回 402」这件事**分解成可证伪的几种**（不经 hermes 主循环）。

    python scripts/diag_billing_402.py                 # 用当前 active_model
    python scripts/diag_billing_402.py 模型档名
    python scripts/diag_billing_402.py 模型档名 --big 60000   # 自定大请求的近似 token 量

为什么要它：`APIStatusError: Error code: 402` 只说"计费问题"，但**余额充足也会回这个码**。
"账户没钱"和下面这些完全不同的成因，在报错文案上长得一模一样：

  A 账户级硬停       —— 小请求也 402（欠费/冻结/key 失效/该模型不在套餐或分组内）
  B 单请求成本闸     —— 小请求 200、大请求 402（按预估成本预扣，超过单请求可用额度）
  C 并发闸           —— 单发都 200、并发 3 路就 402（有些中转把套餐并发超限报成计费错误）
  D 已自愈 / 瞬时    —— 现在全 200（当时余额瞬时见底、风控误伤、或中转抖动）

探针按 A→B→C 顺序打三组请求，**直接看 HTTP 状态码与原始报文**（走 urllib 而非 SDK，
免得异常包装把状态码和 body 吃掉）。委派调研正是"3 路并发 + 每路塞满搜索正文"的形状——
真凶如果是 B 或 C，交互里单发一次搜索**永远复现不出来**。

代价：一次小请求 + 一次大请求 + 三次并发大请求。大请求只让模型回 1 个 token（max_tokens=1），
花的是**输入**那头的钱；不想花就 --big 调小。输出里会带端点主机名，贴出来前自己扫一眼。
"""
from __future__ import annotations

import concurrent.futures
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.config import load_config          # noqa: E402
from agentcore.providers.base import account_problem  # noqa: E402

UA = "hermes-diag/1.0"


def _endpoint(mc, api_key: str):
    """把模型档翻成 (url, headers, 造 body 的函数)。两种协议的 402 长得一样，请求体不一样。"""
    base = (mc.base_url or ("https://api.anthropic.com" if mc.provider == "anthropic"
                            else "https://api.openai.com/v1")).rstrip("/")
    if mc.provider == "anthropic":
        url = f"{base}/v1/messages"        # 与 anthropic SDK 一致：base_url 后面接 /v1/messages
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json", "user-agent": UA}
        def body(text, max_tokens=1):
            return {"model": mc.model, "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": text}]}
    else:
        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}",
                   "content-type": "application/json", "user-agent": UA}
        def body(text, max_tokens=1):
            return {"model": mc.model, "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": text}]}
    return url, headers, body


def _post(url, headers, payload, timeout=120):
    """返回 (status, body_text)。**不抛异常**——402 的 body 才是要看的东西。"""
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return e.code, ""
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def _verdict(status: int, body: str) -> str:
    if status == 200:
        return "OK"
    kind = account_problem(body) or (account_problem(_Coded(status)) if status > 0 else "")
    return f"{status} {kind or ''}".strip()


class _Coded(Exception):
    def __init__(self, code): super().__init__(""); self.status_code = code


def main() -> int:
    args = [a for a in sys.argv[1:]]
    big_tokens = 40000
    if "--big" in args:
        i = args.index("--big")
        big_tokens = int(args[i + 1]); del args[i:i + 2]
    profile = args[0] if args else None

    cfg = load_config()
    mc = cfg.get_model(profile)
    key = cfg.resolve_api_key(mc)
    url, headers, body = _endpoint(mc, key)
    host = url.split("//", 1)[-1].split("/", 1)[0]
    print(f"[探针] 模型档 {profile or cfg.active_model} → {mc.model} @ {host}（{mc.provider} 协议）")
    print(f"[探针] key 取自 {mc.api_key_env}，长度 {len(key)}（不打印内容）\n")

    # A 账户级：最小请求。这一发就 402 = 跟请求大小/并发无关，是账户/权限/套餐问题。
    st, bd = _post(url, headers, body("只回一个字：好"))
    print(f"① 最小请求      → {_verdict(st, bd)}")
    if st != 200:
        print(f"   原始报文：{bd[:500]}\n")

    # B 单请求成本闸：把输入撑到接近委派回灌的量级，只要 1 个输出 token。
    filler = "证据。" * (big_tokens // 2)          # 中文约 1~1.5 token/字，够粗略压满输入
    st2, bd2 = _post(url, headers, body(f"下面是检索到的正文，只回一个字：好\n{filler}"))
    print(f"② 大请求(≈{big_tokens} 字) → {_verdict(st2, bd2)}")
    if st2 != 200:
        print(f"   原始报文：{bd2[:500]}\n")

    # C 并发闸：委派就是这个形状——同一把 key 上 3 路大请求同时在飞。
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(_post, url, headers,
                            body(f"第{i}路检索正文，只回一个字：好\n{filler}")) for i in range(3)]
        trio = [f.result() for f in futs]
    print(f"③ 并发 3 路大请求 → {', '.join(_verdict(s, b) for s, b in trio)}")
    for s, b in trio:
        if s != 200:
            print(f"   原始报文：{b[:500]}")
            break

    print("\n[判读]")
    bad = lambda s: s != 200                                        # noqa: E731
    if bad(st):
        print("  → A 账户级硬停：小请求都过不去。查账户状态/绑卡/风控、key 是否有效、"
              "以及**该模型是否在你的套餐或分组内**（中转最常见的是这条，与余额数字无关）。")
    elif bad(st2):
        print("  → B 单请求成本闸：小的行、大的不行。委派回灌搜索正文时输入量暴涨才触发，"
              "交互里单发一次搜索永远复现不出来。找中转确认单请求额度/预扣规则，"
              "或把 delegate 的检索条数、正文抓取量调小。")
    elif any(bad(s) for s, _ in trio):
        print("  → C 并发闸：单发都行、3 路并发就挂。委派是并行发起的（loop.py 的 _PARALLEL_CAP=4），"
              "正好撞上。找中转确认套餐并发数，或把同一轮发出的 delegate 数量降到 1~2。")
    else:
        print("  → D 当时是瞬时状态（余额瞬时见底/自动续费间隙/风控误伤/中转抖动），现在已恢复。"
              "把报错里的 request_id 给中转客服，他们能查到那一条被拒的确切原因。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
