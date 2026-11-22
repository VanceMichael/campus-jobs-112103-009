"""技能分类规则：版本化、累积式别名归一化。

规则调整（如技能合并、改名）以 skill_rules 记录进入系统，带版本号与生效时间。
- 之后的岗位↔课程匹配使用当前有效版本；
- 已发出的推送保存生成时所用版本与归一化结果，可回看当时依据。
"""
from __future__ import annotations

from dataclasses import dataclass, field

BASELINE_VERSION = 1
_BASELINE_EFFECTIVE = "1970-01-01T00:00:00+00:00"


@dataclass
class SkillRulesVersion:
    version: int
    effective_at: str
    aliases: dict[str, str]  # 原始写法 -> 规范技能名


@dataclass
class SkillTaxonomy:
    versions: list[SkillRulesVersion] = field(default_factory=list)

    def _sorted(self) -> list[SkillRulesVersion]:
        return sorted(self.versions, key=lambda v: v.version)

    def find(self, version: int) -> SkillRulesVersion | None:
        for v in self.versions:
            if v.version == version:
                return v
        return None

    def current(self) -> SkillRulesVersion:
        if self.versions:
            return self._sorted()[-1]
        return SkillRulesVersion(BASELINE_VERSION, _BASELINE_EFFECTIVE, {})

    def version_at(self, ts: str) -> SkillRulesVersion:
        eligible = [v for v in self._sorted() if v.effective_at <= ts]
        if eligible:
            return eligible[-1]
        return SkillRulesVersion(BASELINE_VERSION, _BASELINE_EFFECTIVE, {})

    def aliases_for(self, version: int) -> dict[str, str]:
        """版本号 <= version 的规则累积合并（新版本可覆盖旧别名）。"""
        merged: dict[str, str] = {}
        for v in self._sorted():
            if v.version <= version:
                merged.update(v.aliases)
        return merged

    def normalize(self, skills: list[str], version: int | None = None) -> dict:
        """把原始技能写法归一化为规范技能名。

        返回 {version, canonical, mapping}：canonical 为排序后的规范技能集合，
        mapping 记录每个原始写法被映射成什么，用于解释匹配依据。
        """
        ver = version if version is not None else self.current().version
        aliases = self.aliases_for(ver)
        mapping: dict[str, str] = {}
        canonical: list[str] = []
        for raw in skills:
            s = raw.strip().lower()
            seen = set()
            while s in aliases and s not in seen:
                seen.add(s)
                s = aliases[s]
            mapping[raw] = s
            if s not in canonical:
                canonical.append(s)
        return {"version": ver, "canonical": sorted(canonical), "mapping": mapping}

    def add_version(self, rules: SkillRulesVersion) -> str:
        """返回 "added" 或 "duplicate"；内容冲突时抛 ValueError。"""
        existing = self.find(rules.version)
        if existing is not None:
            if existing.aliases != rules.aliases or existing.effective_at != rules.effective_at:
                raise ValueError(f"技能分类规则 v{rules.version} 已存在且内容不同")
            return "duplicate"
        self.versions.append(rules)
        self.versions.sort(key=lambda v: v.version)
        return "added"
