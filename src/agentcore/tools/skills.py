"""技能加载工具（FR-13.S）：渐进披露的第二层。

技能清单（name + description）已常驻注入 system（第一层，约 100 token/技能）；
模型判断某个技能与当前任务吻合时调 `load_skill` 读它的完整 SKILL.md 正文（第二层），
正文里引用的 references/ 文档、scripts/ 脚本、assets/ 模板则用现成的 read_file /
run_<shell> 按需取用（第三层）——技能包再多也不撑上下文。

只读工具（只读技能目录里的文件），不过权限 gate。**但技能里让干的事照常受管**：
写文件/跑脚本走各自工具的 dangerous 判定与权限确认，不因"技能说要跑"而免确认。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..skills import Skill, SkillError, build_skill_body_block, list_skill_files, load_skill_body
from .base import Tool, ToolError


@dataclass
class SkillBinding:
    """技能工具运行所需的上下文：当前可用技能的取值器（工作区可能切换，故用 getter）。"""
    skills_getter: Callable[[], list[Skill]]


class LoadSkillTool(Tool):
    name = "load_skill"
    description = (
        "读取一个**技能包**的完整说明（SKILL.md 正文 + 附带文件清单）。"
        "system 里的「可用技能」清单只给了每个技能的名字和适用场景；当前任务与其中某个吻合时，"
        "**先用本工具读完整说明，再按其中的步骤和验收标准动手**——技能里通常有踩过坑的既定做法、"
        "现成脚本和模板，比自己从头摸索更稳。正文里提到的 references/ 文档、scripts/ 脚本、"
        "assets/ 模板用 read_file 读、用命令工具跑（按需取用，不必一次全读）。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名（system「可用技能」清单里的名字）"},
        },
        "required": ["name"],
    }
    dangerous = False

    def __init__(self, binding: SkillBinding) -> None:  # 覆盖 Tool.__init__（不需 workspace）
        self._b = binding

    def run(self, params: dict) -> str:
        name = (params.get("name") or "").strip()
        if not name:
            raise ToolError("name 不能为空")
        skills = self._b.skills_getter() or []
        match = next((s for s in skills if s.name == name), None)
        if match is None:
            avail = "、".join(s.name for s in skills) or "（当前没有可用技能）"
            raise ToolError(f"没有名为「{name}」的技能。可用技能：{avail}")
        try:
            loaded = load_skill_body(match)
        except SkillError as e:
            raise ToolError(str(e)) from e
        return build_skill_body_block(loaded, list_skill_files(loaded))
