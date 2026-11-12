"""Prefixed settings: each app sees only its own values, and mistakes stop the start-up."""

from __future__ import annotations

import pytest

from router.settings import ConfigError, env_prefix, load_all


def test_prefix_rule():
    assert env_prefix("all-mo-diary") == "ALL_MO_DIARY__"
    assert env_prefix("mama-stocker") == "MAMA_STOCKER__"


def test_each_app_only_sees_its_own_values():
    s = load_all(["all-mo-diary", "mama-stocker"], {
        "ALL_MO_DIARY__DATABASE_URL": "postgresql://a", "MAMA_STOCKER__DATABASE_URL": "postgresql://b",
        "ALL_MO_DIARY__DAILY_LIMIT": "7", "PATH": "/usr/bin"})
    assert s["all-mo-diary"].text("DATABASE_URL") == "postgresql://a"
    assert s["mama-stocker"].text("DATABASE_URL") == "postgresql://b"
    assert s["all-mo-diary"].int("DAILY_LIMIT", 10) == 7
    assert s["mama-stocker"].int("DAILY_LIMIT", 10) == 10
    assert s["all-mo-diary"].text("PATH") == ""


def test_bare_per_app_key_is_refused():
    with pytest.raises(ConfigError, match="DATABASE_URL is set without an app prefix"):
        load_all(["all-mo-diary"], {"DATABASE_URL": "postgresql://x"})


def test_prefix_for_no_app_is_refused():
    with pytest.raises(ConfigError, match="ALL_MO_DAIRY__DATABASE_URL"):
        load_all(["all-mo-diary"], {"ALL_MO_DAIRY__DATABASE_URL": "postgresql://x"})


def test_unrelated_double_underscore_variables_are_ignored():
    load_all(["all-mo-diary"], {"SOME_TOOL__HOME": "/x", "__CF_USER_TEXT_ENCODING": "0x1F5"})


def test_quoted_value_is_refused_without_printing_it():
    with pytest.raises(ConfigError) as exc:
        load_all(["all-mo-diary"], {"ALL_MO_DIARY__DATABASE_URL": '"postgresql://user@host/db"'})
    assert "pw" not in str(exc.value) and "ALL_MO_DIARY__DATABASE_URL" in str(exc.value)


def test_two_apps_on_one_database_is_refused():
    with pytest.raises(ConfigError, match="same value"):
        load_all(["all-mo-diary", "mama-fufood"], {
            "ALL_MO_DIARY__DATABASE_URL": "postgresql://same", "MAMA_FUFOOD__DATABASE_URL": "postgresql://same"})


def test_bad_number_names_the_key():
    s = load_all(["all-mo-diary"], {"ALL_MO_DIARY__DAILY_LIMIT": "ten"})
    with pytest.raises(ConfigError, match="ALL_MO_DIARY__DAILY_LIMIT"):
        s["all-mo-diary"].int("DAILY_LIMIT", 10)


def test_origins_are_checked():
    s = load_all(["all-mo-diary"], {"ALL_MO_DIARY__ALLOWED_ORIGINS": "https://a.example, http://localhost:8010/"})
    assert s["all-mo-diary"].origins() == ("https://a.example", "http://localhost:8010")
    bad = load_all(["all-mo-diary"], {"ALL_MO_DIARY__ALLOWED_ORIGINS": "https://a.example/admin"})
    with pytest.raises(ConfigError, match="not an origin"):
        bad["all-mo-diary"].origins()


def test_repr_hides_values():
    s = load_all(["all-mo-diary"], {"ALL_MO_DIARY__SESSION_SECRET": "very-secret"})
    assert "very-secret" not in repr(s["all-mo-diary"])
