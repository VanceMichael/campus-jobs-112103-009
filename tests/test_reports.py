import unittest

from careerlink import CareerService


def rec(source_id, kind, collected, payload):
    return {
        "source_id": source_id,
        "record_kind": kind,
        "collected_at": collected,
        "payload": payload,
    }


def posting(source_id, ref, employer, kind, skills, title="岗位",
            location="CITY-A", expires=None):
    payload = {
        "posting_ref": ref, "employer_ref": employer, "title": title,
        "location": location, "employment_kind": kind,
        "updated_at": "2026-09-01T08:00:00+08:00", "skills": skills,
    }
    if expires:
        payload["expires_at"] = expires
    return rec(source_id, "job_posting", "2026-09-01T09:00:00+08:00", payload)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.service = CareerService.create()
        self.service.ingest_records([
            posting("s1", "P-1", "E-1", "full-time", ["vision-basics"]),
            posting("s2", "P-2", "E-2", "internship", ["vision-basics"]),
            posting("s3", "P-3", "E-3", "short-project", ["vision-basics"],
                    expires="2026-09-05T00:00:00+08:00"),
            posting("s4", "P-4", "E-4", "full-time", ["welding"]),
            rec("c1", "course", "2026-09-02T09:00:00+08:00", {
                "course_ref": "C-1", "revision": 1, "term": "2026-autumn",
                "skills": ["welding"],
            }),
            rec("c2", "course", "2026-09-03T09:00:00+08:00", {
                "course_ref": "C-1", "revision": 2, "term": "2026-autumn",
                "skills": ["welding", "vision-basics"],
            }),
            rec("u1", "posting_update", "2026-09-04T09:00:00+08:00", {
                "posting_ref": "P-4", "revision": 2, "state": "closed",
                "effective_at": "2026-09-04T08:00:00+08:00",
            }),
        ])
        self.now = "2026-09-06T00:00:00+08:00"

    def test_course_adjustment_explained_by_valid_postings_only(self):
        explain = self.service.explain_course_adjustment("C-1", now=self.now)
        self.assertEqual(explain["from_revision"], 1)
        self.assertEqual(explain["to_revision"], 2)
        self.assertEqual(explain["added_skills"], ["vision-basics"])
        evidence = explain["evidence"]["vision-basics"]
        ids = {e["canonical_id"] for e in evidence}
        # P-1、P-2 在招有效；P-3 已过有效期，不计入依据
        self.assertEqual(ids, {
            self.service.store.alias_index["P-1"],
            self.service.store.alias_index["P-2"],
        })

    def test_adjustment_without_demand_is_listed_as_unexplained(self):
        self.service.ingest_records([
            rec("c3", "course", "2026-09-05T09:00:00+08:00", {
                "course_ref": "C-1", "revision": 3, "term": "2026-autumn",
                "skills": ["welding", "vision-basics", "quantum-basics"],
            }),
        ])
        explain = self.service.explain_course_adjustment("C-1", now=self.now)
        self.assertEqual(explain["unexplained_skills"], ["quantum-basics"])

    def test_summary_separates_employment_kinds(self):
        summary = self.service.summary(now=self.now)
        by_kind = summary["by_status_and_kind"]
        self.assertEqual(by_kind["open"]["full-time"], 1)
        self.assertEqual(by_kind["open"]["internship"], 1)
        self.assertEqual(by_kind["expired"]["short-project"], 1)
        self.assertEqual(by_kind["closed"]["full-time"], 1)
        # 不混合统计：总数 = 各类之和
        total = sum(sum(k.values()) for k in by_kind.values())
        self.assertEqual(total, summary["postings_total"])

    def test_summary_feedback_coverage(self):
        summary = self.service.summary(now=self.now)
        self.assertEqual(summary["feedback"]["postings_without_feedback"], 4)
        self.assertEqual(summary["feedback"]["received"], 0)

    def test_course_detail_links_postings_and_feedback(self):
        self.service.ingest_records([
            rec("fb1", "placement_feedback", "2026-09-05T10:00:00+08:00", {
                "posting_ref": "P-1", "course_ref": "C-1", "stage": "interview",
                "response_state": "received", "skill_revision": 1,
            }),
        ])
        detail = self.service.course_detail("C-1", now=self.now)
        self.assertEqual(detail["current_revision"], 2)
        self.assertEqual(len(detail["feedback"]), 1)
        linked = {p["canonical_id"] for p in detail["linked_postings"]}
        self.assertIn(self.service.store.alias_index["P-1"], linked)
        # 反馈基于旧课程版本 -> 提示核实
        items = self.service.pending_verifications(now=self.now)
        self.assertTrue(any(
            i["kind"] == "feedback-course-revision-mismatch" for i in items
        ))

    def test_list_postings_never_mixes_kinds(self):
        full_time = self.service.list_postings(employment_kind="full-time", now=self.now)
        self.assertTrue(all(p["employment_kind"] == "full-time" for p in full_time))
        self.assertEqual(len(full_time), 2)


if __name__ == "__main__":
    unittest.main()
