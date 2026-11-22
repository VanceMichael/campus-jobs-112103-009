"""状态存储：内存模型 + JSON 持久化，支持持续多轮采集。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .models import (
    DEFAULT_STALE_DAYS,
    Application,
    CanonicalPosting,
    ContentVersion,
    Course,
    CourseRevision,
    PlacementFeedback,
    PostingAlias,
    Push,
    StateRevision,
    VerificationItem,
)
from .taxonomy import SkillRulesVersion, SkillTaxonomy

SCHEMA_VERSION = 1


@dataclass
class CareerStore:
    postings: dict[str, CanonicalPosting] = field(default_factory=dict)
    courses: dict[str, Course] = field(default_factory=dict)
    applications: dict[str, Application] = field(default_factory=dict)
    feedback: list[PlacementFeedback] = field(default_factory=list)
    taxonomy: SkillTaxonomy = field(default_factory=SkillTaxonomy)
    pushes: dict[str, Push] = field(default_factory=dict)
    verifications: list[VerificationItem] = field(default_factory=list)
    journal: list[dict] = field(default_factory=list)
    seen_sources: set[str] = field(default_factory=set)
    pending_updates: dict[str, list[dict]] = field(default_factory=dict)
    stale_days: int = DEFAULT_STALE_DAYS

    def __post_init__(self) -> None:
        self.reindex()

    def reindex(self) -> None:
        self.alias_index: dict[str, str] = {}
        self.key_index: dict[tuple, str] = {}
        for cid, posting in self.postings.items():
            for alias in posting.aliases:
                self.alias_index[alias.posting_ref] = cid
            self.key_index[
                (
                    posting.employer_ref,
                    posting.title_norm,
                    posting.location,
                    posting.employment_kind,
                )
            ] = cid

    # ---- 持久化 ----

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "stale_days": self.stale_days,
            "postings": {cid: asdict(p) for cid, p in self.postings.items()},
            "courses": {ref: asdict(c) for ref, c in self.courses.items()},
            "applications": {ref: asdict(a) for ref, a in self.applications.items()},
            "feedback": [asdict(f) for f in self.feedback],
            "taxonomy": {"versions": [asdict(v) for v in self.taxonomy.versions]},
            "pushes": {pid: asdict(p) for pid, p in self.pushes.items()},
            "verifications": [asdict(v) for v in self.verifications],
            "journal": list(self.journal),
            "seen_sources": sorted(self.seen_sources),
            "pending_updates": self.pending_updates,
        }

    @staticmethod
    def from_dict(d: dict) -> "CareerStore":
        if d.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"不支持的存储版本: {d.get('schema_version')}")
        postings = {
            cid: CanonicalPosting(
                canonical_id=p["canonical_id"],
                employer_ref=p["employer_ref"],
                title_norm=p["title_norm"],
                location=p["location"],
                employment_kind=p["employment_kind"],
                created_at=p["created_at"],
                aliases=[PostingAlias(**a) for a in p.get("aliases", [])],
                contents=[ContentVersion(**c) for c in p.get("contents", [])],
                revisions=[StateRevision(**r) for r in p.get("revisions", [])],
                converted_to=p.get("converted_to"),
            )
            for cid, p in d.get("postings", {}).items()
        }
        courses = {
            ref: Course(
                course_ref=c["course_ref"],
                revisions=[CourseRevision(**r) for r in c.get("revisions", [])],
            )
            for ref, c in d.get("courses", {}).items()
        }
        taxonomy = SkillTaxonomy(
            versions=[SkillRulesVersion(**v) for v in d.get("taxonomy", {}).get("versions", [])]
        )
        return CareerStore(
            postings=postings,
            courses=courses,
            applications={ref: Application(**a) for ref, a in d.get("applications", {}).items()},
            feedback=[PlacementFeedback(**f) for f in d.get("feedback", [])],
            taxonomy=taxonomy,
            pushes={pid: Push(**p) for pid, p in d.get("pushes", {}).items()},
            verifications=[VerificationItem(**v) for v in d.get("verifications", [])],
            journal=list(d.get("journal", [])),
            seen_sources=set(d.get("seen_sources", [])),
            pending_updates={k: list(v) for k, v in d.get("pending_updates", {}).items()},
            stale_days=d.get("stale_days", DEFAULT_STALE_DAYS),
        )


def new_store() -> CareerStore:
    return CareerStore()


def save_store(store: CareerStore, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(store.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_store(path: str | Path) -> CareerStore:
    return CareerStore.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
