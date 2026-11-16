"""Configuration loader with safe .env parsing and restricted state directory permissions."""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import pathlib
from typing import Mapping
from urllib.parse import urlparse

from .errors import ConfigError

DEFAULT_MC_API_BASE_URL = "https://apiin.nmo.net.cn/api/v1"
PUBLIC_MC_API_BASE_URL = "https://mc-api.windywindie.top/api/v1"
DEFAULT_SKIN_BASE_URL = "https://skin.nmo.net.cn"
DEFAULT_NJU_BASE_URL = "https://api.nju-aia.com/v1"

_DIRECT_SUFFIXES = (
    ".cn",
    ".local",
    "localhost",
    "nju-aia.com",
    "aliyuncs.com",
    "windywindie.top",
    "deepseek.com",
)


_TAILSCALE_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def should_trust_env(url: str) -> bool:
    """Return False for LAN / Tailscale CGNAT / Radmin VPN / domestic competition endpoints so local proxies don't hijack or stall them."""
    if os.environ.get("MC_TRUST_ENV") == "1" or os.environ.get("QWEN_TRUST_ENV") == "1":
        return True
    host = (urlparse(url).hostname or "").strip().lower()
    if not host:
        return True
    try:
        ip = ipaddress.ip_address(host)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip in _TAILSCALE_CGNAT_NET
            or host.startswith("26.")
        ):
            return False
    except ValueError:
        if any(host == s.lstrip(".") or host.endswith(s) for s in _DIRECT_SUFFIXES):
            return False
    return True


def parse_dotenv(path: pathlib.Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file without mutating os.environ."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        elif " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        if key:
            values[key] = val
    return values


@dataclasses.dataclass(slots=True)
class Settings:
    mc_api_base_url: str = DEFAULT_MC_API_BASE_URL
    skin_base_url: str = DEFAULT_SKIN_BASE_URL
    skin_email: str | None = None
    skin_password: str | None = None
    default_bot_username: str | None = None
    state_dir: pathlib.Path = dataclasses.field(
        default_factory=lambda: pathlib.Path(".state")
    )
    nju_base_url: str = DEFAULT_NJU_BASE_URL
    nju_model: str = "default"
    nju_api_key: str | None = None
    nju_rpm_budget: int = 24

    @classmethod
    def load(
        cls,
        *,
        dotenv_path: pathlib.Path | str | None = None,
        environ: Mapping[str, str] | None = None,
        project_root: pathlib.Path | None = None,
    ) -> "Settings":
        env = dict(os.environ if environ is None else environ)
        root = project_root or pathlib.Path.cwd()
        env_file = (
            pathlib.Path(dotenv_path)
            if dotenv_path is not None
            else root / ".env"
        )
        file_vars = parse_dotenv(env_file)

        def get_opt(name: str, default: str | None = None) -> str | None:
            val = env.get(name)
            if val is None or val == "":
                val = file_vars.get(name)
            if val is None or val == "":
                return default
            return val

        state_raw = get_opt("MC_STATE_DIR", ".state") or ".state"
        state_path = pathlib.Path(state_raw)
        if not state_path.is_absolute():
            state_path = root / state_path

        rpm_str = get_opt("NJU_RPM_BUDGET", "24") or "24"
        try:
            rpm_budget = int(rpm_str)
        except ValueError as exc:
            raise ConfigError(f"Invalid NJU_RPM_BUDGET: {rpm_str!r}") from exc

        return cls(
            mc_api_base_url=(
                get_opt("MC_API_BASE_URL", DEFAULT_MC_API_BASE_URL)
                or DEFAULT_MC_API_BASE_URL
            ).rstrip("/"),
            skin_base_url=(
                get_opt("MC_SKIN_BASE_URL", DEFAULT_SKIN_BASE_URL)
                or DEFAULT_SKIN_BASE_URL
            ).rstrip("/"),
            skin_email=get_opt("MC_SKIN_EMAIL"),
            skin_password=get_opt("MC_SKIN_PASSWORD"),
            default_bot_username=get_opt("MC_BOT_USERNAME"),
            state_dir=state_path,
            nju_base_url=(
                get_opt("NJU_BASE_URL", DEFAULT_NJU_BASE_URL)
                or DEFAULT_NJU_BASE_URL
            ).rstrip("/"),
            nju_model=get_opt("NJU_MODEL", "default") or "default",
            nju_api_key=get_opt("NJU_API_KEY"),
            nju_rpm_budget=rpm_budget,
        )

    def ensure_state_dir(self) -> pathlib.Path:
        """Create state directory with 0o700 permissions."""
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.state_dir.chmod(0o700)
        except OSError:
            pass
        return self.state_dir

    def require_skin_credentials(
        self,
        *,
        identification: str | None = None,
        password: str | None = None,
    ) -> tuple[str, str]:
        ident = identification or self.skin_email or self.default_bot_username
        pwd = password or self.skin_password
        if not ident or not pwd:
            raise ConfigError(
                "缺少皮肤站凭据：请在环境变量或 .env 中配置 MC_SKIN_EMAIL 与 MC_SKIN_PASSWORD "
                "（账号可在 https://skin.nmo.net.cn/auth/register 使用 @smail.nju.edu.cn 或 @nju.edu.cn 注册）。"
            )
        return ident, pwd
