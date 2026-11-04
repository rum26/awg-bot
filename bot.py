import asyncio
import json
import logging
import os
import re
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from telegram import ReplyKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, MessageHandler, filters

load_dotenv(Path(__file__).with_name(".env"))
TOKEN = os.getenv("TOKEN", "").strip()
USER_ID = os.getenv("USER_ID", "").strip()
if not USER_ID or not TOKEN:
    USER_ID = input("USER ID:").strip()
    TOKEN = input("TOKEN:").strip()
    if not USER_ID or not TOKEN:
        raise ValueError("USER_ID и TOKEN не могут быть пустыми")
    with open(".env", "w", encoding="utf-8") as file_env:
        file_env.write(f"USER_ID={USER_ID}\n")
        file_env.write(f"TOKEN={TOKEN}\n")
    load_dotenv(override=True)

USER_ID = int(USER_ID)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("awg-bot")
SCRIPTS_DIR = Path("/opt/awg-bot/scripts")
CONFIGS_DIR = Path("/root/awg")
SCRIPTS = {
    "add": SCRIPTS_DIR / "add_user.sh",
    "remove": SCRIPTS_DIR / "remove_user.sh",
    "stats": SCRIPTS_DIR / "stats.sh",
}
BUTTONS = {
    "➕ Добавить": "add",
    "🗑 Удалить": "remove",
    "📊 Статистика": "stats",
}
COMMANDS = {"/add": "add", "/remove": "remove", "/stats": "stats"}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
KEYBOARD = ReplyKeyboardMarkup(
    [["➕ Добавить", "🗑 Удалить"], ["📊 Статистика"]],
    resize_keyboard=True,
    one_time_keyboard=False,
    is_persistent=True,
)


def format_size(value):
    value = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.1f} {unit}"
        value /= 1024


def format_stats(users):
    if not users:
        return "Пользователей пока нет."
    headers = ("Имя", "IP", "Скачано", "Статус")
    rows = [
        (str(u["name"]), str(u["ip"]), format_size(u["tx"]), str(u["status"]))
        for u in users
    ]
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths))]
    lines.append("-" * (sum(widths) + 6))
    lines.extend("  ".join(v.ljust(w) for v, w in zip(row, widths)) for row in rows)
    return "\n".join(lines)


async def send_text(message, text, table=False):
    text = text or "Нет данных."
    for offset in range(0, len(text), 1500):
        part = text[offset:offset + 1500]
        await message.reply_text(
            f"<pre>{escape(part)}</pre>" if table else part,
            parse_mode="HTML" if table else None,
            reply_markup=KEYBOARD,
        )


async def run_script(action, name=None):
    script = SCRIPTS[action]
    if not script.is_file():
        return False, f"Скрипт не найден: {script}"
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/sudo", "-n", str(script), *([name] if name else []),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.communicate()
        return False, "Ошибка: скрипт не завершился за 30 секунд."
    text = output.decode(errors="replace").strip()
    if proc.returncode != 0:
        return False, f"Ошибка скрипта (код {proc.returncode}):\n{text or 'Нет вывода.'}"
    return True, text or "Готово."


async def send_configs(message, name):
    files = (
        (".conf", "Простой конфиг для AmneziaWG"),
        (".png", "Простой QR для AmneziaWG"),
        (".vpnuri.png", "Улучшенный QR для AmneziaVPN"),
    )
    for suffix, title in files:
        file = CONFIGS_DIR / f"{name}{suffix}"
        if file.is_file():
            with file.open("rb") as document:
                print(f"[send_configs] {name}{suffix}")
                await asyncio.sleep(3)
                await message.reply_document(document=document, filename=file.name, caption=title)
    file = CONFIGS_DIR / f"{name}.vpnuri"
    if file.is_file():
        text = file.read_text(encoding="utf-8").strip()
        if text:
            if len(text.encode("utf-16-le")) // 2 <= 4096:
                print(f"[send_configs] {name}.vpnuri")
                await asyncio.sleep(3)
                await message.reply_text(text)
            else:
                with file.open("rb") as document:
                    print(f"[send_configs] {name}.vpnuri")
                    await asyncio.sleep(3)
                    await message.reply_document(document=document, filename=file.name)


async def perform_action(message, context, action, name=None):
    try:
        print(f"[perform_action] action: {action}")
        ok, result = await run_script(action, name)
        if action == "stats" and ok:
            try:
                users = json.loads(result)
            except json.JSONDecodeError:
                users = None
            if isinstance(users, list):
                await send_text(message, format_stats(users), table=True)
                return
        await send_text(message, result)
        if action == "add":
            await send_configs(message, name)
    except Exception:
        logger.exception("Ошибка операции %s", action)
        await send_text(message, "Ошибка выполнения. Подробности в журнале бота.")
    finally:
        context.bot_data.pop("busy", None)


async def handle_message(update, context):
    if not update.effective_user or update.effective_user.id != USER_ID:
        return
    message = update.effective_message
    text = (message.text or "").strip()
    if text in BUTTONS:
        try:
            await message.delete()
        except TelegramError as error:
            logger.warning("Не удалось удалить нажатие: %s", error)

    parts = text.split()
    command = parts[0].split("@", 1)[0] if parts else ""
    if command == "/start":
        context.user_data.pop("pending_action", None)
        await send_text(message, "Управление AmneziaWG:")
        return
    if context.bot_data.get("busy"):
        await send_text(message, "Дождись завершения предыдущей операции.")
        return

    action = BUTTONS.get(text) or COMMANDS.get(command)
    if action == "stats":
        context.user_data.pop("pending_action", None)
        name = None
    else:
        if action in ("add", "remove"):
            context.user_data["pending_action"] = action
            if command not in COMMANDS or len(parts) == 1:
                await send_text(message, "Пришли имя пользователя одним сообщением:")
                return
            name = " ".join(parts[1:])
        else:
            action = context.user_data.get("pending_action")
            name = text
        if action not in ("add", "remove"):
            await send_text(message, "Выбери действие кнопками внизу.")
            return
        if not NAME_RE.fullmatch(name):
            await send_text(message, "Имя: латиница, цифры, _ и -, от 1 до 32 символов.")
            return
        context.user_data.pop("pending_action", None)

    context.bot_data["busy"] = True
    context.application.create_task(
        perform_action(message, context, action, name), update=update,
    )


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "add", "remove", "stats"], handle_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(poll_interval=5, allowed_updates=["message"])


if __name__ == "__main__":
    main()