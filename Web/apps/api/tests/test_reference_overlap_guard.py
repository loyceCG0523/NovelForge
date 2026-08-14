import unittest

from app.services.reference_overlap_guard import build_reference_overlap_report


class ReferenceOverlapGuardTests(unittest.TestCase):
    def test_detects_long_continuous_copy(self):
        excerpt = "雨水顺着生锈的铁门往下淌像一串没人收走的旧账单"
        report = build_reference_overlap_report(
            f"他停在巷口。{excerpt}。随后转身离开。",
            {"references": [{"passage_id": "p1", "excerpt": excerpt}]},
        )

        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["has_risky_overlap"])
        self.assertEqual(report["matches"][0]["passage_id"], "p1")

    def test_allows_technique_transfer_with_original_wording(self):
        report = build_reference_overlap_report(
            "风把窗纸吹得一鼓一瘪，屋里的沉默也跟着有了呼吸。",
            {
                "references": [
                    {
                        "passage_id": "p1",
                        "excerpt": "雨水顺着生锈的铁门往下淌，像一串没人收走的旧账单。",
                    }
                ]
            },
        )

        self.assertEqual(report["status"], "passed")
        self.assertFalse(report["has_risky_overlap"])


if __name__ == "__main__":
    unittest.main()
