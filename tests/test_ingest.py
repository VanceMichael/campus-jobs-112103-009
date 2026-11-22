import json
import unittest
from pathlib import Path

from careerlink import CareerService

FIXTURES = Path(__file__).parents[1] / "fixtures"


def rec(source_id, kind, collected, payload):
    return {
        "source_id": source_id,
        "record_kind": kind,
        "collected_at": collected,
        "payload": payload,
    }


class FixtureIngestTests(unittest.TestCase):
    def setUp(self):
        self.service = CareerService.create()
        self.summary = self.service.ingest_file(FIXTURES / "sources.json")

    def test_fixture_records_applied(self):
        self.assertEqual(self.summary["applied"], 5)
        self.assertEqual(self.summary["skipped_duplicates"], 0)

    def test_same_employer_different_location_and_kind_are_distinct(self):
        # POST-91（CITY-B/全职）与 FAIR-21-POST-8（CITY-C/实习）标题相同但仍是不同岗位
        self.assertEqual(len(self.service.store.postings), 2)
        kinds = {p.employment_kind for p in self.service.store.postings.values()}
        self.assertEqual(kinds, {"full-time", "internship"})

    def test_update_closes_posting_by_effective_time(self):
        cid = self.service.store.alias_index["POST-91"]
        posting = self.service.store.postings[cid]
        self.assertEqual(posting.current_state(), "closed")
        # 生效时间之前回看仍在招
        self.assertEqual(posting.state_at("2026-09-05T00:00:00+00:00"), "open")
        self.assertEqual(posting.state_at("2026-09-09T00:00:00+00:00"), "closed")

    def test_feedback_linked_to_posting_and_course(self):
        cid = self.service.store.alias_index["POST-91"]
        feedback = self.service.store.feedback
        self.assertEqual(len(feedback), 1)
        self.assertEqual(feedback[0].canonical_id, cid)
        self.assertEqual(feedback[0].course_ref, "COURSE-6")

    def test_reingest_is_idempotent(self):
        again = self.service.ingest_file(FIXTURES / "sources.json")
        self.assertEqual(again["applied"], 0)
        self.assertEqual(again["skipped_duplicates"], 5)
        self.assertEqual(len(self.service.store.postings), 2)
        self.assertEqual(len(self.service.store.feedback), 1)

    def test_list_postings_as_of_validity_window(self):
        cid = self.service.store.alias_index["POST-91"]
        before = self.service.list_postings(as_of="2026-09-05T00:00:00+08:00")
        self.assertIn(cid, [p["canonical_id"] for p in before])
        after = self.service.list_postings(
            as_of="2026-09-10T00:00:00+08:00", state="closed"
        )
        self.assertIn(cid, [p["canonical_id"] for p in after])
        open_after = self.service.list_postings(
            as_of="2026-09-10T00:00:00+08:00", state="open"
        )
        self.assertNotIn(cid, [p["canonical_id"] for p in open_after])

    def test_filter_by_source(self):
        fair = self.service.list_postings(source="fair-feed-b")
        self.assertEqual(len(fair), 1)
        self.assertEqual(fair[0]["employment_kind"], "internship")


class DeferredUpdateTests(unittest.TestCase):
    def test_update_arriving_before_posting_is_deferred_then_applied(self):
        service = CareerService.create()
        update = rec("u1", "posting_update", "2026-09-02T09:00:00+08:00", {
            "posting_ref": "P-1", "revision": 2, "state": "closed",
            "effective_at": "2026-09-02T08:00:00+08:00",
        })
        summary = service.ingest_records([update])
        self.assertEqual(summary["deferred"], 1)
        # 岗位到达后暂存修订被补应用
        posting = rec("p1", "job_posting", "2026-09-03T09:00:00+08:00", {
            "posting_ref": "P-1", "employer_ref": "E-1", "title": "设备运维",
            "location": "CITY-A", "employment_kind": "full-time",
            "updated_at": "2026-09-03T08:00:00+08:00", "skills": ["plc-basics"],
        })
        service.ingest_records([posting])
        cid = service.store.alias_index["P-1"]
        self.assertEqual(service.store.postings[cid].current_state(), "closed")
        self.assertEqual(service.store.pending_updates, {})


if __name__ == "__main__":
    unittest.main()
