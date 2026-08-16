"""Shared pytest fixtures. Nothing here touches the network/LLM -- every
fixture below is either a static document string or a pure in-memory object,
so the whole suite runs in well under a second with no API key required.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def denial_letter_text() -> str:
    """A real corpus document (DOC-1000), used across several test modules
    as ground truth text to check grounding/arithmetic against."""
    return """VANTAGE CARE NETWORK
Claims Review Department
P.O. Box 41822, Suite 300

January 15, 2026

St. Alder Community Hospital
Attn: Patient Financial Services

RE: NOTICE OF CLAIM DETERMINATION

Patient Name:       Grace Whitfield
Member Identifier:  Y728720317
Claim Number:       CLM5070378921
Date of Service:    01/19/2026

Dear Provider,

We have completed our review of the above-referenced claim. Following
adjudication, one or more submitted services have not been approved for
payment. The determination for each service line appears below.

SERVICE LINE DETAIL
----------------------------------------------------------------
  Procedure 70450 - CT head/brain without contrast
    Amount Billed .......... $3,837.01
    Amount Allowed ......... $0.00
    Amount Paid ............ $0.00
    Adjustment Reason ...... CO-197

  Procedure 80053 - Comprehensive metabolic panel
    Amount Billed .......... $1,043.09
    Amount Allowed ......... $500.77
    Amount Paid ............ $413.36
    Adjustment Reason ...... CO-45

  Procedure 93010 - Electrocardiogram, interpretation and report
    Amount Billed .......... $1,147.47
    Amount Allowed ......... $737.67
    Amount Paid ............ $529.45
    Adjustment Reason ...... CO-45

----------------------------------------------------------------
  TOTAL BILLED ............. $6,027.57
  TOTAL ALLOWED ............ $1,238.44
  TOTAL PAID ............... $942.81
  PATIENT RESPONSIBILITY ... $295.63

Adjustment reason codes applied to this claim: CO-197, CO-45, PR-3.

APPEAL RIGHTS
If you believe this determination is in error, you may submit a written
appeal with supporting clinical documentation. Appeals must be received
no later than May 19, 2026. Appeals received after this date
will not be considered absent proof of good cause.

Sincerely,
Provider Appeals Unit"""
