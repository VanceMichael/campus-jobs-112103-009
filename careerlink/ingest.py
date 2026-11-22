"""持续采集：解析来源信封、按 source_id 去重、按采集时间顺序应用。

支持的记录类型（record_kind）：
- job_posting        岗位发布/内容更新
- posting_update     企业状态修订（关闭、转化、申报招满、字段变更）
- course             课程（培养活动）版本
- placement_feedback 面试/录用反馈
- application        学生报名
- skill_rules        技能分类规则版本

采集只追加事实，不删除历史：旧报名、旧版本、旧推送全部保留。
"""
from __future__ import annotations

import json
from pathlib import Path

from .identity import canonical_id_for, identity_key
from .models import (
    EMPLOYMENT_KINDS,
    FEEDBACK_STAGES,
    POSTING_STATES,
    RECORD_KINDS,
    RESPONSE_STATES,
    Application,
    CanonicalPosting,
    ContentVersion,
    Course,
    CourseRevision,
    PlacementFeedback,
    PostingAlias,
    StateRevision,
    VerificationItem,
    norm_ts,
)
from .store import CareerStore
from .taxonomy import SkillRulesVersion


class IngestError(Exception):
    pass


def load_source_file(path: str | Path) -> list[dict]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    records = document.get("sources")
    if not isinstance(records, list):
        raise IngestError(f"{path}: 缺少 sources 列表")
    return records


def _validate_envelope(rec: dict) -> str | None:
    for field_name in ("source_id", "record_kind", "collected_at"):
        if not rec.get(field_name):
            return f"缺少字段 {field_name}"
    if not isinstance(rec.get("payload"), dict):
        return "payload 必须是对象"
    if rec["record_kind"] not in RECORD_KINDS:
        return f"未知 record_kind: {rec['record_kind']}"
    try:
        norm_ts(rec["collected_at"])
    except ValueError:
        return f"collected_at 无法解析: {rec['collected_at']}"
    return None


class Ingestor:
    def __init__(self, store: CareerStore):
        self.store = store
        self.summary = {
            "applied": 0,
            "skipped_duplicates": 0,
            "invalid": 0,
            "deferred": 0,
            "verifications_added": 0,
        }

    # ---- 入口 ----

    def ingest(self, records: list[dict]) -> dict:
        fresh = []
        for rec in records:
            err = _validate_envelope(rec)
            if err:
                self.summary["invalid"] += 1
                self._verify(
                    "invalid-record",
                    "warning",
                    f"来源记录格式无效：{err}",
                    {"source_id": rec.get("source_id"), "record_kind": rec.get("record_kind")},
                    norm_ts(rec["collected_at"]) if rec.get("collected_at") else "1970-01-01T00:00:00+00:00",
                )
                continue
            if rec["source_id"] in self.store.seen_sources:
                self.summary["skipped_duplicates"] += 1
                self._journal(rec, "skipped-duplicate", "来源记录已采集过，忽略")
                continue
            fresh.append(rec)
        fresh.sort(key=lambda r: (norm_ts(r["collected_at"]), r["source_id"]))
        for rec in fresh:
            self.store.seen_sources.add(rec["source_id"])
            self._apply(rec)
        return self.summary

    # ---- 分发 ----

    def _apply(self, rec: dict) -> None:
        handler = {
            "job_posting": self._apply_job_posting,
            "posting_update": self._apply_posting_update,
            "course": self._apply_course,
            "placement_feedback": self._apply_feedback,
            "application": self._apply_application,
            "skill_rules": self._apply_skill_rules,
        }[rec["record_kind"]]
        handler(rec)
        self.summary["applied"] += 1

    def _require(self, rec: dict, *fields: str) -> list[str] | None:
        missing = [f for f in fields if rec["payload"].get(f) is None]
        if missing:
            self.summary["invalid"] += 1
            self._verify(
                "invalid-record",
                "warning",
                f"{rec['record_kind']} 缺少字段: {', '.join(missing)}",
                {"source_id": rec["source_id"]},
                norm_ts(rec["collected_at"]),
            )
            self._journal(rec, "invalid", f"缺少字段 {missing}")
            return missing
        return None

    # ---- 岗位发布 ----

    def _apply_job_posting(self, rec: dict) -> None:
        p = rec["payload"]
        if self._require(rec, "posting_ref", "employer_ref", "title", "location",
                         "employment_kind", "updated_at", "skills"):
            return
        if p["employment_kind"] not in EMPLOYMENT_KINDS:
            self._verify(
                "invalid-record",
                "warning",
                f"未知用工性质: {p['employment_kind']}",
                {"source_id": rec["source_id"], "posting_ref": p["posting_ref"]},
                norm_ts(rec["collected_at"]),
            )
            self.summary["invalid"] += 1
            return
        collected = norm_ts(rec["collected_at"])
        content = ContentVersion(
            updated_at=norm_ts(p["updated_at"]),
            collected_at=collected,
            source_id=rec["source_id"],
            title=p["title"],
            skills=list(p["skills"]),
            location=p["location"],
            employment_kind=p["employment_kind"],
            expires_at=norm_ts(p["expires_at"]) if p.get("expires_at") else None,
        )
        ref = p["posting_ref"]
        cid = self.store.alias_index.get(ref)
        if cid is not None:
            posting = self.store.postings[cid]
        else:
            key = identity_key(p["employer_ref"], p["title"], p["location"], p["employment_kind"])
            cid = self.store.key_index.get(key)
            if cid is None:
                cid = canonical_id_for(key)
                posting = CanonicalPosting(
                    canonical_id=cid,
                    employer_ref=p["employer_ref"],
                    title_norm=key[1],
                    location=p["location"],
                    employment_kind=p["employment_kind"],
                    created_at=collected,
                )
                posting.aliases.append(
                    PostingAlias(ref, rec["source_id"], collected, "original", True)
                )
                self.store.postings[cid] = posting
                self._index(posting)
                self._journal(rec, "created", f"新招聘需求 {cid}（{p['employment_kind']}）")
            else:
                # 跨渠道转载：企业+标题+地点+用工性质一致，归为同一招聘需求
                posting = self.store.postings[cid]
                posting.aliases.append(
                    PostingAlias(
                        ref, rec["source_id"], collected,
                        "same-employer-title-location-kind", False,
                    )
                )
                self.store.alias_index[ref] = cid
                self._verify(
                    "cross-channel-repost",
                    "info",
                    f"{rec['source_id']} 的岗位 {ref} 与 {cid} 的企业/标题/地点/用工性质一致，"
                    "已归并为同一招聘需求，请与企业确认",
                    {"canonical_id": cid, "posting_ref": ref, "source_id": rec["source_id"]},
                    collected,
                )
                self._journal(rec, "linked-repost", f"{ref} 归并到 {cid}")
        self._add_content(posting, content, rec)
        self._drain_pending(posting)
        self._relink(posting)

    def _add_content(self, posting: CanonicalPosting, content: ContentVersion, rec: dict) -> None:
        if posting.contents:
            latest = posting.current_content()
            if content.updated_at == latest.updated_at and (
                content.title != latest.title
                or content.skills != latest.skills
                or content.location != latest.location
                or content.employment_kind != latest.employment_kind
                or content.expires_at != latest.expires_at
            ):
                self._verify(
                    "content-conflict",
                    "warning",
                    f"岗位 {content.source_id} 与已有内容 updated_at 相同但字段不一致，需与企业核实",
                    {"canonical_id": posting.canonical_id, "updated_at": content.updated_at},
                    content.collected_at,
                )
                self._journal(rec, "content-conflict", "相同 updated_at 内容冲突，保留先到的版本")
                return
            if (
                content.location != posting.location
                or content.employment_kind != posting.employment_kind
            ):
                self._verify(
                    "key-field-changed",
                    "warning",
                    "岗位地点或用工性质发生变化，可能影响归并结果，需与企业核实",
                    {
                        "canonical_id": posting.canonical_id,
                        "location": f"{posting.location} -> {content.location}",
                        "employment_kind": f"{posting.employment_kind} -> {content.employment_kind}",
                    },
                    content.collected_at,
                )
        posting.contents.append(content)
        posting.contents.sort(key=lambda c: (c.updated_at, c.collected_at))
        self._journal(rec, "content-updated", f"{posting.canonical_id} 内容版本 {content.updated_at}")

    # ---- 企业修订 ----

    def _apply_posting_update(self, rec: dict) -> None:
        p = rec["payload"]
        if self._require(rec, "posting_ref", "revision", "state", "effective_at"):
            return
        if p["state"] not in POSTING_STATES:
            self._verify(
                "invalid-record",
                "warning",
                f"未知岗位状态: {p['state']}",
                {"source_id": rec["source_id"], "posting_ref": p["posting_ref"]},
                norm_ts(rec["collected_at"]),
            )
            self.summary["invalid"] += 1
            return
        cid = self.store.alias_index.get(p["posting_ref"])
        if cid is None:
            # 修订先于岗位到达：暂存，岗位出现时再补应用
            self.store.pending_updates.setdefault(p["posting_ref"], []).append(rec)
            self.summary["deferred"] += 1
            self._journal(rec, "deferred", f"岗位 {p['posting_ref']} 尚未采集到，修订暂存")
            return
        self._apply_update_to(self.store.postings[cid], rec)

    def _apply_update_to(self, posting: CanonicalPosting, rec: dict) -> None:
        p = rec["payload"]
        collected = norm_ts(rec["collected_at"])
        effective = norm_ts(p["effective_at"])
        rev = int(p["revision"])
        cur_no = posting.current_revision_no()
        if rev <= cur_no:
            existing = [r for r in posting.revisions if r.revision == rev]
            if existing and (
                existing[0].state != p["state"] or existing[0].effective_at != effective
            ):
                self._verify(
                    "revision-conflict",
                    "warning",
                    f"修订 rev{rev} 与已记录版本内容不一致，需与企业核实",
                    {"canonical_id": posting.canonical_id, "revision": rev},
                    collected,
                )
                self._journal(rec, "revision-conflict", f"rev{rev} 内容冲突")
            else:
                self._journal(rec, "superseded-update", f"rev{rev} 不新于当前 rev{cur_no}，仅留档")
            return
        if rev > cur_no + 1:
            self._verify(
                "revision-gap",
                "info",
                f"修订从 rev{cur_no} 跳到 rev{rev}，中间版本缺失，需向企业核实",
                {"canonical_id": posting.canonical_id, "from": cur_no, "to": rev},
                collected,
            )
        # 字段级变更（标题/技能/地点/用工性质/有效期）
        changes: dict = {}
        content = posting.current_content()
        for field_name in ("title", "skills", "location", "employment_kind", "expires_at"):
            if p.get(field_name) is None:
                continue
            old = getattr(content, field_name)
            new = norm_ts(p[field_name]) if field_name == "expires_at" else p[field_name]
            if new != old:
                changes[field_name] = {"from": old, "to": new}
        if changes:
            if "location" in changes or "employment_kind" in changes:
                self._verify(
                    "key-field-changed",
                    "warning",
                    "企业修订改变了地点或用工性质，可能影响岗位归并，需与企业核实",
                    {"canonical_id": posting.canonical_id, "changes": changes},
                    collected,
                )
            posting.contents.append(
                ContentVersion(
                    updated_at=effective,
                    collected_at=collected,
                    source_id=rec["source_id"],
                    title=changes.get("title", {}).get("to", content.title),
                    skills=changes.get("skills", {}).get("to", content.skills),
                    location=changes.get("location", {}).get("to", content.location),
                    employment_kind=changes.get("employment_kind", {}).get(
                        "to", content.employment_kind
                    ),
                    expires_at=changes.get("expires_at", {}).get("to", content.expires_at),
                )
            )
            posting.contents.sort(key=lambda c: (c.updated_at, c.collected_at))
        if p["state"] == "converted":
            if not p.get("converted_to"):
                self._verify(
                    "invalid-record",
                    "warning",
                    "converted 状态缺少 converted_to 目标岗位",
                    {"source_id": rec["source_id"], "posting_ref": p["posting_ref"]},
                    collected,
                )
                self.summary["invalid"] += 1
                return
            posting.converted_to = p["converted_to"]
        posting.revisions.append(
            StateRevision(
                revision=rev,
                state=p["state"],
                effective_at=effective,
                collected_at=collected,
                source_id=rec["source_id"],
                changes=changes,
                converted_to=p.get("converted_to"),
            )
        )
        self._journal(rec, "state-updated", f"{posting.canonical_id} rev{rev} -> {p['state']}")

    def _drain_pending(self, posting: CanonicalPosting) -> None:
        for ref in posting.posting_refs:
            for rec in self.store.pending_updates.pop(ref, []):
                self._journal(rec, "deferred-applied", f"补应用暂存修订（岗位 {ref}）")
                self._apply_update_to(posting, rec)

    def _relink(self, posting: CanonicalPosting) -> None:
        """岗位出现后，把此前无法定位的反馈/报名挂到该需求上。"""
        refs = set(posting.posting_refs)
        for fb in self.store.feedback:
            if fb.canonical_id is None and fb.posting_ref in refs:
                fb.canonical_id = posting.canonical_id
        for app in self.store.applications.values():
            if app.canonical_id is None and app.posting_ref in refs:
                app.canonical_id = posting.canonical_id

    # ---- 课程 ----

    def _apply_course(self, rec: dict) -> None:
        p = rec["payload"]
        if self._require(rec, "course_ref", "revision", "skills", "term"):
            return
        collected = norm_ts(rec["collected_at"])
        course = self.store.courses.get(p["course_ref"])
        if course is None:
            course = Course(course_ref=p["course_ref"])
            self.store.courses[p["course_ref"]] = course
        rev = int(p["revision"])
        existing = [r for r in course.revisions if r.revision == rev]
        if existing:
            if existing[0].skills != list(p["skills"]) or existing[0].term != p["term"]:
                self._verify(
                    "course-revision-conflict",
                    "warning",
                    f"课程 {p['course_ref']} rev{rev} 与已记录版本内容不一致",
                    {"course_ref": p["course_ref"], "revision": rev},
                    collected,
                )
                self._journal(rec, "course-revision-conflict", f"rev{rev} 内容冲突")
            else:
                self._journal(rec, "superseded-update", f"课程 rev{rev} 已存在")
            return
        course.revisions.append(
            CourseRevision(
                revision=rev,
                skills=list(p["skills"]),
                term=p["term"],
                collected_at=collected,
                source_id=rec["source_id"],
            )
        )
        course.revisions.sort(key=lambda r: r.revision)
        self._journal(rec, "course-updated", f"{p['course_ref']} rev{rev}")

    # ---- 反馈 / 报名 / 技能规则 ----

    def _apply_feedback(self, rec: dict) -> None:
        p = rec["payload"]
        if self._require(rec, "posting_ref", "stage", "response_state"):
            return
        if p["stage"] not in FEEDBACK_STAGES or p["response_state"] not in RESPONSE_STATES:
            self._verify(
                "invalid-record",
                "warning",
                f"反馈阶段或状态非法: {p['stage']}/{p['response_state']}",
                {"source_id": rec["source_id"]},
                norm_ts(rec["collected_at"]),
            )
            self.summary["invalid"] += 1
            return
        cid = self.store.alias_index.get(p["posting_ref"])
        self.store.feedback.append(
            PlacementFeedback(
                feedback_id=rec["source_id"],
                posting_ref=p["posting_ref"],
                canonical_id=cid,
                course_ref=p.get("course_ref"),
                stage=p["stage"],
                response_state=p["response_state"],
                skill_revision=p.get("skill_revision"),
                collected_at=norm_ts(rec["collected_at"]),
            )
        )
        note = "" if cid else "（岗位尚未采集到，待关联）"
        self._journal(rec, "feedback-recorded", f"{p['posting_ref']} {p['stage']}/{p['response_state']}{note}")

    def _apply_application(self, rec: dict) -> None:
        p = rec["payload"]
        if self._require(rec, "application_ref", "posting_ref", "student_ref", "applied_at"):
            return
        if p["application_ref"] in self.store.applications:
            self._journal(rec, "skipped-duplicate", f"报名 {p['application_ref']} 已存在")
            return
        cid = self.store.alias_index.get(p["posting_ref"])
        self.store.applications[p["application_ref"]] = Application(
            application_ref=p["application_ref"],
            posting_ref=p["posting_ref"],
            canonical_id=cid,
            student_ref=p["student_ref"],
            applied_at=norm_ts(p["applied_at"]),
            source_id=rec["source_id"],
        )
        self._journal(rec, "application-recorded", f"{p['application_ref']} -> {p['posting_ref']}")

    def _apply_skill_rules(self, rec: dict) -> None:
        p = rec["payload"]
        if self._require(rec, "version", "effective_at", "aliases"):
            return
        rules = SkillRulesVersion(
            version=int(p["version"]),
            effective_at=norm_ts(p["effective_at"]),
            aliases={k.strip().lower(): v.strip().lower() for k, v in p["aliases"].items()},
        )
        try:
            result = self.store.taxonomy.add_version(rules)
        except ValueError as exc:
            self._verify(
                "taxonomy-conflict",
                "warning",
                str(exc),
                {"source_id": rec["source_id"], "version": rules.version},
                norm_ts(rec["collected_at"]),
            )
            self._journal(rec, "taxonomy-conflict", str(exc))
            return
        self._journal(
            rec,
            "taxonomy-updated" if result == "added" else "skipped-duplicate",
            f"技能分类规则 v{rules.version}",
        )

    # ---- 工具 ----

    def _index(self, posting: CanonicalPosting) -> None:
        for alias in posting.aliases:
            self.store.alias_index[alias.posting_ref] = posting.canonical_id
        self.store.key_index[
            (posting.employer_ref, posting.title_norm, posting.location, posting.employment_kind)
        ] = posting.canonical_id

    def _verify(self, kind: str, severity: str, message: str, refs: dict, detected_at: str) -> None:
        self.store.verifications.append(
            VerificationItem(
                kind=kind, severity=severity, message=message, refs=refs, detected_at=detected_at
            )
        )
        self.summary["verifications_added"] += 1

    def _journal(self, rec: dict, action: str, detail: str) -> None:
        self.store.journal.append(
            {
                "seq": len(self.store.journal) + 1,
                "source_id": rec.get("source_id"),
                "record_kind": rec.get("record_kind"),
                "collected_at": rec.get("collected_at"),
                "action": action,
                "detail": detail,
            }
        )


def ingest_records(store: CareerStore, records: list[dict]) -> dict:
    return Ingestor(store).ingest(records)


def ingest_file(store: CareerStore, path: str | Path) -> dict:
    return ingest_records(store, load_source_file(path))
