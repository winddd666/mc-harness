"""Contract tests for Skin Station bot creation, MC API authentication, token caching, and ready loop."""

from __future__ import annotations

import json
import pathlib
import stat

import httpx
import pytest

from mc_agent import (
    AuthError,
    BotManager,
    CaptchaRequiredError,
    DeathLoopCooldownError,
    EmailUnverifiedError,
    Settings,
)
from mc_agent.cli import _upsert_dotenv
from mc_agent.config import parse_dotenv, should_trust_env


def _skin_html(*, csrf: str = "csrf_tok_1", extra: dict | None = None) -> str:
    extra_json = json.dumps(extra or {})
    return f"""<!DOCTYPE html>
<html><head><meta name="csrf-token" content="{csrf}"></head>
<body><script id="blessing-extra" type="application/json">{extra_json}</script></body></html>"""


def test_create_bot_end_to_end_with_auto_sign_and_token_reuse(tmp_path: pathlib.Path) -> None:
    """Full flow: Skin login -> auto sign-in for score -> create player -> MC API login by bot name -> wait ready -> reuse token."""
    skin_state = {
        "logged_in": False,
        "score": 50,
        "cost": 100,
        "players": [{"pid": 1, "name": "Existing_Bot", "uid": 10}],
        "create_calls": 0,
        "sign_calls": 0,
    }
    mc_state = {
        "login_calls": 0,
        "status_calls": 0,
        "connect_calls": 0,
        "issued_token": "a" * 64,
        "last_login_username": None,
    }

    def skin_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/auth/login":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=_skin_html(
                    csrf="csrf_login",
                    extra={"tooManyFails": False, "recaptcha": "", "invisible": False},
                ),
            )
        if request.method == "POST" and path == "/auth/login":
            assert request.headers.get("X-CSRF-TOKEN") == "csrf_login"
            body = json.loads(request.content.decode())
            if body["identification"] == "student@smail.nju.edu.cn" and body["password"] == "secret123":
                skin_state["logged_in"] = True
                return httpx.Response(200, json={"code": 0, "message": "ok", "data": {"redirectTo": "/user"}})
            return httpx.Response(200, json={"code": 1, "message": "wrong password"})
        if request.method == "GET" and path == "/user":
            assert skin_state["logged_in"]
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=_skin_html(csrf="csrf_user", extra={"unverified": False}),
            )
        if request.method == "GET" and path == "/user/player/list":
            return httpx.Response(200, json=skin_state["players"])
        if request.method == "GET" and path == "/user/score-info":
            return httpx.Response(
                200,
                json={
                    "user": {"score": skin_state["score"], "lastSignAt": "2026-11-09 00:00:00"},
                    "rate": {"storage": 1, "players": skin_state["cost"]},
                    "usage": {"players": len(skin_state["players"]), "storage": 0},
                    "signAfterZero": True,
                    "signGapTime": 24,
                },
            )
        if request.method == "POST" and path == "/user/sign":
            assert request.headers.get("X-CSRF-TOKEN") == "csrf_user"
            skin_state["sign_calls"] += 1
            skin_state["score"] += 80
            return httpx.Response(
                200,
                json={"code": 0, "message": "signed", "data": {"score": skin_state["score"]}},
            )
        if request.method == "POST" and path == "/user/player":
            assert request.headers.get("X-CSRF-TOKEN") == "csrf_user"
            skin_state["create_calls"] += 1
            body = json.loads(request.content.decode())
            new_player = {"pid": 2, "name": body["name"], "uid": 10, "tid_skin": 0, "tid_cape": 0}
            skin_state["players"].append(new_player)
            skin_state["score"] -= skin_state["cost"]
            return httpx.Response(200, json={"code": 0, "message": "created", "data": new_player})
        raise AssertionError(f"Unexpected skin request: {request.method} {path}")

    def mc_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/v1/auth/login":
            mc_state["login_calls"] += 1
            body = json.loads(request.content.decode())
            mc_state["last_login_username"] = body["username"]
            # Multi-profile behavior: email login returns 401 no_profile!
            if "@" in body["username"]:
                return httpx.Response(
                    401,
                    json={"ok": False, "error": {"code": "no_profile", "message": "No selected profile"}},
                )
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "token": mc_state["issued_token"],
                    "expires_at": "2099-01-01T00:00:00.000Z",
                    "bot_username": body["username"],
                },
            )
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {mc_state['issued_token']}":
            return httpx.Response(
                401,
                json={"ok": False, "error": {"code": "invalid_token", "message": "Invalid token"}},
            )
        if request.method == "GET" and path == "/api/v1/status":
            mc_state["status_calls"] += 1
            # First status call is lobby (ready=False), second is survivaltest (ready=True)
            is_ready = mc_state["status_calls"] >= 2
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "ms": 1,
                    "result": {
                        "username": "NJU_Bot_02",
                        "pos": [10.5, 64.0, -20.0],
                        "health": 20,
                        "food": 20,
                        "ready": is_ready,
                        "busy": None,
                    },
                },
            )
        raise AssertionError(f"Unexpected MC API request: {request.method} {path}")

    settings = Settings(
        skin_email="student@smail.nju.edu.cn",
        skin_password="secret123",
        state_dir=tmp_path / ".state",
    )
    manager = BotManager(
        settings,
        mc_transport=httpx.MockTransport(mc_handler),
        skin_transport=httpx.MockTransport(skin_handler),
    )

    bot1 = manager.create_bot("NJU_Bot_02", poll_interval_s=0.01)
    try:
        assert bot1.created_on_skin_station is True
        assert bot1.signed_in_for_score is True
        assert bot1.reused_token is False
        assert bot1.bot_username == "NJU_Bot_02"
        assert mc_state["last_login_username"] == "NJU_Bot_02"
        assert bot1.status is not None and bot1.status["ready"] is True
        assert skin_state["create_calls"] == 1
        assert mc_state["login_calls"] == 1
    finally:
        bot1.session.close()

    # Check token file permissions (0o600)
    token_file = tmp_path / ".state" / "tokens.json"
    assert token_file.is_file()
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    # Calling create_bot / open_bot again must be idempotent on Skin Station AND reuse the cached token!
    bot2 = manager.create_bot("NJU_Bot_02", poll_interval_s=0.01)
    try:
        assert bot2.created_on_skin_station is False
        assert bot2.reused_token is True
        assert skin_state["create_calls"] == 1
        assert mc_state["login_calls"] == 1  # Did NOT re-login and invalidate previous token!
    finally:
        bot2.session.close()


def test_skin_station_safety_guards_captcha_and_unverified(tmp_path: pathlib.Path) -> None:
    """SkinStationClient stops immediately when CAPTCHA is required or email is unverified."""
    post_attempted = False

    def captcha_handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_attempted
        if request.method == "GET" and request.url.path == "/auth/login":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=_skin_html(extra={"tooManyFails": True, "recaptcha": ""}),
            )
        if request.method == "POST":
            post_attempted = True
        return httpx.Response(500)

    settings = Settings(
        skin_email="u@smail.nju.edu.cn",
        skin_password="pwd",
        state_dir=tmp_path / ".state",
    )
    mgr_captcha = BotManager(
        settings,
        skin_transport=httpx.MockTransport(captcha_handler),
    )
    with pytest.raises(CaptchaRequiredError):
        mgr_captcha.create_bot("Bot_A")
    assert post_attempted is False

    def unverified_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/auth/login":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=_skin_html(csrf="c1", extra={"tooManyFails": False}),
            )
        if request.method == "POST" and request.url.path == "/auth/login":
            return httpx.Response(200, json={"code": 0, "message": "ok"})
        if request.method == "GET" and request.url.path == "/user":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=_skin_html(csrf="c2", extra={"unverified": True}),
            )
        raise AssertionError(f"Unexpected {request.method} {request.url.path}")

    mgr_unverified = BotManager(
        settings,
        skin_transport=httpx.MockTransport(unverified_handler),
    )
    with pytest.raises(EmailUnverifiedError):
        mgr_unverified.create_bot("Bot_A")


def test_session_reauth_and_death_loop_cooldown(tmp_path: pathlib.Path) -> None:
    """BotSession refreshes invalidated token once, and surfaces death_loop_cooldown immediately."""
    state = {"token_gen": 0, "disconnect_state": True}

    def mc_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/auth/login":
            state["token_gen"] += 1
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "token": f"tok_{state['token_gen']}",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "bot_username": "RunnerBot",
                },
            )
        expected = f"Bearer tok_{state['token_gen']}"
        if request.headers.get("Authorization") != expected:
            return httpx.Response(
                401,
                json={"ok": False, "error": {"code": "invalid_token", "message": "expired"}},
            )
        if path == "/api/v1/inventory":
            return httpx.Response(
                200,
                json={"ok": True, "ms": 2, "result": {"items": [], "empty_slots": 36}},
            )
        if path == "/api/v1/status":
            if state["disconnect_state"]:
                return httpx.Response(
                    200,
                    json={"ok": False, "ms": 1, "error": {"code": "not_connected", "message": "offline"}},
                )
            return httpx.Response(200, json={"ok": True, "ms": 1, "result": {"ready": True}})
        if path == "/api/v1/connect":
            return httpx.Response(
                200,
                json={
                    "ok": False,
                    "ms": 1,
                    "error": {
                        "code": "death_loop_cooldown",
                        "message": "Cooling down",
                        "cooldown_remaining_seconds": 240,
                    },
                },
            )
        raise AssertionError(f"Unexpected {path}")

    settings = Settings(
        skin_email="u@smail.nju.edu.cn",
        skin_password="pwd",
        default_bot_username="RunnerBot",
        state_dir=tmp_path / ".state",
    )
    manager = BotManager(settings, mc_transport=httpx.MockTransport(mc_handler))
    provisioned = manager.open_bot("RunnerBot", wait_ready=False)
    try:
        assert provisioned.session.token == "tok_1"
        # Simulate external login invalidating tok_1 on server
        state["token_gen"] = 99
        inv = provisioned.session.inventory()
        assert inv["empty_slots"] == 36
        # Re-auth triggered tok_100
        assert provisioned.session.token == "tok_100"

        # Now wait_until_ready sees not_connected -> calls /connect -> gets death_loop_cooldown -> raises immediately
        with pytest.raises(DeathLoopCooldownError) as exc_info:
            provisioned.session.wait_until_ready(timeout_s=5.0, poll_interval_s=0.01)
        assert exc_info.value.cooldown_remaining_seconds == 240.0
    finally:
        provisioned.session.close()


def test_no_profile_error_includes_actionable_hint(tmp_path: pathlib.Path) -> None:
    def mc_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"ok": False, "error": {"code": "no_profile", "message": "No profile selected"}},
        )

    settings = Settings(
        skin_email="multi@smail.nju.edu.cn",
        skin_password="pwd",
        state_dir=tmp_path / ".state",
    )
    manager = BotManager(settings, mc_transport=httpx.MockTransport(mc_handler))
    with pytest.raises(AuthError) as exc_info:
        manager.open_bot("multi@smail.nju.edu.cn", wait_ready=False)
    assert exc_info.value.code == "no_profile"
    assert "mc-bot create" in exc_info.value.message


def test_upsert_dotenv_preserves_other_keys_and_sets_0600(tmp_path: pathlib.Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nMC_SKIN_EMAIL=\nNJU_API_KEY=keep_me\n", encoding="utf-8")
    _upsert_dotenv(
        env_file,
        {
            "MC_SKIN_EMAIL": "abc@smail.nju.edu.cn",
            "MC_SKIN_PASSWORD": 'p"w\\d#1',
            "MC_BOT_USERNAME": "Bot_One",
        },
    )
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    parsed = parse_dotenv(env_file)
    assert parsed["NJU_API_KEY"] == "keep_me"
    assert parsed["MC_SKIN_EMAIL"] == "abc@smail.nju.edu.cn"
    assert parsed["MC_BOT_USERNAME"] == "Bot_One"


def test_should_trust_env_direct_for_tailscale_and_lan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MC_TRUST_ENV", raising=False)
    monkeypatch.delenv("QWEN_TRUST_ENV", raising=False)

    assert should_trust_env("http://100.93.211.71:18900/api/v1") is False
    assert should_trust_env("http://10.50.248.70:18900/api/v1") is False
    assert should_trust_env("https://example.com") is True

    monkeypatch.setenv("MC_TRUST_ENV", "1")
    assert should_trust_env("http://100.93.211.71:18900/api/v1") is True
