"""Origin and Referer evaluation for cookie-authenticated writes.

`docs/security.md` 2.4 lists four CSRF layers. Layers 1, 2 and 4 - `SameSite`,
the double-submit token and the CORS allowlist - were delivered in Phase 3.
This module is layer 3, and it is deliberately a pure function over headers:
nothing here touches the request object, the database or settings, so the rule
can be reasoned about and tested without an application.

Why this layer earns its place when a double-submit token already exists: the
token defends against a forged *request*, but it is carried in a cookie the
browser is willing to hand to any page on the same site. A subdomain takeover,
a cache-poisoned script or an XSS on a sibling host can read it. The `Origin`
header cannot be set by page JavaScript at all - the browser writes it - so an
attacker's page cannot make it say anything other than the attacker's origin.

The rules, in order:

1. `Origin` is preferred. `Referer` is the fallback, because a few browser
   configurations and proxies strip `Origin` on same-origin requests, and
   `Referer` carries the same information with more privacy noise attached.
2. A header that is present but unparsable is a rejection, never a pass. That
   includes the literal `null` origin a sandboxed iframe or a `data:` document
   sends, which is exactly the context an attacker controls.
3. An origin on the allowlist passes.
4. An origin whose host *and port* match the request's own `Host` passes. This
   is what keeps the first-party dashboard working without every deployment
   having to duplicate its own address into the allowlist. An attacker cannot
   forge it: the browser sets `Origin` from the page that made the request.
5. Absent `Origin` *and* `Referer` is a rejection only when the caller asks for
   presence to be required. That switch is on in production unconditionally
   (see `Settings.require_origin_on_cookie_writes`); elsewhere it is off so
   that curl, the test suite and local tooling can drive the API. A non-browser
   client cannot be the victim of CSRF in the first place - there is no ambient
   cookie for an attacker's page to borrow.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

_NULL_ORIGIN: Final = "null"
_DEFAULT_PORTS: Final[dict[str, int]] = {"http": 80, "https": 443}


@dataclass(frozen=True, slots=True)
class OriginDecision:
    """The outcome of evaluating one request's browser origin.

    `reason` is operator-facing only. It is written to `internal_message`,
    which ADR-0013 guarantees is logged and never serialised, because telling a
    caller *why* their origin was refused tells an attacker which part of the
    allowlist to probe next.
    """

    allowed: bool
    reason: str
    origin: str | None = None


def normalize_origin(value: str | None) -> str | None:
    """Return `scheme://host[:port]` in canonical form, or None if unusable.

    The default port is dropped so that `https://app.example.com` and
    `https://app.example.com:443` are the same allowlist entry. Anything that
    is not an absolute http(s) URL - including the literal `null` - returns
    None, and every caller treats None as a refusal rather than a wildcard.
    """
    if value is None:
        return None
    candidate = value.strip()
    if not candidate or candidate.casefold() == _NULL_ORIGIN:
        return None
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    if port is None or port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _host_matches(origin: str, host_header: str | None) -> bool:
    """True when an origin names the same host *and port* the request went to.

    The scheme is deliberately not compared. Behind a TLS terminator the
    application sees plain HTTP while the browser reports `https://`, so
    comparing schemes would refuse every same-origin write in production. The
    scheme is still policed separately by `require_https`.

    The port is compared, because a different port is a different origin. Two
    services on one host - the dashboard on 443, something experimental on
    8443 - are separate trust domains, and the weaker one must not inherit the
    stronger one's writes. A `Host` with no port means the request arrived on
    the scheme's default port, and `normalize_origin` has already dropped
    default ports, so an origin still carrying one names a different port.
    """
    if not host_header:
        return False
    try:
        stated = urlsplit(f"//{host_header.strip()}")
        stated_port = stated.port
    except ValueError:
        return False
    stated_host = (stated.hostname or "").lower()
    if not stated_host:
        return False
    origin_parts = urlsplit(origin)
    if stated_host != (origin_parts.hostname or "").lower():
        return False
    if stated_port is None:
        return origin_parts.port is None
    return stated_port == (origin_parts.port or _DEFAULT_PORTS[origin_parts.scheme])


def evaluate_origin(
    *,
    origin_header: str | None,
    referer_header: str | None,
    host_header: str | None,
    allowed_origins: Iterable[str],
    require_present: bool,
    require_https: bool,
) -> OriginDecision:
    """Decide whether a browser origin may perform a state-changing request."""
    stated = origin_header.strip() if origin_header and origin_header.strip() else None
    source = "origin"
    if stated is None and referer_header and referer_header.strip():
        stated = referer_header.strip()
        source = "referer"

    if stated is None:
        if require_present:
            return OriginDecision(allowed=False, reason="origin_and_referer_absent")
        return OriginDecision(allowed=True, reason="no_browser_origin_presented")

    origin = normalize_origin(stated)
    if origin is None:
        # Covers the literal `null` origin as well as anything malformed. A
        # header we cannot parse is not a header we may trust.
        return OriginDecision(allowed=False, reason=f"unparsable_{source}")

    if require_https and not origin.startswith("https://"):
        return OriginDecision(allowed=False, reason="insecure_origin_scheme", origin=origin)

    allowlist = {
        normalized
        for entry in allowed_origins
        if (normalized := normalize_origin(entry)) is not None
    }
    if origin in allowlist:
        return OriginDecision(allowed=True, reason="allowlisted", origin=origin)
    if _host_matches(origin, host_header):
        return OriginDecision(allowed=True, reason="same_origin", origin=origin)
    return OriginDecision(allowed=False, reason=f"{source}_not_allowed", origin=origin)
