"""
Управление AmneziaWG через Telegram — версия на aiogram 3.x.

Отличия от bot.py (python-telegram-bot):
  - инлайн-кнопки: меню редактирует одно сообщение, чат не засоряется;
  - FSM вместо ручных флагов pending_action/bot_data["busy"];
  - asyncio.Lock вместо флага busy (устойчивее при ошибках);
  - подтверждение перед перезагрузкой сервера;
  - очистка чата: сообщения с именем пользователя удаляются после обработки;
  - уведомление владельцу при старте бота;
  - очистка webhook и pending updates при старте.

Интерфейс со скриптами не менялся — те же scripts/*.sh и /root/awg.

Зависимости:  pip install aiogram python-dotenv
Запуск:       python3 bot_aiogram.py
"""

import asyncio
import json
import re
from html import escape
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
)
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))
TOKEN = os.getenv("TOKEN", "").strip()
USER_ID_RAW = os.getenv("USER_ID", "").strip()

# Совместимость со старым поведением: при пустом .env спросить и сохранить.
if not TOKEN or not USER_ID_RAW:
    USER_ID_RAW = input("USER ID: ").strip()
    TOKEN = input("TOKEN: ").strip()
    if not TOKEN or not USER_ID_RAW:
        raise ValueError("USER_ID и TOKEN не могут быть пустыми")
    Path(__file__).with_name(".env").write_text(
        f"USER_ID={USER_ID_RAW}\nTOKEN={TOKEN}\n", encoding="utf-8"
    )

USER_ID = int(USER_ID_RAW)
SCRIPTS_DIR = Path("/opt/awg-bot/scripts")
CONFIGS_DIR = Path("/root/awg")

SCRIPTS = {
    "add": SCRIPTS_DIR / "add_user.sh",
    "remove": SCRIPTS_DIR / "remove_user.sh",
    "stats": SCRIPTS_DIR / "stats.sh",
    "restart": SCRIPTS_DIR / "restart.sh",
}
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
SCRIPT_TIMEOUT = 30

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Не даём запустить две операции одновременно (в т.ч. при повторных кликах).
operation_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="act:add"),
         InlineKeyboardButton(text="🗑 Удалить", callback_data="act:remove")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="act:stats"),
         InlineKeyboardButton(text="♻️ Перезагрузка", callback_data="act:restart")],
    ])


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Отмена", callback_data="nav:main")],
    ])


def kb_confirm_restart() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, перезагрузить", callback_data="do:restart")],
        [InlineKeyboardButton(text="« Отмена", callback_data="nav:main")],
    ])


# ---------------------------------------------------------------------------
# FSM-состояния
# ---------------------------------------------------------------------------

class UserForm(StatesGroup):
    waiting_add_name = State()
    waiting_remove_name = State()


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def format_size(value) -> str:
    value = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{value:.1f} {unit}"
        value /= 1024


def format_stats(users) -> str:
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


async def run_script(action: str, name: str | None = None):
    """Запуск скрипта с таймаутом. Возвращает (ok, текст)."""
    script = SCRIPTS[action]
    if not script.is_file():
        return False, f"Скрипт не найден: {script}"
    proc = await asyncio.create_subprocess_exec(
        "/usr/bin/sudo", "-n", str(script), *([name] if name else []),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=SCRIPT_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return False, f"Ошибка: скрипт не завершился за {SCRIPT_TIMEOUT} секунд."
    text = output.decode(errors="replace").strip()
    if proc.returncode != 0:
        return False, f"Ошибка скрипта (код {proc.returncode}):\n{text or 'Нет вывода.'}"
    return True, text or "Готово."


async def send_configs(message: Message, name: str) -> None:
    """Отправляет .conf / QR / vpnuri для пользователя, если файлы есть."""
    files = (
        (".conf", "Простой конфиг для AmneziaWG"),
        (".png", "Простой QR для AmneziaWG"),
        (".vpnuri.png", "Улучшенный QR для AmneziaVPN"),
    )
    for suffix, title in files:
        path = CONFIGS_DIR / f"{name}{suffix}"
        if path.is_file():
            await asyncio.sleep(1)  # не упереться в лимиты Telegram
            await message.answer_document(FSInputFile(path), caption=title)

    uri = CONFIGS_DIR / f"{name}.vpnuri"
    if uri.is_file():
        text = uri.read_text(encoding="utf-8").strip()
        if text:
            await asyncio.sleep(1)
            if len(text.encode("utf-16-le")) // 2 <= 4096:
                await message.answer(text)
            else:
                await message.answer_document(FSInputFile(uri))


async def edit_or_answer(message: Message, text: str, reply_markup=None,
                         table: bool = False) -> Message:
    """Пытается отредактировать сообщение; если текст тот же — отвечает новым."""
    if table:
        text = f"<pre>{escape(text)}</pre>"
    try:
        return await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as ex:
        if "message is not modified" in str(ex).lower():
            return message
        return await message.answer(text, reply_markup=reply_markup)


# ---------------------------------------------------------------------------
# Обработчики
# ---------------------------------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🛠 <b>Управление AmneziaWG</b>\nВыбери действие:",
                         reply_markup=kb_main())


@dp.callback_query(F.data == "nav:main")
async def nav_main(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit_or_answer(query.message, "🛠 <b>Управление AmneziaWG</b>\nВыбери действие:",
                         reply_markup=kb_main())
    await query.answer()


@dp.callback_query(F.data == "act:stats")
async def act_stats(query: CallbackQuery) -> None:
    if operation_lock.locked():
        await query.answer("Подожди, выполняется другая операция…", show_alert=True)
        return
    await query.answer("Собираю статистику…")
    async with operation_lock:
        ok, result = await run_script("stats")
        if ok:
            try:
                users = json.loads(result)
            except json.JSONDecodeError:
                users = None
            if isinstance(users, list):
                text = format_stats(users) or "Нет данных."
                await edit_or_answer(query.message, text, reply_markup=kb_main(), table=True)
                return
        await edit_or_answer(query.message, result or "Нет данных.",
                             reply_markup=kb_main())


@dp.callback_query(F.data == "act:restart")
async def act_restart_menu(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await edit_or_answer(query.message, "⚠️ <b>Перезагрузить AmneziaWG?</b>",
                         reply_markup=kb_confirm_restart())
    await query.answer()


@dp.callback_query(F.data == "do:restart")
async def do_restart(query: CallbackQuery) -> None:
    if operation_lock.locked():
        await query.answer("Подожди, выполняется другая операция…", show_alert=True)
        return
    await query.answer("Перезагружаю…")
    async with operation_lock:
        ok, result = await run_script("restart")
        text = ("✅ Сервер перезагружен." if ok
                else f"❌ Ошибка:\n{escape(result)}")
        await edit_or_answer(query.message, text, reply_markup=kb_main())


@dp.callback_query(F.data.in_({"act:add", "act:remove"}))
async def act_ask_name(query: CallbackQuery, state: FSMContext) -> None:
    action = "add" if query.data == "act:add" else "remove"
    await state.set_state(
        UserForm.waiting_add_name if action == "add"
        else UserForm.waiting_remove_name
    )
    prompt = ("➕ Пришли <b>имя</b> нового пользователя одним сообщением:"
              if action == "add"
              else "🗑 Пришли <b>имя</b> пользователя для удаления:")
    await edit_or_answer(query.message, prompt, reply_markup=kb_cancel())
    await query.answer()


async def handle_name(message: Message, state: FSMContext, action: str) -> None:
    name = (message.text or "").strip()
    # Убираем сообщение с именем, чтобы чат оставался чистым.
    try:
        await message.delete()
    except TelegramAPIError:
        pass

    if not NAME_RE.fullmatch(name):
        await message.answer("Имя: латиница, цифры, _ и -, от 1 до 32 символов.",
                             reply_markup=kb_cancel())
        return

    await state.clear()
    if operation_lock.locked():
        await message.answer("Подожди, выполняется другая операция.",
                             reply_markup=kb_main())
        return

    menu_msg = await message.answer(
        f"{'➕ Добавляю' if action == 'add' else '🗑 Удаляю'} <b>{escape(name)}</b>…",
        reply_markup=kb_cancel())

    async with operation_lock:
        ok, result = await run_script(action, name)
        result = result or "Готово."
        if ok:
            header = ("✅ Пользователь добавлен." if action == "add"
                      else "🗑 Пользователь удалён.")
            await edit_or_answer(menu_msg, f"{header}\n\n{escape(result)}",
                                 reply_markup=kb_main())
            if action == "add":
                await send_configs(message, name)
        else:
            await edit_or_answer(menu_msg, f"❌ Ошибка:\n{escape(result)}",
                                 reply_markup=kb_main())


@dp.message(UserForm.waiting_add_name)
async def name_for_add(message: Message, state: FSMContext) -> None:
    await handle_name(message, state, "add")


@dp.message(UserForm.waiting_remove_name)
async def name_for_remove(message: Message, state: FSMContext) -> None:
    await handle_name(message, state, "remove")


# Команды с аргументами: /add имя, /remove имя, /stats, /restart
@dp.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if operation_lock.locked():
        await message.answer("Подожди, выполняется другая операция.")
        return
    async with operation_lock:
        ok, result = await run_script("stats")
        if ok:
            try:
                users = json.loads(result)
            except json.JSONDecodeError:
                users = None
            if isinstance(users, list):
                await message.answer(format_stats(users) or "Нет данных.",
                                     reply_markup=kb_main())
                return
        await message.answer(result or "Нет данных.", reply_markup=kb_main())


@dp.message(Command("restart"))
async def cmd_restart(message: Message) -> None:
    await message.answer("⚠️ <b>Перезагрузить AmneziaWG?</b>",
                         reply_markup=kb_confirm_restart())


async def _cmd_with_name(message: Message, command: CommandObject,
                         state: FSMContext, action: str) -> None:
    """Общая логика для /add и /remove: с аргументом — сразу, без — спросить."""
    if not command.args:
        await state.set_state(
            UserForm.waiting_add_name if action == "add"
            else UserForm.waiting_remove_name)
        prompt = ("➕ Пришли <b>имя</b> нового пользователя одним сообщением:"
                  if action == "add"
                  else "🗑 Пришли <b>имя</b> пользователя для удаления:")
        await message.answer(prompt, reply_markup=kb_cancel())
        return
    await handle_name(message, state, action)


@dp.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject, state: FSMContext) -> None:
    await _cmd_with_name(message, command, state, "add")


@dp.message(Command("remove"))
async def cmd_remove(message: Message, command: CommandObject, state: FSMContext) -> None:
    await _cmd_with_name(message, command, state, "remove")


@dp.message()
async def fallback(message: Message) -> None:
    await message.answer("Выбери действие:", reply_markup=kb_main())


async def main() -> None:
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await bot.send_message(USER_ID, "✅ Бот AmneziaWG запущен.")
    except TelegramAPIError as ex:
        print(f"[awg-bot] Не удалось уведомить о старте: {ex}")
    print(f"[awg-bot] RUN (aiogram): {USER_ID}")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        # Гарантированно закрываем сессию при любом выходе.
        asyncio.run(bot.session.close())
