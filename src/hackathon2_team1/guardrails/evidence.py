"""Evidence guardrails: citation verification and finding normalisation (FR04/FR05/FR10).

- A citation is valid only if its chunk was actually retrieved in this run AND the quote
  appears (near-)verbatim in that chunk.
- RETRIEVED findings without a valid citation are downgraded to INFERRED/unverified.
- Missing evidence is never PASS (VR-006 §4, PR-001 §7).
"""

from __future__ import annotations

import re

from ..rag import NFS_POLICY_SOURCES, vendor_slug
from ..schemas import Citation, EvidenceBasis, EvidenceChunk, Finding, FindingStatus

_WS = re.compile(r"\s+")
_MANDATORY = re.compile(r"\b(must|required|mandatory|shall|prohibited|cannot|may not|no later than)\b", re.I)
_NEGATIVE = {"not", "never", "no", "without", "cannot"}
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-"})


def _norm(s: str) -> str:
    s = s.translate(_QUOTES).lower()
    s = re.sub(r"[^a-z0-9%.,:;'\"/+\- ]", " ", s)
    return _WS.sub(" ", s).strip().strip(".")


def _safe_exact_match(text: str, start: int) -> bool:
    # A short exact substring can still omit an immediately preceding negation.
    preceding = re.findall(r"\b[a-z]+\b", text[max(0, start - 35):start])[-3:]
    return not any(word in _NEGATIVE for word in preceding)


def quote_in_text(quote: str, text: str) -> bool:
    q, t = _norm(quote), _norm(text)
    if len(q) < 8:
        return False
    # Citations are requested verbatim. Ellipses may omit text, but each piece
    # must appear in order with the same numbers and polarity.
    parts = [p.strip() for p in re.split(r"\.\.\.|\u2026", q) if p.strip()]
    if not parts or any(len(part) < 8 for part in parts):
        return False
    offset = 0
    for part in parts:
        found = False
        while (start := t.find(part, offset)) >= 0:
            offset = start + len(part)
            if _safe_exact_match(t, start):
                found = True
                break
        if not found:
            return False
    return True


def verify_citation(c: Citation, ledger: dict[str, EvidenceChunk]) -> Citation:
    chunk = ledger.get(c.chunk_id)
    if chunk is None:
        return c.model_copy(update={"verified": False, "verification_note": "chunk was not retrieved in this run"})
    if not quote_in_text(c.quote, chunk.text):
        return c.model_copy(update={"verified": False, "verification_note": "quote not found in cited chunk"})
    return c.model_copy(update={"verified": True, "verification_note": None})


def normalize_finding(f: Finding, ledger: dict[str, EvidenceChunk], vendor: str | None = None) -> Finding:
    f = f.model_copy(deep=True)
    f.citations = [verify_citation(c, ledger) for c in f.citations]
    valid = [c for c in f.citations if c.verified]
    reference_ids = set(re.findall(r"[A-Z]{2,3}-\d{3}", f.policy_reference or ""))
    policy = [c for c in valid
              if (source := ledger[c.chunk_id].source) in NFS_POLICY_SOURCES
              and ledger[c.chunk_id].doc_type == "nfs_policy"
              and ledger[c.chunk_id].trust == "nfs_internal"
              and (not reference_ids or NFS_POLICY_SOURCES[source] in reference_ids)]
    vendor_citations = [c for c in valid
                        if ledger[c.chunk_id].doc_type == "vendor_submission"
                        and ledger[c.chunk_id].trust == "untrusted_vendor_supplied"
                        and (vendor is None or vendor_slug(ledger[c.chunk_id].vendor) == vendor_slug(vendor))]
    notes = list(f.guardrail_notes)

    # The model cannot clear a mandatory control. Missing policy evidence
    # leaves the control open until a trusted policy passage is obtained.
    f.mandatory_control = any(_MANDATORY.search(ledger[c.chunk_id].text) for c in policy) if policy else True
    if f.evidence_basis == EvidenceBasis.RETRIEVED and not valid:
        f.evidence_basis = EvidenceBasis.INFERRED
        notes.append("claimed RETRIEVED but no citation could be verified -> downgraded to INFERRED (unverified)")

    unsupported_pass = f.status == FindingStatus.PASS and (
        f.evidence_basis == EvidenceBasis.MISSING or not policy or not vendor_citations)
    if unsupported_pass:
        f.status = FindingStatus.UNKNOWN
        notes.append("PASS requires cited NFS policy and correct-vendor evidence -> UNKNOWN")

    f.verified = bool(policy and vendor_citations) and not unsupported_pass
    f.guardrail_notes = notes
    return f


def effective_status(f: Finding) -> FindingStatus:
    """Status used by decision rules: an unverified PASS counts as UNKNOWN."""
    if f.status == FindingStatus.PASS and not f.verified:
        return FindingStatus.UNKNOWN
    return f.status
