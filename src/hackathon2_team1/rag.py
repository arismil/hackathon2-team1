"""RAG over the NFS knowledge corpus: section-aware PDF chunking + ChromaDB persistence."""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import chromadb
from pypdf import PdfReader

from .config import Settings, get_settings
from .guardrails.injection import detect_injection

log = logging.getLogger(__name__)

_BOILERPLATE = [
    re.compile(r"^Northstar Financial Services \(NFS\) - Fictional Hackathon Material\s*(Page \d+)?$"),
    re.compile(r"^Page \d+$"),
]
_META = re.compile(r"^(Organization|Classification|Effective date):\s*(.*)$")
_NUMBERED = re.compile(r"^(\d{1,2})\.\s+(\S.{0,80})$")  # "3. Encryption"
_LETTERED = re.compile(r"^([A-Z])\.\s+(\S.{0,80})$")  # "B. Encryption"
_POLICY_ID = re.compile(r"\bPolicy ([A-Z]{2,3}-\d{3})\b")


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    source: str
    title: str
    section: str
    text: str
    metadata: dict = field(default_factory=dict)


def doc_profile(rel_path: str, title: str) -> dict:
    """Classify a document by location/name. Vendor submissions are untrusted third-party content."""
    name = Path(rel_path).stem
    if rel_path.startswith("historical-vendor-assessments/"):
        return {"doc_type": "historical_assessment", "trust": "nfs_internal", "vendor": name.split("-")[1]}
    if name.endswith("-policy"):
        return {"doc_type": "nfs_policy", "trust": "nfs_internal", "vendor": ""}
    if name.startswith("vendor-"):
        # vendor display name = words of the title before the document kind
        vendor = re.split(r"\b(Enterprise|Security|Commercial|Proposal|Pricing)\b", title)[0].strip()
        return {"doc_type": "vendor_submission", "trust": "untrusted_vendor_supplied", "vendor": vendor}
    return {"doc_type": "other", "trust": "unknown", "vendor": ""}


def vendor_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _is_heading(line: str, lettered_ok: bool) -> re.Match | None:
    m = _NUMBERED.match(line)
    if m and not line.rstrip().endswith((",", ";")):
        return m
    if lettered_ok:
        return _LETTERED.match(line)
    return None


def parse_pdf(path: Path, knowledge_dir: Path) -> list[Chunk]:
    rel = path.relative_to(knowledge_dir).as_posix()
    doc_id = path.stem
    raw = "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)
    lines = [ln.strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln and not any(p.match(ln) for p in _BOILERPLATE)]

    meta: dict[str, str] = {}
    body: list[str] = []
    header: list[str] = []
    for ln in lines:
        m = _META.match(ln)
        if m:
            meta[m.group(1).lower().replace(" ", "_")] = m.group(2)
        elif not meta and not body:
            header.append(ln)  # title lines precede the metadata block
        else:
            body.append(ln)
    title = header[0] if header else doc_id
    subtitle = " ".join(header[1:])
    m_pid = _POLICY_ID.search(f"{title} {subtitle}")
    policy_id = m_pid.group(1) if m_pid else ""
    profile = doc_profile(rel, title)

    lettered_ok = any(_LETTERED.match(ln) for ln in body)
    has_numbered = any(_NUMBERED.match(ln) for ln in body)
    sections: list[tuple[str, str, list[str]]] = []  # (key, heading, lines)
    for ln in body:
        m = _is_heading(ln, lettered_ok)
        if m:
            sections.append((m.group(1), ln, []))
        elif not has_numbered and not lettered_ok and len(ln) <= 30 and not ln.endswith("."):
            sections.append((vendor_slug(ln), ln, []))  # plain headings e.g. "Key findings"
        elif sections:
            sections[-1][2].append(ln)
        else:
            sections.append(("0", "Preamble", [ln]))

    chunks = []
    for key, heading, sec_lines in sections:
        text = re.sub(r"\s+", " ", " ".join([heading, *sec_lines])).strip()
        flags = detect_injection(text).patterns
        chunks.append(
            Chunk(
                chunk_id=f"{doc_id}::{key}",
                doc_id=doc_id,
                source=rel,
                title=title,
                section=heading,
                text=text,
                metadata={
                    **profile,
                    "vendor_slug": vendor_slug(profile["vendor"]) if profile["vendor"] else "",
                    "policy_id": policy_id or "",
                    "subtitle": subtitle,
                    "classification": meta.get("classification", ""),
                    "injection_flags": ",".join(flags),
                },
            )
        )
    return chunks


def load_corpus(knowledge_dir: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for pdf in sorted(knowledge_dir.rglob("*.pdf")):
        chunks.extend(parse_pdf(pdf, knowledge_dir))
    return chunks


# --------------------------------------------------------------------------- embeddings


class HashEmbedder:
    """Deterministic lexical embedding (feature hashing). Offline tests / demos only."""

    dim = 768

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        toks = re.findall(r"[a-z0-9]+", text.lower())
        for tok in toks + [a + "_" + b for a, b in zip(toks, toks[1:])]:
            h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:4], "little")
            v[h % self.dim] += 1.0 if (h >> 31) & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


def get_embedder(settings: Settings):
    if settings.embedding_provider == "hash":
        return HashEmbedder()
    from .llm import get_embeddings

    return get_embeddings(settings)


# --------------------------------------------------------------------------- store


class KnowledgeStore:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.settings.chroma_dir.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(self.settings.chroma_dir))
        self.collection = self.client.get_or_create_collection(
            f"nfs_knowledge_{self.settings.embedding_provider}", metadata={"hnsw:space": "cosine"}
        )
        self.embedder = get_embedder(self.settings)

    def count(self) -> int:
        return self.collection.count()

    def ingest(self, rebuild: bool = False) -> int:
        if rebuild and self.count():
            self.collection.delete(ids=self.collection.get()["ids"])
        chunks = load_corpus(self.settings.knowledge_dir)
        # contextual header improves retrieval; the stored document stays verbatim for citation checks
        embed_inputs = [f"{c.title} | {c.metadata['subtitle']} | {c.section}\n{c.text}" for c in chunks]
        vectors = self.embedder.embed_documents(embed_inputs)
        self.collection.upsert(
            ids=[c.chunk_id for c in chunks],
            documents=[c.text for c in chunks],
            embeddings=vectors,
            metadatas=[
                {"doc_id": c.doc_id, "source": c.source, "title": c.title, "section": c.section, **c.metadata}
                for c in chunks
            ],
        )
        log.info("ingested %d chunks from %s", len(chunks), self.settings.knowledge_dir)
        return len(chunks)

    def ensure_ingested(self) -> None:
        if self.count() == 0:
            self.ingest()

    def search(self, query: str, doc_types: list[str] | None = None, vendor: str | None = None, top_k: int = 5):
        self.ensure_ingested()
        clauses = []
        if doc_types:
            clauses.append({"doc_type": {"$in": doc_types}})
        if vendor:
            clauses.append({"vendor_slug": vendor_slug(vendor)})
        where = None if not clauses else clauses[0] if len(clauses) == 1 else {"$and": clauses}
        res = self._query(query, where, top_k)
        if not res and vendor:  # vendor name did not match the registry - retry without vendor filter
            res = self._query(query, {"doc_type": {"$in": doc_types}} if doc_types else None, top_k)
        return res

    def _query(self, query: str, where: dict | None, top_k: int) -> list[dict]:
        out = self.collection.query(
            query_embeddings=[self.embedder.embed_query(query)],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        results = []
        for cid, doc, md, dist in zip(out["ids"][0], out["documents"][0], out["metadatas"][0], out["distances"][0]):
            results.append(_to_result(cid, doc, md, round(1 - dist, 4)))
        return results

    def get_document(self, doc_id: str) -> list[dict]:
        self.ensure_ingested()
        out = self.collection.get(where={"doc_id": doc_id}, include=["documents", "metadatas"])
        rows = [_to_result(cid, doc, md, None) for cid, doc, md in zip(out["ids"], out["documents"], out["metadatas"])]
        return sorted(rows, key=lambda r: _section_sort_key(r["chunk_id"]))

    def list_documents(self) -> list[dict]:
        self.ensure_ingested()
        out = self.collection.get(include=["metadatas"])
        docs: dict[str, dict] = {}
        for md in out["metadatas"]:
            d = docs.setdefault(
                md["doc_id"],
                {
                    "doc_id": md["doc_id"],
                    "source": md["source"],
                    "title": md["title"],
                    "subtitle": md.get("subtitle", ""),
                    "policy_id": md.get("policy_id", ""),
                    "doc_type": md["doc_type"],
                    "trust": md["trust"],
                    "vendor": md.get("vendor", ""),
                    "sections": 0,
                },
            )
            d["sections"] += 1
        return sorted(docs.values(), key=lambda d: (d["doc_type"], d["doc_id"]))


def _section_sort_key(chunk_id: str):
    key = chunk_id.split("::")[-1]
    return (0, int(key), "") if key.isdigit() else (1, 0, key)


def _to_result(cid: str, doc: str, md: dict, score: float | None) -> dict:
    return {
        "chunk_id": cid,
        "doc_id": md["doc_id"],
        "source": md["source"],
        "title": md["title"],
        "policy_id": md.get("policy_id", ""),
        "section": md["section"],
        "doc_type": md["doc_type"],
        "trust": md["trust"],
        "vendor": md.get("vendor", ""),
        "score": score,
        "text": doc,
        "injection_flags": [f for f in md.get("injection_flags", "").split(",") if f],
    }


@lru_cache
def get_store() -> KnowledgeStore:
    return KnowledgeStore()
