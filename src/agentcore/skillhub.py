"""技能市场与安装（FR-13.S2）：浏览/下载/安装第三方技能包。

**格式对齐 Claude Code 的插件市场**：一个市场 = 一个 git 仓库，仓库根有
`.claude-plugin/marketplace.json`（`$schema` 见 json.schemastore.org）。这样现有的社区市场
（官方 anthropics/claude-code、社区 345+ 技能那类）不用改一行就能被 hermes 直接读。

**下载走 GitHub 的 zip 归档，不走 git clone**：零新依赖（标准库 urllib + zipfile）、不要求
用户机器装 git、且内容在解压前后都能逐项检查。实测社区市场的 88 个条目**全部**是
`"source": "./相对路径"` 形式——下载一次市场仓库 zip 就拿到了全部插件，不必逐个抓。

**安装即拷贝到技能目录**：技能就是纯文件，落到 `<APP_DIR>/skills/<name>/` 即被发现，
不需要重启、不需要注册（`_refresh_skills` 每次建注册表时重扫，`load_skill` 每次重读磁盘）。

**安全**：安装前一律先跑 `skillscan` 本地扫描并把结论交给调用方，由调用方（GUI）决定
确认强度。本模块**不自作主张安装**——`install_skill` 必须被显式调用。

纯逻辑（解析 marketplace.json / 解析 source / 从解压树里找技能）与 IO（下载/落盘）分离。
"""
from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .skills import SkillError, parse_skill_md

USER_AGENT = "hermes-dev-skills/1.0"
DOWNLOAD_TIMEOUT = 60
MAX_ARCHIVE_BYTES = 80 * 1024 * 1024     # 单个 zip 下载上限（社区大市场约十几 MB）
MAX_UNPACKED_BYTES = 200 * 1024 * 1024   # 解压后总字节上限（zip bomb 防护）
MAX_ENTRIES = 20000                      # 解压条目数上限（同上）
TEXT_SUFFIXES = {".md", ".py", ".sh", ".ps1", ".js", ".ts", ".json", ".yaml", ".yml",
                 ".txt", ".toml", ".rb", ".pl", ".bat", ".cmd", ""}

_GITHUB_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_GITHUB_URL_RE = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?/?$")


class HubError(Exception):
    """市场/安装过程中可预期的失败（网络、格式、不支持的来源），信息可直接给用户看。"""


# ---- 纯逻辑：解析 marketplace.json ---------------------------------------


@dataclass(frozen=True)
class PluginEntry:
    """市场里的一个条目。一个条目可能含多个技能（`skills/<name>/SKILL.md` 布局）。"""
    name: str
    description: str = ""
    version: str = ""
    author: str = ""
    category: str = ""
    keywords: tuple[str, ...] = ()
    source: object = ""          # 原始 source 字段（字符串或对象）


@dataclass(frozen=True)
class Marketplace:
    name: str
    description: str = ""
    owner: str = ""
    homepage: str = ""
    entries: tuple[PluginEntry, ...] = ()


def _text(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _owner_name(v) -> str:
    if isinstance(v, dict):
        return _text(v.get("name"))
    return _text(v)


def parse_marketplace(text: str) -> Marketplace:
    """解析 `.claude-plugin/marketplace.json`。未知字段忽略（向前兼容）。纯函数。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise HubError(f"marketplace.json 不是合法 JSON：{e}") from e
    if not isinstance(data, dict):
        raise HubError("marketplace.json 顶层必须是对象")
    raw = data.get("plugins")
    if not isinstance(raw, list):
        raise HubError("marketplace.json 缺少 plugins 数组")

    entries: list[PluginEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        if not name:
            continue
        kw = item.get("keywords") or item.get("tags") or []
        entries.append(PluginEntry(
            name=name,
            description=_text(item.get("description")),
            version=_text(item.get("version")),
            author=_owner_name(item.get("author")),
            category=_text(item.get("category")),
            keywords=tuple(str(k) for k in kw if isinstance(kw, list))[:12],
            source=item.get("source", ""),
        ))
    return Marketplace(
        name=_text(data.get("name")) or "（未命名市场）",
        description=_text(data.get("description")),
        owner=_owner_name(data.get("owner")),
        homepage=_text(data.get("homepage")) or _text(data.get("repository")),
        entries=tuple(entries),
    )


@dataclass(frozen=True)
class Resolved:
    """条目取材方式：要么在市场仓库内（subdir），要么另一个 GitHub 仓库（repo），要么不支持。"""
    kind: str                # "subdir" | "repo" | "unsupported"
    path: str = ""           # subdir：相对市场根的路径
    repo: str = ""           # repo：owner/name
    ref: str = ""            # 分支/标签
    reason: str = ""         # unsupported：给用户的可读原因


def _norm_repo(url: str) -> str:
    """把 github URL / owner-repo 简写归一成 owner/repo；不是 GitHub 返回空串。"""
    u = _text(url)
    if _GITHUB_REPO_RE.match(u):
        return u
    m = _GITHUB_URL_RE.match(u)
    if m:
        return m.group(1)
    m = re.match(r"^git@github\.com:([\w.-]+/[\w.-]+?)(?:\.git)?$", u)
    return m.group(1) if m else ""


def resolve_source(source, market_root_ref: str = "") -> Resolved:
    """把 marketplace 条目的 `source` 字段解析成取材方式。纯函数。

    支持：相对路径（市场仓库内，实测占绝大多数）、`github`、`git-subdir`、指向 GitHub 的 `url`。
    不支持：npm、非 GitHub 的 git 主机——**明确报不支持而不是静默失败**（不为此硬拉 git/npm 依赖）。
    """
    if isinstance(source, str):
        # 规范里 source 的**字符串形式只表示市场仓库内的相对路径**（`./plugins/foo`；配合
        # metadata.pluginRoot 也可写成裸名 `formatter`）。仓库简写只出现在对象形式的 url 字段里，
        # 所以这里一律按路径解释——否则 `plugins/foo` 会和 `owner/repo` 撞形状、误判成远程仓库。
        s = source.strip()
        if not s:
            return Resolved("unsupported", reason="条目没有 source 字段")
        if ".." in Path(s).parts:
            return Resolved("unsupported", reason="source 路径含 ..，越出市场根目录，已拒绝")
        return Resolved("subdir", path=s.lstrip("./").strip("/"))

    if not isinstance(source, dict):
        return Resolved("unsupported", reason=f"无法识别的 source 类型：{type(source).__name__}")

    kind = _text(source.get("source"))
    ref = _text(source.get("ref")) or _text(source.get("sha"))
    if kind == "github":
        repo = _norm_repo(source.get("repo", ""))
        return (Resolved("repo", repo=repo, ref=ref) if repo
                else Resolved("unsupported", reason="github 来源缺少合法的 repo（owner/name）"))
    if kind == "git-subdir":
        repo = _norm_repo(source.get("url", ""))
        sub = _text(source.get("path"))
        if not repo:
            return Resolved("unsupported", reason="git-subdir 目前只支持 GitHub 仓库")
        if ".." in Path(sub).parts:
            return Resolved("unsupported", reason="git-subdir 的 path 含 ..，已拒绝")
        return Resolved("repo", repo=repo, ref=ref, path=sub)
    if kind == "url":
        repo = _norm_repo(source.get("url", ""))
        return (Resolved("repo", repo=repo, ref=ref) if repo
                else Resolved("unsupported",
                              reason="只支持 GitHub 上的 git 仓库（其它 git 主机需本机 git，暂不支持）"))
    if kind == "npm":
        return Resolved("unsupported", reason="npm 来源需要本机 npm，暂不支持")
    return Resolved("unsupported", reason=f"暂不支持的来源类型：{kind or '(空)'}")


def github_zip_url(repo: str, ref: str = "") -> str:
    """GitHub 仓库归档下载地址（ref 省略时用默认分支）。"""
    return f"https://codeload.github.com/{repo}/zip/{ref}" if ref else \
           f"https://api.github.com/repos/{repo}/zipball"


def find_skills_in_tree(root: Path) -> list[Path]:
    """在一个插件目录里找技能目录。

    兼容两种布局（Claude Code 插件规范）：`skills/<name>/SKILL.md` 多技能，
    或插件根直接放一个 `SKILL.md` 的单技能。
    """
    found: list[Path] = []
    if (root / "SKILL.md").is_file():
        found.append(root)
    skills_dir = root / "skills"
    if skills_dir.is_dir():
        for d in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            if (d / "SKILL.md").is_file():
                found.append(d)
    # plugin.json 可声明额外技能目录
    manifest = root / ".claude-plugin" / "plugin.json"
    if manifest.is_file():
        try:
            extra = json.loads(manifest.read_text(encoding="utf-8", errors="replace")).get("skills")
        except (json.JSONDecodeError, OSError):
            extra = None
        for rel in ([extra] if isinstance(extra, str) else (extra or [])):
            if not isinstance(rel, str) or ".." in Path(rel).parts:
                continue
            d = root / rel.lstrip("./")
            if not d.is_dir():
                continue
            for sub in sorted(p for p in d.iterdir() if p.is_dir()):
                if (sub / "SKILL.md").is_file() and sub not in found:
                    found.append(sub)
    return found


def find_all_skills(root: Path, limit: int = 2000) -> list[Path]:
    """在整个仓库树里找所有含 SKILL.md 的目录（更新检查时上游改了结构的兜底定位）。"""
    out: list[Path] = []
    for md in sorted(root.rglob("SKILL.md")):
        if md.is_file():
            out.append(md.parent)
            if len(out) >= limit:
                break
    return out


def read_text_files(skill_dir: Path, max_files: int = 200) -> dict[str, str]:
    """读技能目录下的文本文件（{相对路径: 内容}），供 skillscan 扫描。"""
    out: dict[str, str] = {}
    for p in sorted(skill_dir.rglob("*")):
        if len(out) >= max_files:
            break
        if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            out[str(p.relative_to(skill_dir))] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return out


# ---- IO：下载与安装 -----------------------------------------------------


def _http_get(url: str, timeout: int = DOWNLOAD_TIMEOUT) -> bytes:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        with urlopen(req, timeout=timeout) as resp:   # noqa: S310 — 只用于 https 下载
            data = resp.read(MAX_ARCHIVE_BYTES + 1)
    except HTTPError as e:
        raise HubError(f"下载失败（HTTP {e.code}）：{url}") from e
    except (URLError, OSError, TimeoutError) as e:
        raise HubError(f"下载失败（网络不通或超时）：{e}") from e
    if len(data) > MAX_ARCHIVE_BYTES:
        raise HubError(f"下载内容超过 {MAX_ARCHIVE_BYTES // 1024 // 1024}MB 上限，已中止")
    return data


def fetch_marketplace(repo_or_url: str, ref: str = "main") -> tuple[Marketplace, str]:
    """拉取一个市场的 marketplace.json，返回 (Marketplace, 归一后的 repo)。

    只接受 GitHub 仓库（`owner/repo` 或 github.com 链接）——先试 `ref`，失败再试 master。
    """
    repo = _norm_repo(repo_or_url)
    if not repo:
        raise HubError(f"暂只支持 GitHub 上的技能市场，无法识别：{repo_or_url}")
    last: HubError | None = None
    for r in ([ref] if ref else []) + ["main", "master"]:
        url = f"https://raw.githubusercontent.com/{repo}/{r}/.claude-plugin/marketplace.json"
        try:
            return parse_marketplace(_http_get(url, timeout=30).decode("utf-8", "replace")), repo
        except HubError as e:
            last = e
    raise HubError(
        f"没能在 {repo} 读到 .claude-plugin/marketplace.json（{last}）——"
        "确认这是个技能市场仓库、且是公开仓库。"
    )


def _safe_extract(data: bytes, dest: Path) -> Path:
    """把 zip 安全解压到 dest，返回解压出的顶层目录。

    防护：拒绝绝对路径/`..`（zip slip）、限制条目数与解压总字节（zip bomb）、跳过符号链接。
    """
    dest.mkdir(parents=True, exist_ok=True)
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise HubError(f"下载到的不是合法 zip 归档：{e}") from e
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise HubError(f"归档条目数超过上限（{len(infos)} > {MAX_ENTRIES}），已中止")
        total = 0
        top: str | None = None
        dest_root = dest.resolve()
        for info in infos:
            name = info.filename
            if name.endswith("/"):
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:   # 符号链接，跳过
                continue
            # **恒用 PureWindowsPath 解析，不跟随本机平台**：它同时看懂 "/" 与 "\"、认盘符与 UNC，
            # 是两套规则里更严的一把尺。原来用 Path()（跟随本机 flavour）在 Windows 上漏掉一整类：
            # PureWindowsPath("/etc/evil.txt").is_absolute() 是 **False**（缺盘符），
            # 于是 dest / 它 直接跳到盘符根 → C:\etc\evil.txt，从市场装个恶意技能包就能写出围栏外。
            # 拆成 drive/root 判而不用 is_absolute()：Windows 语义下后者要求盘符**和**根同时存在。
            pure = PureWindowsPath(name)
            parts = pure.parts
            if not parts or ".." in parts or pure.drive or pure.root:
                raise HubError(f"归档内含非法路径（已中止）：{name}")
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise HubError(f"解压体积超过 {MAX_UNPACKED_BYTES // 1024 // 1024}MB 上限，已中止")
            top = top or parts[0]
            target = dest / Path(*parts)
            # 最后一道闸：不穷举攻击形态，直接断言真正在乎的不变量——落点必须在 dest 内。
            # 上面的模式规则将来漏了什么（新分隔符/编码技巧），这里兜住。
            if not target.resolve().is_relative_to(dest_root):
                raise HubError(f"归档内含非法路径（已中止）：{name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    if top is None:
        raise HubError("归档是空的")
    return dest / top


def download_repo(repo: str, dest: Path, ref: str = "") -> Path:
    """下载 GitHub 仓库 zip 并解压到 dest，返回解压出的仓库根目录。"""
    return _safe_extract(_http_get(github_zip_url(repo, ref)), dest)


def install_skill(src_dir: Path, skills_root: Path, *, overwrite: bool = False) -> Path:
    """把一个技能目录装到技能根目录下，返回安装后的路径。

    安装前先校验 `SKILL.md` 合法且 `name` 与目录名一致（否则装了也会被 discover 跳过）。
    """
    md = src_dir / "SKILL.md"
    if not md.is_file():
        raise HubError(f"{src_dir.name} 里没有 SKILL.md，不是技能包")
    try:
        skill = parse_skill_md(md.read_text(encoding="utf-8", errors="replace"))
    except (SkillError, OSError) as e:
        raise HubError(f"{src_dir.name} 的 SKILL.md 不合规范：{e}") from e

    target = skills_root / skill.name
    if target.exists():
        if not overwrite:
            raise HubError(f"技能「{skill.name}」已存在（{target}）——如要覆盖请选择更新")
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_dir, target, symlinks=False,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    return target


def uninstall_skill(name: str, skills_root: Path) -> bool:
    """删除已安装的技能目录。返回是否真删了（不存在返回 False）。"""
    target = (skills_root / name).resolve()
    root = skills_root.resolve()
    if root not in target.parents or not target.is_dir():   # 防越界删除
        return False
    shutil.rmtree(target)
    return True


# ---- 安装台账与更新检查（FR-13.S3）--------------------------------------
# 已装技能落在磁盘上就是普通文件夹，本身不带"我从哪来"的信息。要能检查更新就得记来源。
# 台账放在**技能目录外**的单独 JSON（不往技能包里塞 sidecar 文件——那会污染技能内容、
# 被扫描器和 copytree 一起带走，也会让同一个技能在不同机器上算出不同哈希）。


def dir_hash(path: Path) -> str:
    """技能目录的内容指纹：对 (相对路径, 内容) 排序后整体 sha256。

    用内容哈希而不是版本号比对——`version` 在规范里是可选字段，实测很多技能根本没有，
    有的也未必随内容更新。哈希不会骗人。
    """
    import hashlib
    h = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(path)).replace("\\", "/")
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()


def read_installs(path: Path) -> dict:
    """读安装台账（技能名 -> {repo, entry, src_path, version, hash, installed_at}）。"""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_installs(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data or {}, ensure_ascii=False, indent=2), encoding="utf-8")


def record_install(name: str, path: Path, *, repo: str, entry: str, src_rel: str,
                   version: str, installed: Path, ledger: Path) -> dict:
    """记一条安装来源。`src_rel` 是技能在市场仓库归档里的相对路径（更新时按它重新定位）。"""
    import time
    data = read_installs(ledger)
    data[name] = {
        "repo": repo, "entry": entry, "src_rel": src_rel, "version": version,
        "hash": dir_hash(installed), "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    write_installs(data, ledger)
    return data


def forget_install(name: str, ledger: Path) -> dict:
    data = read_installs(ledger)
    data.pop(name, None)
    write_installs(data, ledger)
    return data


def prune_installs(ledger: Path, skills_root: Path) -> dict:
    """清掉台账里已经不在磁盘上的条目（用户手删了技能目录）。"""
    data = read_installs(ledger)
    alive = {k: v for k, v in data.items() if (skills_root / k).is_dir()}
    if len(alive) != len(data):
        write_installs(alive, ledger)
    return alive


def relative_in(root: Path, target: Path) -> str:
    """target 相对 root 的 POSIX 风格路径；不在 root 下返回空串。"""
    try:
        return target.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return ""


def load_catalog(path: Path) -> list[dict]:
    """读内置精选市场清单（随程序分发的 JSON）。读不到就返回空列表，不报错。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("marketplaces") if isinstance(data, dict) else data
    return [i for i in (items or []) if isinstance(i, dict) and i.get("repo")]
