"""Minecraft Agent API v1.3 authentication, skin-station bot provisioning, and session client."""

from .auth import AuthOutcome, BotTokenRecord, McAuthClient, TokenStore, mask_secret
from .bot import BotManager, BotSession, ProvisionedBot
from .config import Settings
from .errors import (
    ApiError,
    AuthError,
    BotNotReadyError,
    CaptchaRequiredError,
    ConfigError,
    DeathLoopCooldownError,
    EmailUnverifiedError,
    McAgentError,
    RateLimitError,
    SkinStationError,
)
from .skin_station import (
    CreatePlayerResult,
    PlayerProfile,
    ScoreInfo,
    SkinStationClient,
    validate_player_name,
)

__all__ = [
    "ApiError",
    "AuthError",
    "AuthOutcome",
    "BotManager",
    "BotNotReadyError",
    "BotSession",
    "BotTokenRecord",
    "CaptchaRequiredError",
    "ConfigError",
    "CreatePlayerResult",
    "DeathLoopCooldownError",
    "EmailUnverifiedError",
    "McAgentError",
    "McAuthClient",
    "PlayerProfile",
    "ProvisionedBot",
    "RateLimitError",
    "ScoreInfo",
    "Settings",
    "SkinStationClient",
    "SkinStationError",
    "TokenStore",
    "mask_secret",
    "validate_player_name",
]
