"""NMO Skin Station (Blessing Skin Server 6.0.2 + Yggdrasil) client.

Supports:
- Session login via CSRF + cookies (refuses to proceed if CAPTCHA threshold is active)
- Checking email verification status, score balance, and daily sign-in
- Listing, creating (provisioning new bots), renaming, and deleting player profiles
- Inspecting Yggdrasil profiles (`non_email_login` compatibility check)
"""

from __future__ import annotations

import dataclasses
import html
import json
import re
from typing import Any

import httpx

from .config import DEFAULT_SKIN_BASE_URL
from .errors import CaptchaRequiredError, EmailUnverifiedError, SkinStationError

_CSRF_RE = re.compile(
    r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_EXTRA_RE = re.compile(
    r'<script\s+id=["\']blessing-extra["\']\s+type=["\']application/json["\']>\s*(.*?)\s*</script>',
    re.IGNORECASE | re.DOTALL,
)
_MC_PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


@dataclasses.dataclass(frozen=True, slots=True)
class PlayerProfile:
    pid: int
    name: str
    uid: int | None = None
    tid_skin: int = 0
    tid_cape: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlayerProfile":
        return cls(
            pid=int(data["pid"]),
            name=str(data["name"]),
            uid=int(data["uid"]) if data.get("uid") is not None else None,
            tid_skin=int(data.get("tid_skin") or 0),
            tid_cape=int(data.get("tid_cape") or 0),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class ScoreInfo:
    score: int
    cost_per_player: int
    player_count: int
    last_sign_at: str | None = None
    sign_after_zero: bool = False
    sign_gap_hours: int = 24

    @property
    def available_new_players(self) -> int | None:
        if self.cost_per_player <= 0:
            return None
        return self.score // self.cost_per_player


@dataclasses.dataclass(frozen=True, slots=True)
class CreatePlayerResult:
    player: PlayerProfile
    created: bool
    signed_in: bool = False
    score_after: int | None = None


def validate_player_name(name: str) -> str:
    """Validate standard Minecraft Java username rules (3-16 chars, [A-Za-z0-9_])."""
    cleaned = name.strip()
    if not _MC_PLAYER_NAME_RE.match(cleaned):
        raise ValueError(
            f"非法角色名 {name!r}：Minecraft 角色名需为 3–16 位字母、数字或下划线 [A-Za-z0-9_]。"
        )
    return cleaned


def _extract_csrf_and_extra(page_html: str) -> tuple[str | None, dict[str, Any]]:
    csrf_match = _CSRF_RE.search(page_html)
    csrf = html.unescape(csrf_match.group(1)) if csrf_match else None
    extra: dict[str, Any] = {}
    extra_match = _EXTRA_RE.search(page_html)
    if extra_match:
        try:
            parsed = json.loads(extra_match.group(1))
            if isinstance(parsed, dict):
                extra = parsed
        except json.JSONDecodeError:
            pass
    return csrf, extra


class SkinStationClient:
    """Client for NMO Skin Station (https://skin.nmo.net.cn)."""

    def __init__(
        self,
        base_url: str = DEFAULT_SKIN_BASE_URL,
        *,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            headers={
                "User-Agent": "mc-agent/0.1 (+https://skin.nmo.net.cn)",
            },
        )
        self._csrf_token: str | None = None
        self._logged_in_as: str | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SkinStationClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _json_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self._csrf_token:
            headers["X-CSRF-TOKEN"] = self._csrf_token
        return headers

    def _refresh_page_state(self, path: str) -> dict[str, Any]:
        resp = self._client.get(path, headers={"Accept": "text/html"})
        if resp.status_code >= 400:
            raise SkinStationError(
                f"访问皮肤站页面 {path} 失败 (HTTP {resp.status_code})",
                http_status=resp.status_code,
            )
        csrf, extra = _extract_csrf_and_extra(resp.text)
        if csrf:
            self._csrf_token = csrf
        return extra

    @staticmethod
    def _parse_json_response(resp: httpx.Response, action: str) -> Any:
        try:
            payload = resp.json()
        except ValueError as exc:
            raise SkinStationError(
                f"皮肤站 {action} 返回非 JSON 响应 (HTTP {resp.status_code})",
                http_status=resp.status_code,
            ) from exc

        if resp.status_code == 422 and isinstance(payload, dict):
            errors = payload.get("errors") or {}
            if "captcha" in errors:
                raise CaptchaRequiredError(
                    f"皮肤站 {action} 要求完成验证码，已停止自动请求；请在浏览器中手动登录后再试。",
                    code="captcha_required",
                    http_status=422,
                    details=payload,
                )
            flat_msgs: list[str] = []
            if isinstance(errors, dict):
                for field_msgs in errors.values():
                    if isinstance(field_msgs, list):
                        flat_msgs.extend(str(m) for m in field_msgs)
                    else:
                        flat_msgs.append(str(field_msgs))
            msg = "；".join(flat_msgs) or str(payload.get("message") or "参数校验失败")
            raise SkinStationError(
                f"皮肤站 {action} 校验失败：{msg}",
                code="validation_error",
                http_status=422,
                details=payload,
            )

        if resp.status_code == 401 or resp.status_code == 419:
            raise SkinStationError(
                f"皮肤站会话未认证或 CSRF 已过期 (HTTP {resp.status_code})",
                http_status=resp.status_code,
                details=payload if isinstance(payload, dict) else {},
            )

        if resp.status_code == 403:
            msg = (
                payload.get("message")
                if isinstance(payload, dict)
                else f"HTTP 403 ({action})"
            )
            raise SkinStationError(
                f"皮肤站拒绝访问 ({action})：{msg}（请确认账号已完成南大邮箱验证）",
                code="forbidden",
                http_status=403,
                details=payload if isinstance(payload, dict) else {},
            )

        if resp.status_code >= 400:
            raise SkinStationError(
                f"皮肤站 {action} 请求失败 (HTTP {resp.status_code})",
                http_status=resp.status_code,
                details=payload if isinstance(payload, dict) else {},
            )

        return payload

    def login(self, identification: str, password: str, *, keep: bool = True) -> None:
        """Log in to Blessing Skin web session using email or existing player name."""
        extra = self._refresh_page_state("/auth/login")
        if extra.get("tooManyFails") or extra.get("recaptcha"):
            raise CaptchaRequiredError(
                "皮肤站当前要求输入验证码（连续登录失败超过 3 次或开启了站点验证码），"
                "为遵守安全边界已停止自动登录；请在浏览器打开 https://skin.nmo.net.cn/auth/login 完成一次登录。",
                code="captcha_required",
                details=extra,
            )
        if not self._csrf_token:
            raise SkinStationError("未能从皮肤站 /auth/login 获取 CSRF Token")

        resp = self._client.post(
            "/auth/login",
            headers=self._json_headers(),
            json={
                "identification": identification,
                "password": password,
                "keep": keep,
            },
        )
        payload = self._parse_json_response(resp, "登录 (/auth/login)")
        if not isinstance(payload, dict) or payload.get("code") != 0:
            code = payload.get("code") if isinstance(payload, dict) else None
            msg = (
                payload.get("message")
                if isinstance(payload, dict)
                else str(payload)
            )
            data = payload.get("data") if isinstance(payload, dict) else {}
            raise SkinStationError(
                f"皮肤站登录失败：{msg}",
                code=code,
                http_status=resp.status_code,
                details=data if isinstance(data, dict) else {},
            )

        # Refresh CSRF token in authenticated user session and check verification status
        user_extra = self._refresh_page_state("/user")
        if user_extra.get("unverified"):
            raise EmailUnverifiedError(
                "皮肤站账号尚未完成邮箱验证，无法管理/新建 Bot 角色；请前往南大邮箱点击验证链接。",
                code="email_unverified",
                details=user_extra,
            )
        self._logged_in_as = identification

    def ensure_logged_in(self, identification: str, password: str) -> None:
        if self._logged_in_as != identification or not self._csrf_token:
            self.login(identification, password)

    def get_score_info(self) -> ScoreInfo:
        """Query current user score and player creation cost."""
        resp = self._client.get("/user/score-info", headers=self._json_headers())
        data = self._parse_json_response(resp, "查询积分 (/user/score-info)")
        if not isinstance(data, dict) or "user" not in data:
            raise SkinStationError("皮肤站 /user/score-info 返回格式异常", details={"raw": data})
        user = data.get("user") or {}
        rate = data.get("rate") or {}
        usage = data.get("usage") or {}
        return ScoreInfo(
            score=int(user.get("score") or 0),
            cost_per_player=int(rate.get("players") or 0),
            player_count=int(usage.get("players") or 0),
            last_sign_at=user.get("lastSignAt"),
            sign_after_zero=bool(data.get("signAfterZero")),
            sign_gap_hours=int(data.get("signGapTime") or 24),
        )

    def sign_in(self) -> tuple[bool, int | None, str]:
        """Attempt daily sign-in to earn score.

        Returns `(signed, new_score, message)`. If already signed today (`code == 1`),
        returns `(False, None, message)` without raising.
        """
        resp = self._client.post("/user/sign", headers=self._json_headers(), json={})
        payload = self._parse_json_response(resp, "每日签到 (/user/sign)")
        if not isinstance(payload, dict):
            raise SkinStationError("皮肤站签到返回格式异常")
        code = payload.get("code")
        msg = str(payload.get("message") or "")
        if code == 0:
            new_score = None
            if isinstance(payload.get("data"), dict) and "score" in payload["data"]:
                new_score = int(payload["data"]["score"])
            return True, new_score, msg
        if code == 1:
            return False, None, msg or "今日已签到或未到签到冷却时间"
        raise SkinStationError(f"皮肤站签到失败：{msg}", code=code, details=payload)

    def list_players(self) -> list[PlayerProfile]:
        """List all Minecraft player profiles owned by the logged-in user."""
        resp = self._client.get("/user/player/list", headers=self._json_headers())
        # If email is unverified, Blessing's 'verified' middleware may redirect to /user (HTML)
        content_type = resp.headers.get("content-type", "")
        if "text/html" in content_type:
            raise EmailUnverifiedError(
                "访问 /user/player/list 被重定向，请确认皮肤站账号已完成南大邮箱验证。",
                code="email_unverified",
            )
        payload = self._parse_json_response(resp, "获取角色列表 (/user/player/list)")
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            items = payload["data"]
        elif isinstance(payload, list):
            items = payload
        else:
            raise SkinStationError("皮肤站角色列表返回格式异常", details={"raw": payload})
        return [PlayerProfile.from_dict(item) for item in items]

    def create_player(
        self,
        name: str,
        *,
        auto_sign: bool = True,
        idempotent: bool = True,
    ) -> CreatePlayerResult:
        """Create a new bot player profile under the current Skin Station account."""
        clean_name = validate_player_name(name)

        if idempotent:
            existing = self.list_players()
            for player in existing:
                if player.name.lower() == clean_name.lower():
                    return CreatePlayerResult(player=player, created=False)

        score_info = self.get_score_info()
        signed_in = False
        current_score = score_info.score
        if score_info.cost_per_player > 0 and current_score < score_info.cost_per_player:
            if auto_sign:
                did_sign, updated_score, _ = self.sign_in()
                signed_in = did_sign
                if updated_score is not None:
                    current_score = updated_score
            if current_score < score_info.cost_per_player:
                raise SkinStationError(
                    f"皮肤站积分不足：新建角色需要 {score_info.cost_per_player} 积分，"
                    f"当前仅有 {current_score} 积分。",
                    code=7,
                )

        resp = self._client.post(
            "/user/player",
            headers=self._json_headers(),
            json={"name": clean_name},
        )
        payload = self._parse_json_response(resp, f"新建角色 ({clean_name})")
        if not isinstance(payload, dict) or payload.get("code") != 0:
            code = payload.get("code") if isinstance(payload, dict) else None
            msg = (
                payload.get("message")
                if isinstance(payload, dict)
                else str(payload)
            )
            raise SkinStationError(
                f"新建角色 {clean_name!r} 失败：{msg}",
                code=code,
                http_status=resp.status_code,
                details=payload if isinstance(payload, dict) else {},
            )

        data = payload.get("data")
        if isinstance(data, dict) and "pid" in data and "name" in data:
            player = PlayerProfile.from_dict(data)
        else:
            # Fallback: re-fetch player list
            matched = [
                p for p in self.list_players() if p.name.lower() == clean_name.lower()
            ]
            if not matched:
                raise SkinStationError(f"角色 {clean_name!r} 创建返回成功但未在列表中找到")
            player = matched[0]

        after_score = (
            current_score - score_info.cost_per_player
            if score_info.cost_per_player >= 0
            else None
        )
        return CreatePlayerResult(
            player=player,
            created=True,
            signed_in=signed_in,
            score_after=after_score,
        )

    def rename_player(self, pid: int, new_name: str) -> PlayerProfile:
        """Rename an existing player profile."""
        clean_name = validate_player_name(new_name)
        resp = self._client.put(
            f"/user/player/{pid}/name",
            headers=self._json_headers(),
            json={"name": clean_name},
        )
        payload = self._parse_json_response(resp, f"重命名角色 ({pid} -> {clean_name})")
        if not isinstance(payload, dict) or payload.get("code") != 0:
            msg = payload.get("message") if isinstance(payload, dict) else str(payload)
            raise SkinStationError(f"重命名角色失败：{msg}", details={"raw": payload})
        return PlayerProfile.from_dict(payload["data"])

    def delete_player(self, pid: int) -> None:
        """Delete a player profile by pid."""
        resp = self._client.delete(
            f"/user/player/{pid}",
            headers=self._json_headers(),
        )
        payload = self._parse_json_response(resp, f"删除角色 ({pid})")
        if not isinstance(payload, dict) or payload.get("code") != 0:
            msg = payload.get("message") if isinstance(payload, dict) else str(payload)
            raise SkinStationError(f"删除角色失败：{msg}", details={"raw": payload})

    def yggdrasil_inspect_profiles(
        self, username: str, password: str
    ) -> dict[str, Any]:
        """Inspect Yggdrasil authenticate profiles (diagnoses multi-profile `no_profile`).

        Note: Performs a Yggdrasil authenticate against Skin Station directly (does NOT touch
        mc-api token state), then invalidates the temporary Yggdrasil token immediately.
        """
        resp = self._client.post(
            "/api/yggdrasil/authserver/authenticate",
            headers={"Accept": "application/json"},
            json={"username": username, "password": password},
        )
        if resp.status_code >= 400:
            try:
                err = resp.json()
            except ValueError:
                err = {"errorMessage": resp.text[:300]}
            raise SkinStationError(
                f"Yggdrasil 认证检查失败：{err.get('errorMessage') or err}",
                code=err.get("error"),
                http_status=resp.status_code,
                details=err if isinstance(err, dict) else {},
            )
        data = resp.json()
        access_token = data.get("accessToken")
        client_token = data.get("clientToken")
        if access_token and client_token:
            try:
                self._client.post(
                    "/api/yggdrasil/authserver/invalidate",
                    json={"accessToken": access_token, "clientToken": client_token},
                )
            except httpx.HTTPError:
                pass
        return {
            "selected_profile": data.get("selectedProfile"),
            "available_profiles": data.get("availableProfiles") or [],
        }
