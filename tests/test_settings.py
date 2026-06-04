"""Tests for DEPLOY-004 runtime settings.

These tests are intentionally table-like and offline. They pass small ``env``
dictionaries into ``get_settings(...)`` instead of reading this developer
machine's real environment, so a local secret or shell variable cannot make the
test pass or fail by accident.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.config.settings import (
    PROJECT_ROOT,
    SettingsError,
    get_settings,
    secret_values,
    validate_production_settings,
)


def test_local_defaults_are_safe_for_development():
    """A fresh checkout should run locally without production-only secrets."""
    settings = get_settings(env={})

    # Local defaults should be boring and runnable: no production validation, no
    # required auth gate, and a SQLite DB under the repo's git-ignored data/.
    assert settings.app_env == "development"
    assert settings.is_production is False
    assert settings.data_dir == PROJECT_ROOT / "data"
    assert settings.database_url == f"sqlite:///{(PROJECT_ROOT / 'data' / 'scanner.db').as_posix()}"
    assert settings.allowed_emails == frozenset()
    assert settings.admin_emails == frozenset()
    assert settings.auth_required is False
    assert settings.log_level == "WARNING"


def test_environment_overrides_are_cleaned_and_normalized(tmp_path: Path):
    """Quoted env values, email casing, paths, and booleans should normalize."""
    data_dir = tmp_path / "runtime-data"
    # This mapping simulates values as they often appear in real dashboards or
    # hand-edited .env files: quoted, mixed-case, and padded with spaces.
    settings = get_settings(
        env={
            "APP_ENV": ' "production" ',
            "DATABASE_URL": ' "postgresql+psycopg://scanner:pass@db/scanner" ',
            "DATA_DIR": f" '{data_dir}' ",
            "ALLOWED_EMAILS": " Sunny@Example.COM, friend@example.com, ",
            "ADMIN_EMAILS": " Boss@Example.COM ",
            "ANTHROPIC_API_KEY": "anthropic-secret",
            "SERPAPI_API_KEY": "serp-secret",
            "DHAN_CLIENT_ID": " 1234567890 ",
            "DHAN_ACCESS_TOKEN": " token-secret ",
            "LOG_LEVEL": "debug",
            "AUTH_REQUIRED": "true",
        }
    )

    assert settings.app_env == "production"
    assert settings.is_production is True
    assert settings.database_url == "postgresql+psycopg://scanner:pass@db/scanner"
    assert settings.data_dir == data_dir
    assert settings.allowed_emails == frozenset({"sunny@example.com", "friend@example.com"})
    assert settings.admin_emails == frozenset({"boss@example.com"})
    assert settings.dhan_client_id == "1234567890"
    assert settings.dhan_access_token == "token-secret"
    assert settings.log_level == "DEBUG"
    assert settings.auth_required is True


def test_legacy_aliases_remain_supported(tmp_path: Path):
    """Older deployed/local env names should keep working during the transition."""
    # SCANNER_ENV, SCANNER_DEBUG, and DHAN_CLIENT_CODE are intentionally legacy
    # names. They should still work so existing local .env files do not break on
    # upgrade, but new docs point users to APP_ENV, LOG_LEVEL, and DHAN_CLIENT_ID.
    settings = get_settings(
        env={
            "SCANNER_ENV": "production",
            "DATABASE_URL": "postgresql://legacy-db",
            "DATA_DIR": str(tmp_path / "data"),
            "ADMIN_EMAILS": "admin@example.com",
            "DHAN_CLIENT_CODE": "legacy-client",
            "DHAN_ACCESS_TOKEN": "legacy-token",
            "SCANNER_DEBUG": "1",
        }
    )

    assert settings.app_env == "production"
    assert settings.auth_required is True
    assert settings.dhan_client_id == "legacy-client"
    assert settings.log_level == "DEBUG"
    validate_production_settings(settings)


def test_canonical_env_values_win_over_legacy_aliases(tmp_path: Path):
    """DEPLOY-004 names should take precedence when both old and new names exist."""
    # When both names exist, the new canonical setting must win. That prevents an
    # old leftover variable from overriding a deployment's explicit new value.
    settings = get_settings(
        env={
            "APP_ENV": "production",
            "SCANNER_ENV": "development",
            "DATABASE_URL": "postgresql://canonical-db",
            "DATA_DIR": str(tmp_path / "data"),
            "ADMIN_EMAILS": "admin@example.com",
            "DHAN_CLIENT_ID": "canonical-client",
            "DHAN_CLIENT_CODE": "legacy-client",
            "DHAN_ACCESS_TOKEN": "token",
            "LOG_LEVEL": "INFO",
            "SCANNER_DEBUG": "1",
        }
    )

    assert settings.app_env == "production"
    assert settings.dhan_client_id == "canonical-client"
    assert settings.log_level == "INFO"


def test_invalid_log_level_fails_clearly():
    """A typo in LOG_LEVEL should fail early instead of silently changing logs."""
    with pytest.raises(SettingsError, match="LOG_LEVEL"):
        get_settings(env={"LOG_LEVEL": "chatty"})


def test_production_validation_reports_missing_required_names():
    """Production should fail closed with names, not secret values."""
    settings = get_settings(env={"APP_ENV": "production"})

    # The error should be actionable for an operator: list the missing variable
    # names, but never include any actual secret values.
    with pytest.raises(SettingsError) as exc_info:
        validate_production_settings(settings)

    message = str(exc_info.value)
    for expected in (
        "DATABASE_URL",
        "DATA_DIR",
        "DHAN_CLIENT_ID",
        "DHAN_ACCESS_TOKEN",
        "ALLOWED_EMAILS or ADMIN_EMAILS",
    ):
        assert expected in message


def test_production_cannot_disable_auth(tmp_path: Path):
    """AUTH_REQUIRED=false is safe locally, but never acceptable in production."""
    settings = get_settings(
        env={
            "APP_ENV": "production",
            "DATABASE_URL": "postgresql://db",
            "DATA_DIR": str(tmp_path / "data"),
            "ADMIN_EMAILS": "admin@example.com",
            "DHAN_CLIENT_ID": "client",
            "DHAN_ACCESS_TOKEN": "token",
            "AUTH_REQUIRED": "false",
        }
    )

    with pytest.raises(SettingsError, match="AUTH_REQUIRED"):
        validate_production_settings(settings)


def test_settings_repr_and_safe_dict_never_print_secret_values(tmp_path: Path):
    """Debug output may show which secrets exist, but never the secrets."""
    # Put obvious marker strings in every secret-like field. If a future repr or
    # safe_dict accidentally prints raw values, this test will catch it loudly.
    settings = get_settings(
        env={
            "APP_ENV": "production",
            "DATABASE_URL": "postgresql://user:db-secret@db/scanner",
            "DATA_DIR": str(tmp_path / "data"),
            "ADMIN_EMAILS": "admin@example.com",
            "ANTHROPIC_API_KEY": "anthropic-secret",
            "SERPAPI_API_KEY": "serp-secret",
            "DHAN_CLIENT_ID": "client-secret",
            "DHAN_ACCESS_TOKEN": "token-secret",
        }
    )

    printable = repr(settings) + str(settings.safe_dict())
    for secret in (
        "db-secret",
        "anthropic-secret",
        "serp-secret",
        "client-secret",
        "token-secret",
    ):
        assert secret not in printable

    safe = settings.safe_dict()
    assert safe["has_database_url"] is True
    assert safe["has_anthropic_api_key"] is True
    assert safe["has_serpapi_api_key"] is True
    assert safe["has_dhan_client_id"] is True
    assert safe["has_dhan_access_token"] is True

    assert set(secret_values(settings)) >= {
        "postgresql://user:db-secret@db/scanner",
        "anthropic-secret",
        "serp-secret",
        "client-secret",
        "token-secret",
    }
