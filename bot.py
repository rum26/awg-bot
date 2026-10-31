import asyncio
import json
import logging
import re
import os
from html import escape
from pathlib import Path
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

USER_ID = os.getenv("USER_ID")
TOKEN = os.getenv("TOKEN")

if not USER_ID or not TOKEN:
    USER_ID = input("USER ID:").strip()
    TOKEN = input("TOKEN:").strip()

    if not USER_ID or not TOKEN:
        raise ValueError("USER_ID и TOKEN не могут быть пустыми")

    with open(".env", "w", encoding="utf-8") as file_env:
        file_env.write(f"USER_ID={USER_ID}\n")
        file_env.write(f"TOKEN={TOKEN}\n")

    load_dotenv(override=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("awg-bot")

SCRIPTS_DIR = Path("/opt/awg-bot/scripts")

ALLOWED_SCRIPTS = {
    "add": SCRIPTS_DIR / "add_user.sh",
    "remove": SCRIPTS_DIR / "remove_user.sh",
    "stats": SCRIPTS_DIR / "stats.sh",
}

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def format_size(n: int) -> str:
    value = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.1f} {unit}"
        value /= 1024


async def run_script(name: str, args: list[str] | None = None) -> str:
    script = ALLOWED_SCRIPTS.get(name)

    if not script or not script.is_file():
        return "Скрипт не найден."

    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/sudo", str(script), *(args or []),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "Ошибка: таймаут выполнения."

        output = stdout.decode(errors="replace").strip()
        return output or "Готово."

    except Exception as ex:
        logger.exception(f"Ошибка запуска скрипта {ex}")
        return "Ошибка выполнения."


def is_admin(update: Update) -> bool:
    return update.effective_user.id != int(USER_ID)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    await update.message.reply_text(
        "Команды:\n"
        "/add <имя> — добавить пользователя\n"
        "/remove <имя> — удалить пользователя\n"
        "/stats — статистика"
    )


async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return

    if not context.args or not NAME_RE.fullmatch(context.args[0]):
        await update.message.reply_text("Использование: /add <имя>")
        return

    name = context.args[0]
    result = await run_script("add", [name])
    await update.message.reply_text(result)

    files = [
        [Path(f"/root/awg/{name}.conf"), "Простой конфиг для AmneziaWG"],
        [Path(f"/root/awg/{name}.png"), "Простой QR для AmneziaWG"],
        # [Path(f"/root/awg/{name}.vpnuri"), "Улучшенный конфиг для AmneziaVPN"],
        [Path(f"/root/awg/{name}.vpnuri.png"), "Улучшенный QR для AmneziaVPN"],
    ]

    for file, title in files:
        if file.is_file():
            await update.message.reply_document(document=file, caption=title)

    vpnuri_file = Path(f"/root/awg/{name}.vpnuri")

    if vpnuri_file.is_file():
        vpnuri = vpnuri_file.read_text(encoding="utf-8").strip()
        await update.message.reply_text(vpnuri)


async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        print("не админ!")
        return

    if not context.args or not NAME_RE.fullmatch(context.args[0]):
        await update.message.reply_text("Использование: /remove <имя>")
        return

    result = await run_script("remove", [context.args[0]])
    await update.message.reply_text(result)


def format_stats(users: list[dict]) -> str:
    rows = []

    for user in users:
        rows.append((
            user["name"],
            user["ip"],
            format_size(user["tx"]),
            user["status"],
        ))

    name_w = max(len("Имя"), *(len(r[0]) for r in rows))
    ip_w = max(len("IP"), *(len(r[1]) for r in rows))
    size_w = max(len("Скачано"), *(len(r[2]) for r in rows))
    status_w = max(len("Статус"), *(len(r[3]) for r in rows))

    lines = [
        f"{'Имя':<{name_w}}  {'IP':<{ip_w}}  {'Скачано':<{size_w}}  {'Статус':<{status_w}}",
        "-" * (name_w + ip_w + size_w + status_w + 6),
    ]

    for name, ip, size, status in rows:
        lines.append(
            f"{name:<{name_w}}  {ip:<{ip_w}}  {size:>{size_w}}  {status:<{status_w}}"
        )

    return "\n".join(lines)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return

    result = await run_script("stats")

    try:
        users = json.loads(result)
    except json.JSONDecodeError:
        await update.message.reply_text(result)
        return

    if not isinstance(users, list):
        await update.message.reply_text(result)
        return

    table = format_stats(users)
    await update.message.reply_text(
        f"<pre>{escape(table)}</pre>",
        parse_mode="HTML",
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Ошибка обработки update", exc_info=context.error)


def main() -> None:
    token = TOKEN

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_user))
    app.add_handler(CommandHandler("remove", remove_user))
    app.add_handler(CommandHandler("stats", stats))
    app.add_error_handler(error_handler)

    app.run_polling(
        poll_interval=5,
        timeout=30,
        bootstrap_retries=-1,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
