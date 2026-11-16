"""Command-line interface (`mc-bot`) for authentication, bot creation, and lifecycle control."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import pathlib
import sys
from typing import Sequence

from .auth import mask_secret
from .bot import BotManager
from .config import Settings, parse_dotenv
from .errors import ConfigError, McAgentError


def _resolve_password(settings: Settings, *, prompt_if_missing: bool = False) -> str | None:
    if settings.skin_password:
        return settings.skin_password
    if prompt_if_missing and sys.stdin.isatty():
        return getpass.getpass("请输入 NMO 皮肤站密码: ")
    return None


def _print_json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_skin_info(args: argparse.Namespace, manager: BotManager) -> int:
    ident = args.email or manager.settings.skin_email or manager.settings.default_bot_username
    pwd = _resolve_password(manager.settings, prompt_if_missing=args.prompt_password)
    ident_ok, pwd_ok = manager.settings.require_skin_credentials(
        identification=ident,
        password=pwd,
    )
    with manager.make_skin_client() as skin:
        skin.login(ident_ok, pwd_ok)
        sign_result = None
        if args.sign:
            signed, new_score, msg = skin.sign_in()
            sign_result = {"signed": signed, "score": new_score, "message": msg}
        score = skin.get_score_info()
        players = skin.list_players()

    payload = {
        "skin_base_url": manager.settings.skin_base_url,
        "account": ident_ok,
        "score": score.score,
        "cost_per_player": score.cost_per_player,
        "available_new_players": score.available_new_players,
        "players": [{"pid": p.pid, "name": p.name} for p in players],
    }
    if sign_result is not None:
        payload["sign_in"] = sign_result
    _print_json(payload)
    return 0


def cmd_create(args: argparse.Namespace, manager: BotManager) -> int:
    pwd = _resolve_password(manager.settings, prompt_if_missing=args.prompt_password)
    bot = manager.create_bot(
        args.bot_name,
        skin_email=args.email,
        skin_password=pwd,
        auto_sign=not args.no_auto_sign,
        connect=not args.no_connect,
        wait_ready=not (args.no_connect or args.no_wait),
        ready_timeout_s=args.timeout,
        force_relogin=args.force_login,
    )
    try:
        _print_json(
            {
                "bot_username": bot.bot_username,
                "skin_profile_pid": bot.profile.pid if bot.profile else None,
                "created_on_skin_station": bot.created_on_skin_station,
                "signed_in_for_score": bot.signed_in_for_score,
                "reused_cached_token": bot.reused_token,
                "token_masked": mask_secret(bot.token_record.token),
                "expires_at": bot.token_record.expires_at,
                "ready": bot.status.get("ready") if bot.status else None,
                "pos": bot.status.get("pos") if bot.status else None,
                "health": bot.status.get("health") if bot.status else None,
                "food": bot.status.get("food") if bot.status else None,
            }
        )
    finally:
        bot.session.close()
    return 0


def cmd_login(args: argparse.Namespace, manager: BotManager) -> int:
    pwd = _resolve_password(manager.settings, prompt_if_missing=args.prompt_password)
    bot = manager.open_bot(
        args.bot_name,
        password=pwd,
        wait_ready=not args.no_wait,
        ready_timeout_s=args.timeout,
        force_relogin=args.force,
    )
    try:
        _print_json(
            {
                "bot_username": bot.bot_username,
                "reused_cached_token": bot.reused_token,
                "token_masked": mask_secret(bot.token_record.token),
                "expires_at": bot.token_record.expires_at,
                "ready": bot.status.get("ready") if bot.status else None,
                "pos": bot.status.get("pos") if bot.status else None,
                "health": bot.status.get("health") if bot.status else None,
                "food": bot.status.get("food") if bot.status else None,
            }
        )
    finally:
        bot.session.close()
    return 0


def cmd_status(args: argparse.Namespace, manager: BotManager) -> int:
    pwd = _resolve_password(manager.settings, prompt_if_missing=False)
    bot = manager.open_bot(
        args.bot_name,
        password=pwd,
        wait_ready=False,
    )
    try:
        st = bot.status or bot.session.status()
        _print_json(st)
    finally:
        bot.session.close()
    return 0


def cmd_advancements(args: argparse.Namespace, manager: BotManager) -> int:
    catalog_path = pathlib.Path(__file__).parent / "data" / "advancements_1_21_11.json"
    if args.catalog:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        if args.category:
            catalog["advancements"] = [
                a for a in catalog["advancements"] if a["category"] == args.category
            ]
        _print_json(catalog)
        return 0

    pwd = _resolve_password(manager.settings, prompt_if_missing=False)
    bot = manager.open_bot(args.bot_name, password=pwd, wait_ready=False)
    try:
        live = bot.session.advancements(
            status=args.status,
            include_recipes=args.include_recipes,
        )
        _print_json(live)
    finally:
        bot.session.close()
    return 0


def cmd_connect(args: argparse.Namespace, manager: BotManager) -> int:
    pwd = _resolve_password(manager.settings, prompt_if_missing=False)
    bot = manager.open_bot(args.bot_name, password=pwd, wait_ready=False)
    try:
        res = bot.session.connect()
        if not args.no_wait:
            st = bot.session.wait_until_ready(timeout_s=args.timeout)
            _print_json({"connect": res, "ready": st.get("ready"), "pos": st.get("pos")})
        else:
            _print_json(res)
    finally:
        bot.session.close()
    return 0


def cmd_disconnect(args: argparse.Namespace, manager: BotManager) -> int:
    pwd = _resolve_password(manager.settings, prompt_if_missing=False)
    bot = manager.open_bot(args.bot_name, password=pwd, wait_ready=False)
    try:
        res = bot.session.disconnect()
        _print_json({"bot_username": bot.bot_username, "disconnected": res})
    finally:
        bot.session.close()
    return 0


def cmd_logout(args: argparse.Namespace, manager: BotManager) -> int:
    target = args.bot_name or manager.settings.default_bot_username or manager.settings.skin_email
    if not target:
        records = manager.token_store.list_all()
        if len(records) == 1:
            target = records[0].bot_username
        else:
            raise ConfigError("请指定要登出的 <bot_name>。")
    with manager.make_auth_client() as auth:
        ok = auth.logout(target)
    _print_json({"target": target, "logged_out": ok})
    return 0


def cmd_rename(args: argparse.Namespace, manager: BotManager) -> int:
    ident, pwd = manager.settings.require_skin_credentials(identification=args.email)
    with manager.make_skin_client() as skin:
        skin.login(ident, pwd)
        players = skin.list_players()
        matched = [p for p in players if p.name.lower() == args.old_name.lower()]
        if not matched:
            raise ConfigError(f"皮肤站账号下未找到角色 {args.old_name!r}。")
        updated = skin.rename_player(matched[0].pid, args.new_name)

    # Clear old cached token and update .env if default matched old_name
    with manager.make_auth_client() as auth:
        auth.logout(args.old_name)

    env_path = pathlib.Path(args.env_file) if args.env_file else pathlib.Path.cwd() / ".env"
    if env_path.is_file():
        current_env = parse_dotenv(env_path)
        if (current_env.get("MC_BOT_USERNAME") or "").lower() == args.old_name.lower():
            _upsert_dotenv(env_path, {"MC_BOT_USERNAME": updated.name})

    _print_json({"pid": updated.pid, "old_name": args.old_name, "new_name": updated.name})
    return 0


def _upsert_dotenv(path: pathlib.Path, updates: dict[str, str]) -> None:
    """Write or update keys in `.env` with 0o600 permissions while preserving existing keys."""
    existing_lines: list[str] = []
    if path.is_file():
        existing_lines = path.read_text(encoding="utf-8").splitlines()

    remaining = dict(updates)
    out_lines: list[str] = []
    for raw in existing_lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key_part = stripped.removeprefix("export ").split("=", 1)[0].strip()
            if key_part in remaining:
                val = remaining.pop(key_part)
                escaped = val.replace("\\", "\\\\").replace('"', '\\"')
                out_lines.append(f'{key_part}="{escaped}"')
                continue
        out_lines.append(raw)

    for key, val in remaining.items():
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        out_lines.append(f'{key}="{escaped}"')

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.chmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out_lines).rstrip("\n") + "\n")
    except Exception:
        raise


def cmd_init(args: argparse.Namespace, manager: BotManager) -> int:
    env_path = (
        pathlib.Path(args.env_file)
        if args.env_file
        else pathlib.Path.cwd() / ".env"
    )
    existing = parse_dotenv(env_path)
    default_email = args.email or existing.get("MC_SKIN_EMAIL") or ""
    default_bot = args.bot_name or existing.get("MC_BOT_USERNAME") or ""

    if not sys.stdin.isatty() and not (args.email and manager.settings.skin_password):
        raise ConfigError("非交互终端下请直接在 .env（权限 0600）中填写 MC_SKIN_EMAIL 与 MC_SKIN_PASSWORD。")

    email = args.email
    if not email:
        prompt = f"NMO 皮肤站邮箱 [{default_email}]: " if default_email else "NMO 皮肤站邮箱 (@smail.nju.edu.cn / @nju.edu.cn): "
        entered = input(prompt).strip()
        email = entered or default_email
    if not email:
        raise ConfigError("邮箱不能为空。")

    pwd = getpass.getpass("NMO 皮肤站密码（输入不回显，留空则保留原配置）: ")
    if not pwd:
        pwd = existing.get("MC_SKIN_PASSWORD") or manager.settings.skin_password or ""
    if not pwd:
        raise ConfigError("密码不能为空。")

    bot_name = args.bot_name
    if bot_name is None:
        prompt_bot = f"要新建/默认使用的 Bot 角色名（可留空稍后指定）[{default_bot}]: "
        entered_bot = input(prompt_bot).strip()
        bot_name = entered_bot or default_bot

    updates = {
        "MC_SKIN_EMAIL": email,
        "MC_SKIN_PASSWORD": pwd,
    }
    if bot_name:
        updates["MC_BOT_USERNAME"] = bot_name

    _upsert_dotenv(env_path, updates)
    print(f"已写入 {env_path} (权限 0600)。")

    if args.verify:
        refreshed = BotManager(Settings.load(dotenv_path=env_path))
        if bot_name:
            create_ns = argparse.Namespace(
                bot_name=bot_name,
                email=email,
                no_auto_sign=False,
                no_connect=False,
                no_wait=False,
                force_login=False,
                timeout=45.0,
                prompt_password=False,
            )
            return cmd_create(create_ns, refreshed)
        skin_ns = argparse.Namespace(email=email, sign=True, prompt_password=False)
        return cmd_skin_info(skin_ns, refreshed)
    return 0


def cmd_tokens(_args: argparse.Namespace, manager: BotManager) -> int:
    records = manager.token_store.list_all()
    _print_json(
        {
            "store_path": str(manager.token_store.path),
            "tokens": [
                {
                    "bot_username": r.bot_username,
                    "login_username": r.login_username,
                    "token_masked": mask_secret(r.token),
                    "expires_at": r.expires_at,
                    "expired": r.is_expired(),
                    "obtained_at": r.obtained_at,
                }
                for r in records
            ],
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mc-bot",
        description="Minecraft Agent API v1.3 认证、皮肤站角色创建与 Bot 连接管理工具",
    )
    parser.add_argument("--env-file", default=None, help="指定 .env 配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="交互式录入皮肤站凭据到 .env (0600)，可选立即建号上线")
    p_init.add_argument("--email", default=None, help="皮肤站账号邮箱")
    p_init.add_argument("--bot-name", default=None, help="默认或要新建的 Bot 角色名")
    p_init.add_argument("--no-verify", dest="verify", action="store_false", help="仅保存 .env，不立即连接验证")
    p_init.set_defaults(func=cmd_init, verify=True)

    p_skin = sub.add_parser("skin-info", help="查看皮肤站账号积分、角色列表（可选签到）")
    p_skin.add_argument("--email", default=None, help="皮肤站登录邮箱或已有角色名")
    p_skin.add_argument("--sign", action="store_true", help="顺便执行每日签到")
    p_skin.add_argument("-p", "--prompt-password", action="store_true", help="交互式输入密码")
    p_skin.set_defaults(func=cmd_skin_info)

    p_create = sub.add_parser("create", help="在皮肤站新建 Bot 角色 + 认证换 Token + 连接比赛服")
    p_create.add_argument("bot_name", help="新 Bot 角色名（3-16 位字母数字下划线）")
    p_create.add_argument("--email", default=None, help="皮肤站账号邮箱")
    p_create.add_argument("--no-auto-sign", action="store_true", help="积分不足时不自动签到")
    p_create.add_argument("--no-connect", action="store_true", help="仅创建角色并换取 Token，不主动连接")
    p_create.add_argument("--no-wait", action="store_true", help="不轮询等待 status.ready=true")
    p_create.add_argument("--force-login", action="store_true", help="忽略本地缓存 Token 强制重新登录")
    p_create.add_argument("--timeout", type=float, default=45.0, help="等待 ready 超时秒数")
    p_create.add_argument("-p", "--prompt-password", action="store_true", help="交互式输入密码")
    p_create.set_defaults(func=cmd_create)

    p_login = sub.add_parser("login", help="使用已有角色登录 MC API（优先复用有效缓存 Token）并等待就绪")
    p_login.add_argument("bot_name", nargs="?", default=None, help="Bot 角色名（或单角色账号邮箱）")
    p_login.add_argument("--force", action="store_true", help="强制重新获取新 Token（会使旧 Token 失效）")
    p_login.add_argument("--no-wait", action="store_true", help="不等待 status.ready=true")
    p_login.add_argument("--timeout", type=float, default=45.0, help="等待 ready 超时秒数")
    p_login.add_argument("-p", "--prompt-password", action="store_true", help="交互式输入密码")
    p_login.set_defaults(func=cmd_login)

    p_status = sub.add_parser("status", help="查询指定 Bot 当前游戏内状态 (GET /status)")
    p_status.add_argument("bot_name", nargs="?", default=None, help="Bot 角色名")
    p_status.set_defaults(func=cmd_status)

    p_adv = sub.add_parser("advancements", help="查询 Bot 服务端可见进度或 1.21.11 完整目录")
    p_adv.add_argument("bot_name", nargs="?", default=None, help="Bot 角色名")
    p_adv.add_argument("--status", choices=["completed", "in_progress", "not_started"], default=None)
    p_adv.add_argument("--include-recipes", action="store_true", help="包含 recipes/* 配方进度")
    p_adv.add_argument("--catalog", action="store_true", help="输出内置 1.21.11 全部 125 项进度目录")
    p_adv.add_argument("--category", choices=["story", "nether", "end", "adventure", "husbandry"], default=None)
    p_adv.set_defaults(func=cmd_advancements)

    p_connect = sub.add_parser("connect", help="触发 Bot 连接或重发 /server survivaltest")
    p_connect.add_argument("bot_name", nargs="?", default=None, help="Bot 角色名")
    p_connect.add_argument("--no-wait", action="store_true", help="不等待 ready=true")
    p_connect.add_argument("--timeout", type=float, default=45.0, help="等待 ready 超时秒数")
    p_connect.set_defaults(func=cmd_connect)

    p_disconnect = sub.add_parser("disconnect", help="断开 Bot 游戏连接 (POST /disconnect)")
    p_disconnect.add_argument("bot_name", nargs="?", default=None, help="Bot 角色名")
    p_disconnect.set_defaults(func=cmd_disconnect)

    p_logout = sub.add_parser("logout", help="注销 Bearer Token 并删除本地缓存 (POST /auth/logout)")
    p_logout.add_argument("bot_name", nargs="?", default=None, help="Bot 角色名")
    p_logout.set_defaults(func=cmd_logout)

    p_rename = sub.add_parser("rename", help="在皮肤站重命名已有 Bot 角色并清理旧缓存")
    p_rename.add_argument("old_name", help="原角色名")
    p_rename.add_argument("new_name", help="新角色名（3-16 位字母数字下划线）")
    p_rename.add_argument("--email", default=None, help="皮肤站账号邮箱")
    p_rename.set_defaults(func=cmd_rename)

    p_tokens = sub.add_parser("tokens", help="列出本地缓存的 Bot Token（脱敏显示）")
    p_tokens.set_defaults(func=cmd_tokens)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings.load(dotenv_path=args.env_file)
        manager = BotManager(settings)
        return int(args.func(args, manager))
    except McAgentError as err:
        print(f"错误: {err}", file=sys.stderr)
        return 1
    except ValueError as err:
        print(f"参数错误: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
