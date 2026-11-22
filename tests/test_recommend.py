import unittest

from careerlink import CareerService


def rec(source_id, kind, collected, payload):
    return {
        "source_id": source_id,
        "record_kind": kind,
        "collected_at": collected,
        "payload": payload,
    }


class RecommendTests(unittest.TestCase):
    def setUp(self):
        self.service = CareerService.create()
        self.service.ingest_records([
            rec("s1", "job_posting", "2026-09-01T09:00:00+08:00", {
                "posting_ref": "P-1", "employer_ref": "E-1", "title": "设备运维",
                "location": "CITY-A", "employment_kind": "full-time",
                "updated_at": "2026-09-01T08:00:00+08:00",
                "skills": ["equipment-diagnostics", "plc-basics"],
            }),
            rec("s2", "course", "2026-09-01T10:00:00+08:00", {
                "course_ref": "C-1", "revision": 1, "term": "2026-autumn",
                "skills": ["device-diagnostics", "plc-basics"],
            }),
        ])
        self.cid = self.service.store.alias_index["P-1"]

    def test_push_snapshot_frozen_when_taxonomy_changes(self):
        # 规则 v1（无别名）：equipment-diagnostics 与 device-diagnostics 不匹配
        push1 = self.service.recommend(
            self.cid, course_ref="C-1", student_ref="STU-1",
            now="2026-09-02T08:00:00+08:00",
        )
        self.assertEqual(push1["basis"]["taxonomy_version"], 1)
        self.assertEqual(push1["basis"]["matched_skills"], ["plc-basics"])
        # 规则调整为 v2：equipment-diagnostics 并入 device-diagnostics
        self.service.ingest_records([
            rec("rules-2", "skill_rules", "2026-09-03T09:00:00+08:00", {
                "version": 2, "effective_at": "2026-09-03T00:00:00+08:00",
                "aliases": {"equipment-diagnostics": "device-diagnostics"},
            }),
        ])
        # 回看旧推送：仍是当时依据
        review = self.service.review_push(
            push1["push_id"], now="2026-09-04T08:00:00+08:00"
        )
        self.assertEqual(review["basis_at_push"]["taxonomy_version"], 1)
        self.assertEqual(review["basis_at_push"]["matched_skills"], ["plc-basics"])
        self.assertTrue(review["changed"])
        self.assertTrue(any("v1 -> v2" in c for c in review["changes"]))
        # 之后的推荐使用当前有效规则
        push2 = self.service.recommend(
            self.cid, course_ref="C-1", student_ref="STU-2",
            now="2026-09-04T09:00:00+08:00",
        )
        self.assertEqual(push2["basis"]["taxonomy_version"], 2)
        self.assertEqual(
            push2["basis"]["matched_skills"],
            ["device-diagnostics", "plc-basics"],
        )

    def test_snapshot_keeps_state_at_push_time(self):
        push = self.service.recommend(
            self.cid, now="2026-09-02T08:00:00+08:00"
        )
        self.service.ingest_records([
            rec("u1", "posting_update", "2026-09-03T09:00:00+08:00", {
                "posting_ref": "P-1", "revision": 2, "state": "closed",
                "effective_at": "2026-09-03T08:00:00+08:00",
            }),
        ])
        review = self.service.review_push(
            push["push_id"], now="2026-09-04T08:00:00+08:00"
        )
        self.assertEqual(review["basis_at_push"]["posting"]["status"], "open")
        self.assertEqual(review["basis_current"]["posting"]["status"], "closed")
        self.assertTrue(any("岗位状态" in c for c in review["changes"]))

    def test_recommend_closed_posting_warns(self):
        self.service.ingest_records([
            rec("u1", "posting_update", "2026-09-03T09:00:00+08:00", {
                "posting_ref": "P-1", "revision": 2, "state": "closed",
                "effective_at": "2026-09-03T08:00:00+08:00",
            }),
        ])
        push = self.service.recommend(self.cid, now="2026-09-04T08:00:00+08:00")
        self.assertTrue(push["warnings"])
        self.assertIn("不建议继续推送", push["warnings"][0])

    def test_recommend_stale_posting_warns(self):
        push = self.service.recommend(self.cid, now="2026-11-01T08:00:00+08:00")
        self.assertTrue(any("核实" in w for w in push["warnings"]))

    def test_unknown_posting_or_course_rejected(self):
        with self.assertRaises(KeyError):
            self.service.recommend("C-999", now="2026-09-02T08:00:00+08:00")
        with self.assertRaises(KeyError):
            self.service.recommend(
                self.cid, course_ref="C-404", now="2026-09-02T08:00:00+08:00"
            )


if __name__ == "__main__":
    unittest.main()
