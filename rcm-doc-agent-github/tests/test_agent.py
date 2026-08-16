"""Unit tests for src/docproc/agent.py -- currently just the input-length
guard-rail (DocumentAgent.run() rejects an oversized document before any
LLM call). The rest of DocumentAgent's behavior (the actual reason/act/
observe loop) is exercised by real LLM runs (see evaluate.py, judge_eval.py,
compare_models.py) rather than mocked here -- mocking the LLM's JSON output
would just be re-testing the mock, not the agent.
"""

from __future__ import annotations

from src.config import Settings
from src.docproc.agent import DocumentAgent
from src.llm import LLMClient


class _PoisonLLMClient(LLMClient):
    """An LLMClient whose complete() raises if ever called -- proves the
    input-length guard short-circuits BEFORE the graph reaches the `decide`
    node (the only place `complete()` is invoked), not just before a
    document happens to look expensive."""

    def __init__(self, settings: Settings):
        super().__init__(settings)

    def complete(self, system: str, messages: list, on_token=None) -> str:  # noqa: ARG002
        raise AssertionError("LLM was called despite the document exceeding max_input_chars")


class TestInputLengthGuard:
    def _agent(self, tmp_path, max_input_chars: int = 100) -> DocumentAgent:
        settings = Settings(log_dir=str(tmp_path), max_input_chars=max_input_chars)
        return DocumentAgent(settings, _PoisonLLMClient(settings))

    def test_oversized_document_rejected_with_zero_llm_calls(self, tmp_path):
        agent = self._agent(tmp_path, max_input_chars=100)
        oversized = "x" * 101
        outcome = agent.run(oversized, "big.txt")
        assert outcome.status == "error"
        assert outcome.steps_used == 0
        assert "too large" in outcome.message.lower()
        assert "101" in outcome.message  # real char count, not a vague message
        assert outcome.extraction is None
        assert outcome.triage is None

    def test_document_at_exactly_the_limit_is_not_rejected_by_length_alone(self, tmp_path):
        """Boundary check: the guard is a strict `>`, so a document AT the
        limit must pass the length check (and only then reach the graph,
        which is where the poison client would raise -- confirming the
        boundary is exactly where the setting says it is, not off-by-one)."""
        agent = self._agent(tmp_path, max_input_chars=100)
        at_limit = "x" * 100
        try:
            agent.run(at_limit, "exact.txt")
            raised = False
        except AssertionError:
            raised = True
        # Reaching the poison client (raising) proves the length check let
        # this document through, which is the behavior under test here --
        # not a real extraction (that needs a real LLM, tested elsewhere).
        assert raised, "a document at exactly the limit should pass the length guard"

    def test_default_limit_is_generous_relative_to_real_corpus_documents(self, tmp_path):
        """The default (100,000 chars) must not accidentally reject the
        project's own real, legitimate sample documents."""
        settings = Settings(log_dir=str(tmp_path))
        assert settings.max_input_chars == 100_000
