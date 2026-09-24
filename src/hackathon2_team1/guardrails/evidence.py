"""Evidence guardrails: citation verification and finding normalisation (FR04/FR05/FR10).

- A citation is valid only if its chunk was actually retrieved in this run AND the quote
  appears (near-)verbatim in that chunk.
- RETRIEVED findings without a valid citation are downgraded to INFERRED/unverified.
- Missing evidence is never PASS (VR-006 §4, PR-001 §7).
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..schemas import Citation, EvidenceBasis, EvidenceChunk, Finding, FindingStatus

_WS = re.compile(r"\s+")
_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def _norm(s: str) -> str:
    s = s.translate(_QUOTES).lower()
    s = re.sub(r"[^a-z0-9%.,:;'\"/+\- ]", " ", s)
    return _WS.sub(" ", s).strip().strip(".")


def quote_in_text(quote: str, text: str, threshold: float = 0.88) -> bool:
    q, t = _norm(quote), _norm(text)
    if len(q) < 8:
        return False
    if q in t:
        return True
    # figures must match exactly: a misquoted number ("72 hours" vs "24 hours") is never "close enough"
    if not set(_NUM.findall(q)) <= set(_NUM.findall(t)):
        return False
    # tolerate small paraphrase / ellipsis: best-matching window similarity
    parts = [p.strip() for p in re.split(r"\.\.\.|…", q) if len(p.strip()) >= 8]
    if len(parts) > 1 and all(p in t for p in parts):
        return True
    n = len(q)
    best = 0.0
    step = max(1, n // 8)
    for i in range(0, max(1, len(t) - n + 1), step):
        best = max(best, SequenceMatcher(None, q, t[i : i + n]).ratio())
        if best >= threshold:
            return True
    return False


def verify_citation(c: Citation, ledger: dict[str, EvidenceChunk]) -> Citation:
    chunk = ledger.get(c.chunk_id)
    if chunk is None:
        return c.model_copy(update={"verified": False, "verification_note": "chunk was not retrieved in this run"})
    if not quote_in_text(c.quote, chunk.text):
        return c.model_copy(update={"verified": False, "verification_note": "quote not found in cited chunk"})
    return c.model_copy(update={"verified": True, "verification_note": None})


def normalize_finding(f: Finding, ledger: dict[str, EvidenceChunk]) -> Finding:
    f = f.model_copy(deep=True)
    f.citations = [verify_citation(c, ledger) for c in f.citations]
    valid = [c for c in f.citations if c.verified]
    notes = list(f.guardrail_notes)

    if f.evidence_basis == EvidenceBasis.RETRIEVED and not valid:
        f.evidence_basis = EvidenceBasis.INFERRED
        notes.append("claimed RETRIEVED but no citation could be verified -> downgraded to INFERRED (unverified)")
    if f.evidence_basis == EvidenceBasis.MISSING and f.status == FindingStatus.PASS:
        f.status = FindingStatus.UNKNOWN
        notes.append("PASS without evidence is not allowed -> UNKNOWN (VR-006 §4)")
    if f.status == FindingStatus.PASS and not valid:
        notes.append("PASS is not backed by a verified citation -> treated as UNKNOWN for decision rules")
    only_vendor = valid and all(ledger[c.chunk_id].trust.startswith("untrusted") for c in valid)
    if f.status == FindingStatus.PASS and only_vendor:
        notes.append("PASS rests solely on vendor self-attestation")

    f.verified = bool(valid)
    f.guardrail_notes = notes
    return f


def effective_status(f: Finding) -> FindingStatus:
    """Status used by decision rules: an unverified PASS counts as UNKNOWN."""
    if f.status == FindingStatus.PASS and not any(c.verified for c in f.citations):
        return FindingStatus.UNKNOWN
    return f.status
