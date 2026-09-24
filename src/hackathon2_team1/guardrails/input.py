"""Input guardrail for incoming assessment requests."""

from __future__ import annotations

import re

from pydantic import BaseModel

from ..schemas import VendorAssessmentRequest
from .injection import detect_injection

_SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "api_key": re.compile(r"\b(sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"),
    "password_assignment": re.compile(r"\bpassword\s*[:=]\s*\S+", re.I),
}


class InputCheck(BaseModel):
    allowed: bool
    reasons: list[str] = []


def check_request(req: VendorAssessmentRequest) -> InputCheck:
    reasons = []
    text = " ".join(value for value in req.model_dump(mode="json").values() if isinstance(value, str))
    inj = detect_injection(text)
    if inj.detected:
        reasons.append(f"prompt-injection patterns in request: {inj.patterns}")
    for name, pat in _SECRET_PATTERNS.items():
        if pat.search(text):
            # DC-002 §4: Restricted material (secrets) must not be sent to external AI services
            reasons.append(f"request contains Restricted material ({name}); cannot be sent to an external LLM")
    return InputCheck(allowed=not reasons, reasons=reasons)
