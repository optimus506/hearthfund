"""Build small .ods files in memory for the reader tests."""
from __future__ import annotations

import io
import zipfile


def make_ods(table_xml: str, *, styles: str = '', extra: dict[str, bytes] | None = None) -> bytes:
    """A minimal .ods with one content.xml body (table elements) and optional automatic styles."""
    ns = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
          'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
          'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
          'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
          'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"')
    content = ('<?xml version="1.0" encoding="UTF-8"?><office:document-content %s><office:automatic-styles>%s'
               '</office:automatic-styles><office:body><office:spreadsheet>%s</office:spreadsheet></office:body>'
               '</office:document-content>' % (ns, styles, table_xml))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('mimetype', 'application/vnd.oasis.opendocument.spreadsheet', compress_type=zipfile.ZIP_STORED)
        z.writestr('content.xml', content, compress_type=zipfile.ZIP_DEFLATED)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def cell(text: str, *, value: str | None = None, vtype: str = 'string', attrs: str = '') -> str:
    v = ''
    if vtype in ('float', 'currency'):
        v = ' office:value-type="%s" office:value="%s"' % (vtype, value)
    elif vtype == 'date':
        v = ' office:value-type="date" office:date-value="%s"' % value
    elif vtype == 'string':
        v = ' office:value-type="string"'
    return '<table:table-cell%s %s><text:p>%s</text:p></table:table-cell>' % (v, attrs, text)
