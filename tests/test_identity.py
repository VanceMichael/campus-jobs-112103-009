import unittest

from careerlink import CareerService


def rec(source_id, kind, collected, payload):
    return {
        "source_id": source_id,
        "record_kind": kind,
        "collected_at": collected,
        "payload": payload,
    }


def posting(source_id, ref, title, location, kind, skills, employer="E-1",
            updated="2026-09-01T10:00:00+08:00"):
    return rec(source_id, "job_posting", updated, {
        "posting_ref": ref, "employer_ref": employer, "title": title,
        "location": location, "employment_kind": kind,
        "updated_at": updated, "skills": skills,
    })


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.service = CareerService.create()

    def test_cross_channel_repost_merges_into_one_need(self):
        self.service.ingest_records([
            posting("s1", "P-1", "设备运维", "CITY-A", "full-time", ["plc-basics"]),
            posting("s2", "F-9", "设备运维", "CITY-A", "full-time", ["plc-basics"],
                    updated="2026-09-02T10:00:00+08:00"),
        ])
        self.assertEqual(len(self.service.store.postings), 1)
        cid = self.service.store.alias_index["P-1"]
        self.assertEqual(self.service.store.alias_index["F-9"], cid)
        posting_obj = self.service.store.postings[cid]
        self.assertEqual(sorted(posting_obj.posting_refs), ["F-9", "P-1"])
        # 转载归并需要向企业确认
        items = self.service.pending_verifications(now="2026-09-03T00:00:00+08:00")
        reposts = [i for i in items if i["kind"] == "cross-channel-repost"]
        self.assertEqual(len(reposts), 1)
        self.assertEqual(reposts[0]["refs"]["canonical_id"], cid)

    def test_different_location_is_different_posting(self):
        self.service.ingest_records([
            posting("s1", "P-1", "设备运维", "CITY-A", "full-time", ["plc-basics"]),
            posting("s2", "P-2", "设备运维", "CITY-B", "full-time", ["plc-basics"]),
        ])
        self.assertEqual(len(self.service.store.postings), 2)

    def test_different_employment_kind_is_different_posting(self):
        # 实习/正式/短期项目不混在一起
        self.service.ingest_records([
            posting("s1", "P-1", "设备运维", "CITY-A", "full-time", ["plc-basics"]),
            posting("s2", "P-2", "设备运维", "CITY-A", "internship", ["plc-basics"]),
            posting("s3", "P-3", "设备运维", "CITY-A", "short-project", ["plc-basics"]),
        ])
        self.assertEqual(len(self.service.store.postings), 3)

    def test_title_normalization_matches_reposts(self):
        self.service.ingest_records([
            posting("s1", "P-1", "智能设备 现场支持", "CITY-A", "full-time", ["a"]),
            posting("s2", "P-2", "智能设备现场支持", "CITY-A", "full-time", ["b"]),
        ])
        self.assertEqual(len(self.service.store.postings), 1)

    def test_similar_title_different_skills_kept_separate_content(self):
        # 标题相似不代表技能要求相同：归并只看身份键，技能以各自内容为准
        self.service.ingest_records([
            posting("s1", "P-1", "设备运维", "CITY-A", "full-time", ["plc-basics"]),
            posting("s2", "P-2", "设备运维工程师", "CITY-A", "full-time",
                    ["device-diagnostics"]),
        ])
        self.assertEqual(len(self.service.store.postings), 2)
        skills = {
            p.canonical_id: p.current_content().skills
            for p in self.service.store.postings.values()
        }
        self.assertEqual(sorted(map(tuple, skills.values())),
                         [("device-diagnostics",), ("plc-basics",)])

    def test_related_postings_listed_in_detail(self):
        self.service.ingest_records([
            posting("s1", "P-1", "设备运维", "CITY-A", "full-time", ["a"]),
            posting("s2", "P-2", "设备运维", "CITY-B", "internship", ["a"]),
        ])
        cid = self.service.store.alias_index["P-1"]
        detail = self.service.posting_detail(cid, now="2026-09-03T00:00:00+08:00")
        self.assertEqual(len(detail["related_postings"]), 1)
        self.assertEqual(
            detail["related_postings"][0]["canonical_id"],
            self.service.store.alias_index["P-2"],
        )


if __name__ == "__main__":
    unittest.main()
