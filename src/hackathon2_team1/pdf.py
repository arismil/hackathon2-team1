"""PDF export of the Markdown assessment report."""

from __future__ import annotations

from pathlib import Path

from markdown_pdf import MarkdownPdf, Section

_CSS = """
body { font-family: sans-serif; font-size: 9pt; }
h1 { font-size: 16pt; } h2 { font-size: 13pt; margin-top: 12pt; } h3 { font-size: 11pt; }
table { border-collapse: collapse; width: 100%; font-size: 8pt; }
th, td { border: 1px solid #999; padding: 2pt 4pt; text-align: left; vertical-align: top; }
th { background-color: #e8e8e8; }
code { font-size: 8pt; }
blockquote { border-left: 3px solid #c00; padding-left: 6pt; color: #600; }
"""


def write_pdf(markdown: str, path: Path, title: str = "Vendor Assessment") -> Path:
    """Render the report Markdown to a PDF file at `path`."""
    pdf = MarkdownPdf(toc_level=2)
    pdf.add_section(Section(markdown, toc=False), user_css=_CSS)
    pdf.meta["title"] = title
    pdf.meta["author"] = "NFS Vendor Risk & Procurement Deep Agent"
    pdf.save(str(path))
    return path
