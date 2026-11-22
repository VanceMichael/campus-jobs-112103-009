import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from careerlink import CareerService

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "fixtures"
NOW = "2026-09-21T10:00:00+08:00"


class FullFlowTests(unittest.TestCase):
    """两周来源文件连续采集后的整体行为。"""

    def setUp(self):
        self.service = CareerService.create()
        self.service.ingest_file(FIXTURES / "sources.json")
        self.service.ingest_file(FIXTURES / "sources-week2.json")
        self.store = self.service.store

    def test_canonical_postings(self):
        # POST-91、FAIR-21-POST-8、POST-104/FAIR-22-POST-3（转载归并）、POST-77、POST-208
        self.assertEqual(len(self.store.postings), 5)
        merged = self.store.postings[self.store.alias_index["POST-104"]]
        self.assertEqual(sorted(merged.posting_refs), ["FAIR-22-POST-3", "POST-104"])
        self.assertEqual(sorted(merged.sources), ["career-page-a-2", "fair-feed-b-2"])

    def test_conversion_and_application_preservation(self):
        fair_cid = self.store.alias_index["FAIR-21-POST-8"]
        detail = self.service.posting_detail(fair_cid, now=NOW)
        self.assertEqual(detail["status"], "converted")
        self.assertEqual(len(detail["applications"]), 1)
        self.assertEqual(detail["applications"][0]["application_ref"], "APP-1")
        self.assertEqual(
            detail["converted_to"]["canonical_id"], self.store.alias_index["POST-208"]
        )

    def test_filled_claim_without_sufficient_feedback(self):
        cid = self.store.alias_index["POST-208"]
        detail = self.service.posting_detail(cid, now=NOW)
        # 只有面试反馈：不能标为已招满
        self.assertEqual(detail["status"], "closed")
        self.assertTrue(detail["filled_claim_unconfirmed"])
        items = self.service.pending_verifications(now=NOW)
        self.assertTrue(any(
            i["kind"] == "filled-unconfirmed" and i["refs"]["canonical_id"] == cid
            for i in items
        ))

    def test_pending_feedback_is_not_zero_applicants(self):
        cid = self.store.alias_index["POST-104"]
        detail = self.service.posting_detail(cid, now=NOW)
        self.assertEqual(detail["fill_detail"]["status"], "awaiting-feedback")
        self.assertEqual(detail["applications_recorded"], 1)

    def test_expired_posting(self):
        cid = self.store.alias_index["POST-77"]
        detail = self.service.posting_detail(cid, now=NOW)
        self.assertEqual(detail["status"], "expired")

    def test_taxonomy_upgrade_and_push_review(self):
        cid = self.store.alias_index["POST-104"]
        # 当前规则已是 v2：equipment-diagnostics 归一为 device-diagnostics
        detail = self.service.posting_detail(cid, now=NOW)
        self.assertEqual(detail["taxonomy_version"], 2)
        self.assertIn("device-diagnostics", detail["skills_normalized"])
        push = self.service.recommend(cid, course_ref="COURSE-6", now=NOW)
        self.assertIn("plc-basics", push["basis"]["matched_skills"])
        review = self.service.review_push(push["push_id"], now=NOW)
        self.assertFalse(review["changed"])

    def test_course_adjustment_evidence(self):
        explain = self.service.explain_course_adjustment("COURSE-6", now=NOW)
        self.assertEqual(explain["to_revision"], 4)
        self.assertEqual(explain["added_skills"], ["plc-basics"])
        evidence_ids = {e["canonical_id"] for e in explain["evidence"]["plc-basics"]}
        # 仍有效的需求：POST-104（POST-208 已关闭、POST-77 已过期，不计入）
        self.assertEqual(evidence_ids, {self.store.alias_index["POST-104"]})

    def test_summary_counts(self):
        summary = self.service.summary(now=NOW)
        self.assertEqual(summary["postings_total"], 5)
        self.assertEqual(summary["taxonomy_version"], 2)
        self.assertEqual(summary["applications_total"], 2)
        by_kind = summary["by_status_and_kind"]
        self.assertEqual(by_kind["open"]["full-time"], 1)   # POST-104
        self.assertEqual(by_kind["closed"]["full-time"], 2)  # POST-91、POST-208
        self.assertEqual(by_kind["converted"]["internship"], 1)
        self.assertEqual(by_kind["expired"]["short-project"], 1)

    def test_verification_items_cover_key_changes(self):
        items = self.service.pending_verifications(now=NOW)
        kinds = {i["kind"] for i in items}
        self.assertIn("cross-channel-repost", kinds)
        self.assertIn("filled-unconfirmed", kinds)
        self.assertIn("feedback-course-revision-mismatch", kinds)

    def test_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.json"
            self.service.save(path)
            reloaded = CareerService.load(path)
            self.assertEqual(
                reloaded.summary(now=NOW), self.service.summary(now=NOW)
            )


class CliTests(unittest.TestCase):
    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = str(Path(tmp) / "store.json")

            def run(*args):
                result = subprocess.run(
                    [sys.executable, "-m", "careerlink", *args],
                    cwd=ROOT, capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            run("ingest", store, str(FIXTURES / "sources.json"),
                str(FIXTURES / "sources-week2.json"))
            summary = run("summary", store, "--now", NOW)
            self.assertEqual(summary["postings_total"], 5)
            postings = run("postings", store, "--state", "open", "--now", NOW)
            self.assertEqual(len(postings), 1)
            cid = postings[0]["canonical_id"]
            detail = run("detail", store, cid, "--now", NOW)
            self.assertEqual(detail["applications_recorded"], 1)
            verify = run("verify", store, "--now", NOW)
            self.assertTrue(any(i["severity"] == "critical" for i in verify))
            push = run("recommend", store, cid, "--course", "COURSE-6", "--now", NOW)
            review = run("push", store, push["push_id"], "--now", NOW)
            self.assertEqual(
                review["basis_at_push"]["taxonomy_version"],
                summary["taxonomy_version"],
            )
            explain = run("course-explain", store, "COURSE-6", "--now", NOW)
            self.assertEqual(explain["added_skills"], ["plc-basics"])


if __name__ == "__main__":
    unittest.main()
