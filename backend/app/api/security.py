"""Cross-site request protection and the cart cookie (ARCHITECTURE §6, ADR-024).

**Same-origin check.** Every cart change must carry an ``Origin`` header equal to the site's own
origin, and must not be marked cross-site by ``Sec-Fetch-Site``. SameSite cookies are not
relied on alone.

The expected origin is built only from information a browser cannot forge:

- **Host:** the ``Host`` header. uvicorn never rewrites it from forwarded headers (its proxy
  middleware reads only ``X-Forwarded-Proto`` and ``X-Forwarded-For``), and Render passes the
  public host through.
- **Scheme:** in production, always ``https``: Render serves the site only over TLS. It is
  deliberately *not* taken from ``X-Forwarded-Proto``: ``scripts/start.sh`` runs uvicorn with
  ``--forwarded-allow-ips '*'``, so that header is trusted from any client. Elsewhere
  (development, tests) the scheme is the connection's own; uvicorn's default trusts forwarded
  headers only from 127.0.0.1.

A browser always sends ``Origin`` on PUT, POST and DELETE requests, so a request without one is
refused. Non-browser clients (the smoke test) must send it explicitly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request, Response

from app.config import Environment, Settings
from app.errors import AtlasError

CART_COOKIE = "atlas_cart"
CART_COOKIE_PATH = "/api/cart"
_DEFAULT_PORTS = {"http": 80, "https": 443}


class CrossOriginRequest(AtlasError):
    status_code = 403
    code = "cross_origin_request"

    def __init__(self) -> None:
        super().__init__("This request must come from the Atlas site itself.")


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def normalize_origin(value: str) -> str | None:
    """``scheme://host[:port]`` in canonical form, or None if ``value`` is not a plain origin.

    Lower-cases the scheme and host and drops the scheme's default port, so
    ``HTTPS://Example.com:443`` and ``https://example.com`` compare equal. Anything with a path,
    query, fragment or credentials is not an origin.
    """
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host or parts.username is not None or parts.password is not None:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}" if port in (None, _DEFAULT_PORTS[scheme]) else f"{scheme}://{host}:{port}"


def expected_origin(request: Request, settings: Settings) -> str | None:
    """The site's own origin for this request: trusted scheme plus the ``Host`` header."""
    hosts = request.headers.getlist("host")
    if len(hosts) != 1 or not hosts[0]:
        return None
    scheme = "https" if settings.app_env is Environment.PRODUCTION else request.url.scheme
    return normalize_origin(f"{scheme}://{hosts[0]}")


def require_same_origin(request: Request, settings: SettingsDep) -> None:
    """Refuse a cart change unless it demonstrably comes from the site itself."""
    fetch_site = request.headers.getlist("sec-fetch-site")
    if fetch_site and fetch_site != ["same-origin"]:
        raise CrossOriginRequest()
    origins = request.headers.getlist("origin")
    if len(origins) != 1:
        raise CrossOriginRequest()
    expected = expected_origin(request, settings)
    if expected is None or normalize_origin(origins[0]) != expected:
        raise CrossOriginRequest()


SameOrigin = Depends(require_same_origin)


def _secure(settings: Settings) -> bool:
    # Production is HTTPS-only; local development over http would otherwise lose the cookie.
    return settings.app_env is Environment.PRODUCTION


def set_cart_cookie(response: Response, token: str, *, expires_at: datetime, now: datetime, settings: Settings) -> None:
    """Send the cart token. It lives exactly as long as the cart does on the server."""
    response.set_cookie(
        CART_COOKIE,
        token,
        max_age=max(0, int((expires_at - now).total_seconds())),
        path=CART_COOKIE_PATH,
        secure=_secure(settings),
        httponly=True,
        samesite="lax",
    )


def delete_cart_cookie(response: Response, settings: Settings) -> None:
    """Remove a stale cart cookie, with the same attributes it was set with."""
    response.delete_cookie(
        CART_COOKIE,
        path=CART_COOKIE_PATH,
        secure=_secure(settings),
        httponly=True,
        samesite="lax",
    )
