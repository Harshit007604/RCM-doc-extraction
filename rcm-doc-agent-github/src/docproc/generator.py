"""Synthetic payer-document generator.

This is the backbone of the evaluation story. Documents are rendered FROM a
known structured record, so the ground truth is exact by construction — no hand
labelling, and field-level precision/recall is computable rather than judged.

Three surface formats (denial letter, EOB table, remittance advice) express the
same underlying schema with different layouts, wording, and date/currency
formats. That is deliberate: an extractor that only works on one template is
overfitting to layout, and the multi-format set exposes it.

All PHI-shaped values are fabricated. Nothing here is real patient data.
"""

from __future__ import annotations

import json
import os
import random
from datetime import date, timedelta

PAYERS = ["Meridian Health Plan", "BlueCrest Mutual", "Vantage Care Netwrk",
          "Sunstate Medical Assurance", "Northlake Benefit Administrators"]
PROVIDERS = ["Riverbend Regional Medical Center", "St. Alder Community Hospital",
             "Lakeshore Surgical Associates", "Cardinal Point Health System"]
FIRST = ["Marcus", "Ana", "Devon", "Priya", "Elena", "Omar", "Grace", "Tobias", "Nina", "Rafael"]
LAST = ["Whitfield", "Okonkwo", "Barrera", "Lindqvist", "Nakamura",
        "Delacroix", "Abernathy", "Moreau", "Sandoval", "Ferraro"]

CPTS = [
    ("99285", "Emergency dept visit, high complexity"),
    ("70450", "CT head/brain without contrast"),
    ("80053", "Comprehensive metabolic panel"),
    ("29881", "Arthroscopy, knee, with meniscectomy"),
    ("93010", "Electrocardiogram, interpretation and report"),
    ("71046", "Radiologic exam, chest, 2 views"),
    ("36415", "Collection of venous blood by venipuncture"),
    ("45378", "Colonoscopy, diagnostic"),
]

# Codes the generator draws from, paired with how the document phrases them.
DENIAL_POOL = ["CO-197", "CO-50", "CO-16", "CO-29", "CO-11", "CO-97", "CO-22", "CO-27"]
PATIENT_RESP_POOL = ["PR-1", "PR-2", "PR-3"]
CONTRACTUAL = "CO-45"


def _money(x: float) -> float:
    """Round to cents -- every dollar amount in a record passes through this."""
    return round(x, 2)


def _fmt_date(d: date, style: str) -> str:
    """Render one date in one of the three formats the corpus mixes across
    formats (ISO on remittances, US on letters, long-form in prose)."""
    return {"iso": d.isoformat(),
            "us": d.strftime("%m/%d/%Y"),
            "long": d.strftime("%B %d, %Y")}[style]


def _fmt_money(x: float, style: str) -> str:
    """Render one amount with (`symbol`, e.g. "$1,234.56") or without
    (`plain`, e.g. "1234.56") a currency sign -- the two conventions the
    corpus mixes across formats."""
    return f"${x:,.2f}" if style == "symbol" else f"{x:,.2f}"


def build_record(rng: random.Random, doc_id: int) -> dict:
    """Create one ground-truth record (the source of truth for both doc + eval)."""
    payer = rng.choice(PAYERS)
    provider = rng.choice(PROVIDERS)
    patient = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
    dos = date(2026, 1, 1) + timedelta(days=rng.randint(0, 150))

    n_lines = rng.randint(1, 3)
    chosen = rng.sample(CPTS, n_lines)
    denial_code = rng.choice(DENIAL_POOL)

    line_items, total_charged, total_allowed, total_paid = [], 0.0, 0.0, 0.0
    for idx, (cpt, desc) in enumerate(chosen):
        charge = _money(rng.uniform(180, 4200))
        # First line carries the denial; remaining lines adjudicate normally.
        if idx == 0:
            allowed, paid, code = 0.0, 0.0, denial_code
        else:
            allowed = _money(charge * rng.uniform(0.45, 0.8))
            paid = _money(allowed * rng.uniform(0.7, 1.0))
            code = CONTRACTUAL
        line_items.append({"cpt_code": cpt, "description": desc,
                           "charge_amount": charge, "allowed_amount": allowed,
                           "paid_amount": paid, "denial_code": code})
        total_charged += charge
        total_allowed += allowed
        total_paid += paid

    patient_resp = _money(max(0.0, total_allowed - total_paid))
    codes = sorted({li["denial_code"] for li in line_items})
    if patient_resp > 0:
        codes = sorted(set(codes) | {rng.choice(PATIENT_RESP_POOL)})

    return {
        "doc_id": f"DOC-{1000 + doc_id}",
        "payer_name": payer,
        "provider_name": provider,
        "patient_name": patient,
        "claim_number": f"CLM{rng.randint(10**9, 10**10 - 1)}",
        "member_id": f"{rng.choice('ABCXYZ')}{rng.randint(10**8, 10**9 - 1)}",
        "date_of_service": dos.isoformat(),
        "appeal_deadline": (dos + timedelta(days=rng.choice([90, 120, 180]))).isoformat(),
        "total_charged": _money(total_charged),
        "total_allowed": _money(total_allowed),
        "total_paid": _money(total_paid),
        "patient_responsibility": patient_resp,
        "denial_codes": codes,
        "line_items": line_items,
    }


# --------------------------------------------------------------------------- #
# Renderers — same record, three very different surfaces
# --------------------------------------------------------------------------- #
def render_denial_letter(r: dict, rng: random.Random) -> str:
    """Render `r` as a prose adverse-determination letter -- labeled fields,
    a per-line service breakdown, and ERISA/ACA-style appeal-rights language."""
    dos = date.fromisoformat(r["date_of_service"])
    dl = date.fromisoformat(r["appeal_deadline"])
    lines = [
        r["payer_name"].upper(),
        "Claims Review Department",
        "P.O. Box 41822, Suite 300",
        "",
        f"{_fmt_date(dos - timedelta(days=rng.randint(3, 20)), 'long')}",
        "",
        f"{r['provider_name']}",
        "Attn: Patient Financial Services",
        "",
        "RE: NOTICE OF CLAIM DETERMINATION",
        "",
        f"Patient Name:       {r['patient_name']}",
        f"Member Identifier:  {r['member_id']}",
        f"Claim Number:       {r['claim_number']}",
        f"Date of Service:    {_fmt_date(dos, 'us')}",
        "",
        "Dear Provider,",
        "",
        "We have completed our review of the above-referenced claim. Following",
        "adjudication, one or more submitted services have not been approved for",
        "payment. The determination for each service line appears below.",
        "",
        "SERVICE LINE DETAIL",
        "-" * 64,
    ]
    for li in r["line_items"]:
        lines += [
            f"  Procedure {li['cpt_code']} - {li['description']}",
            f"    Amount Billed .......... {_fmt_money(li['charge_amount'], 'symbol')}",
            f"    Amount Allowed ......... {_fmt_money(li['allowed_amount'], 'symbol')}",
            f"    Amount Paid ............ {_fmt_money(li['paid_amount'], 'symbol')}",
            f"    Adjustment Reason ...... {li['denial_code']}",
            "",
        ]
    lines += [
        "-" * 64,
        f"  TOTAL BILLED ............. {_fmt_money(r['total_charged'], 'symbol')}",
        f"  TOTAL ALLOWED ............ {_fmt_money(r['total_allowed'], 'symbol')}",
        f"  TOTAL PAID ............... {_fmt_money(r['total_paid'], 'symbol')}",
        f"  PATIENT RESPONSIBILITY ... {_fmt_money(r['patient_responsibility'], 'symbol')}",
        "",
        f"Adjustment reason codes applied to this claim: {', '.join(r['denial_codes'])}.",
        "",
        "APPEAL RIGHTS",
        "If you believe this determination is in error, you may submit a written",
        "appeal with supporting clinical documentation. Appeals must be received",
        f"no later than {_fmt_date(dl, 'long')}. Appeals received after this date",
        "will not be considered absent proof of good cause.",
        "",
        "Sincerely,",
        "Provider Appeals Unit",
    ]
    return "\n".join(lines)


def render_eob(r: dict, rng: random.Random) -> str:
    """Render `r` as a member-facing Explanation of Benefits: a fixed-width
    CPT/billed/allowed/paid table plus a totals row."""
    dos = date.fromisoformat(r["date_of_service"])
    dl = date.fromisoformat(r["appeal_deadline"])
    out = [
        f"{r['payer_name']}",
        "EXPLANATION OF BENEFITS — THIS IS NOT A BILL",
        "=" * 72,
        f"Member: {r['patient_name']}   ID: {r['member_id']}",
        f"Claim #: {r['claim_number']}   Serviced: {_fmt_date(dos, 'iso')}",
        f"Rendering Facility: {r['provider_name']}",
        "",
        f"{'CPT':<8}{'BILLED':>12}{'ALLOWED':>12}{'PAID':>12}{'CODE':>10}",
        "-" * 72,
    ]
    for li in r["line_items"]:
        out.append(
            f"{li['cpt_code']:<8}"
            f"{_fmt_money(li['charge_amount'], 'plain'):>12}"
            f"{_fmt_money(li['allowed_amount'], 'plain'):>12}"
            f"{_fmt_money(li['paid_amount'], 'plain'):>12}"
            f"{li['denial_code']:>10}"
        )
    out += [
        "-" * 72,
        f"{'TOTALS':<8}"
        f"{_fmt_money(r['total_charged'], 'plain'):>12}"
        f"{_fmt_money(r['total_allowed'], 'plain'):>12}"
        f"{_fmt_money(r['total_paid'], 'plain'):>12}",
        "",
        f"Amount you may owe: {_fmt_money(r['patient_responsibility'], 'plain')}",
        "",
        "Remark codes on this claim: " + " ".join(r["denial_codes"]),
        f"Deadline to dispute: {_fmt_date(dl, 'us')}",
    ]
    return "\n".join(out)


def render_remittance(r: dict, rng: random.Random) -> str:
    """Render `r` as a plain-text 835-style remittance summary: labeled
    header fields and one `SVC ... | ADJ ...` line per service."""
    dos = date.fromisoformat(r["date_of_service"])
    dl = date.fromisoformat(r["appeal_deadline"])
    out = [
        "ELECTRONIC REMITTANCE ADVICE (835 SUMMARY)",
        f"PAYER............: {r['payer_name']}",
        f"PROVIDER.........: {r['provider_name']}",
        f"PATIENT..........: {r['patient_name']}",
        f"SUBSCRIBER ID....: {r['member_id']}",
        f"ICN/CLAIM........: {r['claim_number']}",
        f"SVC DATE.........: {_fmt_date(dos, 'us')}",
        "",
        "CLAIM LEVEL ADJUSTMENTS",
    ]
    for li in r["line_items"]:
        out.append(
            f"  SVC {li['cpt_code']} | BILLED {_fmt_money(li['charge_amount'], 'plain')}"
            f" | ALLOW {_fmt_money(li['allowed_amount'], 'plain')}"
            f" | PAID {_fmt_money(li['paid_amount'], 'plain')}"
            f" | ADJ {li['denial_code']}"
        )
    out += [
        "",
        f"CLAIM TOTALS: BILLED {_fmt_money(r['total_charged'], 'plain')}"
        f" ALLOWED {_fmt_money(r['total_allowed'], 'plain')}"
        f" PAID {_fmt_money(r['total_paid'], 'plain')}",
        f"PATIENT RESP: {_fmt_money(r['patient_responsibility'], 'plain')}",
        f"ADJUSTMENT CODES: {'/'.join(r['denial_codes'])}",
        f"RECONSIDERATION MUST BE FILED BY {_fmt_date(dl, 'iso')}",
    ]
    return "\n".join(out)


RENDERERS = {
    "denial_letter": render_denial_letter,
    "eob": render_eob,
    "remittance_advice": render_remittance,
}


def generate(out_dir: str, n: int = 9, seed: int = 7) -> str:
    """Write `n` documents + a ground-truth JSON. Returns the ground-truth path."""
    rng = random.Random(seed)
    os.makedirs(out_dir, exist_ok=True)
    truth = []
    kinds = list(RENDERERS)
    for i in range(n):
        rec = build_record(rng, i)
        kind = kinds[i % len(kinds)]          # even coverage across formats
        text = RENDERERS[kind](rec, rng)
        path = os.path.join(out_dir, f"{rec['doc_id']}_{kind}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        rec["doc_type"] = kind
        rec["file"] = os.path.basename(path)
        truth.append(rec)

    truth_path = os.path.join(out_dir, "ground_truth.json")
    with open(truth_path, "w", encoding="utf-8") as fh:
        json.dump(truth, fh, indent=2)
    return truth_path


# --------------------------------------------------------------------------- #
# Matched claim triads — same claim, three formats, for cross-document
# reconciliation (src/docproc/workflows/reconcile.py). A single-document generator can't
# exercise this: reconciliation needs more than one document about the SAME
# claim to have something to disagree about.
# --------------------------------------------------------------------------- #
def build_corrected_record(rec: dict, rng: random.Random) -> tuple[dict, list[str]]:
    """A realistic post-adjudication correction: the payer takes back part of
    a payment on a later remittance (e.g. a coordination-of-benefits
    recoupment). The correction is applied to a DEEP COPY so the original
    `rec` (what the denial letter / EOB already told the provider) is
    untouched — the two versions now genuinely disagree.

    The correction keeps the corrected record's OWN arithmetic consistent
    (total_allowed = total_paid + patient_responsibility still holds), so a
    single-document validator sees nothing wrong with the remittance in
    isolation. Only comparing it against the other documents catches it.

    Returns `(rec, [])` unchanged if there is nothing to take back (a fully
    denied claim with total_paid already 0) — a real payer can't recoup a
    payment that was never made, so injecting a "correction" there would be
    dishonest: it wouldn't actually change any number.
    """
    corrected = json.loads(json.dumps(rec))  # deep copy
    idx = max(range(len(corrected["line_items"])),
              key=lambda i: corrected["line_items"][i]["paid_amount"])
    max_paid = corrected["line_items"][idx]["paid_amount"]
    if max_paid <= 0:
        return rec, []
    delta = _money(min(max_paid, rng.uniform(20, 120)))
    corrected["line_items"][idx]["paid_amount"] = _money(max_paid - delta)
    corrected["total_paid"] = _money(corrected["total_paid"] - delta)
    corrected["patient_responsibility"] = _money(corrected["patient_responsibility"] + delta)
    return corrected, ["total_paid", "patient_responsibility"]


def generate_triads(out_dir: str, n_claims: int = 4, seed: int = 11,
                    discrepancy_rate: float = 0.5) -> str:
    """Write matched claim triads: the SAME claim rendered as a denial letter,
    an EOB, and a remittance advice. Writes `manifest.json` mapping each claim
    group to its three files and (for scoring) whether/where a discrepancy was
    injected. Returns the manifest path.
    """
    rng = random.Random(seed)
    os.makedirs(out_dir, exist_ok=True)
    manifest = []
    for i in range(n_claims):
        rec = build_record(rng, i)
        group = rec["doc_id"]
        remittance_rec, discrepancy_fields = (
            build_corrected_record(rec, rng) if rng.random() < discrepancy_rate else (rec, []))
        has_discrepancy = bool(discrepancy_fields)

        files = {}
        for kind, record in [("denial_letter", rec), ("eob", rec),
                             ("remittance_advice", remittance_rec)]:
            text = RENDERERS[kind](record, rng)
            fname = f"{group}__{kind}.txt"
            with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as fh:
                fh.write(text)
            files[kind] = fname

        manifest.append({
            "group": group,
            "claim_number": rec["claim_number"],
            "files": files,
            "has_injected_discrepancy": has_discrepancy,
            "discrepancy_fields": discrepancy_fields,
            "original": rec,
        })

    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest_path


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Generate synthetic RCM documents.")
    ap.add_argument("--out", default="data/docs")
    ap.add_argument("--n", type=int, default=9,
                    help="Number of documents (single mode) or claims (triads mode).")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--mode", choices=["single", "triads"], default="single",
                    help="single: one doc per claim (the standard corpus). "
                         "triads: same claim rendered in all 3 formats, for "
                         "cross-document reconciliation.")
    ap.add_argument("--discrepancy-rate", type=float, default=0.5,
                    help="(triads mode) fraction of claims with an injected "
                         "remittance correction.")
    args = ap.parse_args()
    if args.mode == "triads":
        print(generate_triads(args.out, args.n, args.seed, args.discrepancy_rate))
    else:
        print(generate(args.out, args.n, args.seed))
