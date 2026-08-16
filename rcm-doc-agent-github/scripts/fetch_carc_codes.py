"""Fetch the REAL, official Claim Adjustment Reason Codes (CARC) list from
X12.org and generate `src/docproc/carc_codes.py`.

Why this exists: `src/docproc/codes.py`'s curated `_REGISTRY` hand-picks ~16
representative codes with rich triage metadata (category/appealable/typical
action) -- useful for the synthetic corpus, but it returns `None` for any of
the ~280 other real CARC codes a real payer document could contain. This
script pulls the authoritative source directly: X12 External Code Source 139,
published at https://x12.org/codes/claim-adjustment-reason-codes -- the
actual HIPAA-mandated code set referenced by every X12 835 remittance advice
in the US. `codes.py` uses the generated output as a fallback so *any* real
CARC code resolves to a real, sourced description instead of `None`.

This is a data *fetch+generate* step, not something to hand-maintain: re-run
it to refresh the registry on X12's update cadence (they publish a "Last
updated" date on the page).

Run:
    python scripts/fetch_carc_codes.py
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://x12.org/codes/claim-adjustment-reason-codes"
OUT_PATH = Path(__file__).resolve().parent.parent / "src" / "docproc" / "carc_codes.py"


def fetch_current_codes() -> dict[str, str]:
    """Download the CARC page and parse every *current* (non-deactivated)
    code into {code: description}. Deactivated codes are skipped -- a real
    document should not be adjudicated against a retired code."""
    resp = requests.get(URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", id="codelist")
    if table is None:
        raise RuntimeError(f"Could not find the code table at {URL} -- page structure may have changed.")

    codes: dict[str, str] = {}
    for row in table.find_all("tr", class_=lambda c: c and "current" in c.split()):
        code = row.find("td", class_="code").get_text(strip=True)
        desc_td = row.find("td", class_="description")
        dates_span = desc_td.find("span", class_="dates")
        if dates_span:
            dates_span.extract()  # dates aren't part of the code's meaning
        description = re.sub(r"\s+", " ", desc_td.get_text(" ", strip=True)).strip()
        codes[code] = description
    return codes


def render_module(codes: dict[str, str]) -> str:
    """Render the fetched codes as a Python source file: a plain
    `code -> description` dict, verbatim from X12.org, with no invented
    fields (category/appealable/action are derived at runtime in
    `codes.py`, not baked into this generated data)."""
    lines = [
        '"""Auto-generated. Do NOT hand-edit -- regenerate with',
        '`python scripts/fetch_carc_codes.py`.',
        "",
        f"Source: {URL}",
        '("X12 External Code Source 139" -- the official HIPAA-mandated',
        "Claim Adjustment Reason Code list.)",
        f"Fetched: {datetime.date.today().isoformat()}",
        f"Current (non-deactivated) codes: {len(codes)}",
        '"""',
        "",
        "RAW_CARC: dict[str, str] = {",
    ]
    for code in sorted(codes, key=lambda c: (len(c), c)):
        lines.append(f"    {code!r}: {codes[code]!r},")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    codes = fetch_current_codes()
    OUT_PATH.write_text(render_module(codes), encoding="utf-8")
    print(f"Wrote {len(codes)} current CARC codes to {OUT_PATH}")


if __name__ == "__main__":
    main()
