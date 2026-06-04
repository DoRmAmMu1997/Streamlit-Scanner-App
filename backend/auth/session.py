"""Central Streamlit OIDC authentication gate.

Beginner note:
Streamlit's built-in authentication flow stores the current browser session in
``st.user`` after the user completes an OpenID Connect (OIDC) login. OIDC is the
standard protocol Google uses to tell an app "this person signed in, and here
are their identity claims". The app does not handle Google passwords directly;
it only asks Streamlit to start the Google login flow and then reads the trusted
identity information Streamlit exposes.

AUTH-001 deliberately keeps authorization out of scope. This module only answers
"is someone signed in, and what email did Google give us?" Later tasks can use
the returned email for allowlists or roles, but this file should not grow those
rules yet.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from backend.config import load_environment


# Streamlit names each OIDC provider by the nested table under [auth].
# With [auth.google] in .streamlit/secrets.toml, the provider name passed to
# st.login(...) is exactly "google".
AUTH_PROVIDER = "google"

# Production mode is intentionally strict. If the deployed app is missing SSO
# config, it must stop before exposing scanner controls or market data actions.
_PRODUCTION_ENV_VALUES = {"prod", "production"}

# These are the minimum Streamlit auth settings needed before we can even offer
# a Google login button. Keeping them in tuples makes the validation loop small
# and makes future provider/config changes obvious.
_SHARED_AUTH_KEYS = ("redirect_uri", "cookie_secret")
_PROVIDER_AUTH_KEYS = ("client_id", "client_secret", "server_metadata_url")


@dataclass(frozen=True)
class AuthenticatedUser:
    """Identity details the rest of the app may safely use.

    ``email`` is required because it is the stable value future AUTH tasks can
    compare against an allowlist or role table. ``name`` is optional because
    identity providers may omit display names, but the email must be present.
    """

    email: str
    name: str | None = None


def auth_config_status(st_module: Any) -> dict[str, object]:
    """Return whether Streamlit has enough OIDC config to attempt login.

    The scanner calls this before showing the login button. That may feel
    defensive, but it avoids a confusing half-working UI where a user can press
    "Log in" only to discover that the deployed app is missing client secrets.

    ``st_module`` is injected instead of importing the global ``streamlit`` name
    here so tests can pass a tiny fake object. That keeps auth behavior easy to
    test without launching a real browser or contacting Google.
    """
    auth_config = _mapping_get(_streamlit_secrets(st_module), "auth", {})
    missing_keys: list[str] = []

    # First validate the shared [auth] keys. These are not Google-specific:
    # Streamlit uses them to know where Google should redirect back and how to
    # sign the session cookie it stores in the user's browser.
    if not auth_config:
        missing_keys.append("auth")
    for key in _SHARED_AUTH_KEYS:
        if not _clean_value(_mapping_get(auth_config, key, "")):
            missing_keys.append(f"auth.{key}")

    # Then validate the provider-specific [auth.google] keys. Google gives the
    # client_id/client_secret when you create the OAuth client; the metadata URL
    # tells Streamlit where Google's OIDC endpoints live.
    provider_config = _mapping_get(auth_config, AUTH_PROVIDER, {})
    for key in _PROVIDER_AUTH_KEYS:
        if not _clean_value(_mapping_get(provider_config, key, "")):
            missing_keys.append(f"auth.{AUTH_PROVIDER}.{key}")

    return {
        "provider": AUTH_PROVIDER,
        "production": _is_production_mode(),
        "ready": not missing_keys,
        "missing_keys": tuple(missing_keys),
    }


def get_authenticated_user(st_module: Any) -> AuthenticatedUser | None:
    """Return the current logged-in Streamlit user, or None.

    Streamlit exposes ``st.user`` as an object with fields such as
    ``is_logged_in``, ``email``, and ``name`` after a successful login. Tests use
    simple dictionaries or ``SimpleNamespace`` objects instead, so the helper
    functions below read both attribute-style and mapping-style values.
    """
    raw_user = getattr(st_module, "user", None)
    if not _is_user_logged_in(raw_user):
        return None

    # Google normally returns "email". "preferred_username" is a harmless
    # fallback for OIDC providers that name the same concept differently.
    email = _user_value(raw_user, "email") or _user_value(raw_user, "preferred_username")
    if not email:
        return None
    return AuthenticatedUser(email=email, name=_user_value(raw_user, "name") or None)


def require_authenticated_user(st_module: Any) -> AuthenticatedUser:
    """Render login/logout controls and stop when the session is not authenticated.

    This is the single gate the Streamlit app should call before loading any
    scanner features. If the user is not authenticated, ``st.stop()`` halts the
    current script run, which means code below the gate does not execute.
    """
    status = auth_config_status(st_module)
    if not status["ready"]:
        message = (
            "Google SSO is not configured. Add the required [auth] and "
            f"[auth.{AUTH_PROVIDER}] values to .streamlit/secrets.toml before "
            "using the scanner."
        )
        if status["production"]:
            st_module.error(message)
        else:
            st_module.warning(message)
        _stop(st_module)

    raw_user = getattr(st_module, "user", None)
    if not _is_user_logged_in(raw_user):
        # Streamlit reruns the script when a widget is clicked. Passing
        # st.login as the button callback lets Streamlit begin the Google OIDC
        # redirect flow at the exact moment the user presses the button.
        st_module.button(
            "Log in with Google",
            type="primary",
            on_click=st_module.login,
            args=(AUTH_PROVIDER,),
        )
        _stop(st_module)

    user = get_authenticated_user(st_module)
    if user is None:
        st_module.error("Your login did not provide an email address.")
        _stop(st_module)

    # Keep account controls in the sidebar where the scanner's command controls
    # already live. st.logout clears Streamlit's auth cookie and triggers a new
    # unauthenticated run on the next rerun.
    with st_module.sidebar:
        st_module.caption(f"Signed in as {user.email}")
        st_module.button("Log out", on_click=st_module.logout)

    return user


def auth_secret_values(st_module: Any) -> list[str]:
    """Return OIDC secret-like values that should be masked in UI errors.

    Error messages from SDKs and frameworks can sometimes echo configuration
    values. The app's central redaction helper asks for these values and masks
    them before writing any exception text into Streamlit.
    """
    auth_config = _mapping_get(_streamlit_secrets(st_module), "auth", {})
    provider_config = _mapping_get(auth_config, AUTH_PROVIDER, {})
    values = [
        _mapping_get(auth_config, "cookie_secret", ""),
        _mapping_get(provider_config, "client_id", ""),
        _mapping_get(provider_config, "client_secret", ""),
    ]
    return [cleaned for value in values if (cleaned := _clean_value(value))]


def _is_production_mode() -> bool:
    """Return True when local/deployment config says this is production."""
    load_environment()
    return _clean_value(os.getenv("SCANNER_ENV")).lower() in _PRODUCTION_ENV_VALUES


def _streamlit_secrets(st_module: Any) -> Any:
    """Read st.secrets safely, even from tests or partially configured runs."""
    try:
        return getattr(st_module, "secrets", {})
    except Exception:
        return {}


def _mapping_get(source: Any, key: str, default: Any = None) -> Any:
    """Read one value from dict-like or Streamlit secrets/user objects."""
    if source is None:
        return default
    try:
        if hasattr(source, "get"):
            return source.get(key, default)
        return source[key]
    except Exception:
        return default


def _is_user_logged_in(user: Any) -> bool:
    """Return Streamlit's logged-in flag without assuming one object shape."""
    if user is None:
        return False
    value = getattr(user, "is_logged_in", None)
    if value is None:
        value = _mapping_get(user, "is_logged_in", False)
    return bool(value)


def _user_value(user: Any, key: str) -> str:
    """Read and normalize one identity claim from st.user."""
    value = getattr(user, key, None)
    if value is None:
        value = _mapping_get(user, key, "")
    return _clean_value(value)


def _clean_value(value: Any) -> str:
    """Convert a possibly-empty config/user value into trimmed text."""
    return str(value or "").strip()


def _stop(st_module: Any) -> None:
    """Call st.stop() and guard against fakes that accidentally return."""
    st_module.stop()
    raise RuntimeError("Streamlit did not stop execution.")
