"""大库相关性检索自测（纯逻辑 tokenize/chunk/BM25 + 端到端检索质量）。

运行：python tests/test_retrieval.py
"""
from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentcore.retrieval import (  # noqa: E402
    Bm25, chunk_source, rank_chunks, search_code, tokenize,
)
from agentcore.tools.searchcode import SearchCodeTool  # noqa: E402
from agentcore.tools.base import ToolError  # noqa: E402


# ---- tokenize（标识符感知）-------------------------------------------------

def test_tokenize_splits_snake_and_camel():
    assert tokenize("get_user_id") == ["get", "user", "id"]
    assert tokenize("parseHTMLDocument") == ["parse", "html", "document"]
    assert tokenize("tiered_discount") == ["tiered", "discount"]


def test_tokenize_drops_stopwords_and_short():
    toks = tokenize("def the user x return value")
    assert "user" in toks and "def" not in toks and "the" not in toks and "x" not in toks


def test_tokenize_chinese_bigrams():
    # 中文出二元组，让中文查询/注释可检索（否则非 ASCII 全丢、中文用户查不到）
    assert tokenize("折扣计算") == ["折扣", "扣计", "计算"]
    assert "折扣" in tokenize("分级折扣策略")  # 与查询「折扣计算」有交集 -> 可命中


def test_search_finds_by_chinese_query(tmp: Path):
    _make_repo(tmp)
    out = search_code(tmp, "折扣计算", limit=5)  # 纯中文查询命中带中文 docstring 的折扣函数
    assert "discount.py" in out and "tiered_discount" in out


# ---- chunk_source ----------------------------------------------------------

def test_chunk_by_symbols():
    src = textwrap.dedent('''\
        import os
        def alpha():
            return 1
        def beta(x):
            return x + 1
    ''')
    chunks = chunk_source("m.py", src)
    names = {c.name for c in chunks}
    assert "alpha" in names and "beta" in names
    beta = next(c for c in chunks if c.name == "beta")
    assert "x + 1" in beta.text and beta.start == 4


def test_chunk_windows_when_no_symbols():
    src = "\n".join(f"line {i}" for i in range(100))
    chunks = chunk_source("notes.txt".replace(".txt", ".py"), src)  # 无 def/class -> 滑窗
    assert len(chunks) >= 2 and all(c.name == "" for c in chunks)


# ---- BM25 ------------------------------------------------------------------

def test_bm25_ranks_relevant_higher():
    docs = [tokenize("login auth password verify"),
            tokenize("render html template view"),
            tokenize("auth token session login")]
    bm = Bm25(docs)
    q = tokenize("login auth")
    scores = [bm.score(q, i) for i in range(3)]
    assert scores[1] == 0          # 无关文档 0 分
    assert scores[0] > 0 and scores[2] > 0


def test_rank_chunks_empty_query():
    assert rank_chunks("", [], 5) == []


# ---- 端到端检索质量（合成"大库"）------------------------------------------

def _make_repo(root: Path):
    (root / "billing").mkdir(parents=True)
    (root / "auth").mkdir(parents=True)
    (root / "billing" / "discount.py").write_text(textwrap.dedent('''\
        def tiered_discount(amount):
            """分级折扣：金额越高折扣越大。"""
            if amount >= 1000: return amount * 0.7
            if amount >= 100: return amount * 0.9
            return amount
    '''))
    (root / "auth" / "login.py").write_text(textwrap.dedent('''\
        def verify_password(user, password):
            """校验用户密码并签发会话 token。"""
            return check_hash(user.hash, password)
        def issue_session_token(user):
            return make_jwt(user.id)
    '''))
    (root / "utils.py").write_text("def slugify(s):\n    return s.lower().replace(' ', '-')\n")


def test_search_finds_discount_logic(tmp: Path):
    _make_repo(tmp)
    out = search_code(tmp, "分级折扣 计算 discount", limit=5)
    # 最相关应是 billing/discount.py 的 tiered_discount
    assert "billing/discount.py" in out and "tiered_discount" in out
    # 排第一（出现在登录之前）
    assert out.index("discount.py") < out.index("login.py") if "login.py" in out else True


def test_search_finds_auth_by_intent(tmp: Path):
    _make_repo(tmp)
    out = search_code(tmp, "用户登录密码校验 token", limit=5)
    assert "auth/login.py" in out and ("verify_password" in out or "issue_session_token" in out)


def test_search_no_match_message(tmp: Path):
    _make_repo(tmp)
    out = search_code(tmp, "量子纠缠 区块链 神经网络", limit=5)
    assert "未找到" in out


def test_tool_validates_and_runs(tmp: Path):
    _make_repo(tmp)
    tool = SearchCodeTool(tmp)
    assert "discount" in tool.run({"query": "折扣计算", "limit": 3})
    try:
        tool.run({"query": "  "}); assert False, "空 query 应报错"
    except ToolError:
        pass


def _run_all():
    import inspect
    fns = [(n, f) for n, f in globals().items()
           if n.startswith("test_") and inspect.isfunction(f)]
    passed = 0
    for name, fn in fns:
        with tempfile.TemporaryDirectory() as d:
            if "tmp" in inspect.signature(fn).parameters:
                fn(Path(d))
            else:
                fn()
        print(f"  ok  {name}")
        passed += 1
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
