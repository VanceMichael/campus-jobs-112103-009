import unittest

from careerlink import CareerService


def rec(source_id, kind, collected, payload):
    return {
        "source_id": source_id,
        "record_kind": kind,
        "collected_at": collected,
        "payload": payload,
    }


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.service = CareerService.create()
        self.service.ingest_records([
            rec("s1", "job_posting", "2026-09-01T09:00:00+08:00", {
                "posting_ref": "P-1", "employer_ref": "E-1", "title": "设备运维",
                "location": "CITY-A", "employment_kind": "full-time",
                "updated_at": "2026-09-01T08:00:00+08:00", "skills": ["plc-basics"],
            }),
        ])
        self.cid = self.service.store.alias_index["P-1"]

    def test_no_feedback_is_not_no_applicants(self):
        status = self.service.fill_status(self.cid)
        self.assertEqual(status["status"], "no-feedback")
        self.assertTrue(any("不代表无人申请" in n for n in status["notes"]))

    def test_pending_feedback_means_unknown_not_zero(self):
        self.service.ingest_records([
            rec("app-1", "application", "2026-09-02T10:00:00+08:00", {
                "application_ref": "A-1", "posting_ref": "P-1",
                "student_ref": "STU-1", "applied_at": "2026-09-02T09:00:00+08:00",
            }),
            rec("fb-1", "placement_feedback", "2026-09-03T10:00:00+08:00", {
                "posting_ref": "P-1", "stage": "interview", "response_state": "pending",
            }),
        ])
        status = self.service.fill_status(self.cid)
        self.assertEqual(status["status"], "awaiting-feedback")
        self.assertTrue(any("不代表无人申请" in n for n in status["notes"]))
        detail = self.service.posting_detail(self.cid, now="2026-09-04T00:00:00+08:00")
        self.assertEqual(detail["applications_recorded"], 1)

    def test_interview_feedback_alone_cannot_confirm_filled(self):
        self.service.ingest_records([
            rec("fb-1", "placement_feedback", "2026-09-03T10:00:00+08:00", {
                "posting_ref": "P-1", "stage": "interview", "response_state": "received",
            }),
            rec("u1", "posting_update", "2026-09-04T09:00:00+08:00", {
                "posting_ref": "P-1", "revision": 2, "state": "filled",
                "effective_at": "2026-09-04T08:00:00+08:00",
            }),
        ])
        now = "2026-09-05T00:00:00+08:00"
        posting_obj = self.service.store.postings[self.cid]
        self.assertEqual(self.service.effective_status(posting_obj, now), "closed")
        self.assertTrue(self.service.filled_claim_unconfirmed(posting_obj, now))

    def test_offer_stage_still_not_filled(self):
        self.service.ingest_records([
            rec("fb-1", "placement_feedback", "2026-09-03T10:00:00+08:00", {
                "posting_ref": "P-1", "stage": "offer", "response_state": "received",
            }),
        ])
        self.assertEqual(self.service.fill_status(self.cid)["status"], "offer-received")
        self.assertFalse(self.service.filled_confirmed(self.cid))

    def test_hired_feedback_confirms_filled(self):
        self.service.ingest_records([
            rec("fb-1", "placement_feedback", "2026-09-03T10:00:00+08:00", {
                "posting_ref": "P-1", "stage": "hired", "response_state": "received",
            }),
        ])
        self.assertEqual(self.service.fill_status(self.cid)["status"], "filled-confirmed")
        self.assertTrue(self.service.filled_confirmed(self.cid))

    def test_feedback_for_unknown_posting_kept_and_flagged(self):
        self.service.ingest_records([
            rec("fb-x", "placement_feedback", "2026-09-03T10:00:00+08:00", {
                "posting_ref": "P-404", "stage": "interview",
                "response_state": "received",
            }),
        ])
        self.assertEqual(len(self.service.store.feedback), 1)
        items = self.service.pending_verifications(now="2026-09-04T00:00:00+08:00")
        self.assertTrue(any(i["kind"] == "unresolved-reference" for i in items))
        # 岗位后到时自动关联
        self.service.ingest_records([
            rec("s-late", "job_posting", "2026-09-05T09:00:00+08:00", {
                "posting_ref": "P-404", "employer_ref": "E-9", "title": "产线巡检",
                "location": "CITY-C", "employment_kind": "internship",
                "updated_at": "2026-09-05T08:00:00+08:00", "skills": ["plc-basics"],
            }),
        ])
        cid = self.service.store.alias_index["P-404"]
        self.assertEqual(self.service.store.feedback[0].canonical_id, cid)
        items = self.service.pending_verifications(now="2026-09-06T00:00:00+08:00")
        self.assertFalse(any(
            i["kind"] == "unresolved-reference"
            and i["refs"].get("posting_ref") == "P-404"
            for i in items
        ))


if __name__ == "__main__":
    unittest.main()
