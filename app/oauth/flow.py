"""Authorization-code flow with PKCE, and the browser handoff back to Aperture.

The wrinkle this solves: OAuth needs a public, stable callback URL, and Aperture is
a desktop app that has none. Bloom does. So Bloom hosts the callback and Aperture
only triggers the flow and observes the result — which is why the deep link at the
end exists at all.

The sequence:

1. Aperture ``POST``s to ``/admin/agents/{id}/oauth/{provider}/start`` and gets an
   authorize URL.
2. It opens that URL in the system browser.
3. The user approves; the provider redirects to Bloom's callback.
4. Bloom exchanges the code, encrypts the tokens, binds the connection, and
   answers with a page that redirects to
   ``aperture://oauth-complete?provider=…&status=…`` **and** says "you can close
   this tab".

That last "and" is the load-bearing part. Aperture does not register the
``aperture://`` scheme yet, so today the deep link silently does nothing and the
visible text is the whole user experience — which works. When the handler lands,
the same page starts closing the loop automatically with no server change.

**PKCE is used wherever the provider supports it**, not only for public clients.
The callback is the one unauthenticated route in the service; its security is a
single-use ``state`` row plus a code verifier the interceptor of a redirect URL
never sees.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from app.config import Settings, get_settings
from app.crypto import encrypt
from app.db import Store
from app.providers import Provider

logger = logging.getLogger(__name__)

# How long a started flow stays valid. Long enough to log in and approve, short
# enough that a captured authorize URL is not useful tomorrow.
STATE_TTL_S = 600


class OAuthError(RuntimeError):
    """The handshake cannot proceed. Carries a message safe to show a user."""


def redirect_uri(provider_name: str, settings: Settings | None = None) -> str:
    """The callback URL, which must match what is registered with the provider."""
    settings = settings or get_settings()
    base = (settings.public_url or "").rstrip("/")
    if not base:
        raise OAuthError(
            "BLOOM_PUBLIC_URL is not set, so there is no callback address to give "
            "the provider. Set it to the public https:// origin of this service."
        )
    return f"{base}/admin/oauth/{provider_name}/callback"


def _pkce_pair() -> tuple[str, str]:
    """A code verifier and its S256 challenge, both base64url without padding."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def start(
    store: Store,
    provider: Provider,
    agent_config_id: str,
    *,
    scopes: list[str] | None = None,
    settings: Settings | None = None,
) -> dict:
    """Mint a state row and build the authorize URL Aperture should open."""
    settings = settings or get_settings()
    if not provider.configured:
        raise OAuthError(
            f"{provider.display_name} has no client credentials. Set "
            f"{provider.client_id_env} and {provider.client_secret_env} and restart."
        )

    callback = redirect_uri(provider.name, settings)
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair() if provider.pkce else ("", "")
    wanted = list(scopes or provider.scopes_default)
    expires = (datetime.now(UTC) + timedelta(seconds=STATE_TTL_S)).replace(microsecond=0)

    store.create_oauth_state(
        state=state,
        agent_config_id=agent_config_id,
        provider=provider.name,
        code_verifier=verifier,
        redirect_uri=callback,
        expires_at=expires.isoformat(),
    )

    query = {
        "client_id": provider.client_id,
        "response_type": "code",
        "redirect_uri": callback,
        "state": state,
        "scope": " ".join(wanted),
    }
    if provider.pkce:
        query["code_challenge"] = challenge
        query["code_challenge_method"] = "S256"

    return {
        "authorize_url": f"{provider.authorize_url}?{urlencode(query)}",
        "state": state,
        "expires_at": expires.isoformat(),
        "scopes": wanted,
        "redirect_uri": callback,
    }


async def exchange(
    store: Store,
    provider: Provider,
    code: str,
    state_row: dict,
    *,
    settings: Settings | None = None,
    http_client_factory=None,
) -> dict:
    """Trade the authorization code for tokens and store the connection."""
    settings = settings or get_settings()

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": state_row["redirect_uri"],
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if state_row.get("code_verifier"):
        payload["code_verifier"] = state_row["code_verifier"]
    if provider.auth_style == "basic":
        raw = f"{provider.client_id}:{provider.client_secret}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    else:
        payload["client_id"] = provider.client_id
        payload["client_secret"] = provider.client_secret

    import httpx2

    factory = http_client_factory or httpx2.AsyncClient
    async with factory() as client:
        response = await client.post(
            provider.token_url, data=payload, headers=headers, timeout=15.0
        )

    if response.status_code >= 400:
        # The body can contain the client_secret on some providers' error paths,
        # so only the status and the standardised `error` field are surfaced.
        detail = ""
        try:
            detail = str(response.json().get("error", ""))
        except Exception:  # noqa: BLE001 — a non-JSON error body is still an error
            pass
        raise OAuthError(
            f"{provider.display_name} refused the authorization code "
            f"(HTTP {response.status_code}{': ' + detail if detail else ''})."
        )

    data = response.json()
    access = data.get("access_token", "")
    if not access:
        raise OAuthError(f"{provider.display_name} returned no access token.")

    granted = data.get("scope")
    scopes = (
        granted.split() if isinstance(granted, str) and granted else list(provider.scopes_default)
    )

    expires_at = None
    if data.get("expires_in"):
        when = datetime.now(UTC) + timedelta(seconds=float(data["expires_in"]))
        expires_at = when.replace(microsecond=0).isoformat()

    refresh = data.get("refresh_token")
    connection = store.upsert_connection(
        provider=provider.name,
        agent_config_id=state_row["agent_config_id"],
        access_token=encrypt(access, settings),
        refresh_token=encrypt(refresh, settings) if refresh else None,
        expires_at=expires_at,
        scopes=scopes,
        status="active",
    )
    logger.info(
        "Connected %s for agent config %s (%d scope(s), refresh token: %s)",
        provider.name,
        state_row["agent_config_id"],
        len(scopes),
        "yes" if refresh else "no",
    )
    return connection


def completion_page(provider: Provider, status: str, message: str = "") -> str:
    """The page the provider's redirect lands on.

    Two mechanisms, on purpose. ``location.replace`` fires the ``aperture://`` deep
    link for the day the desktop app registers that scheme; the visible text is
    what makes the flow usable *today*, when nothing handles it and the browser
    quietly does nothing. Neither depends on the other working.

    Self-contained markup: this renders inside whatever browser the user happens to
    use, with no network available to it beyond the page itself.
    """
    ok = status == "success"
    heading = (
        f"{provider.display_name} connected" if ok else f"{provider.display_name} not connected"
    )
    body = (
        "You can close this tab and go back to Aperture."
        if ok
        else (message or "Something went wrong. Try connecting again from Aperture.")
    )
    deep_link = f"aperture://oauth-complete?provider={provider.name}&status={status}"
    accent = "#2f8f4e" if ok else "#b3261e"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{heading}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.55 system-ui, -apple-system, Segoe UI, sans-serif;
         margin: 0; min-height: 100vh; display: grid; place-items: center;
         padding: 2rem; }}
  .card {{ max-width: 26rem; text-align: center; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 .5rem; color: {accent}; }}
  p {{ margin: 0; opacity: .8; }}
  a {{ color: inherit; }}
</style></head>
<body><main class="card">
  <h1>{heading}</h1>
  <p>{body}</p>
  <p style="margin-top:1.5rem;font-size:.85rem;opacity:.6">
    <a href="{deep_link}">Return to Aperture</a>
  </p>
</main>
<script>
  // Fires the deep link if a handler is registered, and is a harmless no-op if
  // not — which is the state today. The text above is the real instruction.
  setTimeout(function () {{ location.replace({deep_link!r}); }}, 300);
</script>
</body></html>"""
