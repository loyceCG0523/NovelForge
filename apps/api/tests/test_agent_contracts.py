import unittest

from app.services.agent_contracts import (
    AGENT_INPUT_PROTOCOL_VERSION,
    AGENT_PROTOCOL_VERSION,
    AgentFailure,
    agent_contract,
    agent_input_contract,
)


class AgentContractTests(unittest.TestCase):
    def test_input_envelope_is_stable_and_traceable(self) -> None:
        payload = agent_input_contract(
            "ChapterWritingAgent",
            "generate_chapter",
            task_id="task-1",
            novel_id="novel-1",
            payload={"chapter_index": 3},
        )

        self.assertEqual(payload["schema_version"], AGENT_INPUT_PROTOCOL_VERSION)
        self.assertEqual(payload["agent"], "ChapterWritingAgent")
        self.assertEqual(payload["payload"]["chapter_index"], 3)

    def test_failure_output_preserves_retry_semantics(self) -> None:
        failure = AgentFailure(
            code="provider_timeout",
            message="LLM timed out",
            stage="chapter_generation",
            retryable=True,
        )

        payload = agent_contract(
            "ChapterWritingAgent",
            "chapter_pipeline",
            status="failed",
            failure=failure,
        )

        self.assertEqual(payload["schema_version"], AGENT_PROTOCOL_VERSION)
        self.assertEqual(payload["status"], "failed")
        self.assertTrue(payload["failure"]["retryable"])
        self.assertEqual(payload["failure"]["code"], "provider_timeout")


if __name__ == "__main__":
    unittest.main()
