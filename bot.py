import asyncio
import logging
import os
import sqlite3
from datetime import datetime
from contextlib import closing

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

# ─── CONFIG ────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "ЗАМЕНИ_НА_ТОКЕН_ОТ_BOTFATHER")
# Список админов: через запятую, например: 123456789,987654321
_ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "0"))
ADMIN_IDS = [int(x.strip()) for x in _ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]
CHANNEL_ID = os.getenv("CHANNEL_ID", "0")           # ID закрытого канала/группы

if BOT_TOKEN == "ЗАМЕНИ_НА_ТОКЕН_ОТ_BOTFATHER":
    raise ValueError("Укажи BOT_TOKEN в переменных окружения или прямо в коде!")
if not ADMIN_IDS:
    raise ValueError("Укажи ADMIN_ID или ADMIN_IDS (Telegram ID админов через запятую)!")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())

# ─── DATABASE ──────────────────────────────────────────────────
DB_PATH = "users.db"


def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                phone TEXT,
                child_name TEXT,
                child_age INTEGER,
                marketing_consent INTEGER DEFAULT 0,
                status TEXT DEFAULT 'new',
                created_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                photo_file_id TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
        """)
        conn.commit()


def save_user(user_id, username, full_name, phone, child_name, child_age, marketing_consent=0):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO users
            (user_id, username, full_name, phone, child_name, child_age, marketing_consent, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'lead', ?)
        """, (user_id, username, full_name, phone, child_name, child_age, marketing_consent, datetime.now().isoformat()))
        conn.commit()


def get_user(user_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return c.fetchone()


def update_marketing_consent(user_id, consent_value):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE users SET marketing_consent=? WHERE user_id=?",
            (consent_value, user_id),
        )
        conn.commit()
        return c.rowcount


# ─── KEYBOARDS ─────────────────────────────────────────────────
def main_menu_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📋 Оставить заявку")]],
        resize_keyboard=True,
    )


def share_phone_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def marketing_consent_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, согласен", callback_data="consent:yes")],
        [InlineKeyboardButton(text="❌ Нет, не согласен", callback_data="consent:no")],
    ])


def payment_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Я оплатил", callback_data="pay")],
        [InlineKeyboardButton(text="❓ Задать вопрос", callback_data="question")],
    ])


def admin_approve_kb(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve:{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{user_id}"),
        ]
    ])


# ─── FSM ───────────────────────────────────────────────────────
class LeadForm(StatesGroup):
    parent_name = State()
    phone = State()
    child_name = State()
    child_age = State()
    marketing_consent = State()


class PaymentForm(StatesGroup):
    screenshot = State()


# ─── HANDLERS ──────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    text = (
        "👋 Привет! Я бот «Я — Лидер» — курсы по развитию лидерских качеств для детей 7–14 лет.\n\n"
        "Здесь ты можешь:\n"
        "• Оставить заявку на курс\n"
        "• Получить доступ к материалам после оплаты\n"
        "• Задать вопрос\n\n"
        "Нажми кнопку ниже, чтобы начать 👇"
    )
    await message.answer(text, reply_markup=main_menu_kb())


@dp.message(F.text == "📋 Оставить заявку")
async def start_lead(message: Message, state: FSMContext):
    await state.set_state(LeadForm.parent_name)
    await message.answer(
        "Отлично! Давай познакомимся.\n\nКак тебя зовут? (имя родителя)",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[]], resize_keyboard=True),
    )


@dp.message(LeadForm.parent_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(parent_name=message.text)
    await state.set_state(LeadForm.phone)
    await message.answer(
        "Приятно познакомиться! Теперь отправь свой номер телефона — он нужен для связи.",
        reply_markup=share_phone_kb(),
    )


@dp.message(LeadForm.phone, F.contact)
async def process_phone(message: Message, state: FSMContext):
    phone = message.contact.phone_number
    await state.update_data(phone=phone)
    await state.set_state(LeadForm.child_name)
    await message.answer(
        "Отлично! Как зовут твоего ребёнка?",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[]], resize_keyboard=True),
    )


@dp.message(LeadForm.child_name)
async def process_child_name(message: Message, state: FSMContext):
    await state.update_data(child_name=message.text)
    await state.set_state(LeadForm.child_age)
    await message.answer("Сколько лет ребёнку? (только цифра, например: 10)")


@dp.message(LeadForm.child_age)
async def process_child_age(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введи возраст цифрами (например: 10)")
        return

    age = int(message.text)
    await state.update_data(child_age=age)
    await state.set_state(LeadForm.marketing_consent)

    consent_text = (
        "📬 <b>Согласие на получение информации</b>\n\n"
        "Мы можем присылать тебе полезные материалы, напоминания о занятиях и информацию о новых наборах «Я — Лидер».\n\n"
        "• Каналы: Telegram, WhatsApp, SMS, звонки\n"
        "• Темы: новые курсы, акции, расписание, полезные статьи\n"
        "• Отказаться можно в любой момент — просто напиши «Отписаться»\n\n"
        "<b>Ты согласен?</b>"
    )
    await message.answer(consent_text, reply_markup=marketing_consent_kb())


@dp.callback_query(F.data == "consent:yes")
async def cb_consent_yes(callback: CallbackQuery, state: FSMContext):
    await state.update_data(marketing_consent=1)
    await callback.message.edit_text("✅ Отлично! Записали.")
    await callback.answer()
    await finish_lead(callback.message, state)


@dp.callback_query(F.data == "consent:no")
async def cb_consent_no(callback: CallbackQuery, state: FSMContext):
    await state.update_data(marketing_consent=0)
    await callback.message.edit_text("Понял, будем связываться только по текущей заявке.")
    await callback.answer()
    await finish_lead(callback.message, state)


async def finish_lead(message: Message, state: FSMContext):
    data = await state.get_data()
    age = data.get("child_age", 0)
    consent = data.get("marketing_consent", 0)

    save_user(
        user_id=message.chat.id,
        username=message.chat.username,
        full_name=data.get("parent_name", ""),
        phone=data.get("phone", ""),
        child_name=data.get("child_name", ""),
        child_age=age,
        marketing_consent=consent,
    )

    await state.clear()

    text = (
        f"🎉 Спасибо, {data.get('parent_name', '')}!\n\n"
        f"Заявка принята:\n"
        f"• Ребёнок: {data.get('child_name', '')}, {age} лет\n"
        f"• Телефон: {data.get('phone', '')}\n\n"
        "Стоимость курса: уточняй у менеджера.\n"
        "После оплаты ты получишь доступ к закрытому каналу с материалами."
    )
    await message.answer(text, reply_markup=main_menu_kb())
    await message.answer(
        "Что дальше?",
        reply_markup=payment_kb(),
    )

    # Уведомляем админов
    consent_text = "✅ Согласен на получение сообщений" if consent else "❌ Не согласен на получение сообщений"
    for admin_id in ADMIN_IDS:
        await bot.send_message(
            admin_id,
            f"📥 Новая заявка!\n\n"
            f"Родитель: {data.get('parent_name', '')}\n"
            f"Телефон: {data.get('phone', '')}\n"
            f"Ребёнок: {data.get('child_name', '')}, {age} лет\n"
            f"Telegram: @{message.chat.username or 'нет'}\n"
            f"ID: {message.chat.id}\n\n"
            f"{consent_text}",
        )


# ─── PAYMENT FLOW ──────────────────────────────────────────────
@dp.callback_query(F.data == "pay")
async def cb_pay(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PaymentForm.screenshot)
    await callback.message.edit_text(
        "Отлично! Отправь, пожалуйста, скриншот об оплате (квитанцию или чек).\n\n"
        "После проверки менеджер вышлет тебе доступ к каналу."
    )
    await callback.answer()


@dp.message(PaymentForm.screenshot, F.photo)
async def process_screenshot(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    user = get_user(message.from_user.id)

    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO payments (user_id, photo_file_id, created_at) VALUES (?, ?, ?)",
            (message.from_user.id, photo_id, datetime.now().isoformat()),
        )
        conn.commit()

    await state.clear()
    await message.answer(
        "✅ Скриншот получен! Менеджер проверит оплату и вышлет доступ. Обычно это занимает 10–30 минут.",
        reply_markup=main_menu_kb(),
    )

    # Уведомляем админов
    user_info = f"@{message.from_user.username}" if message.from_user.username else f"ID {message.from_user.id}"
    consent_status = f"Получение сообщений: {'ДА' if user and user[6] else 'НЕТ'}" if user else ""
    for admin_id in ADMIN_IDS:
        await bot.send_photo(
            admin_id,
            photo=photo_id,
            caption=(
                f"💳 Новая оплата от {user_info}\n\n"
                f"Родитель: {user[2] if user else '—'}\n"
                f"Телефон: {user[3] if user else '—'}\n"
                f"Ребёнок: {user[5] if user else '—'} лет\n"
                f"{consent_status}"
            ),
            reply_markup=admin_approve_kb(message.from_user.id),
        )


@dp.message(PaymentForm.screenshot)
async def process_screenshot_invalid(message: Message):
    await message.answer("Пожалуйста, отправь фото (скриншот оплаты).")


# ─── UNSUBSCRIBE ───────────────────────────────────────────────
UNSUBSCRIBE_WORDS = {"отписаться", "отписка", "стоп", "не пишите", "не пиши", "отменить", "отказаться"}


@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message):
    await handle_unsubscribe(message)


@dp.message(F.text.lower().in_(UNSUBSCRIBE_WORDS))
async def text_unsubscribe(message: Message):
    await handle_unsubscribe(message)


async def handle_unsubscribe(message: Message):
    user = get_user(message.from_user.id)

    if not user:
        await message.answer(
            "Ты ещё не оставлял заявку в нашем боте.\n\n"
            "Если хочешь записаться на курс — нажми /start"
        )
        return

    if user[6] == 0:
        await message.answer(
            "✅ Ты уже отписан от сообщений. Мы не присылаем тебе ничего, кроме ответов по твоей заявке."
        )
        return

    rows = update_marketing_consent(message.from_user.id, 0)

    if rows > 0:
        await message.answer(
            "✅ Ты успешно отписан.\n\n"
            "Больше не будем присылать сообщения о курсах, акциях и новых наборах.\n\n"
            "Если передумаешь — просто оставь заявку снова через /start и нажми «Да, согласен»."
        )

        for admin_id in ADMIN_IDS:
            await bot.send_message(
                admin_id,
                f"🔕 Пользователь отписался от сообщений:\n\n"
                f"Имя: {user[3]}\n"
                f"Телефон: {user[4]}\n"
                f"Telegram: @{message.from_user.username or 'нет'}\n"
                f"ID: {message.from_user.id}",
            )
    else:
        await message.answer("Что-то пошло не так. Напиши менеджеру напрямую — он отпишет вручную.")


# ─── ADMIN ACTIONS ─────────────────────────────────────────────
@dp.callback_query(F.data.startswith("approve:"))
async def cb_approve(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])

    # Генерируем одноразовую ссылку
    try:
        link = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            name=f"Access {user_id}",
        )
        invite_url = link.invite_link
    except Exception as e:
        logging.error(f"Ошибка создания ссылки: {e}")
        await callback.answer("Ошибка! Проверь, что бот админ в канале", show_alert=True)
        return

    # Отправляем пользователю
    await bot.send_message(
        user_id,
        f"🎉 Оплата подтверждена!\n\n"
        f"Вот твоя персональная ссылка на закрытый канал:\n{invite_url}\n\n"
        f"Ссылка действует на 1 вход. Если будут проблемы — пиши сюда.",
    )

    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n✅ ПОДТВЕРЖДЕНО — доступ выдан",
        reply_markup=None,
    )
    await callback.answer("Доступ выдан")


@dp.callback_query(F.data.startswith("reject:"))
async def cb_reject(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])
    await bot.send_message(
        user_id,
        "❌ К сожалению, оплата не подтверждена.\n"
        "Пожалуйста, свяжись с менеджером для уточнения.",
    )
    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n❌ ОТКЛОНЕНО",
        reply_markup=None,
    )
    await callback.answer("Отклонено")


# ─── ADMIN COMMANDS ────────────────────────────────────────────
@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*), status FROM users GROUP BY status")
        rows = c.fetchall()
        c.execute("SELECT COUNT(*) FROM payments WHERE status='pending'")
        pending = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM users WHERE marketing_consent=1")
        marketing = c.fetchone()[0]

    stats_text = "📊 Статистика:\n\n"
    for cnt, st in rows:
        stats_text += f"• {st}: {cnt}\n"
    stats_text += f"\n⏳ Ожидают проверки оплаты: {pending}"
    stats_text += f"\n📬 Согласны на получение сообщений: {marketing}"
    await message.answer(stats_text)


# ─── WEB SERVER FOR RENDER ─────────────────────────────────────
async def health(request):
    return web.Response(text="Bot is running")

app = web.Application()
app.router.add_get("/", health)

async def start_web_server():
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Web server started on port {port}")

# ─── MAIN ──────────────────────────────────────────────────────
async def main():
    init_db()
    await start_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
