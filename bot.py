import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TELEGRAM_TOKEN")
    or os.getenv("TOKEN")
)
if not BOT_TOKEN:
    raise ValueError("Не найден токен бота!")

DB_PATH = "taxi_data.db"

# ===================== БАЗА ДАННЫХ =====================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                hours REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS incomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL,
                income_type TEXT DEFAULT 'наличные'
            )
        """)
        # Миграция: добавить колонку если её нет
        try:
            await db.execute("ALTER TABLE incomes ADD COLUMN income_type TEXT DEFAULT 'наличные'")
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                amount REAL NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER PRIMARY KEY,
                last_receipts_reset TEXT,
                yandex_balance REAL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                order_date TEXT NOT NULL,
                count INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.commit()


async def get_setting(user_id: int, key: str, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"SELECT {key} FROM settings WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if row and row[0] is not None:
            return row[0]
        return default


async def set_setting(user_id: int, key: str, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"""INSERT INTO settings (user_id, {key}) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET {key} = excluded.{key}""",
            (user_id, value)
        )
        await db.commit()


async def get_open_session(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sessions WHERE user_id = ? AND end_time IS NULL ORDER BY id DESC LIMIT 1",
            (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def start_session(user_id: int, start_time: Optional[datetime] = None):
    dt = start_time or datetime.now()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sessions (user_id, start_time) VALUES (?, ?)",
            (user_id, dt.isoformat())
        )
        await db.commit()


async def end_session(user_id: int, end_time: Optional[datetime] = None) -> Optional[float]:
    session = await get_open_session(user_id)
    if not session:
        return None
    start = datetime.fromisoformat(session["start_time"])
    end = end_time or datetime.now()
    if end < start:
        end += timedelta(days=1)
    hours = round((end - start).total_seconds() / 3600, 2)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET end_time = ?, hours = ? WHERE id = ?",
            (end.isoformat(), hours, session["id"])
        )
        await db.commit()
    return hours


async def add_completed_shift(user_id: int, start: datetime, end: datetime) -> float:
    if end < start:
        end += timedelta(days=1)
    hours = round((end - start).total_seconds() / 3600, 2)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sessions (user_id, start_time, end_time, hours) VALUES (?, ?, ?, ?)",
            (user_id, start.isoformat(), end.isoformat(), hours)
        )
        await db.commit()
    return hours


async def add_income(user_id: int, amount: float, income_type: str = "наличные", note: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO incomes (user_id, amount, note, created_at, income_type) VALUES (?, ?, ?, ?, ?)",
            (user_id, amount, note, datetime.now().isoformat(), income_type)
        )
        # Если карта — копим в Яндекс-баланс
        if income_type == "карта":
            current = await get_setting(user_id, "yandex_balance", 0) or 0
            await set_setting(user_id, "yandex_balance", current + amount)
        await db.commit()


async def add_expense(user_id: int, category: str, amount: float, note: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO expenses (user_id, category, amount, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, category, amount, note, datetime.now().isoformat())
        )
        await db.commit()


async def delete_last_session(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM sessions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return False
        await db.execute("DELETE FROM sessions WHERE id = ?", (row[0],))
        await db.commit()
        return True


async def delete_last_income(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, income_type, amount FROM incomes WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return False
        await db.execute("DELETE FROM incomes WHERE id = ?", (row[0],))
        if row[1] == "карта":
            current = await get_setting(user_id, "yandex_balance", 0) or 0
            await set_setting(user_id, "yandex_balance", max(0, current - row[2]))
        await db.commit()
        return True


async def delete_last_expense(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM expenses WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return False
        await db.execute("DELETE FROM expenses WHERE id = ?", (row[0],))
        await db.commit()
        return True


async def get_receipts_count(user_id: int) -> int:
    """Количество закрытых смен с момента последней сдачи чеков (или с 1-го числа)."""
    last_reset = await get_setting(user_id, "last_receipts_reset")
    if last_reset:
        start = last_reset
    else:
        now = datetime.now()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id = ? AND end_time IS NOT NULL AND end_time >= ?",
            (user_id, start)
        )
        return (await cursor.fetchone())[0] or 0


async def reset_receipts(user_id: int):
    await set_setting(user_id, "last_receipts_reset", datetime.now().isoformat())


async def add_orders(user_id: int, count: int, order_date: Optional[str] = None):
    date = order_date or datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO orders (user_id, order_date, count, created_at) VALUES (?, ?, ?, ?)",
            (user_id, date, count, datetime.now().isoformat())
        )
        await db.commit()


async def get_orders_count(user_id: int, period: str = "all") -> int:
    start_iso = parse_period(period)
    start_date = start_iso[:10]  # YYYY-MM-DD
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(count), 0) FROM orders WHERE user_id = ? AND order_date >= ?",
            (user_id, start_date)
        )
        return (await cursor.fetchone())[0] or 0


async def delete_last_orders(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return False
        await db.execute("DELETE FROM orders WHERE id = ?", (row[0],))
        await db.commit()
        return True


def parse_period(period: str):
    now = datetime.now()
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = datetime(2000, 1, 1)
    return start.isoformat()


async def get_stats(user_id: int, period: str = "all"):
    start_iso = parse_period(period)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(hours), 0) FROM sessions WHERE user_id = ? AND end_time IS NOT NULL AND start_time >= ?",
            (user_id, start_iso)
        )
        hours = (await cursor.fetchone())[0] or 0

        cursor = await db.execute(
            "SELECT income_type, COALESCE(SUM(amount), 0) FROM incomes WHERE user_id = ? AND created_at >= ? GROUP BY income_type",
            (user_id, start_iso)
        )
        by_type = {row[0]: row[1] for row in await cursor.fetchall()}

        cursor = await db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM incomes WHERE user_id = ? AND created_at >= ?",
            (user_id, start_iso)
        )
        income = (await cursor.fetchone())[0] or 0

        cursor = await db.execute(
            "SELECT category, COALESCE(SUM(amount), 0) FROM expenses WHERE user_id = ? AND created_at >= ? GROUP BY category",
            (user_id, start_iso)
        )
        expenses = {row[0]: row[1] for row in await cursor.fetchall()}

        cursor = await db.execute(
            "SELECT COUNT(DISTINCT date(start_time)) FROM sessions WHERE user_id = ? AND end_time IS NOT NULL AND start_time >= ?",
            (user_id, start_iso)
        )
        days = (await cursor.fetchone())[0] or 0

        total_exp = sum(expenses.values())
        yandex = await get_setting(user_id, "yandex_balance", 0) or 0
        orders = await get_orders_count(user_id, period)

        avg_income = round(income / orders, 2) if orders > 0 else 0
        avg_profit = round((income - total_exp) / orders, 2) if orders > 0 else 0

        return {
            "hours": round(hours, 2),
            "income": round(income, 2),
            "by_type": by_type,
            "expenses": expenses,
            "total_expenses": round(total_exp, 2),
            "profit": round(income - total_exp, 2),
            "days": days,
            "yandex": round(yandex, 2),
            "receipts": await get_receipts_count(user_id),
            "orders": orders,
            "avg_income_per_order": avg_income,
            "avg_profit_per_order": avg_profit,
        }


async def get_worked_days(user_id: int, limit: int = 20):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT date(start_time) as day, SUM(hours) as hours,
                   (SELECT COALESCE(SUM(amount),0) FROM incomes WHERE user_id = ? AND date(created_at) = day) as income
            FROM sessions
            WHERE user_id = ? AND end_time IS NOT NULL
            GROUP BY day
            ORDER BY day DESC
            LIMIT ?
            """,
            (user_id, user_id, limit)
        )
        rows = await cursor.fetchall()
        return [{"day": r[0], "hours": r[1] or 0, "income": r[2] or 0} for r in rows]


# ===================== FSM =====================
class AddIncome(StatesGroup):
    income_type = State()
    amount = State()
    note = State()


class AddExpense(StatesGroup):
    category = State()
    amount = State()
    note = State()


class AddPastShift(StatesGroup):
    start = State()
    end = State()


class WithdrawYandex(StatesGroup):
    amount = State()


class AddOrders(StatesGroup):
    count = State()
    date = State()


# ===================== КЛАВИАТУРЫ =====================
def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="▶️ Начал работу"), KeyboardButton(text="⏹️ Закончил работу")],
            [KeyboardButton(text="📝 Добавить смену"), KeyboardButton(text="🗑️ Удалить последнюю смену")],
            [KeyboardButton(text="💰 Добавить доход"), KeyboardButton(text="⛽ Добавить расход")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📅 Отработанные дни")],
            [KeyboardButton(text="🧾 Чеки к сдаче"), KeyboardButton(text="💳 Яндекс баланс")],
            [KeyboardButton(text="📦 Добавить заказы"), KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True
    )


def income_type_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 Наличные", callback_data="inc_наличные")],
        [InlineKeyboardButton(text="🙏 Чаевые", callback_data="inc_чаевые")],
        [InlineKeyboardButton(text="💳 Карта (Яндекс)", callback_data="inc_карта")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ])


def expense_categories_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛽ Бензин", callback_data="exp_бензин")],
        [InlineKeyboardButton(text="🔥 Газ", callback_data="exp_газ")],
        [InlineKeyboardButton(text="🚗 Аренда авто", callback_data="exp_аренда")],
        [InlineKeyboardButton(text="📦 Другое", callback_data="exp_другое")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ])


def stats_period_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Сегодня", callback_data="stats_today"),
            InlineKeyboardButton(text="Неделя", callback_data="stats_week"),
        ],
        [
            InlineKeyboardButton(text="Месяц", callback_data="stats_month"),
            InlineKeyboardButton(text="Всё время", callback_data="stats_all"),
        ],
    ])


def yandex_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Вывел деньги", callback_data="yandex_withdraw")],
        [InlineKeyboardButton(text="➖ Записать комиссию", callback_data="yandex_commission")],
    ])


# ===================== ПАРСЕР ВРЕМЕНИ =====================
def parse_time_str(s: str) -> Optional[datetime]:
    s = s.strip().replace(".", ":").replace("-", ":")
    now = datetime.now()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            t = datetime.strptime(s, fmt)
            return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        except ValueError:
            continue
    return None


def parse_shift_text(text: str):
    times = re.findall(r"(\d{1,2}[:.]\d{2})", text)
    if len(times) >= 2:
        start = parse_time_str(times[0])
        end = parse_time_str(times[1])
        if start and end:
            return start, end
    return None, None


# ===================== ХЕНДЛЕРЫ =====================
router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Бот для учёта работы в такси.\n\n"
        "**Новое:**\n"
        "• Чеки к сдаче 1-го числа\n"
        "• Доход: наличные / чаевые / карта\n"
        "• Яндекс-баланс (копится карта)\n\n"
        "Кнопки или текст. Напиши /help",
        reply_markup=main_keyboard(),
        parse_mode="Markdown"
    )


@router.message(Command("help"))
@router.message(F.text == "❓ Помощь")
async def cmd_help(message: Message):
    text = (
        "📖 **Как пользоваться**\n\n"
        "**Смены / чеки:**\n"
        "• Начал / закончил работу = 1 чек\n"
        "• Текстом: `смена 21:49 00:37`\n"
        "• «🧾 Чеки к сдаче» — сколько нужно сдать\n"
        "• После сдачи нажми «Сдал чеки»\n\n"
        "**Доход:**\n"
        "• Кнопка → выбери тип (наличные/чаевые/карта)\n"
        "• Или: `наличные 3000`, `чаевые 200`, `карта 1500`\n\n"
        "**Яндекс:**\n"
        "• Карта копится в «Яндекс баланс»\n"
        "• «Вывел деньги» — когда пришли на карту\n\n"
        "**Заказы:**\n"
        "• Кнопка «📦 Добавить заказы» или `заказы 15`\n"
        "• Можно с датой: `12 2026-10-28`\n"
        "• В статистике будет средняя с 1 заказа\n\n"
        "**Расход:** `бензин 1200` / `газ 700` / `аренда 2000`\n"
        "**Удалить последнее:** кнопки или /delshift /delincome /delexpense"
    )
    await message.answer(text, parse_mode="Markdown", reply_markup=main_keyboard())


# --- Смены ---
@router.message(F.text == "▶️ Начал работу")
async def start_work(message: Message):
    if await get_open_session(message.from_user.id):
        await message.answer("Смена уже открыта. Сначала закончи её.")
        return
    await start_session(message.from_user.id)
    await message.answer("✅ Смена начата!", reply_markup=main_keyboard())


@router.message(F.text == "⏹️ Закончил работу")
async def end_work(message: Message):
    hours = await end_session(message.from_user.id)
    if hours is None:
        await message.answer("Нет открытой смены.")
        return
    receipts = await get_receipts_count(message.from_user.id)
    await message.answer(
        f"✅ Смена закончена. Отработано: **{hours} ч.**\n"
        f"🧾 Чеков к сдаче: **{receipts}**",
        parse_mode="Markdown"
    )


@router.message(F.text == "📝 Добавить смену")
async def add_past_shift_start(message: Message, state: FSMContext):
    await state.set_state(AddPastShift.start)
    await message.answer("Введи время начала (например `21:49`) или сразу оба: `21:49 00:37`")


@router.message(AddPastShift.start)
async def add_past_shift_start_time(message: Message, state: FSMContext):
    start, end = parse_shift_text(message.text)
    if start and end:
        hours = await add_completed_shift(message.from_user.id, start, end)
        await state.clear()
        receipts = await get_receipts_count(message.from_user.id)
        await message.answer(
            f"✅ Смена добавлена ({hours} ч.)\n🧾 Чеков к сдаче: **{receipts}**",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )
        return
    start = parse_time_str(message.text.strip())
    if not start:
        await message.answer("Не понял время. Пример: `21:49`")
        return
    await state.update_data(start=start.isoformat())
    await state.set_state(AddPastShift.end)
    await message.answer("Введи время окончания:")


@router.message(AddPastShift.end)
async def add_past_shift_end_time(message: Message, state: FSMContext):
    end = parse_time_str(message.text.strip())
    if not end:
        await message.answer("Не понял время.")
        return
    data = await state.get_data()
    start = datetime.fromisoformat(data["start"])
    hours = await add_completed_shift(message.from_user.id, start, end)
    await state.clear()
    receipts = await get_receipts_count(message.from_user.id)
    await message.answer(
        f"✅ Смена добавлена ({hours} ч.)\n🧾 Чеков к сдаче: **{receipts}**",
        parse_mode="Markdown", reply_markup=main_keyboard()
    )


@router.message(F.text.regexp(r"(?i).*(смена|отработал|смену).*"))
async def text_add_shift(message: Message):
    start, end = parse_shift_text(message.text)
    if not start or not end:
        await message.answer("Пример: `смена 21:49 00:37`")
        return
    hours = await add_completed_shift(message.from_user.id, start, end)
    receipts = await get_receipts_count(message.from_user.id)
    await message.answer(
        f"✅ Смена: {start.strftime('%H:%M')} → {end.strftime('%H:%M')} ({hours} ч.)\n"
        f"🧾 Чеков к сдаче: **{receipts}**",
        parse_mode="Markdown"
    )


@router.message(F.text == "🗑️ Удалить последнюю смену")
@router.message(Command("delshift"))
async def del_last_shift(message: Message):
    if await delete_last_session(message.from_user.id):
        await message.answer("✅ Последняя смена удалена.")
    else:
        await message.answer("Нет смен.")


# --- Чеки ---
@router.message(F.text == "🧾 Чеки к сдаче")
async def show_receipts(message: Message):
    count = await get_receipts_count(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сдал чеки (обнулить)", callback_data="receipts_reset")]
    ])
    await message.answer(
        f"🧾 **Чеков к сдаче:** {count}\n\n"
        f"Каждая закрытая смена = 1 чек.\n"
        f"1-го числа сдаёшь всю стопку и нажимаешь кнопку ниже.",
        parse_mode="Markdown",
        reply_markup=kb
    )


@router.callback_query(F.data == "receipts_reset")
async def receipts_reset(callback: CallbackQuery):
    await reset_receipts(callback.from_user.id)
    await callback.message.edit_text("✅ Чеки сданы. Счётчик обнулён.")
    await callback.answer()


# --- Доход ---
@router.message(F.text == "💰 Добавить доход")
async def add_income_start(message: Message, state: FSMContext):
    await state.set_state(AddIncome.income_type)
    await message.answer("Какой тип поступления?", reply_markup=income_type_kb())


@router.callback_query(F.data.startswith("inc_"))
async def income_type_selected(callback: CallbackQuery, state: FSMContext):
    income_type = callback.data.replace("inc_", "")
    await state.update_data(income_type=income_type)
    await state.set_state(AddIncome.amount)
    await callback.message.edit_text(f"Тип: {income_type}\nВведи сумму:")
    await callback.answer()


@router.message(AddIncome.amount)
async def add_income_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", ".").replace(" ", ""))
        if amount <= 0:
            raise ValueError
        await state.update_data(amount=amount)
        await state.set_state(AddIncome.note)
        await message.answer("Комментарий (или «-»):")
    except ValueError:
        await message.answer("Введи число.")


@router.message(AddIncome.note)
async def add_income_note(message: Message, state: FSMContext):
    data = await state.get_data()
    note = message.text if message.text != "-" else ""
    await add_income(message.from_user.id, data["amount"], data["income_type"], note)
    await state.clear()
    extra = ""
    if data["income_type"] == "карта":
        y = await get_setting(message.from_user.id, "yandex_balance", 0) or 0
        extra = f"\n💳 Яндекс баланс: {y:.0f} ₽"
    await message.answer(
        f"✅ {data['income_type'].capitalize()} {data['amount']} ₽ добавлены!{extra}",
        reply_markup=main_keyboard()
    )


@router.message(F.text.regexp(r"(?i)^(наличные|чаевые|карта)\s+[\d\s.,]+"))
async def text_add_income(message: Message):
    text = message.text.lower()
    income_type = "наличные"
    for t in ["наличные", "чаевые", "карта"]:
        if text.startswith(t):
            income_type = t
            break
    nums = re.findall(r"[\d]+(?:[.,]\d+)?", message.text)
    if not nums:
        await message.answer("Пример: `наличные 3000`")
        return
    amount = float(nums[0].replace(",", "."))
    await add_income(message.from_user.id, amount, income_type)
    extra = ""
    if income_type == "карта":
        y = await get_setting(message.from_user.id, "yandex_balance", 0) or 0
        extra = f"\n💳 Яндекс: {y:.0f} ₽"
    await message.answer(f"✅ {income_type.capitalize()} {amount} ₽{extra}")


@router.message(Command("delincome"))
async def del_last_inc(message: Message):
    if await delete_last_income(message.from_user.id):
        await message.answer("✅ Последний доход удалён.")
    else:
        await message.answer("Нет доходов.")


# --- Яндекс ---
@router.message(F.text == "💳 Яндекс баланс")
async def show_yandex(message: Message):
    bal = await get_setting(message.from_user.id, "yandex_balance", 0) or 0
    await message.answer(
        f"💳 **Яндекс баланс:** {bal:.0f} ₽\n\n"
        f"Сюда копятся деньги «по карте».\n"
        f"Когда Яндекс выведет на карту — нажми «Вывел деньги».",
        parse_mode="Markdown",
        reply_markup=yandex_kb()
    )


@router.callback_query(F.data == "yandex_withdraw")
async def yandex_withdraw_start(callback: CallbackQuery, state: FSMContext):
    bal = await get_setting(callback.from_user.id, "yandex_balance", 0) or 0
    if bal <= 0:
        await callback.answer("Баланс пустой", show_alert=True)
        return
    await state.set_state(WithdrawYandex.amount)
    await callback.message.answer(f"Сколько вывел? (сейчас на балансе {bal:.0f} ₽)\nИли напиши «всё»")
    await callback.answer()


@router.message(WithdrawYandex.amount)
async def yandex_withdraw_amount(message: Message, state: FSMContext):
    bal = await get_setting(message.from_user.id, "yandex_balance", 0) or 0
    text = message.text.strip().lower()
    if text in ("всё", "все", "all"):
        amount = bal
    else:
        try:
            amount = float(text.replace(",", "."))
        except ValueError:
            await message.answer("Введи число или «всё»")
            return
    if amount <= 0 or amount > bal:
        await message.answer(f"Сумма должна быть от 1 до {bal:.0f}")
        return
    await set_setting(message.from_user.id, "yandex_balance", bal - amount)
    await state.clear()
    await message.answer(
        f"✅ Вывел {amount:.0f} ₽ с Яндекса.\n"
        f"Остаток на Яндексе: {bal - amount:.0f} ₽",
        reply_markup=main_keyboard()
    )


@router.callback_query(F.data == "yandex_commission")
async def yandex_commission(callback: CallbackQuery):
    await callback.message.answer(
        "Запиши комиссию как обычный расход:\n"
        "`другое 150` или кнопка «Добавить расход» → Другое"
    )
    await callback.answer()


# --- Расходы ---
@router.message(F.text == "⛽ Добавить расход")
async def add_expense_start(message: Message, state: FSMContext):
    await state.set_state(AddExpense.category)
    await message.answer("Категория:", reply_markup=expense_categories_kb())


@router.callback_query(F.data.startswith("exp_"))
async def expense_category_selected(callback: CallbackQuery, state: FSMContext):
    category = callback.data.replace("exp_", "")
    await state.update_data(category=category)
    await state.set_state(AddExpense.amount)
    await callback.message.edit_text(f"Категория: {category}\nСумма:")
    await callback.answer()


@router.message(AddExpense.amount)
async def add_expense_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", ".").replace(" ", ""))
        if amount <= 0:
            raise ValueError
        await state.update_data(amount=amount)
        await state.set_state(AddExpense.note)
        await message.answer("Комментарий (или «-»):")
    except ValueError:
        await message.answer("Введи число.")


@router.message(AddExpense.note)
async def add_expense_note(message: Message, state: FSMContext):
    data = await state.get_data()
    note = message.text if message.text != "-" else ""
    await add_expense(message.from_user.id, data["category"], data["amount"], note)
    await state.clear()
    await message.answer(
        f"✅ Расход {data['amount']} ₽ ({data['category']}) добавлен!",
        reply_markup=main_keyboard()
    )


@router.message(F.text.regexp(r"(?i)^(бензин|газ|аренда|другое)\s+[\d\s.,]+"))
async def text_add_expense(message: Message):
    text = message.text.lower()
    category = "другое"
    for cat in ["бензин", "газ", "аренда", "другое"]:
        if text.startswith(cat):
            category = cat
            break
    nums = re.findall(r"[\d]+(?:[.,]\d+)?", message.text)
    if not nums:
        return
    amount = float(nums[0].replace(",", "."))
    await add_expense(message.from_user.id, category, amount)
    await message.answer(f"✅ Расход {amount} ₽ ({category})")


@router.message(Command("delexpense"))
async def del_last_exp(message: Message):
    if await delete_last_expense(message.from_user.id):
        await message.answer("✅ Последний расход удалён.")
    else:
        await message.answer("Нет расходов.")


@router.message(F.text == "📦 Добавить заказы")
async def add_orders_start(message: Message, state: FSMContext):
    await state.set_state(AddOrders.count)
    await message.answer(
        "Сколько заказов сделал?\n"
        "Просто число (за сегодня) или `15 2026-10-29` (число и дата)."
    )


@router.message(AddOrders.count)
async def add_orders_count(message: Message, state: FSMContext):
    text = message.text.strip()
    parts = text.split()
    try:
        count = int(parts[0])
        if count <= 0:
            raise ValueError
        order_date = parts[1] if len(parts) > 1 else None
        if order_date:
            # validate date
            datetime.strptime(order_date, "%Y-%m-%d")
        await add_orders(message.from_user.id, count, order_date)
        await state.clear()
        date_str = order_date or "сегодня"
        await message.answer(
            f"✅ Добавлено **{count}** заказов за {date_str}.",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )
    except ValueError:
        await message.answer("Пример: `18` или `12 2026-10-28`")


@router.message(F.text.regexp(r"(?i)^заказы?\s+\d+"))
async def text_add_orders(message: Message):
    nums = re.findall(r"\d+", message.text)
    if not nums:
        return
    count = int(nums[0])
    await add_orders(message.from_user.id, count)
    await message.answer(f"✅ Добавлено {count} заказов за сегодня.")


@router.message(Command("delorders"))
async def del_last_orders(message: Message):
    if await delete_last_orders(message.from_user.id):
        await message.answer("✅ Последняя запись заказов удалена.")
    else:
        await message.answer("Нет записей заказов.")


# --- Статистика и дни ---
@router.message(F.text == "📊 Статистика")
async def show_stats_menu(message: Message):
    await message.answer("Период:", reply_markup=stats_period_kb())


@router.callback_query(F.data.startswith("stats_"))
async def show_stats(callback: CallbackQuery):
    period = callback.data.replace("stats_", "")
    names = {"today": "сегодня", "week": "за неделю", "month": "за месяц", "all": "за всё время"}
    s = await get_stats(callback.from_user.id, period)

    type_lines = []
    for t, v in s["by_type"].items():
        type_lines.append(f"  • {t}: {v:.0f} ₽")
    types_text = "\n".join(type_lines) if type_lines else "  нет"

    exp_lines = [f"  • {c}: {v:.0f} ₽" for c, v in s["expenses"].items()]
    exp_text = "\n".join(exp_lines) if exp_lines else "  нет"

    text = (
        f"📊 **Статистика {names.get(period, period)}**\n\n"
        f"⏱ Часов: **{s['hours']}**\n"
        f"📅 Дней: **{s['days']}**\n"
        f"📦 Заказов: **{s['orders']}**\n"
        f"🧾 Чеков к сдаче: **{s['receipts']}**\n\n"
        f"💰 Доход всего: **{s['income']:.0f} ₽**\n{types_text}\n\n"
        f"💸 Расходы:\n{exp_text}\n"
        f"💸 Всего расходов: **{s['total_expenses']:.0f} ₽**\n\n"
        f"💳 Яндекс баланс: **{s['yandex']:.0f} ₽**\n"
        f"✅ Чистыми: **{s['profit']:.0f} ₽**\n\n"
        f"📈 Средний доход с 1 заказа: **{s['avg_income_per_order']:.0f} ₽**\n"
        f"📈 Средняя чистая с 1 заказа: **{s['avg_profit_per_order']:.0f} ₽**"
    )
    await callback.message.edit_text(text, parse_mode="Markdown")
    await callback.answer()


@router.message(F.text == "📅 Отработанные дни")
async def show_days(message: Message):
    days = await get_worked_days(message.from_user.id, 20)
    if not days:
        await message.answer("Пока нет смен.")
        return
    lines = ["📅 Последние дни:\n"]
    for d in days:
        lines.append(f"• {d['day']}: {d['hours']:.1f} ч. | {d['income']:.0f} ₽")
    await message.answer("\n".join(lines))


@router.callback_query(F.data == "cancel")
async def cancel_action(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.answer()


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    await show_stats_menu(message)


@router.message(Command("days"))
async def cmd_days(message: Message):
    await show_days(message)


# ===================== ЗАПУСК =====================
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    print("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
