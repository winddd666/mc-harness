"""Structured exceptions for MC Agent API and NMO Skin Station."""

from __future__ import annotations

from typing import Any


class McAgentError(Exception):
    """Base exception for mc_agent."""


class ConfigError(McAgentError):
    """Missing or invalid local configuration."""


class SkinStationError(McAgentError):
    """Error returned by or encountered when talking to NMO Skin Station."""

    def __init__(
        self,
        message: str,
        *,
        code: int | str | None = None,
        http_status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.details = details or {}


class CaptchaRequiredError(SkinStationError):
    """Raised when an action on Skin Station requires human CAPTCHA completion."""


class EmailUnverifiedError(SkinStationError):
    """Raised when the Skin Station account has not completed email verification."""


class ApiError(McAgentError):
    """Error returned by Minecraft Agent API v1.3."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 200,
        ms: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.http_status = http_status
        self.ms = ms
        self.context = context or {}

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        http_status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> "ApiError":
        err = payload.get("error")
        if not isinstance(err, dict):
            err = {"code": "unknown_error", "message": str(err or payload)}
        code = str(err.get("code") or "unknown_error")
        message = str(err.get("message") or code)
        context = {k: v for k, v in err.items() if k not in ("code", "message")}
        ms = payload.get("ms")

        retry_after_header = None
        if headers:
            for k, v in headers.items():
                if k.lower() == "retry-after":
                    try:
                        retry_after_header = float(v)
                    except ValueError:
                        pass

        if http_status == 429 or code == "rate_limited":
            retry_after_s = context.get("retry_after_s")
            if retry_after_s is None:
                retry_after_s = retry_after_header
            return RateLimitError(
                code=code,
                message=message,
                http_status=http_status,
                ms=ms,
                context=context,
                retry_after_s=float(retry_after_s) if retry_after_s is not None else None,
            )

        if http_status == 401 or code in (
            "unauthorized",
            "invalid_token",
            "invalid_credentials",
            "no_profile",
        ):
            return AuthError(
                code=code,
                message=message,
                http_status=http_status,
                ms=ms,
                context=context,
            )

        if code == "death_loop_cooldown":
            cooldown = context.get("cooldown_remaining_seconds")
            return DeathLoopCooldownError(
                code=code,
                message=message,
                http_status=http_status,
                ms=ms,
                context=context,
                cooldown_remaining_seconds=float(cooldown) if cooldown is not None else None,
            )

        return cls(
            code=code,
            message=message,
            http_status=http_status,
            ms=ms,
            context=context,
        )


class AuthError(ApiError):
    """Authentication or token error (401, invalid_credentials, no_profile, invalid_token)."""


class RateLimitError(ApiError):
    """Rate limit error (429 or rate_limited)."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 429,
        ms: int | None = None,
        context: dict[str, Any] | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            http_status=http_status,
            ms=ms,
            context=context,
        )
        self.retry_after_s = retry_after_s


class DeathLoopCooldownError(ApiError):
    """Bot is in 5-minute death loop protection cooldown."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 200,
        ms: int | None = None,
        context: dict[str, Any] | None = None,
        cooldown_remaining_seconds: float | None = None,
    ) -> None:
        super().__init__(
            code=code,
            message=message,
            http_status=http_status,
            ms=ms,
            context=context,
        )
        self.cooldown_remaining_seconds = cooldown_remaining_seconds


class BotNotReadyError(McAgentError):
    """Raised when a bot fails to reach status.ready=True within the timeout."""

    def __init__(self, message: str, *, last_status: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.last_status = last_status
