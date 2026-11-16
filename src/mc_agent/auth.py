"""Authentication and persistent Bearer token management for Minecraft Agent API v1.3."""

from __future__ import annotations

import dataclasses
import datetime
import json
import os
import pathlib
import tempfile
import threading
import time
from typing import Any

import httpx

from .config import DEFAULT_MC_API_BASE_URL, should_trust_env
from .errors import ApiError, AuthError, RateLimitError


def mask_secret(secret: str | None, keep: int = 8) -> str:
    if not secret:
        return "<empty>"
    if len(secret) <= keep:
        return "*" * len(secret)
    return f"{secret[:keep]}...({len(secret)} chars)"


def _parse_iso_utc(ts: str | None) -> datetime.datetime | None:
    if not ts:
        return None
    normalized = ts.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


@dataclasses.dataclass(frozen=True, slots=True)
class BotTokenRecord:
    bot_username: str
    token: str = dataclasses.field(repr=False)
    expires_at: str | None = None
    login_username: str | None = None
    obtained_at: str = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )

    def is_expired(self, *, leeway_seconds: float = 60.0, now: datetime.datetime | None = None) -> bool:
        exp = _parse_iso_utc(self.expires_at)
        if exp is None:
            return False
        ref = now or datetime.datetime.now(datetime.timezone.utc)
        return ref + datetime.timedelta(seconds=leeway_seconds) >= exp

    def to_dict(self) -> dict[str, Any]:
        return {
            "bot_username": self.bot_username,
            "token": self.token,
            "expires_at": self.expires_at,
            "login_username": self.login_username,
            "obtained_at": self.obtained_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BotTokenRecord":
        return cls(
            bot_username=str(data["bot_username"]),
            token=str(data["token"]),
            expires_at=data.get("expires_at"),
            login_username=data.get("login_username"),
            obtained_at=str(
                data.get("obtained_at")
                or datetime.datetime.now(datetime.timezone.utc).isoformat()
            ),
        )


class TokenStore:
    """Thread-safe local token cache stored with 0o600 file permissions."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _read_all_unlocked(self) -> dict[str, BotTokenRecord]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        tokens = raw.get("tokens") if isinstance(raw, dict) else None
        if not isinstance(tokens, dict):
            return {}
        records: dict[str, BotTokenRecord] = {}
        for key, item in tokens.items():
            if isinstance(item, dict) and "bot_username" in item and "token" in item:
                records[key.lower()] = BotTokenRecord.from_dict(item)
        return records

    def _write_all_unlocked(self, records: dict[str, BotTokenRecord]) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            parent.chmod(0o700)
        except OSError:
            pass

        payload = {
            "version": 1,
            "tokens": {k: rec.to_dict() for k, rec in sorted(records.items())},
        }
        fd, tmp_name = tempfile.mkstemp(prefix=".tokens-", suffix=".tmp", dir=str(parent))
        tmp_path = pathlib.Path(tmp_name)
        try:
            os.chmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            tmp_path.replace(self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    def get(self, identifier: str) -> BotTokenRecord | None:
        """Look up token by bot_username or login_username (case-insensitive)."""
        needle = identifier.strip().lower()
        if not needle:
            return None
        with self._lock:
            records = self._read_all_unlocked()
            if needle in records:
                return records[needle]
            matches = [
                rec
                for rec in records.values()
                if (rec.login_username or "").lower() == needle
            ]
            # Only return by login_username when unambiguous (single profile cached)
            if len(matches) == 1:
                return matches[0]
            return None

    def list_all(self) -> list[BotTokenRecord]:
        with self._lock:
            return list(self._read_all_unlocked().values())

    def put(self, record: BotTokenRecord) -> None:
        with self._lock:
            records = self._read_all_unlocked()
            records[record.bot_username.lower()] = record
            self._write_all_unlocked(records)

    def remove(self, identifier: str) -> bool:
        needle = identifier.strip().lower()
        with self._lock:
            records = self._read_all_unlocked()
            removed = False
            if needle in records:
                del records[needle]
                removed = True
            else:
                to_delete = [
                    k
                    for k, rec in records.items()
                    if (rec.login_username or "").lower() == needle
                    or rec.token == identifier
                ]
                for k in to_delete:
                    del records[k]
                    removed = True
            if removed:
                self._write_all_unlocked(records)
            return removed


@dataclasses.dataclass(frozen=True, slots=True)
class AuthOutcome:
    record: BotTokenRecord
    reused_cached: bool
    probed_status: dict[str, Any] | None = None


class McAuthClient:
    """Handles `/auth/login`, `/auth/logout`, and safe cached-token reuse."""

    def __init__(
        self,
        base_url: str = DEFAULT_MC_API_BASE_URL,
        *,
        token_store: TokenStore | None = None,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_store = token_store
        self._transport = transport
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            trust_env=should_trust_env(self.base_url),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "McAuthClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def _decode_response(resp: httpx.Response) -> dict[str, Any]:
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ApiError(
                code="bad_json",
                message=f"Server returned non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}",
                http_status=resp.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise ApiError(
                code="bad_json",
                message=f"Unexpected JSON type {type(payload).__name__}",
                http_status=resp.status_code,
            )
        return payload

    def probe_token(self, token: str) -> tuple[bool, dict[str, Any] | None]:
        """Verify whether `token` is still accepted by `GET /status`.

        Returns `(valid, status_result_or_none)`:
        - `(True, result_dict)` if `/status` returned `ok: true`.
        - `(True, None)` if `/status` returned a business error unrelated to auth
          (e.g., `not_connected`), proving the token itself is still valid.
        - `(False, None)` if HTTP 401 or `unauthorized` / `invalid_token`.
        """
        resp = self._client.get(
            "/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        payload = self._decode_response(resp)
        if resp.status_code == 401:
            return False, None
        if payload.get("ok") is True:
            result = payload.get("result")
            return True, result if isinstance(result, dict) else None
        err = ApiError.from_payload(
            payload, http_status=resp.status_code, headers=dict(resp.headers)
        )
        if isinstance(err, AuthError):
            return False, None
        return True, None

    def login(
        self,
        username: str,
        password: str,
        *,
        max_rate_limit_retries: int = 1,
        max_retry_wait_s: float = 10.0,
    ) -> BotTokenRecord:
        """Exchange Skin Station credentials for an MC Agent API Bearer token.

        Note: `username` can be either the Skin Station email (works only when the
        account owns exactly 1 profile) or the exact Minecraft `bot_username`
        (works even when a single Skin Station account owns multiple bot profiles).
        """
        attempts = 0
        while True:
            attempts += 1
            resp = self._client.post(
                "/auth/login",
                json={"username": username, "password": password},
            )
            payload = self._decode_response(resp)
            if resp.status_code == 200 and payload.get("ok") is True:
                body = (
                    payload["result"]
                    if isinstance(payload.get("result"), dict) and "token" in payload["result"]
                    else payload
                )
                token = body.get("token")
                bot_username = body.get("bot_username") or username
                if not token or not isinstance(token, str):
                    raise ApiError(
                        code="invalid_login_response",
                        message="登录响应缺少 token 字段",
                        http_status=resp.status_code,
                    )
                record = BotTokenRecord(
                    bot_username=str(bot_username),
                    token=token,
                    expires_at=body.get("expires_at"),
                    login_username=username,
                )
                if self.token_store is not None:
                    self.token_store.put(record)
                return record

            err = ApiError.from_payload(
                payload, http_status=resp.status_code, headers=dict(resp.headers)
            )
            if err.code == "no_profile":
                hint = (
                    f"{err.message} —— 若该皮肤站账号尚未创建角色，请用 `mc-bot create <角色名>` 新建；"
                    "若账号下已有多个角色，请将登录 username 指定为具体的角色名（而非邮箱）。"
                )
                raise AuthError(
                    code=err.code,
                    message=hint,
                    http_status=err.http_status,
                    ms=err.ms,
                    context=err.context,
                )

            if (
                isinstance(err, RateLimitError)
                and attempts <= max_rate_limit_retries
            ):
                wait_s = err.retry_after_s if err.retry_after_s is not None else 3.0
                if 0 <= wait_s <= max_retry_wait_s:
                    time.sleep(wait_s)
                    continue
            raise err

    def authenticate(
        self,
        username: str,
        password: str | None = None,
        *,
        force_refresh: bool = False,
    ) -> AuthOutcome:
        """Obtain a valid token, reusing a cached unexpired token when accepted by server.

        Important: Re-logging in for the same bot immediately invalidates any old token
        on the server (`agent-api-spec-dist.md` §1.1), so reusing a verified cached token
        prevents kicking active sessions and avoids `/auth/login` rate limits.
        """
        if not force_refresh and self.token_store is not None:
            cached = self.token_store.get(username)
            if cached is not None and not cached.is_expired():
                valid, status_data = self.probe_token(cached.token)
                if valid:
                    return AuthOutcome(
                        record=cached,
                        reused_cached=True,
                        probed_status=status_data,
                    )
                self.token_store.remove(cached.bot_username)

        if not password:
            raise AuthError(
                code="missing_password",
                message=f"本地没有 {username!r} 的有效缓存 Token，且未提供密码以重新登录。",
                http_status=401,
            )

        record = self.login(username, password)
        return AuthOutcome(record=record, reused_cached=False, probed_status=None)

    def logout(
        self,
        identifier: str | None = None,
        *,
        token: str | None = None,
    ) -> bool:
        """Invalidate a Bearer token on the server (`POST /auth/logout`) and purge local cache."""
        resolved_token = token
        record: BotTokenRecord | None = None
        if resolved_token is None and identifier and self.token_store is not None:
            record = self.token_store.get(identifier)
            if record is not None:
                resolved_token = record.token

        if not resolved_token:
            return False

        try:
            resp = self._client.post(
                "/auth/logout",
                headers={"Authorization": f"Bearer {resolved_token}"},
                json={},
            )
            if resp.status_code not in (200, 401):
                payload = self._decode_response(resp)
                if not payload.get("ok"):
                    raise ApiError.from_payload(
                        payload, http_status=resp.status_code, headers=dict(resp.headers)
                    )
        finally:
            if self.token_store is not None:
                if record is not None:
                    self.token_store.remove(record.bot_username)
                elif identifier:
                    self.token_store.remove(identifier)
                else:
                    self.token_store.remove(resolved_token)
        return True
