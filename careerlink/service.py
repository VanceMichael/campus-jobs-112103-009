"""就业中心服务层：岗位状态视图、推送快照、培养调整解释、待核实清单。"""
from __future__ import annotations

from pathlib import Path

from .ingest import ingest_file, ingest_records
from .models import (
    FILLED_CONFIRM_STAGES,
    STATUS_LABELS,
    CanonicalPosting,
    Push,
    parse_ts,
    utcnow,
)
from .store import CareerStore, load_store, new_store, save_store

_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


class CareerService:
    def __init__(self, store: CareerStore):
        self.store = store

    @classmethod
    def create(cls) -> "CareerService":
        return cls(new_store())

    @classmethod
    def load(cls, path: str | Path) -> "CareerService":
        return cls(load_store(path))

    @classmethod
    def load_or_create(cls, path: str | Path) -> "CareerService":
        if Path(path).exists():
            return cls.load(path)
        return cls.create()

    def save(self, path: str | Path) -> None:
        save_store(self.store, path)

    # ---- 采集 ----

    def ingest_file(self, path: str | Path) -> dict:
        return ingest_file(self.store, path)

    def ingest_records(self, records: list[dict]) -> dict:
        return ingest_records(self.store, records)

    # ---- 派生状态 ----

    def _now(self, now: str | None) -> str:
        return now or utcnow()

    def is_expired(self, posting: CanonicalPosting, now: str) -> bool:
        expires = posting.current_content().expires_at
        return bool(expires and expires <= now)

    def last_activity(self, posting: CanonicalPosting, now: str) -> str:
        candidates = [c.updated_at for c in posting.contents]
        candidates += [r.collected_at for r in posting.revisions]
        candidates += [r.effective_at for r in posting.revisions if r.effective_at <= now]
        return max(candidates)

    def is_stale(self, posting: CanonicalPosting, now: str) -> bool:
        last = parse_ts(self.last_activity(posting, now))
        return (parse_ts(now) - last).days > self.store.stale_days

    def fill_status(self, canonical_id: str, at: str | None = None) -> dict:
        """反馈充分性。未收到反馈不等于无人申请，面试反馈不足不能算招满。"""
        entries = [
            f for f in self.store.feedback
            if f.canonical_id == canonical_id and (at is None or f.collected_at <= at)
        ]
        received = [f for f in entries if f.response_state == "received"]
        pending = [f for f in entries if f.response_state == "pending"]
        if any(f.stage in FILLED_CONFIRM_STAGES for f in received):
            status = "filled-confirmed"
        elif any(f.stage == "offer" for f in received):
            status = "offer-received"
        elif received:
            status = "in-progress"
        elif pending:
            status = "awaiting-feedback"
        else:
            status = "no-feedback"
        notes = []
        if status == "no-feedback":
            notes.append("暂无企业反馈（不代表无人申请）")
        elif status == "awaiting-feedback":
            notes.append("反馈尚未返回，申请结果未知（不代表无人申请）")
        elif status in ("in-progress", "offer-received"):
            notes.append("反馈不足以确认招满")
        return {
            "status": status,
            "received": len(received),
            "pending": len(pending),
            "stages_received": sorted({f.stage for f in received}),
            "notes": notes,
        }

    def filled_confirmed(self, canonical_id: str, at: str | None = None) -> bool:
        return self.fill_status(canonical_id, at)["status"] == "filled-confirmed"

    def effective_status(self, posting: CanonicalPosting, now: str) -> str:
        """展示状态。企业申报 filled 但反馈不足时按 closed 处理，绝不直接标已招满。"""
        state = posting.current_state()
        if state == "filled":
            return "filled" if self.filled_confirmed(posting.canonical_id, now) else "closed"
        if state == "open" and self.is_expired(posting, now):
            return "expired"
        return state

    def filled_claim_unconfirmed(self, posting: CanonicalPosting, now: str) -> bool:
        return posting.current_state() == "filled" and not self.filled_confirmed(
            posting.canonical_id, now
        )

    # ---- 岗位视图 ----

    def _posting_summary(self, posting: CanonicalPosting, now: str) -> dict:
        content = posting.current_content()
        tax = self.store.taxonomy.current()
        normalized = self.store.taxonomy.normalize(content.skills)
        status = self.effective_status(posting, now)
        applications = [
            a for a in self.store.applications.values() if a.canonical_id == posting.canonical_id
        ]
        return {
            "canonical_id": posting.canonical_id,
            "title": content.title,
            "employer_ref": posting.employer_ref,
            "location": posting.location,
            "employment_kind": posting.employment_kind,
            "status": status,
            "status_label": STATUS_LABELS[status],
            "filled_claim_unconfirmed": self.filled_claim_unconfirmed(posting, now),
            "stale": status == "open" and self.is_stale(posting, now),
            "expires_at": content.expires_at,
            "last_activity": self.last_activity(posting, now),
            "skills": list(content.skills),
            "skills_normalized": normalized["canonical"],
            "taxonomy_version": tax.version,
            "posting_refs": posting.posting_refs,
            "sources": posting.sources,
            "applications_recorded": len(applications),
            "fill_status": self.fill_status(posting.canonical_id, now)["status"],
        }

    def list_postings(
        self,
        source: str | None = None,
        state: str | None = None,
        employment_kind: str | None = None,
        employer: str | None = None,
        as_of: str | None = None,
        now: str | None = None,
    ) -> list[dict]:
        """按来源和有效期查看岗位。as_of 给出时回到该时点的状态。"""
        point = as_of or self._now(now)
        out = []
        for posting in self.store.postings.values():
            if posting.created_at > point:
                continue
            if source and source not in posting.sources:
                continue
            if employment_kind and posting.employment_kind != employment_kind:
                continue
            if employer and posting.employer_ref != employer:
                continue
            summary = self._posting_summary(posting, point)
            if as_of:
                # 回到 as_of 时点：状态按当时生效的修订计算
                raw = posting.state_at(as_of)
                if raw == "filled":
                    summary["status"] = (
                        "filled" if self.filled_confirmed(posting.canonical_id, as_of) else "closed"
                    )
                elif raw == "open" and self.is_expired(posting, as_of):
                    summary["status"] = "expired"
                else:
                    summary["status"] = raw
                summary["status_label"] = STATUS_LABELS[summary["status"]]
                content = posting.content_at(as_of)
                summary["title"] = content.title
                summary["skills"] = list(content.skills)
                summary["skills_normalized"] = self.store.taxonomy.normalize(content.skills)["canonical"]
            if state and summary["status"] != state:
                continue
            out.append(summary)
        out.sort(key=lambda s: (s["employer_ref"], s["canonical_id"]))
        return out

    def posting_detail(self, canonical_id: str, now: str | None = None) -> dict:
        posting = self.store.postings.get(canonical_id)
        if posting is None:
            raise KeyError(f"未知岗位: {canonical_id}")
        now = self._now(now)
        summary = self._posting_summary(posting, now)
        status_label = summary["status_label"]
        applications = [
            {
                "application_ref": a.application_ref,
                "student_ref": a.student_ref,
                "applied_at": a.applied_at,
                "note": None
                if summary["status"] == "open"
                else f"岗位{status_label}，报名记录保留",
            }
            for a in sorted(
                (a for a in self.store.applications.values() if a.canonical_id == canonical_id),
                key=lambda a: a.applied_at,
            )
        ]
        feedback = [
            {
                "feedback_id": f.feedback_id,
                "stage": f.stage,
                "response_state": f.response_state,
                "course_ref": f.course_ref,
                "skill_revision": f.skill_revision,
                "collected_at": f.collected_at,
            }
            for f in self.store.feedback
            if f.canonical_id == canonical_id
        ]
        # 关联培养活动：技能有交集或反馈引用的课程
        linked_courses = []
        pskills = set(summary["skills_normalized"])
        for course in self.store.courses.values():
            current = course.current()
            cnorm = self.store.taxonomy.normalize(current.skills)
            matched = sorted(pskills & set(cnorm["canonical"]))
            fb_count = sum(1 for f in feedback if f["course_ref"] == course.course_ref)
            if matched or fb_count:
                linked_courses.append(
                    {
                        "course_ref": course.course_ref,
                        "revision": current.revision,
                        "term": current.term,
                        "matched_skills": matched,
                        "taxonomy_version": cnorm["version"],
                        "feedback_count": fb_count,
                    }
                )
        # 同企业同标题但地点/用工性质不同的岗位：明确是不同岗位
        related = [
            {
                "canonical_id": other.canonical_id,
                "location": other.location,
                "employment_kind": other.employment_kind,
                "note": "同企业同标题，但地点或用工性质不同，视为不同岗位",
            }
            for other in self.store.postings.values()
            if other.canonical_id != canonical_id
            and other.employer_ref == posting.employer_ref
            and other.title_norm == posting.title_norm
        ]
        converted_to = None
        if posting.converted_to:
            target = self.store.alias_index.get(posting.converted_to)
            converted_to = {"posting_ref": posting.converted_to, "canonical_id": target}
        converted_from = [
            p.canonical_id
            for p in self.store.postings.values()
            if p.converted_to and p.converted_to in posting.posting_refs
        ]
        verifications = [
            v for v in self.pending_verifications(now)
            if v["refs"].get("canonical_id") == canonical_id
        ]
        return {
            **summary,
            "aliases": [
                {
                    "posting_ref": a.posting_ref,
                    "source_id": a.source_id,
                    "first_seen": a.first_seen,
                    "match_reason": a.match_reason,
                    "confirmed": a.confirmed,
                }
                for a in posting.aliases
            ],
            "contents": [vars(c) for c in posting.contents],
            "revisions": [vars(r) for r in posting.revisions],
            "applications": applications,
            "feedback": feedback,
            "fill_detail": self.fill_status(canonical_id, now),
            "linked_courses": linked_courses,
            "related_postings": related,
            "converted_to": converted_to,
            "converted_from": converted_from,
            "pushes": [
                {"push_id": p.push_id, "created_at": p.created_at, "course_ref": p.course_ref}
                for p in self.store.pushes.values()
                if p.canonical_id == canonical_id
            ],
            "verifications": verifications,
        }

    # ---- 待核实清单 ----

    def pending_verifications(self, now: str | None = None) -> list[dict]:
        now = self._now(now)
        items = [
            {"kind": v.kind, "severity": v.severity, "message": v.message,
             "refs": v.refs, "detected_at": v.detected_at}
            for v in self.store.verifications
        ]
        for posting in self.store.postings.values():
            status = self.effective_status(posting, now)
            if status == "open" and self.is_stale(posting, now):
                items.append({
                    "kind": "stale-open",
                    "severity": "warning",
                    "message": (
                        f"岗位 {posting.canonical_id} 超过 {self.store.stale_days} 天未更新仍显示在招，"
                        "需与企业核实是否仍在招聘"
                    ),
                    "refs": {"canonical_id": posting.canonical_id},
                    "detected_at": now,
                })
            if self.filled_claim_unconfirmed(posting, now):
                items.append({
                    "kind": "filled-unconfirmed",
                    "severity": "critical",
                    "message": (
                        f"企业申报岗位 {posting.canonical_id} 已招满，但录用反馈不足，"
                        "不能标记为已招满，需与企业核实"
                    ),
                    "refs": {"canonical_id": posting.canonical_id},
                    "detected_at": now,
                })
            if posting.converted_to and posting.converted_to not in self.store.alias_index:
                items.append({
                    "kind": "conversion-target-missing",
                    "severity": "warning",
                    "message": (
                        f"岗位 {posting.canonical_id} 已转化为 {posting.converted_to}，"
                        "但目标岗位尚未采集到，需与企业核实"
                    ),
                    "refs": {"canonical_id": posting.canonical_id,
                             "posting_ref": posting.converted_to},
                    "detected_at": now,
                })
        for fb in self.store.feedback:
            if fb.canonical_id is None:
                items.append({
                    "kind": "unresolved-reference",
                    "severity": "warning",
                    "message": f"反馈 {fb.feedback_id} 指向未采集到的岗位 {fb.posting_ref}",
                    "refs": {"posting_ref": fb.posting_ref, "feedback_id": fb.feedback_id},
                    "detected_at": fb.collected_at,
                })
            if fb.course_ref and fb.skill_revision is not None:
                course = self.store.courses.get(fb.course_ref)
                if course and fb.skill_revision < course.current().revision:
                    items.append({
                        "kind": "feedback-course-revision-mismatch",
                        "severity": "info",
                        "message": (
                            f"反馈 {fb.feedback_id} 基于课程 {fb.course_ref} "
                            f"rev{fb.skill_revision}，当前为 rev{course.current().revision}"
                        ),
                        "refs": {"course_ref": fb.course_ref, "feedback_id": fb.feedback_id},
                        "detected_at": now,
                    })
        for app in self.store.applications.values():
            if app.canonical_id is None:
                items.append({
                    "kind": "unresolved-reference",
                    "severity": "warning",
                    "message": f"报名 {app.application_ref} 指向未采集到的岗位 {app.posting_ref}",
                    "refs": {"posting_ref": app.posting_ref,
                             "application_ref": app.application_ref},
                    "detected_at": app.applied_at,
                })
        for ref, updates in self.store.pending_updates.items():
            items.append({
                "kind": "unresolved-reference",
                "severity": "warning",
                "message": f"{len(updates)} 条修订指向未采集到的岗位 {ref}",
                "refs": {"posting_ref": ref},
                "detected_at": now,
            })
        items.sort(key=lambda i: (_SEVERITY_RANK.get(i["severity"], 3), i["detected_at"]))
        return items

    # ---- 推送（快照可回看） ----

    def _recommendation_basis(self, posting: CanonicalPosting, course, now: str) -> dict:
        tax = self.store.taxonomy.current()
        content = posting.current_content()
        pnorm = self.store.taxonomy.normalize(content.skills)
        basis = {
            "taxonomy_version": tax.version,
            "posting": {
                "canonical_id": posting.canonical_id,
                "title": content.title,
                "employer_ref": posting.employer_ref,
                "location": posting.location,
                "employment_kind": posting.employment_kind,
                "status": self.effective_status(posting, now),
                "skills_raw": list(content.skills),
                "skills_normalized": pnorm["canonical"],
                "content_updated_at": content.updated_at,
            },
            "course": None,
            "matched_skills": [],
        }
        if course is not None:
            current = course.current()
            cnorm = self.store.taxonomy.normalize(current.skills)
            basis["course"] = {
                "course_ref": course.course_ref,
                "revision": current.revision,
                "term": current.term,
                "skills_normalized": cnorm["canonical"],
            }
            basis["matched_skills"] = sorted(
                set(pnorm["canonical"]) & set(cnorm["canonical"])
            )
        basis["explanation"] = self._explain_basis(basis)
        return basis

    @staticmethod
    def _explain_basis(basis: dict) -> str:
        p = basis["posting"]
        text = (
            f"依据技能分类规则 v{basis['taxonomy_version']}，岗位《{p['title']}》"
            f"（{p['employer_ref']} / {p['location']} / {p['employment_kind']}，"
            f"状态 {STATUS_LABELS.get(p['status'], p['status'])}）"
            f"技能要求 {p['skills_normalized']}"
        )
        if basis["course"]:
            c = basis["course"]
            text += (
                f"，与课程 {c['course_ref']}（rev{c['revision']}）"
                f"匹配技能 {basis['matched_skills']}"
            )
        return text + "。"

    def recommend(
        self,
        canonical_id: str,
        course_ref: str | None = None,
        student_ref: str | None = None,
        now: str | None = None,
    ) -> dict:
        """生成一条学生推送。快照冻结当前依据，之后的推荐才使用新规则。"""
        posting = self.store.postings.get(canonical_id)
        if posting is None:
            raise KeyError(f"未知岗位: {canonical_id}")
        course = None
        if course_ref is not None:
            course = self.store.courses.get(course_ref)
            if course is None:
                raise KeyError(f"未知课程: {course_ref}")
        now = self._now(now)
        basis = self._recommendation_basis(posting, course, now)
        warnings = []
        if basis["posting"]["status"] != "open":
            warnings.append(
                f"岗位当前状态为 {STATUS_LABELS[basis['posting']['status']]}，不建议继续推送"
            )
        elif self.is_stale(posting, now):
            warnings.append(
                f"岗位超过 {self.store.stale_days} 天未更新，推送前请先与企业核实"
            )
        push_id = f"PUSH-{len(self.store.pushes) + 1:04d}"
        push = Push(
            push_id=push_id,
            created_at=now,
            canonical_id=canonical_id,
            student_ref=student_ref,
            course_ref=course_ref,
            warnings=warnings,
            snapshot={**basis, "note": "本快照为生成时依据；后续规则或岗位变化不影响其内容"},
        )
        self.store.pushes[push_id] = push
        self.store.journal.append({
            "seq": len(self.store.journal) + 1,
            "source_id": None,
            "record_kind": "push",
            "collected_at": now,
            "action": "push-created",
            "detail": f"{push_id} -> {canonical_id}",
        })
        return {
            "push_id": push_id,
            "created_at": now,
            "canonical_id": canonical_id,
            "course_ref": course_ref,
            "student_ref": student_ref,
            "warnings": warnings,
            "basis": basis,
        }

    def review_push(self, push_id: str, now: str | None = None) -> dict:
        """回看已发推送：当时依据原样返回，并对照当前有效信息。"""
        push = self.store.pushes.get(push_id)
        if push is None:
            raise KeyError(f"未知推送: {push_id}")
        now = self._now(now)
        posting = self.store.postings.get(push.canonical_id)
        course = (
            self.store.courses.get(push.course_ref) if push.course_ref else None
        )
        current = self._recommendation_basis(posting, course, now) if posting else None
        changes = []
        if current:
            old = push.snapshot
            if old["taxonomy_version"] != current["taxonomy_version"]:
                changes.append(
                    f"技能分类规则 v{old['taxonomy_version']} -> v{current['taxonomy_version']}"
                )
            if old["posting"]["status"] != current["posting"]["status"]:
                changes.append(
                    f"岗位状态 {old['posting']['status']} -> {current['posting']['status']}"
                )
            if old["posting"]["skills_normalized"] != current["posting"]["skills_normalized"]:
                changes.append("岗位技能归一化结果已变化")
            old_rev = (old.get("course") or {}).get("revision")
            new_rev = (current.get("course") or {}).get("revision")
            if old_rev != new_rev:
                changes.append(f"课程版本 rev{old_rev} -> rev{new_rev}")
            if old["matched_skills"] != current["matched_skills"]:
                changes.append(
                    f"匹配技能 {old['matched_skills']} -> {current['matched_skills']}"
                )
        return {
            "push_id": push_id,
            "warnings_at_push": push.warnings,
            "basis_at_push": push.snapshot,
            "basis_current": current,
            "changed": bool(changes),
            "changes": changes,
        }

    # ---- 培养跟进 ----

    def explain_course_adjustment(self, course_ref: str, now: str | None = None) -> dict:
        """解释课程调整来自哪些仍有效的企业需求。"""
        course = self.store.courses.get(course_ref)
        if course is None:
            raise KeyError(f"未知课程: {course_ref}")
        now = self._now(now)
        revisions = sorted(course.revisions, key=lambda r: r.revision)
        latest = revisions[-1]
        previous = revisions[-2] if len(revisions) > 1 else None
        tax = self.store.taxonomy.current()
        latest_norm = self.store.taxonomy.normalize(latest.skills)
        prev_norm = (
            self.store.taxonomy.normalize(previous.skills)
            if previous
            else {"canonical": []}
        )
        added = sorted(set(latest_norm["canonical"]) - set(prev_norm["canonical"]))
        removed = sorted(set(prev_norm["canonical"]) - set(latest_norm["canonical"]))
        # 只有当前仍有效（在招且未过期）的岗位才能作为调整依据
        valid_postings = [
            p for p in self.store.postings.values()
            if self.effective_status(p, now) == "open"
        ]
        evidence: dict[str, list[dict]] = {}
        for skill in added:
            hits = []
            for posting in valid_postings:
                pnorm = self.store.taxonomy.normalize(
                    posting.current_content().skills
                )
                if skill in pnorm["canonical"]:
                    hits.append({
                        "canonical_id": posting.canonical_id,
                        "title": posting.current_content().title,
                        "employer_ref": posting.employer_ref,
                        "location": posting.location,
                        "employment_kind": posting.employment_kind,
                        "sources": posting.sources,
                        "last_activity": self.last_activity(posting, now),
                        "stale": self.is_stale(posting, now),
                    })
            evidence[skill] = sorted(hits, key=lambda h: h["canonical_id"])
        unexplained = [s for s in added if not evidence[s]]
        feedback = [
            {
                "feedback_id": f.feedback_id,
                "posting_ref": f.posting_ref,
                "canonical_id": f.canonical_id,
                "stage": f.stage,
                "response_state": f.response_state,
                "skill_revision": f.skill_revision,
                "collected_at": f.collected_at,
            }
            for f in self.store.feedback
            if f.course_ref == course_ref
        ]
        return {
            "course_ref": course_ref,
            "term": latest.term,
            "from_revision": previous.revision if previous else None,
            "to_revision": latest.revision,
            "taxonomy_version": tax.version,
            "added_skills": added,
            "removed_skills": removed,
            "evidence": evidence,
            "unexplained_skills": unexplained,
            "feedback_context": feedback,
            "note": "依据为当前仍在招且未过期的企业需求；已关闭/已转化/已过期的岗位不计入",
        }

    def course_detail(self, course_ref: str, now: str | None = None) -> dict:
        course = self.store.courses.get(course_ref)
        if course is None:
            raise KeyError(f"未知课程: {course_ref}")
        now = self._now(now)
        current = course.current()
        cnorm = self.store.taxonomy.normalize(current.skills)
        linked_postings = []
        for posting in self.store.postings.values():
            pnorm = self.store.taxonomy.normalize(posting.current_content().skills)
            matched = sorted(set(cnorm["canonical"]) & set(pnorm["canonical"]))
            if matched:
                status = self.effective_status(posting, now)
                linked_postings.append({
                    "canonical_id": posting.canonical_id,
                    "title": posting.current_content().title,
                    "status": status,
                    "status_label": STATUS_LABELS[status],
                    "matched_skills": matched,
                })
        return {
            "course_ref": course_ref,
            "current_revision": current.revision,
            "term": current.term,
            "skills": list(current.skills),
            "skills_normalized": cnorm["canonical"],
            "taxonomy_version": cnorm["version"],
            "revisions": [vars(r) for r in sorted(course.revisions, key=lambda r: r.revision)],
            "linked_postings": sorted(linked_postings, key=lambda p: p["canonical_id"]),
            "feedback": [
                {
                    "feedback_id": f.feedback_id,
                    "posting_ref": f.posting_ref,
                    "stage": f.stage,
                    "response_state": f.response_state,
                    "skill_revision": f.skill_revision,
                    "collected_at": f.collected_at,
                }
                for f in self.store.feedback
                if f.course_ref == course_ref
            ],
        }

    # ---- 汇总 ----

    def summary(self, now: str | None = None) -> dict:
        """汇总统计。实习/正式/短期项目分开计数，不混合。"""
        now = self._now(now)
        by_status_kind: dict[str, dict[str, int]] = {}
        for posting in self.store.postings.values():
            status = self.effective_status(posting, now)
            bucket = by_status_kind.setdefault(status, {})
            bucket[posting.employment_kind] = bucket.get(posting.employment_kind, 0) + 1
        by_source: dict[str, int] = {}
        for posting in self.store.postings.values():
            for source in posting.sources:
                by_source[source] = by_source.get(source, 0) + 1
        received = sum(1 for f in self.store.feedback if f.response_state == "received")
        pending = sum(1 for f in self.store.feedback if f.response_state == "pending")
        with_feedback = {f.canonical_id for f in self.store.feedback if f.canonical_id}
        verifications = self.pending_verifications(now)
        by_severity: dict[str, int] = {}
        for item in verifications:
            by_severity[item["severity"]] = by_severity.get(item["severity"], 0) + 1
        return {
            "now": now,
            "postings_total": len(self.store.postings),
            "by_status_and_kind": by_status_kind,
            "by_source": by_source,
            "applications_total": len(self.store.applications),
            "feedback": {
                "received": received,
                "pending": pending,
                "postings_without_feedback": len(self.store.postings) - len(with_feedback),
            },
            "courses_total": len(self.store.courses),
            "taxonomy_version": self.store.taxonomy.current().version,
            "pushes_total": len(self.store.pushes),
            "verifications": by_severity,
        }
