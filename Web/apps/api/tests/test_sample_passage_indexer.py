import unittest
from unittest.mock import patch

from app.models.sample_analysis import SampleAnalysis
from app.services.sample_passage_indexer import extract_sample_passage_candidates


class SamplePassageIndexerTests(unittest.TestCase):
    @patch("app.services.sample_passage_indexer.iter_text_object_chunks")
    def test_extracts_real_passages_with_type_and_technique(self, iter_chunks):
        metaphor = (
            "雨水沿着生锈的卷帘门往下淌，仿佛一串迟迟没人来认领的旧账。"
            "巷子里的光被风吹得发抖，脚步声一下一下靠近，他却没有回头。"
            "潮湿的气味贴在喉咙里，连呼吸都像是在替谁隐瞒什么。"
            "远处的车灯扫过墙面，又迅速沉进积水深处，他听见自己的心跳越来越响。"
        )
        dialogue = (
            "“你昨晚到底去了哪里？”她按住门把，没有让路。"
            "“如果我说一直在楼下，你会信吗？”他把湿透的袖口往上卷。"
            "“我只信证据。”她看了一眼他手背的新伤，“尤其是不肯开口的证据。”"
            "“那就去找。”他终于抬眼，“找到以后，再决定今晚该拦住谁。”"
        )
        iter_chunks.return_value = iter([f"第一章 雨夜\n{metaphor}\n{dialogue}"])
        analysis = SampleAnalysis(source_object_key="sample.txt")

        passages = extract_sample_passage_candidates(analysis)

        self.assertGreaterEqual(len(passages), 2)
        types = {item["passage_type"] for item in passages}
        self.assertIn("metaphor", types)
        self.assertIn("dialogue", types)
        self.assertTrue(all(item["technique_summary"] for item in passages))
        self.assertTrue(all(len(item["content_hash"]) == 64 for item in passages))


if __name__ == "__main__":
    unittest.main()
