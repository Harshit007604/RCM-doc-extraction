"""Shared agent helpers.

`_extract_json` is used by both the document-extraction agent
(`src/docproc/agent.py`) and the legacy CSV agent (`legacy/graph.py`,
`legacy/loop.py`). CSV-specific types (`AgentOutcome`, `ClarifyHandler`) moved
to `legacy/common_csv.py` since they depend on the CSV-only `FinalAnswer`
schema.
"""

from __future__ import annotations

import re


def _extract_json(text: str) -> str:
    """Pull the first *complete, balanced* JSON object out of a model
    response (tolerates code fences, leading prose, and trailing content
    after the object closes).

    Uses a brace-depth scan rather than a greedy regex. The greedy version
    (`re.search(r"\\{.*\\}", text, re.DOTALL)`) matches from the first `{` to
    the LAST `}` in the entire response -- if the model appends anything
    after the real JSON object closes (a stray repeated fragment, trailing
    punctuation, a second malformed block), the greedy match swallows that
    trailing content INTO the "extracted" JSON, which then fails to parse
    with a "trailing characters" error despite the actual object being fine.
    This was 69 of 198 real parse failures across this project's trace logs
    (see LEARNING.md) -- a bug in the extractor, not the model's output.

    The scan tracks nesting depth and skips over characters inside string
    literals (respecting `\\"` escapes) so braces that are part of a quoted
    value never affect the depth count, then returns as soon as depth
    returns to zero -- i.e. the first complete object, ignoring everything
    after it.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()

    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in model output")

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]

    raise ValueError("no complete JSON object found in model output (unbalanced braces)")
