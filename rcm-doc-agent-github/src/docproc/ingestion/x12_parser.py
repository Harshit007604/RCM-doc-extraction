"""X12 835 (Electronic Remittance Advice) segment-grammar parser.

Promoted out of `scripts/extract_x12_835.py` so the ingestion router
(`src/docproc/ingestion/ingest.py`) can call it directly for `.edi` files instead of
routing them through the LLM extraction loop -- an 835 is already-structured
EDI, not prose, so there is nothing for an LLM to read a "payer name"
*sentence* out of. See `scripts/extract_x12_835.py` for the narrated demo /
write-up of why this is a genuinely different extraction problem.

Deliberately minimal: real 835s have loops, repeats, and optional segments
this does not handle -- see `LEARNING.md` for what a production parser needs
(a proper 2000/2100/2110-loop-aware grammar).
"""

from __future__ import annotations

from ..schemas import ClaimExtraction, DocType, FieldValue, LineItem


def parse_835(raw: str) -> ClaimExtraction:
    """Segment-grammar parser for a single-claim 835. Splits on `~` then `*`
    and dispatches on segment tag (N1/CLP/NM1/DTM/SVC/CAS)."""
    ext = ClaimExtraction(doc_type=DocType.REMITTANCE)
    current_line: LineItem | None = None
    lines: list[LineItem] = []

    for seg in raw.split("~"):
        seg = seg.strip()
        if not seg:
            continue
        el = seg.split("*")
        tag = el[0]

        if tag == "N1" and el[1] == "PR":                      # payer
            name = el[2]
            ext.payer_name = FieldValue(value=name.title(), source_text=name)

        elif tag == "N1" and el[1] == "PE":                    # payee/provider
            name = el[2]
            ext.provider_name = FieldValue(value=name.title(), source_text=name)

        elif tag == "CLP":                                     # claim summary
            ext.claim_number = FieldValue(value=el[1], source_text=el[1])
            ext.total_charged = FieldValue(value=el[3], source_text=el[3])
            ext.total_paid = FieldValue(value=el[4], source_text=el[4])
            ext.patient_responsibility = FieldValue(value=el[5], source_text=el[5])

        elif tag == "NM1" and el[1] == "QC":                    # patient
            last, first = el[3], el[4]
            member_id = el[9] if len(el) > 9 and el[8] == "MI" else None
            # value is normalized "First Last"; source_text must be the raw
            # EDI token order (LAST*FIRST) because that is what's literally
            # in the document -- normalization and grounding are separate axes.
            ext.patient_name = FieldValue(
                value=f"{first.title()} {last.title()}", source_text=f"{last}*{first}")
            if member_id:
                ext.member_id = FieldValue(value=member_id, source_text=member_id)

        elif tag == "DTM" and el[1] in ("232", "233") and not ext.date_of_service.value:
            raw_date = el[2]                                    # CCYYMMDD
            iso = f"{raw_date[0:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
            ext.date_of_service = FieldValue(value=iso, source_text=raw_date)

        elif tag == "SVC":
            cpt = el[1].split(":")[-1]
            charge = float(el[2])
            paid = float(el[3])
            current_line = LineItem(cpt_code=cpt, charge_amount=charge,
                                     allowed_amount=charge, paid_amount=paid)
            lines.append(current_line)

        elif tag == "CAS" and current_line is not None:
            group, reason, amount = el[1], el[2], float(el[3])
            code = f"{group}-{reason}"
            if code not in ext.denial_codes:
                ext.denial_codes.append(code)
            if group == "CO":                                   # writes off charge -> allowed
                current_line.allowed_amount -= amount

    ext.line_items = lines
    # total_allowed has NO literal element in an 835 -- it is derived from
    # SVC charge minus CO-group CAS adjustments, never quoted as one number
    # anywhere in the transaction. So, honestly: no source_text for it.
    total_allowed = sum(li.allowed_amount for li in lines)
    ext.total_allowed = FieldValue(value=f"{total_allowed:.2f}", source_text=None)
    # appeal_deadline: genuinely absent. An 835 is a payment transaction, not
    # a denial letter -- appeal-rights language is ACA/ERISA content that only
    # exists in the paper/portal notice, never in the EDI remittance.
    return ext
