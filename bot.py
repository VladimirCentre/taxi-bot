import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from dotenv import load_dotenv

load_dotenv()

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не найден BOT_TOKEN в .env файле!")

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
                created_at TEXT NOT NULL
            )
        """)
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


async def start_session(user_id: int):
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO sessions (user_id, start_time) VALUES (?, ?)",
            (user_id, now)
        )
        await db.commit()


async def end_session(user_id: int) -> Optional[float]:
    session = await get_open_session(user_id)
    if not session:
        return None
    start = datetime.fromisoformat(session["start_time"])
    end = datetime.now()
    hours = round((end - start).total_seconds() / 3600, 2)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET end_time = ?, hours = ? WHERE id = ?",
            (end.isoformat(), hours, session["id"])
        )
        await db.commit()
    return hours


async def add_income(user_id: int, amount: float, note: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO incomes (user_id, amount, note, created_at) VALUES (?, ?, ?, ?)",
            (user_id, amount, note, datetime.now().isoformat())
        )
        await db.commit()


async def add_expense(user_id: int, category: str, amount: float, note: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO expenses (user_id, category, amount, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, category, amount, note, datetime.now().isoformat())
        )
        await db.commit()


def parse_period(period: str):
    now = datetime.now()
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # all
        start = datetime(2000, 1, 1)
    return start.isoformat()


async def get_stats(user_id: int, period: str = "all"):
    start_iso = parse_period(period)
    async with aiosqlite.connect(DB_PATH) as db:
        # Часы
        cursor = await db.execute(
            "SELECT COALESCE(SUM(hours), 0) FROM sessions WHERE user_id = ? AND end_time IS NOT NULL AND start_time >= ?",
            (user_id, start_iso)
        )
        hours = (await cursor.fetchone())[0] or 0

        # Доход
        cursor = await db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM incomes WHERE user_id = ? AND created_at >= ?",
            (user_id, start_iso)
        )
        income = (await cursor.fetchone())[0] or 0

        # Расходы по категориям
        cursor = await db.execute(
            "SELECT category, COALESCE(SUM(amount), 0) FROM expenses WHERE user_id = ? AND created_at >= ? GROUP BY category",
            (user_id, start_iso)
        )
        expenses_rows = await cursor.fetchall()
        expenses = {row[0]: row[1] for row in expenses_rows}
        total_expenses = sum(expenses.values())

        # Дни работы
        cursor = await db.execute(
            "SELECT COUNT(DISTINCT date(start_time)) FROM sessions WHERE user_id = ? AND end_time IS NOT NULL AND start_time >= ?",
            (user_id, start_iso)
        )
        days = (await cursor.fetchone())[0] or 0

    return {
        "hours": round(hours, 2),
        "income": round(income, 2),
        "expenses": expenses,
        "total_expenses": round(total_expenses, 2),
        "profit": round(income - total_expenses, 2),
        "days": days
    }


async def get_worked_days(user_id: int, limit: int = 30):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT date(start_time) as day,
                   SUM(hours) as hours,
                   (SELECT COALESCE(SUM(amount), 0) FROM incomes i WHERE i.user_id = s.user_id AND date(i.created_at) = date(s.start_time)) as income
            FROM sessions s
            WHERE user_id = ? AND end_time IS NOT NULL
            GROUP BY date(start_time)
            ORDER BY day DESC
            LIMIT ?
            """,
            (user_id, limit)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


# ===================== FSM =====================
class AddIncome(StatesGroup):
    amount = State()
    note = State()


class AddExpense(StatesGroup):
    category = State()
    amount = State()
    note = State()


# ===================== КЛАВИАТУРЫ =====================
def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="▶️ Начал работу"), KeyboardButton(text="⏹️ Закончил работу")],
            [KeyboardButton(text="💰 Добавить доход"), KeyboardButton(text="⛽ Добавить расход")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📅 Отработанные дни")],
        ],
        resize_keyboard=True
    )


def expense_categories_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⛽ Бензин", callback_data="exp_бензин")],
            [InlineKeyboardButton(text="🔥 Газ", callback_data="exp_газ")],
            [InlineKeyboardButton(text="🚗 Аренда авто", callback_data="exp_аренда")],
            [InlineKeyboardButton(text="📦 Другое", callback_data="exp_другое")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
        ]
    )


def stats_period_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="stats_today"),
                InlineKeyboardButton(text="Неделя", callback_data="stats_week"),
            ],
            [
                InlineKeyboardButton(text="Месяц", callback_data="stats_month"),
                InlineKeyboardButton(text="Всё время", callback_data="stats_all"),
            ],
        ]
    )


# ===================== ХЕНДЛЕРЫ =====================
router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот для учёта работы в такси.\n\n"
        "Что я умею:\n"
        "• Считать часы работы\n"
        "• Записывать доход\n"
        "• Учитывать расходы (бензин, газ, аренда)\n"
        "• Показывать статистику и отработанные дни\n\n"
        "Выбери действие кнопками ниже:",
        reply_markup=main_keyboard()
    )


@router.message(F.text == "▶️ Начал работу")
async def start_work(message: Message):
    open_sess = await get_open_session(message.from_user.id)
    if open_sess:
        start = datetime.fromisoformat(open_sess["start_time"])
        await message.answer(
            f"У тебя уже открыта смена с {start.strftime('%d.%m.%Y %H:%M')}.\n"
            "Сначала закончи её кнопкой «⏹️ Закончил работу»."
        )
        return
    await start_session(message.from_user.id)
    await message.answer("✅ Смена начата! Удачной работы.")


@router.message(F.text == "⏹️ Закончил работу")
async def end_work(message: Message):
    hours = await end_session(message.from_user.id)
    if hours is None:
        await message.answer("Нет открытой смены. Сначала нажми «▶️ Начал работу».")
        return
    await message.answer(f"✅ Смена закончена.\nОтработано: **{hours} ч.**", parse_mode="Markdown")


@router.message(F.text == "💰 Добавить доход")
async def add_income_start(message: Message, state: FSMContext):
    await state.set_state(AddIncome.amount)
    await message.answer("Введи сумму дохода (только число, например 3500):")


@router.message(AddIncome.amount)
async def add_income_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", ".").replace(" ", ""))
        if amount <= 0:
            raise ValueError
        await state.update_data(amount=amount)
        await state.set_state(AddIncome.note)
        await message.answer("Можешь добавить комментарий (или просто отправь «-» чтобы пропустить):")
    except ValueError:
        await message.answer("Некорректная сумма. Введи число, например: 2500")


@router.message(AddIncome.note)
async def add_income_note(message: Message, state: FSMContext):
    data = await state.get_data()
    note = message.text if message.text != "-" else ""
    await add_income(message.from_user.id, data["amount"], note)
    await state.clear()
    await message.answer(f"✅ Доход {data['amount']} ₽ добавлен!", reply_markup=main_keyboard())


@router.message(F.text == "⛽ Добавить расход")
async def add_expense_start(message: Message, state: FSMContext):
    await state.set_state(AddExpense.category)
    await message.answer("Выбери категорию расхода:", reply_markup=expense_categories_kb())


@router.callback_query(F.data.startswith("exp_"))
async def expense_category_selected(callback: CallbackQuery, state: FSMContext):
    category = callback.data.replace("exp_", "")
    await state.update_data(category=category)
    await state.set_state(AddExpense.amount)
    await callback.message.edit_text(f"Категория: {category}\nВведи сумму расхода:")
    await callback.answer()


@router.message(AddExpense.amount)
async def add_expense_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", ".").replace(" ", ""))
        if amount <= 0:
            raise ValueError
        await state.update_data(amount=amount)
        await state.set_state(AddExpense.note)
        await message.answer("Комментарий (или «-» чтобы пропустить):")
    except ValueError:
        await message.answer("Некорректная сумма. Введи число.")


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


@router.message(F.text == "📊 Статистика")
async def show_stats_menu(message: Message):
    await message.answer("Выбери период:", reply_markup=stats_period_kb())


@router.callback_query(F.data.startswith("stats_"))
async def show_stats(callback: CallbackQuery):
    period = callback.data.replace("stats_", "")
    period_names = {
        "today": "сегодня",
        "week": "за неделю",
        "month": "за месяц",
        "all": "за всё время"
    }
    stats = await get_stats(callback.from_user.id, period)

    exp_lines = []
    for cat, val in stats["expenses"].items():
        exp_lines.append(f"  • {cat}: {val} ₽")
    exp_text = "\n".join(exp_lines) if exp_lines else "  нет расходов"

    text = (
        f"📊 **Статистика {period_names.get(period, period)}**\n\n"
        f"⏱ Часов отработано: **{stats['hours']}**\n"
        f"📅 Дней работы: **{stats['days']}**\n\n"
        f"💰 Доход: **{stats['income']} ₽**\n"
        f"💸 Расходы:\n{exp_text}\n"
        f"💸 Всего расходов: **{stats['total_expenses']} ₽**\n\n"
        f"✅ Чистыми: **{stats['profit']} ₽**"
    )
    await callback.message.edit_text(text, parse_mode="Markdown")
    await callback.answer()


@router.message(F.text == "📅 Отработанные дни")
async def show_days(message: Message):
    days = await get_worked_days(message.from_user.id, 20)
    if not days:
        await message.answer("Пока нет завершённых смен.")
        return
    lines = ["📅 Последние отработанные дни:\n"]
    for d in days:
        lines.append(f"• {d['day']}: {d['hours']:.1f} ч. | доход {d['income']:.0f} ₽")
    await message.answer("\n".join(lines))


@router.callback_query(F.data == "cancel")
async def cancel_action(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Действие отменено.")
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
