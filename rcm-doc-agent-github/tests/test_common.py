"""Unit tests for src/common.py::_extract_json -- the JSON-from-model-output
extractor. Every case here is a REAL failure string captured from this
project's trace logs (see LEARNING.md, 2026-08-14), not an invented example.
"""

from __future__ import annotations

import json

import pytest

from src.common import _extract_json


class TestExtractJson:
    def test_plain_json_object(self):
        raw = '{"thought": "ok", "action": "extract"}'
        assert json.loads(_extract_json(raw)) == {"thought": "ok", "action": "extract"}

    def test_fenced_with_language_tag(self):
        raw = '```json\n{"action": "finalize"}\n```'
        assert json.loads(_extract_json(raw)) == {"action": "finalize"}

    def test_fenced_without_language_tag(self):
        raw = '```\n{"action": "finalize"}\n```'
        assert json.loads(_extract_json(raw)) == {"action": "finalize"}

    def test_leading_prose_before_json(self):
        raw = 'Here is the result:\n{"action": "lookup_code", "codes_to_look_up": ["CO-45"]}'
        assert json.loads(_extract_json(raw))["action"] == "lookup_code"

    def test_trailing_garbage_after_valid_json_is_ignored(self):
        """Real captured failure: the model appended `]}}}` after a
        perfectly valid object closed. The OLD greedy regex
        (`\\{.*\\}`, re.DOTALL) swallowed this into the "extracted" JSON
        and failed with a misleading 'trailing characters' error -- the
        brace-depth scanner must return only the first complete object."""
        raw = '{"thought":"CO-29 means x","action":"finalize"}]}}}'
        extracted = _extract_json(raw)
        assert json.loads(extracted) == {"thought": "CO-29 means x", "action": "finalize"}

    def test_nested_objects_are_not_confused_with_trailing_content(self):
        raw = '{"action": "extract", "extraction": {"doc_type": "eob"}}'
        parsed = json.loads(_extract_json(raw))
        assert parsed["extraction"]["doc_type"] == "eob"

    def test_braces_inside_string_literals_do_not_affect_depth_counting(self):
        """A quoted value containing literal `{`/`}` characters must not
        confuse the brace-depth scan into stopping early or never closing."""
        raw = '{"thought": "the JSON shape is {\\"value\\": 1}", "action": "extract"}'
        parsed = json.loads(_extract_json(raw))
        assert parsed["action"] == "extract"

    def test_no_json_object_raises(self):
        with pytest.raises(ValueError):
            _extract_json("I refuse to answer in JSON today.")

    def test_unbalanced_braces_raises(self):
        with pytest.raises(ValueError):
            _extract_json('{"thought": "unterminated')
