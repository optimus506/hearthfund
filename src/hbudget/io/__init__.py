"""Importers and exporters (ARCHITECTURE section 12). Every reader takes bytes and raises ImportProblem."""
from __future__ import annotations

import re

from hbudget.core.textmask import mask

__all__ = ['ImportProblem', 'clean_filename']


class ImportProblem(ValueError):
    """A file could not be read. The message is meant for the user and never contains account numbers."""

    def __init__(self, message: str, *, line: int | None = None) -> None:
        self.message = message
        self.line = line
        super().__init__(message if line is None else 'line %d: %s' % (line, message))


def clean_filename(name: str, *, limit: int = 120) -> str:
    """An upload's filename as it may be stored and shown: no folders, safe characters, numbers masked."""
    base = re.split(r'[\\/]', name or '')[-1]
    base = mask(base)      # mask first, while the digits and their separators are still together
    base = re.sub(r'[^A-Za-z0-9._ ()#-]+', '_', base).strip(' ._') or 'upload'
    return base[:limit]
