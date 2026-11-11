"""Container HEALTHCHECK: exit 0 when the local server answers /healthz with 200.

Standard library only and independent of the app package, so the check works with any app version the
image is built around (the upgrade test builds old app code with the current Dockerfile).

Plain HTTP by default. When HBUDGET_TLS_CERT is set, HTTPS that trusts exactly that certificate (or
chain); the name is not checked because it connects to 127.0.0.1, not the name on the certificate, so an
expired or replaced certificate makes the container unhealthy.
"""
from __future__ import annotations

import os
import ssl
import sys
import urllib.request


def main() -> int:
    port = (os.environ.get('HBUDGET_PORT') or '8080').strip()
    cert = (os.environ.get('HBUDGET_TLS_CERT') or '').strip()
    ctx = None
    if cert:
        ctx = ssl.create_default_context(cafile=cert)
        ctx.check_hostname = False
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    url = '%s://127.0.0.1:%s/healthz' % ('https' if cert else 'http', port)
    try:
        with urllib.request.urlopen(url, timeout=4, context=ctx) as r:  # noqa: S310 (fixed http/https URL)
            return 0 if r.status == 200 else 1
    except (OSError, ValueError) as e:
        print('unhealthy: %s' % e, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
