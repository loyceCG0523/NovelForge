"""章节样本参考门槛的条件化行为：与 Web 版 test_dual_channel_sample_rag.py 同步。

Windows 后端没有独立的单元测试套件（历史测试都走桌面前壳或 API 全链路），
这里直接复用 backend/api 包做纯单元测试，保持两个版本的门槛行为一致。

app 包在导入时会立即解析 Settings（需要 DATABASE_URL 等环境变量）。为了不与
test_desktop_fullstack.py 的运行时配置互相污染，本模块在 setUpClass 里惰性
导入 app 包、只在 app 尚未被导入时补 dummy 环境变量，并在 tearDownClass 里
弹出本次新导入的 app/worker 模块，保证测试执行顺序无关。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_BACKEND_API = Path(__file__).resolve().parents[1] / "backend" / "api"

_DUMMY_ENV = {
    "DATABASE_URL": "sqlite:///:memory:",
    "REDIS_URL": "redis://127.0.0.1:1/0",
    "S3_ENDPOINT": "http://127.0.0.1:1",
    "S3_BUCKET": "novelforge-test",
    "S3_ACCESS_KEY_ID": "test",
    "S3_SECRET_ACCESS_KEY": "test",
    "JWT_SECRET": "novelforge-test",
}


class ChapterReferenceGateTests(unittest.TestCase):
    sample_rag = None
    _modules_before: frozenset = frozenset()

    @classmethod
    def setUpClass(cls) -> None:
        cls._modules_before = frozenset(sys.modules)
        if str(_BACKEND_API) not in sys.path:
            sys.path.insert(0, str(_BACKEND_API))
        if "app.core.config" not in sys.modules:
            for key, value in _DUMMY_ENV.items():
                os.environ.setdefault(key, value)
        from app.services import sample_rag

        cls.sample_rag = sample_rag

    @classmethod
    def tearDownClass(cls) -> None:
        for name in set(sys.modules) - set(cls._modules_before):
            if name == "app" or name.startswith(("app.", "worker")):
                sys.modules.pop(name, None)

    def _db(self) -> SimpleNamespace:
        return SimpleNamespace(get=lambda *_args, **_kwargs: SimpleNamespace(preferences={}))

    @patch("app.services.sample_rag._count_available_trusted_annotations", return_value=1)
    @patch("app.services.sample_rag._retrieve_reference_pack")
    @patch("app.services.sample_rag.build_expression_query", return_value="当前章检索条件")
    def test_chapter_reference_automatically_falls_back_to_plot_annotation(
        self,
        _build_query,
        retrieve_pack,
        _count_annotations,
    ) -> None:
        retrieve_pack.side_effect = [
            {"status": "empty", "reason": "无表达标注", "references": []},
            {
                "status": "completed",
                "references": [
                    {"annotation_id": "plot-1", "excerpt": "一条可信剧情参考"}
                ],
                "total_chars": 8,
            },
        ]
        result = self.sample_rag.build_chapter_reference_pack(
            self._db(),
            novel=SimpleNamespace(owner_id="owner"),
            context={},
        )

        self.assertEqual(result["fallback_channel"], "plot")
        self.assertEqual(len(result["references"]), 1)
        self.assertEqual(retrieve_pack.call_args_list[0].kwargs["channel"], "language")
        self.assertEqual(retrieve_pack.call_args_list[1].kwargs["channel"], "plot")

    @patch("app.services.sample_rag._count_available_trusted_annotations", return_value=0)
    @patch("app.services.sample_rag._retrieve_reference_pack")
    @patch("app.services.sample_rag.build_expression_query", return_value="当前章检索条件")
    def test_chapter_reference_gate_is_skipped_when_no_trusted_annotations_exist(
        self,
        _build_query,
        retrieve_pack,
        _count_annotations,
    ) -> None:
        """用户还没有任何可信标注时，门槛不生效，允许无参考生成。"""
        retrieve_pack.side_effect = [
            {"status": "empty", "reason": "暂无可用的可信language协同标注", "references": []},
            {"status": "empty", "reason": "暂无可用的可信plot协同标注", "references": []},
        ]
        result = self.sample_rag.build_chapter_reference_pack(
            self._db(),
            novel=SimpleNamespace(owner_id="owner"),
            context={},
        )

        self.assertEqual(result["references"], [])
        self.assertEqual(result["minimum_required"], 0)
        self.assertFalse(result["requirement_satisfied"])
        self.assertEqual(result["requirement_skipped"], "no_trusted_annotations")

    @patch("app.services.sample_rag._count_available_trusted_annotations", return_value=3)
    @patch("app.services.sample_rag._retrieve_reference_pack")
    @patch("app.services.sample_rag.build_expression_query", return_value="当前章检索条件")
    def test_chapter_reference_gate_still_blocks_when_annotations_exist_but_retrieve_empty(
        self,
        _build_query,
        retrieve_pack,
        _count_annotations,
    ) -> None:
        """已有可信标注但一条都检索不到（如索引未建/模型过期）时，门槛仍然拦截。"""
        retrieve_pack.side_effect = [
            {"status": "empty", "reason": "暂无可用的可信language协同标注", "references": []},
            {"status": "empty", "reason": "暂无可用的可信plot协同标注", "references": []},
        ]
        with self.assertRaises(self.sample_rag.ChapterReferenceRequirementError):
            self.sample_rag.build_chapter_reference_pack(
                self._db(),
                novel=SimpleNamespace(owner_id="owner"),
                context={},
            )


if __name__ == "__main__":
    unittest.main()
