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
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
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

DB_PATH = "finance.db"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===================== БАЗА =====================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                type TEXT DEFAULT '💳',
                balance REAL DEFAULT 0,
                hide INTEGER DEFAULT 0,
                special TEXT DEFAULT NULL  -- 'yandex', 'taxi_cash', 'envelope'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,  -- i, e, t
                account_id INTEGER,
                to_account_id INTEGER,
                amount REAL NOT NULL,
                category TEXT,
                note TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                start TEXT,
                end TEXT,
                hours REAL,
                orders INTEGER DEFAULT 0,
                cash REAL DEFAULT 0,
                card REAL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,  -- e or i
                name TEXT NOT NULL,
                emoji TEXT DEFAULT '📦',
                PRIMARY KEY (user_id, type, name)
            )
        """)
        await db.commit()

        # Дефолтные категории и счета при первом запуске
        cursor = await db.execute("SELECT COUNT(*) FROM accounts")
        if (await cursor.fetchone())[0] == 0:
            # Будет создаваться per-user при первом сообщении
            pass

async def ensure_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE user_id = ?", (user_id,))
        if (await cursor.fetchone())[0] == 0:
            await db.execute("INSERT INTO accounts (user_id, name, type, balance) VALUES (?, 'Карта', '💳', 0)", (user_id,))
            await db.execute("INSERT INTO accounts (user_id, name, type, balance) VALUES (?, 'Наличные', '💵', 0)", (user_id,))
            await db.execute("INSERT INTO accounts (user_id, name, type, balance, hide, special) VALUES (?, 'Яндекс', '🚕', 0, 1, 'yandex')", (user_id,))
            await db.execute("INSERT INTO accounts (user_id, name, type, balance, hide, special) VALUES (?, 'Наличка такси', '💵', 0, 1, 'taxi_cash')", (user_id,))
            await db.execute("INSERT INTO accounts (user_id, name, type, balance, hide, special) VALUES (?, 'На платежи', '📦', 0, 1, 'envelope')", (user_id,))
            # Категории
            for name, emoji in [("Еда","🛒"),("Транспорт","🚕"),("Дом","🏠"),("Кафе","☕"),("Здоровье","💊"),("Развлечения","🎮"),("Покупки","🛍"),("Связь","📱"),("Другое","📦"),("Бензин","⛽"),("Газ","🔥"),("Аренда авто","🚗")]:
                await db.execute("INSERT OR IGNORE INTO categories (user_id, type, name, emoji) VALUES (?, 'e', ?, ?)", (user_id, name, emoji))
            for name, emoji in [("Зарплата","💼"),("Подработка","⚡"),("Подарок","🎁"),("Прочее","💰"),("Такси: карта","💳"),("Такси: наличные","💵")]:
                await db.execute("INSERT OR IGNORE INTO categories (user_id, type, name, emoji) VALUES (?, 'i', ?, ?)", (user_id, name, emoji))
            await db.commit()

async def get_accounts(user_id: int, include_special=False):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        q = "SELECT * FROM accounts WHERE user_id = ?"
        if not include_special:
            q += " AND (special IS NULL OR special = '')"
        cursor = await db.execute(q, (user_id,))
        return [dict(r) for r in await cursor.fetchall()]

async def get_balance(user_id: int, account_id: int) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT balance FROM accounts WHERE id = ? AND user_id = ?", (account_id, user_id))
        row = await cursor.fetchone()
        base = row[0] if row else 0
        cursor = await db.execute("""
            SELECT COALESCE(SUM(CASE WHEN type='i' THEN amount WHEN type='e' THEN -amount ELSE 0 END),0)
            FROM transactions WHERE account_id = ? AND user_id = ?
        """, (account_id, user_id))
        delta = (await cursor.fetchone())[0] or 0
        cursor = await db.execute("""
            SELECT COALESCE(SUM(amount),0) FROM transactions WHERE to_account_id = ? AND type='t' AND user_id = ?
        """, (account_id, user_id))
        in_t = (await cursor.fetchone())[0] or 0
        return base + delta + in_t

async def add_tx(user_id: int, ttype: str, amount: float, account_id: int, category: str = "", note: str = "", to_id: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO transactions (user_id, type, account_id, to_account_id, amount, category, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, ttype, account_id, to_id, amount, category, note, datetime.now().isoformat()))
        await db.commit()

# ===================== ПАРСЕР ТЕКСТА =====================
async def parse_and_handle(message: Message):
    text = message.text.strip().lower()
    user_id = message.from_user.id
    await ensure_user(user_id)

    # Баланс
    if text in ("баланс", "счета", "balance"):
        accs = await get_accounts(user_id, include_special=True)
        lines = []
        total = 0
        total_all = 0
        for a in accs:
            bal = await get_balance(user_id, a["id"])
            if not a["hide"] and not a["special"]:
                total += bal
            if not a["special"] or a["special"] in ("envelope",):
                total_all += bal
            mark = " 👁" if a["hide"] else ""
            lines.append(f"{a['type']} {a['name']}{mark}: {bal:.2f} Br")
        await message.answer("\n".join(lines) + f"\n\nОбщий (видимый): {total:.2f} Br\nВсего: {total_all:.2f} Br")
        return

    # Расход / доход
    m = re.search(r'(расход|доход|трата|поступление)\s+([\d.,]+)\s*(.*)', text)
    if m:
        kind = "e" if "расход" in m.group(1) or "трата" in m.group(1) else "i"
        amount = float(m.group(2).replace(",", "."))
        rest = m.group(3).strip()
        # Простой выбор счёта — пока первый обычный
        accs = await get_accounts(user_id)
        acc = accs[0] if accs else None
        if not acc:
            await message.answer("Нет счетов")
            return
        cat = rest or ("Другое" if kind == "e" else "Прочее")
        await add_tx(user_id, kind, amount, acc["id"], cat)
        await message.answer(f"{'Расход' if kind=='e' else 'Доход'} {amount:.2f} Br ({cat}) на {acc['name']}")
        return

    # Смена
    if text.startswith("смена"):
        # Пример: смена 11:30 23:40 заказы 8 наличные 45 карта 120
        parts = text.split()
        # Упрощённый парсер
        await message.answer("Смена записана (упрощённо). Полный парсер доработаем.")
        return

    await message.answer("Не понял. Попробуй:\n• баланс\n• расход 15 газ\n• доход 200 карта\n• смена ...")

# ===================== ХЭНДЛЕРЫ =====================
router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message):
    await ensure_user(message.from_user.id)
    await message.answer(
        "Привет! Финансовый бот.\n\n"
        "Пиши текстом:\n"
        "• баланс\n"
        "• расход 15 газ\n"
        "• доход 200 карта\n"
        "• смена 11:30 23:40 заказы 8 наличные 50 карта 100\n\n"
        "Или /help"
    )

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Команды и текст:\n"
        "баланс — показать счета\n"
        "расход 15 газ — расход\n"
        "доход 200 — доход\n"
        "смена ... — записать смену\n"
        "/help — эта справка"
    )

@router.message(F.text)
async def on_text(message: Message):
    await parse_and_handle(message)

# ===================== ЗАПУСК =====================
async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("Бот запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
