"""领域模型：岗位、课程、报名、反馈、推送与核实项。

所有数据均为合成数据。时间一律以 UTC ISO 字符串存储（见 norm_ts），
因此字符串比较与时间比较一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

POSTING_STATES = ("open", "closed", "converted", "filled")
EMPLOYMENT_KINDS = ("internship", "full-time", "part-time", "short-project")
RECORD_KINDS = (
    "job_posting",
    "posting_update",
    "course",
    "placement_feedback",
    "application",
    "skill_rules",
)
FEEDBACK_STAGES = ("applied", "screening", "interview", "offer", "hired", "rejected")
RESPONSE_STATES = ("received", "pending")

#: 只有这些阶段且 response_state=received 的反馈，才允许把岗位标为“已招满”
FILLED_CONFIRM_STAGES = frozenset({"hired"})

#: 岗位超过该天数没有任何更新且仍显示在招时，标记为“需向企业核实”
DEFAULT_STALE_DAYS = 30

STATUS_LABELS = {
    "open": "在招",
    "closed": "已关闭",
    "converted": "已转化",
    "filled": "已招满（反馈确认）",
    "expired": "已过有效期",
}


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def norm_ts(value: str) -> str:
    """把任意 ISO 时间统一为 UTC 字符串，保证可互相比较。"""
    return iso(parse_ts(value))


def utcnow() -> str:
    return iso(datetime.now(timezone.utc))


@dataclass
class ContentVersion:
    """岗位内容的一个版本（标题/技能/地点等以 updated_at 排序）。"""

    updated_at: str
    collected_at: str
    source_id: str
    title: str
    skills: list[str]
    location: str
    employment_kind: str
    expires_at: Optional[str] = None


@dataclass
class StateRevision:
    """企业侧状态修订（在招/关闭/转化/申报招满），按 revision 号生效。"""

    revision: int
    state: str
    effective_at: str
    collected_at: str
    source_id: str
    changes: dict = field(default_factory=dict)
    converted_to: Optional[str] = None


@dataclass
class PostingAlias:
    """同一招聘需求在某个渠道上的岗位编号。"""

    posting_ref: str
    source_id: str
    first_seen: str
    match_reason: str
    confirmed: bool = False


@dataclass
class CanonicalPosting:
    """归并后的招聘需求。跨渠道转载共享一个 canonical_id。"""

    canonical_id: str
    employer_ref: str
    title_norm: str
    location: str
    employment_kind: str
    created_at: str
    aliases: list[PostingAlias] = field(default_factory=list)
    contents: list[ContentVersion] = field(default_factory=list)
    revisions: list[StateRevision] = field(default_factory=list)
    converted_to: Optional[str] = None

    @property
    def posting_refs(self) -> list[str]:
        return [a.posting_ref for a in self.aliases]

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for a in self.aliases:
            if a.source_id not in seen:
                seen.append(a.source_id)
        return seen

    def current_content(self) -> ContentVersion:
        return max(self.contents, key=lambda c: (c.updated_at, c.collected_at))

    def content_at(self, ts: str) -> ContentVersion:
        eligible = [c for c in self.contents if c.updated_at <= ts]
        if not eligible:
            return min(self.contents, key=lambda c: (c.updated_at, c.collected_at))
        return max(eligible, key=lambda c: (c.updated_at, c.collected_at))

    def current_revision_no(self) -> int:
        # 首次发布视为 rev1，企业修订从 rev2 起算
        return max([r.revision for r in self.revisions] + [1])

    def current_state(self) -> str:
        if not self.revisions:
            return "open"
        return max(self.revisions, key=lambda r: r.revision).state

    def state_at(self, ts: str) -> str:
        eligible = [r for r in self.revisions if r.effective_at <= ts]
        if not eligible:
            return "open"
        return max(eligible, key=lambda r: r.revision).state


@dataclass
class CourseRevision:
    revision: int
    skills: list[str]
    term: str
    collected_at: str
    source_id: str


@dataclass
class Course:
    course_ref: str
    revisions: list[CourseRevision] = field(default_factory=list)

    def current(self) -> CourseRevision:
        return max(self.revisions, key=lambda r: r.revision)

    def revision_at(self, ts: str) -> CourseRevision:
        eligible = [r for r in self.revisions if r.collected_at <= ts]
        if not eligible:
            return min(self.revisions, key=lambda r: r.revision)
        return max(eligible, key=lambda r: r.revision)


@dataclass
class Application:
    """学生报名记录。岗位关闭/转化后记录保留，永不删除。"""

    application_ref: str
    posting_ref: str
    canonical_id: Optional[str]
    student_ref: str
    applied_at: str
    source_id: str


@dataclass
class PlacementFeedback:
    feedback_id: str
    posting_ref: str
    canonical_id: Optional[str]
    course_ref: Optional[str]
    stage: str
    response_state: str
    skill_revision: Optional[int]
    collected_at: str


@dataclass
class Push:
    """发给学生的岗位推送。snapshot 冻结生成时的依据，供事后回看。"""

    push_id: str
    created_at: str
    canonical_id: str
    student_ref: Optional[str]
    course_ref: Optional[str]
    warnings: list[str]
    snapshot: dict


@dataclass
class VerificationItem:
    """仍需向企业核实的变化。"""

    kind: str
    severity: str  # critical / warning / info
    message: str
    refs: dict
    detected_at: str
