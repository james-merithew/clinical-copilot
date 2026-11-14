"""Guideline corpus loader + deterministic heading-based chunker (W2 §6.2).

`chunk_id` (``doc_id#heading-slug:n``) is a CITATION TARGET — it lands in
`GuidelineRef.chunk_id` on claims shown to the physician — so the same corpus
bytes must always produce the same ids and the same texts. Everything here is
pure string processing: no randomness, no dict-iteration-order dependence,
files processed in sorted order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# copilot/app/rag/corpus.py -> copilot/corpus/guidelines
DEFAULT_CORPUS_DIR = Path(__file__).resolve().parents[2] / "corpus" / "guidelines"

# Target chunk size ~150-300 tokens. Whitespace words are a stable,
# dependency-free proxy (~0.75 words per token → 230 words ≈ 300 tokens).
MAX_CHUNK_WORDS = 230

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_PARA_SPLIT = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class Chunk:
    """One retrievable unit of the corpus, with the attribution metadata that
    travels to the UI on every guideline citation (source_org / source_url)."""

    chunk_id: str
    doc_id: str
    title: str
    section: str
    text: str
    source_org: str
    source_url: str

    @property
    def label(self) -> str:
        return f"{self.title} — {self.section}"


def slugify(heading: str) -> str:
    return _SLUG_STRIP.sub("-", heading.strip().lower()).strip("-") or "section"


def parse_front_matter(raw: str) -> tuple[dict, str]:
    """Split a corpus file into (yaml front-matter dict, markdown body)."""
    m = _FRONT_MATTER.match(raw)
    if not m:
        raise ValueError("corpus file missing YAML front-matter")
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict) or "doc_id" not in meta:
        raise ValueError("corpus front-matter missing doc_id")
    return meta, raw[m.end():]


def _sections(body: str) -> list[tuple[str, str]]:
    """(heading, text) pairs, split on H2 (`## `) headings. Prose before the
    first H2 is attached to the H1 title (or 'overview' if there is no H1)."""
    sections: list[tuple[str, list[str]]] = []
    current = "overview"
    buf: list[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            sections.append((current, buf))
            current, buf = line[3:].strip(), []
        elif line.startswith("# ") and not sections and not any(s.strip() for s in buf):
            current = line[2:].strip()  # H1 heads the preamble section
        else:
            buf.append(line)
    sections.append((current, buf))
    return [(h, "\n".join(b).strip()) for h, b in sections if "\n".join(b).strip()]


def _pack(text: str, max_words: int) -> list[str]:
    """Greedily pack paragraphs into pieces of at most `max_words` words; a
    single oversized paragraph is split at word boundaries. Deterministic."""
    paras = [p.strip() for p in _PARA_SPLIT.split(text) if p.strip()]
    pieces: list[str] = []
    buf: list[str] = []
    count = 0

    def flush() -> None:
        nonlocal buf, count
        if buf:
            pieces.append("\n\n".join(buf))
            buf, count = [], 0

    for p in paras:
        w = len(p.split())
        if w > max_words:
            flush()
            words = p.split()
            for i in range(0, len(words), max_words):
                pieces.append(" ".join(words[i : i + max_words]))
            continue
        if count + w > max_words:
            flush()
        buf.append(p)
        count += w
    flush()
    return pieces


def chunk_document(meta: dict, body: str) -> list[Chunk]:
    doc_id = str(meta["doc_id"])
    title = str(meta.get("title", doc_id))
    org = str(meta.get("source_org", ""))
    url = str(meta.get("source_url", ""))
    out: list[Chunk] = []
    counters: dict[str, int] = {}  # per-slug, so duplicate headings can't collide
    for heading, text in _sections(body):
        slug = slugify(heading)
        for piece in _pack(text, MAX_CHUNK_WORDS):
            n = counters.get(slug, 0)
            counters[slug] = n + 1
            out.append(
                Chunk(
                    chunk_id=f"{doc_id}#{slug}:{n}",
                    doc_id=doc_id,
                    title=title,
                    section=heading,
                    text=piece,
                    source_org=org,
                    source_url=url,
                )
            )
    return out


def load_corpus(corpus_dir: Path | str = DEFAULT_CORPUS_DIR) -> list[Chunk]:
    """Load every corpus file in sorted order → deterministic chunk list."""
    chunks: list[Chunk] = []
    for path in sorted(Path(corpus_dir).glob("*.md")):
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        chunks.extend(chunk_document(meta, body))
    return chunks
