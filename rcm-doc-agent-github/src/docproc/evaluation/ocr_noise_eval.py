"""OCR-noise detection-rate eval -- the measurable answer to "does the new
semantic/fuzzy machinery actually help," instead of the single anecdote from
the real Docling handwritten-document test.

Pure mechanical eval: NO LLM calls. Takes the real ground-truth denial codes
this project's own synthetic corpus uses, applies realistic OCR character
confusions to them, and measures what fraction of each corruption BUCKET the
existing validators (`check_business_rules`, `check_code_semantics`) catch:

  invalid           -- corrupted code doesn't resolve in the registry at all
                       (expected: ~100%, this was already true before today)
  valid_but_different -- corrupted code resolves to a DIFFERENT real code
                       (the exact real gap found: CO-197 -> CO-19). Only
                       ~10% of real CARC codes carry a scope restriction
                       `check_code_semantics` can key off of, so this number
                       is expected to be low, not 100% -- reporting the real
                       number is the point, not claiming a full fix.
  unchanged         -- the substitution produced the same code back (e.g. a
                       confusion pair applied to a digit not present in this
                       code); excluded from detection-rate math entirely.

Run:
    python -m src.docproc.evaluation.ocr_noise_eval
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.docproc.registry.codes import lookup_code           # noqa: E402
from src.docproc.schemas import ClaimExtraction              # noqa: E402
from src.docproc.validation import check_business_rules, check_code_semantics  # noqa: E402

# Real, bidirectional OCR digit/letter confusions -- the exact classes named
# in the request, plus the two this project's own real Docling test actually
# produced (7<->F on "70450"->"F0450", and a trailing-digit truncation on
# "PR-3"->"PR-.").
_CONFUSIONS: dict[str, str] = {
    "1": "I", "I": "1",
    "0": "O", "O": "0",
    "5": "S", "S": "5",
    "9": "g", "g": "9",
    "7": "F", "F": "7",
}


def corrupt_code(code: str, seed: int) -> str:
    """Apply ONE realistic OCR confusion to the first confusable character
    found (deterministic given `seed`, so a re-run reproduces the same
    corruption -- this is a measurement tool, not a fuzzer)."""
    chars = list(code)
    confusable_positions = [i for i, c in enumerate(chars) if c in _CONFUSIONS]
    if not confusable_positions:
        # No confusable character in this code (e.g. all-unique digits) --
        # fall back to truncating the last character, the OTHER real
        # failure mode this project's own test produced ("PR-3" -> "PR-.").
        return code[:-1] if len(code) > 1 else code
    pos = confusable_positions[seed % len(confusable_positions)]
    chars[pos] = _CONFUSIONS[chars[pos]]
    return "".join(chars)


def truncate_code(code: str) -> str:
    """The OTHER real corruption class this project's own Docling test
    produced ("PR-3" -> "PR-."): OCR drops the trailing character entirely,
    which -- checked against this project's real 9 registry codes -- is the
    ACTUAL mechanism that turns a real code into a DIFFERENT real code
    (`CO-197` -> `CO-19` is exactly this: drop the trailing digit). A
    single-character substitution almost never lands on another valid code
    by chance (confirmed: 0/27 in a real run of this eval); truncation does,
    deterministically, which is why it's tested as its own corruption class
    rather than folded into `corrupt_code`'s fallback path."""
    return code[:-1] if len(code) > 1 else code


def classify(original: str, corrupted: str) -> str:
    if corrupted.upper() == original.upper():
        return "unchanged"
    if lookup_code(corrupted) is None:
        return "invalid"
    return "valid_but_different"


def run(ground_truth_path: str = "data/docs/ground_truth.json",
       docs_dir: str = "data/docs") -> dict:
    """For every denial code in the real ground truth, apply one OCR
    confusion, classify the corruption, and check whether the mechanical
    validators (business-rules registry check + semantic scope check) flag
    the corrupted code anywhere in their issues for that document."""
    records = json.load(open(ground_truth_path))
    buckets: dict[str, int] = defaultdict(int)
    caught: dict[str, int] = defaultdict(int)
    examples: list[dict] = []

    seed_counter = 0
    for rec in records:
        doc_path = os.path.join(docs_dir, rec["file"])
        document = open(doc_path, encoding="utf-8").read()
        for original in rec["denial_codes"]:
            seed_counter += 1
            corruptions = {
                "substitution": corrupt_code(original, seed_counter),
                "truncation": truncate_code(original),
            }
            for corruption_kind, corrupted in corruptions.items():
                bucket = classify(original, corrupted)
                if bucket == "unchanged":
                    continue
                buckets[bucket] += 1

                ext = ClaimExtraction(denial_codes=[corrupted])
                issues = check_business_rules(ext) + check_code_semantics(ext, document)
                flagged = any(corrupted in i.message or original in i.message for i in issues)
                if flagged:
                    caught[bucket] += 1
                examples.append({
                    "doc": rec["file"], "original": original, "corrupted": corrupted,
                    "kind": corruption_kind, "bucket": bucket, "caught": flagged,
                })

    return {"buckets": dict(buckets), "caught": dict(caught), "examples": examples}


def render(result: dict) -> str:
    lines = ["=" * 72, "OCR-NOISE DETECTION-RATE EVAL (mechanical, no LLM calls)", "=" * 72]
    for bucket, total in sorted(result["buckets"].items()):
        got = result["caught"].get(bucket, 0)
        pct = 100 * got / total if total else 0
        lines.append(f"  {bucket:<22} {got:>3}/{total:<3} caught ({pct:5.1f}%)")
    lines.append("-" * 72)
    lines.append("Per-example detail:")
    for ex in result["examples"]:
        mark = "CAUGHT" if ex["caught"] else "missed"
        lines.append(f"  [{mark}] {ex['doc']:<32} {ex['kind']:<12} "
                     f"{ex['original']!r:>10} -> {ex['corrupted']!r:<10} ({ex['bucket']})")
    lines.append("=" * 72)
    return "\n".join(lines)


def main() -> int:
    result = run()
    print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
