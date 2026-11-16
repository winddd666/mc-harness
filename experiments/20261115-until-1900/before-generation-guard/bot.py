"""Bot session wrapper and end-to-end bot creation / connection manager."""

from __future__ import annotations

import dataclasses
import time
from typing import Any, Callable

import httpx

from .auth import AuthOutcome, BotTokenRecord, McAuthClient, TokenStore
from .config import DEFAULT_MC_API_BASE_URL, Settings, should_trust_env
from .errors import (
    ApiError,
    AuthError,
    BotNotReadyError,
    ConfigError,
    DeathLoopCooldownError,
)
from .skin_station import CreatePlayerResult, PlayerProfile, SkinStationClient, validate_player_name

MIN_RECONNECT_INTERVAL_S = 30.0


class BotSession:
    """Authenticated client for a single Minecraft bot (`agent-api-spec-dist.md` v1.3.7)."""

    def __init__(
        self,
        token: str,
        *,
        bot_username: str | None = None,
        base_url: str = DEFAULT_MC_API_BASE_URL,
        timeout: float = 45.0,
        reauth_callback: Callable[[], BotTokenRecord] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = token
        self.bot_username = bot_username
        self.base_url = base_url.rstrip("/")
        self._reauth_callback = reauth_callback
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=15.0),
            transport=transport,
            trust_env=should_trust_env(self.base_url),
        )
        self._last_disconnect_mono: float | None = None

    @property
    def token(self) -> str:
        return self._token

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BotSession":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def call(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        method: str = "POST",
        params: dict[str, Any] | None = None,
        allow_reauth: bool = True,
    ) -> dict[str, Any]:
        """Invoke an MC Agent API endpoint and unwrap `result` on `ok: true`."""
        rel_path = "/" + path.lstrip("/")
        upper_method = method.upper()

        for attempt in range(2):
            if upper_method == "GET":
                resp = self._client.get(rel_path, headers=self._headers(), params=params)
            else:
                resp = self._client.request(
                    upper_method,
                    rel_path,
                    headers=self._headers(),
                    params=params,
                    json={} if body is None else body,
                )

            try:
                payload = resp.json()
            except ValueError as exc:
                raise ApiError(
                    code="bad_json",
                    message=f"Invalid JSON from {rel_path} (HTTP {resp.status_code}): {resp.text[:200]}",
                    http_status=resp.status_code,
                ) from exc

            if not isinstance(payload, dict):
                raise ApiError(
                    code="bad_json",
                    message=f"Unexpected payload type {type(payload).__name__} from {rel_path}",
                    http_status=resp.status_code,
                )

            if resp.status_code == 200 and payload.get("ok") is True:
                result = payload.get("result")
                if isinstance(result, dict):
                    return result
                return {k: v for k, v in payload.items() if k not in ("ok", "ms")}

            err = ApiError.from_payload(
                payload, http_status=resp.status_code, headers=dict(resp.headers)
            )
            if (
                attempt == 0
                and allow_reauth
                and isinstance(err, AuthError)
                and self._reauth_callback is not None
            ):
                new_record = self._reauth_callback()
                self._token = new_record.token
                if new_record.bot_username:
                    self.bot_username = new_record.bot_username
                continue
            raise err

        raise RuntimeError("Unreachable")

    # ---- Connection & Lifecycle (§2) ----

    def connect(self, *, respect_reconnect_interval: bool = True) -> dict[str, Any]:
        """Connect the bot or trigger `/server survivaltest` switch if still in lobby."""
        if respect_reconnect_interval and self._last_disconnect_mono is not None:
            elapsed = time.monotonic() - self._last_disconnect_mono
            if elapsed < MIN_RECONNECT_INTERVAL_S:
                time.sleep(MIN_RECONNECT_INTERVAL_S - elapsed)
        res = self.call("connect", {})
        self._last_disconnect_mono = None
        return res

    def disconnect(self) -> dict[str, Any]:
        """Disconnect the bot from the server (`POST /disconnect`)."""
        res = self.call("disconnect", {})
        self._last_disconnect_mono = time.monotonic()
        return res

    # ---- Core Observations & Controls (§3 & §4.15) ----

    def status(self) -> dict[str, Any]:
        """Fetch live bot status (`GET /status`). Does not acquire action lock."""
        st = self.call("status", method="GET")
        if not self.bot_username and isinstance(st.get("username"), str):
            self.bot_username = st["username"]
        return st

    def inventory(self) -> dict[str, Any]:
        """Fetch inventory (`GET /inventory`). Does not acquire action lock."""
        return self.call("inventory", method="GET")

    def events(self, *, since: int = 0, limit: int = 50) -> dict[str, Any]:
        """Poll incremental event stream (`GET /events`)."""
        return self.call("events", method="GET", params={"since": since, "limit": limit})

    def advancements(
        self,
        *,
        status: str | None = None,
        include_recipes: bool = False,
    ) -> dict[str, Any]:
        """Query server-synced visible advancement tree (`POST /advancements`)."""
        body: dict[str, Any] = {"include_recipes": include_recipes}
        if status is not None:
            body["status"] = status
        return self.call("advancements", body)

    def stop(self) -> dict[str, Any]:
        """Interrupt active pathfinding/mining/item usage (`POST /stop`)."""
        return self.call("stop", {})

    def wait_until_ready(
        self,
        *,
        timeout_s: float = 45.0,
        poll_interval_s: float = 1.5,
        resend_connect_after_s: float = 8.0,
        initial_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Poll `/status` until `ready: true` on the competition server (`survivaltest`).

        Handles:
        - `not_connected`: invokes `POST /connect` once and continues polling.
        - `ready == False` while connected (still in proxy lobby): triggers `POST /connect`
          after `resend_connect_after_s` so the backend immediately resends `/server survivaltest`.
        - `death_loop_cooldown`: fails fast with `DeathLoopCooldownError` instead of hanging.
        """
        if initial_status is not None and initial_status.get("ready") is True:
            if not self.bot_username and isinstance(initial_status.get("username"), str):
                self.bot_username = initial_status["username"]
            return initial_status

        started = time.monotonic()
        deadline = started + timeout_s
        last_connect_call: float | None = None
        last_status: dict[str, Any] | None = initial_status

        while True:
            now = time.monotonic()
            try:
                st = self.status()
                last_status = st
                if st.get("ready") is True:
                    return st

                # Connected, but still in lobby or dead/respawning: nudge `/connect` periodically
                since_start = now - started
                since_last_connect = (
                    (now - last_connect_call) if last_connect_call is not None else float("inf")
                )
                if (
                    resend_connect_after_s >= 0
                    and since_start >= resend_connect_after_s
                    and since_last_connect >= max(resend_connect_after_s, 8.0)
                ):
                    self.connect(respect_reconnect_interval=False)
                    last_connect_call = time.monotonic()
            except DeathLoopCooldownError:
                raise
            except ApiError as err:
                if err.code == "not_connected":
                    since_last_connect = (
                        (now - last_connect_call)
                        if last_connect_call is not None
                        else float("inf")
                    )
                    if since_last_connect >= MIN_RECONNECT_INTERVAL_S or last_connect_call is None:
                        self.connect()
                        last_connect_call = time.monotonic()
                else:
                    raise

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BotNotReadyError(
                    f"等待 Bot {self.bot_username or '<unknown>'} 就绪超时（{timeout_s:.1f}s），"
                    f"最后状态 ready={last_status.get('ready') if last_status else None}",
                    last_status=last_status,
                )
            time.sleep(min(poll_interval_s, remaining))


@dataclasses.dataclass(slots=True)
class ProvisionedBot:
    bot_username: str
    token_record: BotTokenRecord
    reused_token: bool
    session: BotSession
    profile: PlayerProfile | None = None
    created_on_skin_station: bool = False
    signed_in_for_score: bool = False
    status: dict[str, Any] | None = None


class BotManager:
    """High-level coordinator for Skin Station profile creation + MC API auth + bot session."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        mc_transport: httpx.BaseTransport | None = None,
        skin_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings or Settings.load()
        self._mc_transport = mc_transport
        self._skin_transport = skin_transport
        state_dir = self.settings.ensure_state_dir()
        self.token_store = TokenStore(state_dir / "tokens.json")

    def make_skin_client(self) -> SkinStationClient:
        return SkinStationClient(
            base_url=self.settings.skin_base_url,
            transport=self._skin_transport,
        )

    def make_auth_client(self) -> McAuthClient:
        return McAuthClient(
            base_url=self.settings.mc_api_base_url,
            token_store=self.token_store,
            transport=self._mc_transport,
        )

    def create_bot(
        self,
        bot_name: str,
        *,
        skin_email: str | None = None,
        skin_password: str | None = None,
        auto_sign: bool = True,
        connect: bool = True,
        wait_ready: bool = True,
        ready_timeout_s: float = 45.0,
        poll_interval_s: float = 1.5,
        force_relogin: bool = False,
    ) -> ProvisionedBot:
        """Create (or verify existing) bot profile on Skin Station, authenticate, and connect.

        Solves the multi-bot `no_profile` caveat by logging into MC Agent API using
        `username=player.name` (supported via NMO Skin's `feature.non_email_login: true`).
        """
        clean_name = validate_player_name(bot_name)
        ident, pwd = self.settings.require_skin_credentials(
            identification=skin_email,
            password=skin_password,
        )

        with self.make_skin_client() as skin:
            skin.login(ident, pwd)
            create_res: CreatePlayerResult = skin.create_player(
                clean_name,
                auto_sign=auto_sign,
                idempotent=True,
            )

        canonical_name = create_res.player.name
        with self.make_auth_client() as auth:
            outcome: AuthOutcome = auth.authenticate(
                canonical_name,
                pwd,
                force_refresh=force_relogin,
            )

        def _reauth() -> BotTokenRecord:
            with self.make_auth_client() as refresh_auth:
                return refresh_auth.login(canonical_name, pwd)

        session = BotSession(
            outcome.record.token,
            bot_username=outcome.record.bot_username,
            base_url=self.settings.mc_api_base_url,
            reauth_callback=_reauth,
            transport=self._mc_transport,
        )

        status_data = outcome.probed_status
        try:
            if connect and wait_ready:
                status_data = session.wait_until_ready(
                    timeout_s=ready_timeout_s,
                    poll_interval_s=poll_interval_s,
                    initial_status=status_data,
                )
            elif connect:
                if status_data is None:
                    try:
                        status_data = session.status()
                    except ApiError as err:
                        if err.code == "not_connected":
                            session.connect()
                            status_data = session.status()
                        else:
                            raise
        except Exception:
            session.close()
            raise

        return ProvisionedBot(
            bot_username=outcome.record.bot_username,
            token_record=outcome.record,
            reused_token=outcome.reused_cached,
            session=session,
            profile=create_res.player,
            created_on_skin_station=create_res.created,
            signed_in_for_score=create_res.signed_in,
            status=status_data,
        )

    def open_bot(
        self,
        bot_name: str | None = None,
        *,
        password: str | None = None,
        wait_ready: bool = True,
        ready_timeout_s: float = 45.0,
        poll_interval_s: float = 1.5,
        force_relogin: bool = False,
    ) -> ProvisionedBot:
        """Open a session for an existing bot (reuses cached token first if valid)."""
        target = bot_name or self.settings.default_bot_username or self.settings.skin_email
        if not target:
            cached_all = self.token_store.list_all()
            if len(cached_all) == 1:
                target = cached_all[0].bot_username
            else:
                raise ConfigError(
                    "请指定 Bot 角色名，或在环境变量 / .env 中设置 MC_BOT_USERNAME / MC_SKIN_EMAIL。"
                )

        pwd = password or self.settings.skin_password
        with self.make_auth_client() as auth:
            outcome = auth.authenticate(
                target,
                pwd,
                force_refresh=force_relogin,
            )

        reauth_cb: Callable[[], BotTokenRecord] | None = None
        if pwd:
            login_user = outcome.record.bot_username

            def _reauth() -> BotTokenRecord:
                with self.make_auth_client() as refresh_auth:
                    return refresh_auth.login(login_user, pwd)

            reauth_cb = _reauth

        session = BotSession(
            outcome.record.token,
            bot_username=outcome.record.bot_username,
            base_url=self.settings.mc_api_base_url,
            reauth_callback=reauth_cb,
            transport=self._mc_transport,
        )

        status_data = outcome.probed_status
        try:
            if wait_ready:
                status_data = session.wait_until_ready(
                    timeout_s=ready_timeout_s,
                    poll_interval_s=poll_interval_s,
                    initial_status=status_data,
                )
        except Exception:
            session.close()
            raise

        return ProvisionedBot(
            bot_username=outcome.record.bot_username,
            token_record=outcome.record,
            reused_token=outcome.reused_cached,
            session=session,
            status=status_data,
        )
