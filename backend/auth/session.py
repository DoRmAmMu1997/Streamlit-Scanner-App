"""Central Streamlit OIDC authentication gate.

Beginner note:
Streamlit's built-in authentication flow stores the current browser session in
``st.user`` after the user completes an OpenID Connect (OIDC) login. OIDC is the
standard protocol Google uses to tell an app "this person signed in, and here
are their identity claims". The app does not handle Google passwords directly;
it only asks Streamlit to start the Google login flow and then reads the trusted
identity information Streamlit exposes.

This module now owns both parts of the gate:
- authentication: "is someone signed in, and what email did Google give us?"
- authorization: "is that email allowed to use this scanner?"

DEPLOY-004 keeps the allowlist and environment-mode reads in
``backend.config.settings`` so this file does not need to parse environment
variables directly.
"""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, replace
from typing import Any, NoReturn

from backend.audit import record_audit_event, record_audit_event_once
from backend.auth.roles import DEFAULT_ROLE, Role, resolve_role, role_has_capability
from backend.config import get_settings
from backend.observability import (
    EVENT_AUTH_DENIED,
    EVENT_LOGIN_DENIED,
    EVENT_ROLE_DENIED,
    log_event,
)
from backend.storage import ensure_database_schema, get_user_role, session_scope

logger = logging.getLogger(__name__)


# Streamlit names each OIDC provider by the nested table under [auth].
# With [auth.google] in .streamlit/secrets.toml, the provider name passed to
# st.login(...) is exactly "google".
AUTH_PROVIDER = "google"

# These are the minimum Streamlit auth settings needed before we can even offer
# a Google login button. Keeping them in tuples makes the validation loop small
# and makes future provider/config changes obvious.
_SHARED_AUTH_KEYS = ("redirect_uri", "cookie_secret")
_PROVIDER_AUTH_KEYS = ("client_id", "client_secret", "server_metadata_url")


@dataclass(frozen=True)
class AuthenticatedUser:
    """Identity details the rest of the app may safely use.

    ``email`` is required because it is the stable value the allowlist and role
    table compare against. ``name`` is optional because identity providers may
    omit display names, but the email must be present.

    ``role`` is the AUTH-003 effective role (viewer/analyst/admin). AUTH-001's
    authentication gate builds the user without it (defaults to the lowest tier,
    ``VIEWER``); ``require_authorized_user`` fills the real role via
    ``backend.auth.roles.resolve_role``. ``is_admin`` is derived from it so every
    existing ``is_admin`` reader keeps working without a separate, drift-prone field.
    """

    email: str
    name: str | None = None
    role: Role = Role.VIEWER

    @property
    def is_admin(self) -> bool:
        """True when this user holds the admin role (back-compat convenience)."""
        return self.role is Role.ADMIN


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

    # Google always returns an "email" claim, so we read only that one field
    # (the provider is fixed to Google above). The email is the stable value the
    # app exposes and that future AUTH tasks will match against an allowlist, so
    # we lower-case it to one canonical form instead of trusting Google's casing.
    email = _normalize_email(_user_value(raw_user, "email"))
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
        # Without Authlib, st.login raises StreamlitAuthError the instant the
        # button is pressed. The app cannot function without it in any
        # environment, so treat it as a hard setup error and stop before
        # rendering a login button that would only throw.
        if not _is_authlib_available():
            st_module.error(
                "Google SSO needs the 'Authlib' package (>=1.3.2), which is not "
                "installed. Install it with `pip install Authlib` and restart the app."
            )
            _stop(st_module)
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

    # Defense in depth for the future allowlist work: only trust an email as an
    # identity once the provider has confirmed it belongs to this user. Google
    # always sends this claim; an explicit "unverified" is the only thing we
    # reject (see _is_email_verified for why an absent claim is allowed).
    if not _is_email_verified(raw_user):
        st_module.error("Your Google account email is not verified.")
        _stop(st_module)

    # Keep account controls in the sidebar where the scanner's command controls
    # already live. st.logout clears Streamlit's auth cookie and triggers a new
    # unauthenticated run on the next rerun.
    with st_module.sidebar:
        st_module.caption(f"Signed in as {user.email}")
        st_module.button("Log out", on_click=st_module.logout)

    return user


def require_authorized_user(st_module: Any) -> AuthenticatedUser:
    """Authenticate (AUTH-001) AND authorize (AUTH-002) the current user.

    This is the single gate the Streamlit app should call before loading any
    scanner feature. It first runs the AUTH-001 sign-in gate
    (``require_authenticated_user``), then enforces the email allowlist:

    - ``ADMIN_EMAILS`` are always allowed and come back with ``is_admin=True``.
    - If ``ALLOWED_EMAILS`` is non-empty, the signed-in email must be on it.
    - If ``ALLOWED_EMAILS`` is empty, development permits any verified user but
      production fails closed (only admins get in). This mirrors how AUTH-001
      treats missing SSO config as a dev warning but a production hard error.

    A rejected user sees a generic message and the run stops before any scanner
    control, result, or download renders. They stay signed in (AUTH-001 already
    drew the sidebar "Log out") so they can switch to an authorized account.

    Beginner note:
    Authentication answers "who is signed in?" Authorization answers "is that
    signed-in person allowed to use this app?" Those are separate checks on
    purpose. Even though AUTH-001 already lowercases real Google emails, this
    AUTH-002 boundary normalizes the email again before allowlist/admin checks.
    That makes this function robust if a future auth provider, fake test object,
    or refactor returns ``Sunny@Example.COM`` instead of ``sunny@example.com``.
    """
    user = require_authenticated_user(st_module)

    # Convert the signed-in user's email to the exact same lowercase key format
    # used by ALLOWED_EMAILS and ADMIN_EMAILS. This keeps all authorization
    # comparisons case-insensitive and prevents subtle admin-flag bugs.
    email = _normalize_email(user.email)

    # These helpers read from the process environment after load_environment()
    # has pulled in Dependencies/.env for local runs. Both helpers return
    # frozensets of already-normalized email keys.
    allowed = _allowed_emails()
    admins = _admin_emails()
    # AUTH-003: a user_roles row both authorizes sign-in (unioned with the env
    # lists) and supplies the role. One best-effort lookup serves both; a DB error
    # yields None, which fails closed for a table-only user (env allow-listed /
    # admin users are unaffected) — see ``_lookup_table_role``.
    table_role = _lookup_table_role(email)

    if not is_email_authorized(
        email,
        allowed=allowed,
        admins=admins,
        production=_is_production_mode(),
        in_role_table=table_role is not None,
    ):
        # Record the denied attempt for the operator's own audit trail. Only the
        # email is logged — never the allowlist itself — so the log cannot leak
        # who else has access.
        # OBS-001: auth_denied records who was turned away (email only - never the
        # allowlist itself) so operators can diagnose access problems.
        log_event(
            logger,
            EVENT_AUTH_DENIED,
            level=logging.WARNING,
            email=email,
            reason="not_on_allowlist",
        )
        # OBS-003: also record the denied attempt in the durable audit trail. A
        # denied user stops before app.main() reaches its schema bootstrap, so a
        # real Streamlit session gets one local bootstrap attempt here before the
        # audit write. The once-helper marks the session only after persistence
        # succeeds, letting a transient DB failure retry on the next rerun.
        session_state = getattr(st_module, "session_state", None)
        if not hasattr(session_state, "get"):
            record_audit_event(
                event=EVENT_LOGIN_DENIED,
                user_email=email,
                metadata={"reason": "not_on_allowlist"},
                level=logging.WARNING,
            )
        else:
            ensure_database_schema()
            record_audit_event_once(
                session_state=session_state,
                dedup_key=f"_audit_login_denied:{email}",
                event=EVENT_LOGIN_DENIED,
                user_email=email,
                metadata={"reason": "not_on_allowlist"},
                level=logging.WARNING,
            )
        st_module.error(
            "You are not authorized to access this app. "
            "Ask the administrator to add your email to the allowlist."
        )
        _stop(st_module)

    # Authorized. Resolve the effective AUTH-003 role: ADMIN_EMAILS is a floor,
    # otherwise the table role, otherwise the analyst default (see resolve_role).
    # Replacing the email keeps the returned state in the canonical lowercase form
    # used for every authorization comparison.
    role = resolve_role(
        email,
        in_admin_env=email in admins,
        table_role=table_role,
        default_role=DEFAULT_ROLE,
        auth_required=True,
    )
    return replace(user, email=email, role=role)


def is_email_authorized(
    email: str,
    *,
    allowed: frozenset[str],
    admins: frozenset[str],
    production: bool,
    in_role_table: bool = False,
) -> bool:
    """Pure allowlist decision — no Streamlit and no env reads, so it tests easily.

    Rules, in order:
      1. Admins are always authorized.
      2. A user with an AUTH-003 ``user_roles`` row (``in_role_table``) is
         authorized — the database doubles as a self-service allowlist.
      3. If an env allowlist is configured, the email must be on it.
      4. If no allowlist is configured, permit in development but deny in
         production (fail closed). Admins already passed at rule 1.

    ``in_role_table`` is passed in (not read here) so the function stays pure and
    DB-free; the gate computes it from one best-effort lookup.
    """
    email = _normalize_email(email)
    if email in admins:
        return True
    if in_role_table:
        return True
    if allowed:
        return email in allowed
    # No allowlist configured: dev permits everyone signed in; prod locks down.
    return not production


def require_capability(
    st_module: Any,
    *,
    role: Role,
    capability: str,
    email: str | None = None,
) -> None:
    """Stop the current run when ``role`` lacks ``capability`` (AUTH-003 guard).

    The defense-in-depth companion to the view-list/button hiding in ``app.py``:
    hiding a control is UX, this re-check at the action handler is the boundary.
    On a miss it logs (OBS-001) and durably audits (OBS-003) a ``role_denied``
    event — the actor's email and the attempted capability only, never the role
    table — then shows a generic message and ``st.stop()``s so code below never runs.

    ``role``/``email`` are passed in (not read from a user object) so the auth-off
    local-owner path (``role=ADMIN``, ``email=None``) and tests both work without
    constructing an ``AuthenticatedUser``. Mirrors the AUTH-002 denial branch,
    including the once-per-session dedup that keeps a viewer hammering a hidden
    control to a single audit row.
    """
    if role_has_capability(role, capability):
        return

    log_event(
        logger,
        EVENT_ROLE_DENIED,
        level=logging.WARNING,
        email=email,
        required_capability=capability,
        role=role.name.lower(),
    )
    metadata = {"required_capability": capability, "actual_role": role.name.lower()}
    session_state = getattr(st_module, "session_state", None)
    if not hasattr(session_state, "get"):
        record_audit_event(
            event=EVENT_ROLE_DENIED,
            user_email=email,
            metadata=metadata,
            level=logging.WARNING,
        )
    else:
        ensure_database_schema()
        record_audit_event_once(
            session_state=session_state,
            dedup_key=f"_audit_role_denied:{email}:{capability}",
            event=EVENT_ROLE_DENIED,
            user_email=email,
            metadata=metadata,
            level=logging.WARNING,
        )
    st_module.error("You do not have permission to perform this action.")
    _stop(st_module)


def _lookup_table_role(email: str) -> Role | None:
    """Return the AUTH-003 ``user_roles`` role for ``email``, best-effort.

    The role store lives in the database, but the auth gate runs before
    ``app.main()`` bootstraps the schema, so this ensures the schema first (a
    per-process no-op after the first call) and then reads the row. Any database
    problem (or an unknown stored role) yields ``None`` — which both authorization
    (treat as "no table grant") and ``resolve_role`` (fall back to the default
    role) handle as fail-closed, so a transient DB error can never *grant* access.
    """
    try:
        ensure_database_schema()
        with session_scope() as session:
            stored = get_user_role(session, email)
    except Exception:  # noqa: BLE001 - role lookup is best-effort and fails closed.
        logger.warning(
            "Could not read the user_roles table; treating as no assignment.",
            exc_info=True,
        )
        return None
    return Role.parse(stored)


def _allowed_emails() -> frozenset[str]:
    """Return the configured non-admin allowlist.

    ``ALLOWED_EMAILS`` is a comma-separated environment variable, for example
    ``sunny@example.com,friend@example.com``. Empty text is a meaningful value:
    development mode treats it as "let any signed-in user through", while
    production mode treats it as "deny everyone except admins".
    """
    # Settings already handles Dependencies/.env loading, trimming, lowercasing,
    # and empty-entry removal. Keeping that parsing centralized prevents auth and
    # production validation from drifting apart.
    return get_settings().allowed_emails


def _admin_emails() -> frozenset[str]:
    """Return the configured administrator email set.

    ``ADMIN_EMAILS`` uses the same comma-separated format as ``ALLOWED_EMAILS``.
    Admins are always authorized, even when ``ALLOWED_EMAILS`` is empty or does
    not include them. AUTH-002 only identifies admins; actual admin-only feature
    gating remains intentionally out of scope until AUTH-003.
    """
    # Admin parsing uses the same settings path as ALLOWED_EMAILS so casing and
    # whitespace behave identically for both lists.
    return get_settings().admin_emails


def _parse_email_set(raw: str) -> frozenset[str]:
    """Split a comma-separated email list into a normalized lowercase set.

    Whitespace around each entry is stripped and casing is collapsed so that
    "  Sunny@Example.COM " in the env matches the lowercased identity email the
    auth gate produces. Empty entries (e.g. a trailing comma) are dropped.

    A ``frozenset`` is used because the result is a read-only collection for one
    authorization decision. That makes it clear the parsed allowlist should not
    be mutated elsewhere in the app.
    """
    return frozenset(
        email for part in raw.split(",") if (email := _normalize_email(part))
    )


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
    # APP_ENV is the canonical DEPLOY-004 setting; SCANNER_ENV is still accepted
    # inside get_settings() as a legacy alias. Auth should not know about those
    # env names directly.
    return get_settings().is_production


def _is_authlib_available() -> bool:
    """Return True when the Authlib package needed for Google login is importable.

    Streamlit's ``st.login`` performs the Google OIDC redirect through Authlib. If
    Authlib is not installed, Streamlit raises ``StreamlitAuthError`` the moment
    the login button is pressed. Detecting it up front lets the gate fail with a
    clear setup message instead of a raw stack trace. ``find_spec`` only checks
    importability and does not import the package.
    """
    return importlib.util.find_spec("authlib") is not None


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


def _is_email_verified(user: Any) -> bool:
    """Return False only when the provider explicitly marks the email unverified.

    Google always sends an ``email_verified`` boolean in its OIDC claims, and the
    email is the value future AUTH tasks will compare against an allowlist. We
    only trust it as an identity when Google has verified it. When the claim is
    absent (other providers, or test fakes that omit it) we do not lock the user
    out, since this gate intentionally avoids inventing authorization rules.
    """
    value = getattr(user, "email_verified", None)
    if value is None:
        value = _mapping_get(user, "email_verified", None)
    return True if value is None else bool(value)


def _user_value(user: Any, key: str) -> str:
    """Read and normalize one identity claim from st.user."""
    value = getattr(user, key, None)
    if value is None:
        value = _mapping_get(user, key, "")
    return _clean_value(value)


def _clean_value(value: Any) -> str:
    """Convert a possibly-empty config/user value into trimmed text."""
    return str(value or "").strip()


def _normalize_email(value: Any) -> str:
    """Convert an email-like value into the canonical allowlist comparison key.

    Beginner note:
    Email addresses are usually treated case-insensitively by people and by most
    identity-provider workflows, but raw strings are not. Python considers
    ``"BOSS@example.com"`` and ``"boss@example.com"`` different strings. Every
    auth decision in this module therefore compares the same trimmed,
    lowercased representation.
    """
    return _clean_value(value).lower()


def _stop(st_module: Any) -> NoReturn:
    """Call st.stop() and guard against fakes that accidentally return."""
    st_module.stop()
    raise RuntimeError("Streamlit did not stop execution.")
