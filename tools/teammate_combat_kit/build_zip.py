#!/usr/bin/env python3
"""Build a 0600 distributable combat testkit zip with prefilled nju-beast bot/RCON credentials.

Never reads or bundles LLM keys (`NJU_API_KEY`), never leaves plaintext `testkit.env` on disk
inside `tools/`, and never prints secret values to stdout/stderr.
"""

from __future__ import annotations

import os
import pathlib
import time
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
ENV_BEAST = REPO_ROOT / ".env.beast"
OUT_ZIP = REPO_ROOT / "dist/mc-combat-testkit-v1.3.14.zip"
ARC_PREFIX = "mc-combat-testkit-v1.3.14"

STATIC_FILES = (
    "README-给同学的测试指南.md",
    "nju-beast-服务器与OP实操手册.md",
    "micro-action-test-plan.md",
    "API-v1.3.14.md",
    "run_combat_tests.py",
    "testkit.env.example",
)


def parse_env(path: pathlib.Path) -> dict[str, str]:
    if not path.is_file():
        raise SystemExit(f"Missing required env file: {path}")
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def render_testkit_env(bot_pass: str, rcon_pass: str) -> str:
    return (
        "# nju-beast 测试服预填配置（仅含 Bot 与测试服 RCON 口令，禁止外泄或提交入库）\n"
        "# 校园网默认直连 10.50.248.70；若你在校外通过 Tailscale 访问，将 IP 改为 100.93.211.71 即可。\n"
        "\n"
        "MC_API_URL=http://10.50.248.70:18900/api/v1\n"
        "# MC_API_URL=http://100.93.211.71:18900/api/v1\n"
        "\n"
        "# 独立测试账号名（切勿改成 BeastBot_01，以免把主控制台 Bot 踢下线）\n"
        "MC_BOT_USER=FightBot_01\n"
        f"MC_BOT_PASS={bot_pass}\n"
        "\n"
        "# Source RCON（仅跑 exp1~exp7 全自动脚本时使用；使用 --interactive 联机模式无需 RCON）\n"
        "RCON_HOST=10.50.248.70\n"
        "RCON_PORT=25575\n"
        f"RCON_PASS={rcon_pass}\n"
    )


def main() -> None:
    vars_map = parse_env(ENV_BEAST)
    bot_pass = vars_map.get("MC_SKIN_PASSWORD", "").strip()
    rcon_pass = vars_map.get("MC_LOCAL_RCON_PASSWORD", "").strip()
    if not bot_pass:
        raise SystemExit("MC_SKIN_PASSWORD missing or empty in .env.beast")
    if not rcon_pass:
        raise SystemExit("MC_LOCAL_RCON_PASSWORD missing or empty in .env.beast")

    env_bytes = render_testkit_env(bot_pass, rcon_pass).encode("utf-8")

    OUT_ZIP.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = OUT_ZIP.with_name(OUT_ZIP.name + ".tmp")
    fd = os.open(tmp_zip, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(fd, 0o600)
    with os.fdopen(fd, "wb") as fh:
        with zipfile.ZipFile(fh, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name in STATIC_FILES:
                src = HERE / name
                if not src.is_file():
                    raise SystemExit(f"Missing kit file: {src}")
                zf.write(src, arcname=f"{ARC_PREFIX}/{name}")

            zi = zipfile.ZipInfo(f"{ARC_PREFIX}/testkit.env", date_time=time.localtime()[:6])
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.create_system = 3
            zi.external_attr = (0o100600 & 0xFFFF) << 16
            zf.writestr(zi, env_bytes)

    os.chmod(tmp_zip, 0o600)
    os.replace(tmp_zip, OUT_ZIP)
    os.chmod(OUT_ZIP, 0o600)

    mode = oct(OUT_ZIP.stat().st_mode & 0o777)
    size = OUT_ZIP.stat().st_size
    with zipfile.ZipFile(OUT_ZIP, "r") as zf:
        names = zf.namelist()
    print(f"built {OUT_ZIP} ({size} bytes, mode={mode})")
    for n in names:
        print(f"  - {n}")


if __name__ == "__main__":
    main()
