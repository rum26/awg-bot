from __future__ import annotations

import asyncio
import json
import os
import re
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

load_dotenv()
USER_ID = os.getenv("USER_ID")
TOKEN = os.getenv("TOKEN")

if not USER_ID or not TOKEN:
    USER_ID = USER_ID or input("USER ID: ").strip()
    TOKEN = TOKEN or input("TOKEN: ").strip()
    if not USER_ID or not TOKEN:
        raise ValueError("USER_ID и TOKEN не могут быть пустыми")
    with open(".env", "w", encoding="utf-8") as file_env:
        file_env.write(f"USER_ID={USER_ID}\nTOKEN={TOKEN}\n")

USER_ID = int(USER_ID)

SCRIPTS_DIR = Path("/opt/awg-bot/scripts")
ALLOWED_SCRIPTS = {
    "add": SCRIPTS_DIR / "add_user.sh",
    "remove": SCRIPTS_DIR / "remove_user.sh",
    "stats": SCRIPTS_DIR / "stats.sh",
}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == USER_ID


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить", callback_data="add")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("🗑 Удалить", callback_data="remove")],
    ])


def format_size(n: int) -> str:
    value = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.1f} {unit}"
        value /= 1024


def format_stats(users: list[dict]) -> str:
    if not users:
        return "Пользователей пока нет."
    rows = [
        (str(u["name"]), str(u["ip"]), format_size(int(u["tx"])), str(u["status"]))
        for u in users
    ]
    headers = ("Имя", "IP", "Скачано", "Статус")
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(4)]
    name_w, ip_w, size_w, status_w = widths
    lines = [
        f"{'Имя':<{name_w}}  {'IP':<{ip_w}}  {'Скачано':<{size_w}}  {'Статус':<{status_w}}",
        "-" * (sum(widths) + 6),
    ]
    for name, ip, size, status in rows:
        lines.append(
            f"{name:<{name_w}}  {ip:<{ip_w}}  {size:>{size_w}}  {status:<{status_w}}"
        )
    return "\n".join(lines)


async def run_script(name: str, args: list[str] | None = None) -> tuple[bool, str]:
    script = ALLOWED_SCRIPTS.get(name)
    if not script or not script.is_file():
        return False, "Скрипт не найден."
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/sudo", "-n", str(script), *(args or []),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.communicate()
            return False, "Ошибка: таймаут выполнения скрипта."
        output = stdout.decode(errors="replace").strip()
        if proc.returncode != 0:
            print(f"Скрипт {name}: {output}")
            return False, f"Ошибка скрипта (код {proc.returncode}):\n{output or 'Нет вывода.'}"
        return True, output or "Готово."
    except Exception as ex:
        print("Ошибка запуска скрипта:", name, ex)
        return False, "Ошибка выполнения. Подробности в журнале."


async def send_text(message, text: str, *, html: bool = False) -> None:
    # До 1500 символов на часть: запас для лимита Telegram и UTF-16.
    text = text or "Нет данных."
    for offset in range(0, len(text), 1500):
        part = text[offset:offset + 1500]
        await message.reply_text(
            f"<pre>{escape(part)}</pre>" if html else part,
            parse_mode="HTML" if html else None,
            reply_markup=main_menu() if offset + 1500 >= len(text) else None,
        )


async def send_configs(message, name: str) -> None:
    files = [
        (Path(f"/root/awg/{name}.conf"), "Простой конфиг для AmneziaWG"),
        (Path(f"/root/awg/{name}.png"), "Простой QR для AmneziaWG"),
        (Path(f"/root/awg/{name}.vpnuri.png"), "Улучшенный QR для AmneziaVPN"),
    ]
    for file, title in files:
        if file.is_file():
            with file.open("rb") as document:
                await message.reply_document(document=document, filename=file.name, caption=title)
    vpnuri_file = Path(f"/root/awg/{name}.vpnuri")
    if vpnuri_file.is_file():
        vpnuri = vpnuri_file.read_text(encoding="utf-8").strip()
        if vpnuri:
            # Не разрезаем строку подключения на неработающие фрагменты.
            if len(vpnuri.encode("utf-16-le")) // 2 <= 4096:
                await message.reply_text(vpnuri)
            else:
                with vpnuri_file.open("rb") as document:
                    await message.reply_document(document=document, filename=vpnuri_file.name)


async def perform_action(update: Update, context, action: str, name: str | None = None) -> None:
    message = update.effective_message
    try:
        ok, result = await run_script(action, [name] if name else None)
        if action == "stats" and ok:
            try:
                users = json.loads(result)
            except json.JSONDecodeError:
                users = None
            if isinstance(users, list):
                try:
                    table = format_stats(users)
                except (KeyError, TypeError, ValueError, OverflowError):
                    print("Некорректный формат статистики")
                    await send_text(message, "Ошибка формата статистики. Подробности в журнале.")
                    return
                query = update.callback_query
                if query and len(table.encode("utf-16-le")) // 2 <= 4096:
                    try:
                        # await query.edit_message_text(
                        #     f"<pre>{escape(table)}</pre>",
                        #     parse_mode="HTML", reply_markup=main_menu(),
                        # )
                        await send_text(message, table, html=True)
                    except BadRequest as exc:
                        if "message is not modified" not in str(exc).lower():
                            raise
                else:
                    await send_text(message, table, html=True)
                return
        await send_text(message, result)
        if action == "add" and ok:
            await send_configs(message, name)
    finally:
        context.bot_data.pop("script_busy", None)


def launch_action(update: Update, context, action: str, name: str | None = None) -> None:
    # Задача работает в фоне, обработчик сразу освобождает очередь обновлений.
    # Флаг устанавливается до create_task: скрипты не запускаются параллельно.
    context.bot_data["script_busy"] = True
    context.application.create_task(perform_action(update, context, action, name), update=update)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    context.user_data.pop("pending_action", None)
    await update.effective_message.reply_text("Управление AmneziaWG:", reply_markup=main_menu())


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    if not is_admin(update):
        await query.answer("Нет доступа. Проверь USER_ID в .env.", show_alert=True)
        return
    if context.bot_data.get("script_busy"):
        await query.answer("Предыдущая операция ещё выполняется.", show_alert=True)
        return
    await query.answer()
    action = query.data
    if action == "stats":
        context.user_data.pop("pending_action", None)
        launch_action(update, context, action)
    elif action in ("add", "remove"):
        context.user_data["pending_action"] = action
        await update.effective_message.reply_text("Пришли имя пользователя одним сообщением:")


async def handle_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    action = context.user_data.get("pending_action")
    if action not in ("add", "remove"):
        return
    message = update.effective_message
    if context.bot_data.get("script_busy"):
        await message.reply_text("Дождись завершения предыдущей операции.")
        return
    name = (message.text or "").strip()
    if not NAME_RE.fullmatch(name):
        await message.reply_text("Недопустимое имя: латиница, цифры, _ и -, от 1 до 32 символов.")
        return
    context.user_data.pop("pending_action", None)
    launch_action(update, context, action, name)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    if context.bot_data.get("script_busy"):
        await update.effective_message.reply_text("Дождись завершения предыдущей операции.")
        return
    context.user_data.pop("pending_action", None)
    launch_action(update, context, "stats")


async def name_command(update: Update, context, action: str) -> None:
    if not is_admin(update):
        return
    if context.bot_data.get("script_busy"):
        await update.effective_message.reply_text("Дождись завершения предыдущей операции.")
        return
    if len(context.args) != 1 or not NAME_RE.fullmatch(context.args[0]):
        await update.effective_message.reply_text(f"Использование: /{action} <имя>")
        return
    context.user_data.pop("pending_action", None)
    launch_action(update, context, action, context.args[0])


async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await name_command(update, context, "add")


async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await name_command(update, context, "remove")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    print("Ошибка обработки обновления:", error)
    if isinstance(update, Update) and is_admin(update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Ошибка. Подробности в журнале бота.")
        except Exception as ex:
            print("Не удалось отправить сообщение об ошибке", ex)


def main() -> None:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("add", add_user))
    app.add_handler(CommandHandler("remove", remove_user))
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(add|remove|stats)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_name))
    app.add_error_handler(error_handler)
    app.run_polling(
        poll_interval=10,
        timeout=60,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
