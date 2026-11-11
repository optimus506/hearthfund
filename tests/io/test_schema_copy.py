from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_packaged_schema_is_byte_identical() -> None:
    canonical = (ROOT / 'schema' / 'bill-system.schema.json').read_bytes()
    packaged = (ROOT / 'src' / 'hbudget' / 'schema' / 'bill-system.schema.json').read_bytes()
    assert canonical == packaged, 'run `make schema` to copy schema/bill-system.schema.json into the package'
