import unittest

from careerlink import CareerService


def rec(source_id, kind, collected, payload):
    return {
        "source_id": source_id,
        "record_kind": kind,
        "collected_at": collected,
        "payload": payload,
    }


def posting(ref="P-1", skills=None, kind="full-time", location="CITY-A"):
    return rec("s-post", "job_posting", "2026-09-01T09:00:00+08:00", {
        "posting_ref": ref, "employer_ref": "E-1", "title": "设备运维",
        "location": location, "employment_kind": kind,
        "updated_at": "2026-09-01T08:00:00+08:00",
        "skills": skills or ["plc-basics"],
    })


def update(ref, revision, state, effective, source="s-upd", **extra):
    payload = {"posting_ref": ref, "revision": revision, "state": state,
               "effective_at": effective}
    payload.update(extra)
    return rec(source, "posting_update", effective, payload)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.service = CareerService.create()
        self.service.ingest_records([posting()])
        self.cid = self.service.store.alias_index["P-1"]

    def test_higher_revision_wins_regardless_of_arrival_order(self):
        self.service.ingest_records([
            update("P-1", 3, "open", "2026-09-10T00:00:00+08:00", source="u3"),
            update("P-1", 2, "closed", "2026-09-05T00:00:00+08:00", source="u2"),
        ])
        posting_obj = self.service.store.postings[self.cid]
        self.assertEqual(posting_obj.current_state(), "open")
        self.assertEqual(posting_obj.state_at("2026-09-07T00:00:00+00:00"), "closed")

    def test_stale_update_kept_in_journal_only(self):
        self.service.ingest_records([
            update("P-1", 2, "closed", "2026-09-05T00:00:00+08:00", source="u2"),
            update("P-1", 2, "closed", "2026-09-05T00:00:00+08:00", source="u2b"),
        ])
        posting_obj = self.service.store.postings[self.cid]
        self.assertEqual(len(posting_obj.revisions), 1)
        actions = [j["action"] for j in self.service.store.journal]
        self.assertIn("superseded-update", actions)

    def test_conflicting_same_revision_flagged_for_verification(self):
        self.service.ingest_records([
            update("P-1", 2, "closed", "2026-09-05T00:00:00+08:00", source="u2"),
            update("P-1", 2, "open", "2026-09-06T00:00:00+08:00", source="u2x"),
        ])
        items = self.service.pending_verifications(now="2026-09-07T00:00:00+08:00")
        self.assertTrue(any(i["kind"] == "revision-conflict" for i in items))

    def test_key_field_change_flagged(self):
        self.service.ingest_records([
            update("P-1", 2, "open", "2026-09-05T00:00:00+08:00",
                   source="u2", location="CITY-B"),
        ])
        items = self.service.pending_verifications(now="2026-09-07T00:00:00+08:00")
        self.assertTrue(any(i["kind"] == "key-field-changed" for i in items))

    def test_conversion_preserves_old_applications(self):
        self.service.ingest_records([
            rec("app-1", "application", "2026-09-02T10:00:00+08:00", {
                "application_ref": "A-1", "posting_ref": "P-1",
                "student_ref": "STU-1", "applied_at": "2026-09-02T09:00:00+08:00",
            }),
            update("P-1", 2, "converted", "2026-09-05T00:00:00+08:00",
                   source="u2", converted_to="P-9"),
            rec("s-p9", "job_posting", "2026-09-06T09:00:00+08:00", {
                "posting_ref": "P-9", "employer_ref": "E-1", "title": "设备运维",
                "location": "CITY-A", "employment_kind": "full-time",
                "updated_at": "2026-09-06T08:00:00+08:00", "skills": ["plc-basics"],
            }),
        ])
        posting_obj = self.service.store.postings[self.cid]
        self.assertEqual(posting_obj.current_state(), "converted")
        # 旧报名仍在原岗位下，未被删除
        detail = self.service.posting_detail(self.cid, now="2026-09-07T00:00:00+08:00")
        self.assertEqual(len(detail["applications"]), 1)
        self.assertIn("报名记录保留", detail["applications"][0]["note"])
        # 转化链路双向可查
        self.assertEqual(
            detail["converted_to"]["canonical_id"],
            self.service.store.alias_index["P-9"],
        )
        new_detail = self.service.posting_detail(
            self.service.store.alias_index["P-9"], now="2026-09-07T00:00:00+08:00"
        )
        self.assertEqual(new_detail["converted_from"], [self.cid])

    def test_missing_conversion_target_flagged(self):
        self.service.ingest_records([
            update("P-1", 2, "converted", "2026-09-05T00:00:00+08:00",
                   source="u2", converted_to="P-404"),
        ])
        items = self.service.pending_verifications(now="2026-09-07T00:00:00+08:00")
        self.assertTrue(any(i["kind"] == "conversion-target-missing" for i in items))

    def test_filled_claim_requires_hired_feedback(self):
        self.service.ingest_records([
            update("P-1", 2, "filled", "2026-09-05T00:00:00+08:00", source="u2"),
        ])
        now = "2026-09-07T00:00:00+08:00"
        posting_obj = self.service.store.postings[self.cid]
        # 反馈不足：不能标为已招满
        self.assertEqual(self.service.effective_status(posting_obj, now), "closed")
        items = self.service.pending_verifications(now=now)
        critical = [i for i in items if i["kind"] == "filled-unconfirmed"]
        self.assertEqual(len(critical), 1)
        self.assertEqual(critical[0]["severity"], "critical")
        # 收到录用反馈后，申报被确认，核实项消失
        self.service.ingest_records([
            rec("fb-1", "placement_feedback", "2026-09-06T10:00:00+08:00", {
                "posting_ref": "P-1", "stage": "hired", "response_state": "received",
            }),
        ])
        self.assertEqual(self.service.effective_status(posting_obj, now), "filled")
        items = self.service.pending_verifications(now=now)
        self.assertFalse(any(i["kind"] == "filled-unconfirmed" for i in items))

    def test_stale_open_posting_flagged(self):
        items = self.service.pending_verifications(now="2026-11-01T00:00:00+08:00")
        stale = [i for i in items if i["kind"] == "stale-open"]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["refs"]["canonical_id"], self.cid)
        # 刚更新过则不标记
        items = self.service.pending_verifications(now="2026-09-10T00:00:00+08:00")
        self.assertFalse(any(i["kind"] == "stale-open" for i in items))

    def test_expired_posting_not_counted_as_open(self):
        self.service.ingest_records([
            rec("s-exp", "job_posting", "2026-09-01T09:00:00+08:00", {
                "posting_ref": "P-EXP", "employer_ref": "E-2", "title": "数据标注",
                "location": "CITY-B", "employment_kind": "short-project",
                "updated_at": "2026-09-01T08:00:00+08:00",
                "expires_at": "2026-09-10T00:00:00+08:00", "skills": ["data-labeling"],
            }),
        ])
        cid = self.service.store.alias_index["P-EXP"]
        posting_obj = self.service.store.postings[cid]
        self.assertEqual(
            self.service.effective_status(posting_obj, "2026-09-05T00:00:00+08:00"),
            "open",
        )
        self.assertEqual(
            self.service.effective_status(posting_obj, "2026-09-11T00:00:00+08:00"),
            "expired",
        )


if __name__ == "__main__":
    unittest.main()
