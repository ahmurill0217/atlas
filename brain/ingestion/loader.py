"""File discovery: plain text, Markdown and PDF."""

from __future__ import annotations

from pathlib import Path

SUPPORTED_EXTENSIONS = {".txt", ".md", ".markdown", ".pdf"}


def discover_files(path: str | Path) -> list[Path]:
    """Return supported files under `path` (or `path` itself), sorted so that
    ingestion and extraction order is deterministic across runs."""
    root = Path(path)
    if root.is_file():
        return [root] if root.suffix.lower() in SUPPORTED_EXTENSIONS else []
    if not root.exists():
        raise FileNotFoundError(root)
    return sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    )
