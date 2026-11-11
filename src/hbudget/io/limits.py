"""Size, zip-bomb and row-count limits shared by every reader (ARCHITECTURE 12.1)."""
from __future__ import annotations

import io
import zipfile
import zlib

from . import ImportProblem

__all__ = [
    'MAX_CELLS',
    'MAX_CELL_CHARS',
    'MAX_DEPTH',
    'MAX_FIELD_CHARS',
    'MAX_RATIO',
    'MAX_REPEAT_COLS',
    'MAX_REPEAT_ROWS',
    'MAX_ROWS',
    'MAX_SHEETS',
    'MAX_SPACES',
    'MAX_TEXT_CHARS',
    'MAX_UNCOMPRESSED',
    'MAX_UPLOAD',
    'MAX_ZIP_MEMBERS',
    'Totals',
    'check_size',
    'open_zip',
    'read_member',
]

MAX_UPLOAD = 5 * 1024 * 1024          # bytes per uploaded file (the web layer may lower or raise it)
MAX_UNCOMPRESSED = 50 * 1024 * 1024   # total bytes read out of one ODS/XLSX
MAX_ZIP_MEMBERS = 2000
MAX_RATIO = 100                       # uncompressed / compressed, per member
MAX_ROWS = 20_000                     # rows per sheet or file
MAX_FIELD_CHARS = 10_000              # characters per CSV field
MAX_REPEAT_COLS = 1024                # ODS number-columns-repeated per element (non-empty cells)
MAX_REPEAT_ROWS = 20_000              # ODS number-rows-repeated per element (non-empty rows)
MAX_CELLS = 1_000_000                 # cells per workbook (all sheets together)
MAX_SHEETS = 50                       # sheets per workbook
MAX_CELL_CHARS = 10_000               # characters of text per spreadsheet cell
MAX_TEXT_CHARS = 5_000_000            # characters of text per workbook (repeated cells count each time)
MAX_SPACES = 1_000                    # ODS <text:s text:c="N"/>: spaces written by one element
MAX_DEPTH = 64                        # nesting depth of XML / OFX structures we walk


class Totals:
    """Running totals for one workbook, so many small sheets cannot add up to a huge one."""

    def __init__(self) -> None:
        self.cells = 0
        self.chars = 0

    def add(self, cells: int, chars: int, *, max_cells: int | None = None, max_chars: int | None = None) -> None:
        self.cells += cells
        self.chars += chars
        cap_cells = MAX_CELLS if max_cells is None else max_cells
        cap_chars = MAX_TEXT_CHARS if max_chars is None else max_chars
        if self.cells > cap_cells:
            raise ImportProblem('The workbook has more than %d cells.' % cap_cells)
        if self.chars > cap_chars:
            raise ImportProblem('The workbook holds more than %d characters of text.' % cap_chars)


def check_size(data: bytes, limit: int = MAX_UPLOAD) -> None:
    if len(data) > limit:
        raise ImportProblem('The file is larger than %d MB.' % (limit // (1024 * 1024)))


def open_zip(data: bytes) -> zipfile.ZipFile:
    """Open a zip and check every member by its ACTUAL decompressed size (headers can lie)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ImportProblem('The file is damaged or is not a spreadsheet.') from None
    infos = z.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        raise ImportProblem('The file has too many parts to be a normal spreadsheet.')
    budget = MAX_UNCOMPRESSED
    for info in infos:
        budget -= len(read_member(z, info.filename, budget))
    return z


def read_member(z: zipfile.ZipFile, name: str, budget: int = MAX_UNCOMPRESSED) -> bytes:
    """Read one member through a counting stream that stops at the limit or at an unsafe compression ratio."""
    try:
        info = z.getinfo(name)
    except KeyError:
        raise ImportProblem('The spreadsheet is missing its %s part.' % name) from None
    limit = min(budget, MAX_UNCOMPRESSED)
    ratio_limit = max(info.compress_size, 1) * MAX_RATIO
    out = bytearray()
    try:
        with z.open(info) as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                out += chunk
                if len(out) > limit:
                    raise ImportProblem('The spreadsheet is too large when unpacked.')
                if len(out) > ratio_limit and len(out) > 1024 * 1024:
                    raise ImportProblem('The spreadsheet is compressed suspiciously well (a zip bomb?).')
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError, zlib.error, NotImplementedError):
        raise ImportProblem('The file is damaged or is not a spreadsheet.') from None
    return bytes(out)
