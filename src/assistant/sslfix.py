from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# Env vars various TLS stacks consult for a CA bundle.
_CA_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def sanitize_ca_env() -> None:
    """Drop CA-bundle env vars that point at non-existent files.

    This machine has a machine/user-level SSL_CERT_FILE left over from a
    PostgreSQL install (C:\\Program Files\\PostgreSQL\\16\\ssl\\certs\\ca-bundle.crt)
    that doesn't exist. requests/httpx/msal then fail every HTTPS call.
    If the configured path is missing, repoint at certifi's bundle so
    networking works regardless of how the process was launched (shell,
    service, container).
    """
    try:
        import certifi

        good_bundle = certifi.where()
    except Exception:  # pragma: no cover - certifi always present
        good_bundle = None

    for var in _CA_ENV_VARS:
        path = os.environ.get(var)
        if path and not os.path.isfile(path):
            if good_bundle:
                os.environ[var] = good_bundle
                log.warning("%s pointed at missing %r; repointed to certifi bundle", var, path)
            else:
                del os.environ[var]
                log.warning("%s pointed at missing %r; cleared it", var, path)
