"""Independent LLM-judge faithfulness eval -- a complementary signal to the
mechanical grounding check in validation.py.

Why this exists instead of RAGAS: RAGAS's `Faithfulness` metric is the right
CONCEPT (decompose an answer into atomic claims, verify each against the
source) -- but the real `ragas` package (checked: v0.4.3) pulls in ~8
LangChain-family packages plus HuggingFace `datasets`, and even then FAILS
TO IMPORT out of the box in a clean install:

    ModuleNotFoundError: No module named 'langchain_community.chat_models.vertexai'

That's a verified, reproduced failure, not a hypothetical concern. This
project also has a standing architectural decision to use ONE
provider-agnostic gateway (LiteLLM, src/llm.py) rather than LangChain --
adding RAGAS would mean two separate, overlapping ways to call an LLM.
Structured extraction gets a real simplification pure RAG-faithfulness
checking doesn't: each extracted field IS already an atomic claim, so there
is no separate "decompose the answer into claims" step to build.

What this checks that mechanical grounding does NOT: `check_grounding` is a
literal substring test on `source_text` -- it can be fooled by evidence text
that is correctly quoted but assigned to the WRONG field, and it says
nothing about whether a value that IS grounded is actually semantically
correct. This judge sees ONLY the document and the claimed (field, value)
pairs -- deliberately NOT the extractor's own `source_text` -- and
independently re-assesses each claim with a fresh, separate LLM call, so it
can't be fooled by the same shortcut a substring check can be.

Run:
    python -m src.docproc.evaluation.judge_eval --docs data/docs
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.common import _extract_json
from src.config import get_settings
from src.llm import LLMClient
from src.logging_utils import setup_logging
from src.docproc.agent import DocumentAgent
from src.docproc.schemas import ClaimExtraction

JUDGE_FIELDS = [
    "payer_name", "provider_name", "patient_name", "member_id", "claim_number",
    "date_of_service", "total_charged", "total_allowed", "total_paid",
    "patient_responsibility", "appeal_deadline",
]

JUDGE_SYSTEM_PROMPT = """\
You are an independent auditor. You will be shown a document and a list of
field=value claims that some OTHER system extracted from it. For EACH claim,
judge using ONLY the document text -- not the fact that a system extracted
it -- whether the document actually supports that value.

Respond with ONE JSON object and nothing else:
{"verdicts": [{"field": "...", "verdict": "supported" | "unsupported" | "uncertain",
              "reason": "<one short sentence>"}, ...]}
-- exactly one entry per field listed, in the same order. "unsupported" means
the document states something different or nothing at all; "uncertain" means
the document is ambiguous.
"""


class FieldVerdict(BaseModel):
    field: str
    verdict: str = "uncertain"  # "supported" | "unsupported" | "uncertain"
    reason: str = ""


class JudgeReport(BaseModel):
    verdicts: list[FieldVerdict] = Field(default_factory=list)

    @property
    def supported(self) -> int:
        return sum(1 for v in self.verdicts if v.verdict == "supported")

    @property
    def score(self) -> float:
        return self.supported / len(self.verdicts) if self.verdicts else 0.0


def build_judge_message(document: str, claims: dict[str, str]) -> str:
    """Deliberately omits `source_text` -- the judge must re-derive support
    from the raw document, not rubber-stamp the extractor's own evidence."""
    lines = "\n".join(f"  {field} = {value!r}" for field, value in claims.items())
    return (f"DOCUMENT:\n<<<BEGIN>>>\n{document}\n<<<END>>>\n\n"
            f"Claims to judge:\n{lines}")


def judge_extraction(llm: LLMClient, document: str, ext: ClaimExtraction) -> JudgeReport:
    """One judge call per document: independently verify every populated
    scalar field against the raw document text."""
    claims = {f: getattr(ext, f).value for f in JUDGE_FIELDS if getattr(ext, f).value is not None}
    if not claims:
        return JudgeReport(verdicts=[])
    message = build_judge_message(document, claims)
    raw = llm.complete(JUDGE_SYSTEM_PROMPT, [{"role": "user", "content": message}])
    data = json.loads(_extract_json(raw))
    verdicts = [FieldVerdict(**v) for v in data.get("verdicts", [])]
    return JudgeReport(verdicts=verdicts)


def run(docs_dir: str, limit: int | None = None) -> int:
    """Extract every document with the real agent, then judge each
    extraction with a SEPARATE LLM call, and report agreement with the
    mechanical grounding check already computed by evaluate.py."""
    with open(os.path.join(docs_dir, "ground_truth.json"), encoding="utf-8") as fh:
        truth_records = json.load(fh)
    if limit:
        truth_records = truth_records[:limit]

    settings = get_settings()
    setup_logging("WARNING")
    llm = LLMClient(settings)
    agent = DocumentAgent(settings, llm)

    total_fields = total_supported = total_unsupported = total_uncertain = 0
    docs_fully_supported = 0

    print(f"\nLLM-judge faithfulness eval — {len(truth_records)} docs, "
          f"model='{settings.model}'\n" + "=" * 68)

    for truth in truth_records:
        path = os.path.join(docs_dir, truth["file"])
        document = open(path, encoding="utf-8").read()
        outcome = agent.run(document, truth["file"])
        if outcome.extraction is None:
            print(f"  {truth['file']:<34} (no extraction to judge)")
            continue

        report = judge_extraction(llm, document, outcome.extraction)
        total_fields += len(report.verdicts)
        total_supported += report.supported
        total_unsupported += sum(1 for v in report.verdicts if v.verdict == "unsupported")
        total_uncertain += sum(1 for v in report.verdicts if v.verdict == "uncertain")
        if report.score == 1.0:
            docs_fully_supported += 1

        flags = [v for v in report.verdicts if v.verdict != "supported"]
        flag_note = "; ".join(f"{v.field}={v.verdict} ({v.reason})" for v in flags)
        print(f"  {truth['file']:<34} judge score {report.score:.2f} "
              f"({report.supported}/{len(report.verdicts)})"
              + (f"  -- {flag_note}" if flag_note else ""))

    print("\n" + "=" * 68)
    print("AGGREGATE (independent LLM judge, separate from mechanical grounding)")
    print(f"  fields judged           {total_fields}")
    print(f"  supported               {total_supported}/{total_fields} "
          f"({100 * total_supported / total_fields if total_fields else 0:.1f}%)")
    print(f"  unsupported             {total_unsupported}")
    print(f"  uncertain               {total_uncertain}")
    print(f"  documents fully supported  {docs_fully_supported}/{len(truth_records)}")
    print("=" * 68)
    return 0 if total_unsupported == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Independent LLM-judge faithfulness eval.")
    ap.add_argument("--docs", default="data/docs")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)
    return run(args.docs, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
