"""ChapterWritingAgent：负责章节正文生成的公共命名边界。"""

from __future__ import annotations


AGENT_NAME = "ChapterWritingAgent"


def build_chapter_agent_result(
    generation_mode: str,
    llm_model: str = "",
) -> dict[str, str]:
    """返回章节生成结果中统一使用的 Agent 元信息。"""
    return {
        "agent": AGENT_NAME,
        "generation_mode": generation_mode,
        "llm_model": llm_model,
    }
