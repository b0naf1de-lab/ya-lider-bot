import asyncio
import json
import logging
import os
import sqlite3
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import Command
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ─── CONFIG ────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "ЗАМЕНИ_НА_ТОКЕН_ОТ_BOTFATHER")
_ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "0"))
ADMIN_IDS = [int(x.strip()) for x in _ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]
CHANNEL_ID = os.getenv("CHANNEL_ID", "0")                 # ID закрытого канала/группы
BITRIX_WEBHOOK = os.getenv("BITRIX_WEBHOOK", "").rstrip("/")  # inbound webhook, может быть пустым
LESSON_LINK = os.getenv("LESSON_LINK", "https://mts-link.ru/")  # ссылка «ВОЙТИ В ЗАНЯТИЕ»
LESSON_TIME = os.getenv("LESSON_TIME", "11:00")           # время старта занятий в выходные

if BOT_TOKEN == "ЗАМЕНИ_НА_ТОКЕН_ОТ_BOTFATHER":
    raise ValueError("Укажи BOT_TOKEN в переменных окружения!")
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
                child_class TEXT,
                marketing_consent INTEGER DEFAULT 0,
                status TEXT DEFAULT 'new',
                source TEXT DEFAULT 'menu',
                call_done INTEGER DEFAULT 0,
                probnoe_at TEXT,
                reached_m13 INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        # Миграция старой базы: добавляем новые колонки, если их нет
        for col, ddl in [
            ("child_class", "TEXT"),
            ("source", "TEXT DEFAULT 'menu'"),
            ("call_done", "INTEGER DEFAULT 0"),
            ("probnoe_at", "TEXT"),
            ("reached_m13", "INTEGER DEFAULT 0"),
        ]:
            try:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError:
                pass
        c.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                photo_file_id TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                kind TEXT,
                send_at TEXT,
                payload TEXT,
                sent INTEGER DEFAULT 0
            )
        """)
        conn.commit()


def save_user(user_id, username, full_name, phone, child_name, child_age,
              marketing_consent=0, source="menu", child_class=None):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO users
            (user_id, username, full_name, phone, child_name, child_age, child_class,
             marketing_consent, status, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'lead', ?, ?)
        """, (user_id, username, full_name, phone, child_name, child_age, child_class,
              marketing_consent, source, datetime.now().isoformat()))
        conn.commit()


def get_user(user_id):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return c.fetchone()


def update_user(user_id, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute(f"UPDATE users SET {sets} WHERE user_id=?", (*fields.values(), user_id))
        conn.commit()


def update_marketing_consent(user_id, consent_value):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET marketing_consent=? WHERE user_id=?",
                  (consent_value, user_id))
        conn.commit()
        return c.rowcount


def add_reminder(user_id, kind, send_at: datetime, payload=""):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO reminders (user_id, kind, send_at, payload) VALUES (?, ?, ?, ?)",
            (user_id, kind, send_at.isoformat(), payload),
        )
        conn.commit()


def due_reminders():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, user_id, kind, payload FROM reminders WHERE sent=0 AND send_at<=?",
            (datetime.now().isoformat(),),
        )
        return c.fetchall()


def mark_reminder_sent(rid):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("UPDATE reminders SET sent=1 WHERE id=?", (rid,))
        conn.commit()


# ─── BITRIX24 ──────────────────────────────────────────────────
def bitrix_call(method, params):
    """Вызов Bitrix24 REST. Молча пропускает, если webhook не настроен."""
    if not BITRIX_WEBHOOK:
        return None
    try:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(
            f"{BITRIX_WEBHOOK}/{method}.json", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logging.error(f"Bitrix {method}: {e}")
        return None


def bitrix_create_lead(name, phone, child_age, child_class, source, tg_username):
    """Контакт + сделка + задача менеджеру «позвонить». Не падает без webhook."""
    contact = bitrix_call("crm.contact.add", {
        "fields[NAME]": name,
        "fields[PHONE][0][VALUE]": phone,
        "fields[PHONE][0][VALUE_TYPE]": "WORK",
    })
    contact_id = None
    if contact and "result" in contact:
        contact_id = contact["result"]
    comment = (f"Точка входа: {source}. Ребёнок: {child_age} лет"
               + (f", класс {child_class}" if child_class else "")
               + f". TG: @{tg_username or 'нет'}")
    deal = bitrix_call("crm.deal.add", {
        "fields[TITLE]": f"СОЗВОН НАЗНАЧЕН — Я-Лидер ({name})",
        "fields[COMMENTS]": comment,
        **({"fields[CONTACT_ID]": contact_id} if contact_id else {}),
    })
    deadline = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    bitrix_call("tasks.task.add", {
        "fields[TITLE]": f"Позвонить: {phone} ({name}, точка входа «{source}»)",
        "fields[DESCRIPTION]": f"Перезвонить в течение 2 часов. {comment}",
        "fields[DEADLINE]": deadline,
        "fields[RESPONSIBLE_ID]": 1,
    })


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


def remove_kb():
    return ReplyKeyboardMarkup(keyboard=[[]], resize_keyboard=True)


def test_answer_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да", callback_data="t:2"),
         InlineKeyboardButton(text="🤔 Иногда", callback_data="t:1"),
         InlineKeyboardButton(text="❌ Нет", callback_data="t:0")],
    ])


def class_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(n), callback_data=f"cls:{n}") for n in range(1, 6)],
        [InlineKeyboardButton(text=str(n), callback_data=f"cls:{n}") for n in range(6, 12)],
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


def joined_kb(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, я в занятии", callback_data=f"join:1:{user_id}"),
         InlineKeyboardButton(text="❌ Не получилось", callback_data=f"join:0:{user_id}")],
    ])


def miss_reason_kb(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1️⃣ Не успели по времени", callback_data=f"miss:1:{user_id}")],
        [InlineKeyboardButton(text="2️⃣ Технические проблемы", callback_data=f"miss:2:{user_id}")],
        [InlineKeyboardButton(text="3️⃣ Ребёнок отказался", callback_data=f"miss:3:{user_id}")],
        [InlineKeyboardButton(text="4️⃣ Забыли / не было напоминания", callback_data=f"miss:4:{user_id}")],
        [InlineKeyboardButton(text="5️⃣ Другое", callback_data=f"miss:5:{user_id}")],
    ])


def after_lesson_kb(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Хочу разбор ребёнка", callback_data=f"next:1:{user_id}")],
        [InlineKeyboardButton(text="🚀 Записаться на курс", callback_data=f"next:2:{user_id}")],
        [InlineKeyboardButton(text="📚 Получить материалы", callback_data=f"next:3:{user_id}")],
    ])


# ─── FSM ───────────────────────────────────────────────────────
class LeadForm(StatesGroup):          # старая короткая заявка (кнопка меню)
    parent_name = State()
    phone = State()
    child_name = State()
    child_age = State()
    marketing_consent = State()


class Funnel(StatesGroup):            # новая воронка по кодовым словам
    m01_hook = State()                # вступление по точке входа
    age = State()
    ask_class = State()
    test_q = State()                  # вопросы теста (ЛИДЕР / ТЕСТ)
    parent_name = State()             # M-СОЗВОН
    phone = State()


class PaymentForm(StatesGroup):
    screenshot = State()


# ─── ТЕСТЫ ─────────────────────────────────────────────────────
TEST_LEADER = {
    "title": "🧭 Тест «Лидерские качества»",
    "questions": [
        "Ребёнок легко знакомится с новыми детьми?",
        "Если ребёнок не согласен с правилами игры — он говорит об этом?",
        "Ребёнок берётся за дело, которого раньше не делал?",
        "После проигрыша ребёнок готов попробовать ещё раз?",
        "К ребёнку тянутся другие дети — зовут играть, садятся рядом?",
        "Ребёнок может объяснить другим свою идею так, чтобы его поняли?",
        "В конфликте ребёнок ищет решение, а не обижается?",
    ],
    "result": [
        (6, "🌟 Отличные задатки лидера!\nРебёнок уже проявляет большинство лидерских качеств. Главная задача сейчас — закрепить их в систему: умение доводить начатое до конца, отвечать за команду и выступать публично. Этому как раз и учит наш курс."),
        (3, "💪 Потенциал есть — нужна практика!\nОснова сформирована, но в стрессовых ситуациях (конфликт, публичность, неудача) ребёнок пока раскрывается не всегда. Курс «Я — Лидер» даёт именно тренировку: 8 живых занятий, где каждое качество отрабатывается в игре и реальных задачах."),
        (0, "🌱 Сейчас самое время начать.\nСкорее всего, ребёнок послушный и старательный, но избегает инициативы, публичности и ответственности. Это нормальный этап — и именно с него начинается наш курс. Первые результаты родители видят уже через 3–4 занятия."),
    ],
}

TEST_SELF = {
    "title": "🔍 Тест «Самостоятельность»",
    "questions": [
        "Ребёнок спокойно остаётся с незнакомыми взрослыми (родителями друзей, преподавателями)?",
        "Ребёнок сам просит помощь, когда что-то не получается, — вместо того чтобы бросить или разозлиться?",
        "Ребёнок может спокойно объяснить свою позицию взрослому, даже если не согласен?",
        "Ребёнок доводит начатое до конца — домашние поручения, увлечения, обещания?",
        "Ребёнок включается в командные игры и не боится отвечать за результат команды?",
    ],
    "result": [
        (4, "🌟 Зелёная зона!\nСамостоятельность на хорошем уровне. Следующий шаг — вывести её в лидерство: влияние на группу, публичные выступления, ответственность за других."),
        (2, "🟡 Жёлтая зона.\nБаза есть, но ребёнку не хватает уверенности действовать без подсказки взрослых. Курс даёт безопасную среду, где ребёнок тренирует самостоятельность на каждом занятии."),
        (0, "🔴 Красная зона — повод разобраться.\nСамостоятельность пока ниже возрастной нормы. Хорошая новость: она тренируется, как мышцы. Начните с бесплатного пробного занятия — покажем, как именно мы это делаем."),
    ],
}

TRIGGERS = {
    "ЛИДЕР": {
        "test": TEST_LEADER,
        "hook": (
            "🧭 <b>Тест лидерских качеств</b>\n\n"
            "7 коротких вопросов — ответь честно, как есть на самом деле. "
            "В конце дадим разбор: какие качества уже сформированы, а какие стоит докачать."
        ),
    },
    "ТЕСТ": {
        "test": TEST_SELF,
        "hook": (
            "🔍 <b>Тест самостоятельности</b>\n\n"
            "5 вопросов о том, насколько ребёнок уверенно действует без вашей подсказки. "
            "Результат — зелёная, жёлтая или красная зона, с пояснением что с этим делать."
        ),
    },
    "ДИАГНОСТИКА": {
        "test": None,
        "hook": (
            "🩺 <b>Диагностика ребёнка от психолога</b>\n\n"
            "Наш педагог-психолог проведёт диагностику лидерских качеств и самостоятельности "
            "и расскажет, что конкретно стоит развивать именно вашему ребёнку. "
            "Оставь заявку — перезвоним и договоримся об удобном времени."
        ),
    },
    "ЗАНЯТИЕ": {
        "test": None,
        "hook": (
            "🚀 <b>Пробное занятие</b>\n\n"
            "Ребёнок попробует себя в настоящем уроке «Я — Лидер»: командные задачи, "
            "публичные выступления, разбор конфликтов. Вы увидите формат изнутри. "
            "Оставь заявку — перезвоним и подберём удобный слот."
        ),
    },
}


def trigger_of(text: str):
    t = (text or "").upper()
    for word in ("ЛИДЕР", "ДИАГНОСТИКА", "ЗАНЯТИЕ", "ТЕСТ"):
        if word in t:
            return word
    return None


# ─── /start ────────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    text = (
        "👋 Привет! Я бот «Я — Лидер» — курсы по развитию лидерских качеств для детей 6–16 лет.\n\n"
        "Напиши мне кодовое слово — и я сразу начну:\n"
        "• <b>ЛИДЕР</b> — тест лидерских качеств\n"
        "• <b>ТЕСТ</b> — тест самостоятельности\n"
        "• <b>ДИАГНОСТИКА</b> — диагностика от психолога\n"
        "• <b>ЗАНЯТИЕ</b> — пробное занятие\n\n"
        "Или нажми кнопку ниже, чтобы оставить заявку 👇"
    )
    await message.answer(text, reply_markup=main_menu_kb())


# ─── ВОРОНКА: вход по кодовым словам ──────────────────────────
@dp.message(F.text)
async def funnel_entry(message: Message, state: FSMContext):
    word = trigger_of(message.text)
    if not word:
        raise SkipHandler()  # не кодовое слово — отдаём другим хендлерам
    if await state.get_state():
        return  # человек внутри сценария — не перебиваем
    await state.update_data(source=word, test=TRIGGERS[word]["test"],
                            q_index=0, score=0)
    await state.set_state(Funnel.m01_hook)
    await message.answer(TRIGGERS[word]["hook"], reply_markup=remove_kb())
    await state.set_state(Funnel.age)
    await message.answer("Сколько лет ребёнку? (только цифра, например: 10)")


@dp.message(Funnel.age)
async def funnel_age(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введи возраст цифрами (например: 10)")
        return
    age = int(message.text)
    await state.update_data(child_age=age)
    if age >= 12:
        await state.set_state(Funnel.ask_class)
        await message.answer("В каком классе ребёнок?", reply_markup=class_kb())
    else:
        await go_to_content(message, state)


@dp.callback_query(F.data.startswith("cls:"), Funnel.ask_class)
async def funnel_class(callback: CallbackQuery, state: FSMContext):
    await state.update_data(child_class=int(callback.data.split(":")[1]))
    await callback.answer()
    await go_to_content(callback.message, state)


async def go_to_content(message: Message, state: FSMContext):
    data = await state.get_data()
    test = data.get("test")
    if test:
        await state.set_state(Funnel.test_q)
        await send_test_question(message, state)
    else:
        await ask_call(message, state)


async def send_test_question(message: Message, state: FSMContext):
    data = await state.get_data()
    test = data["test"]
    idx = data["q_index"]
    header = f"{test['title']}\n\nВопрос {idx + 1} из {len(test['questions'])}:"
    await message.answer(
        f"{header}\n\n{test['questions'][idx]}",
        reply_markup=test_answer_kb(),
    )


@dp.callback_query(F.data.startswith("t:"), Funnel.test_q)
async def funnel_test_answer(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    test = data["test"]
    idx = data["q_index"] + 1
    score = data["score"] + int(callback.data.split(":")[1])
    await state.update_data(q_index=idx, score=score)

    if idx < len(test["questions"]):
        await send_test_question(callback.message, state)
        return

    # Результат теста
    for min_score, text in test["result"]:
        if score >= min_score:
            break
    await callback.message.edit_text(f"🏁 <b>Результат:</b> {score} из {len(test['questions'])}\n\n{text}")
    await ask_call(callback.message, state)


# ─── M-СОЗВОН: заявка на звонок менеджера ─────────────────────
async def ask_call(message: Message, state: FSMContext):
    await state.set_state(Funnel.parent_name)
    await message.answer(
        "Отлично! Чтобы назначить созвон, представьтесь, пожалуйста.\n\nКак вас зовут? (имя родителя)"
    )


@dp.message(Funnel.parent_name)
async def funnel_parent_name(message: Message, state: FSMContext):
    await state.update_data(parent_name=message.text)
    await state.set_state(Funnel.phone)
    await message.answer(
        "Приятно познакомиться! Теперь отправьте номер телефона — менеджер перезвонит в течение 2 часов.",
        reply_markup=share_phone_kb(),
    )


@dp.message(Funnel.phone, F.contact)
async def funnel_phone_contact(message: Message, state: FSMContext):
    await funnel_phone_done(message, state, message.contact.phone_number)


@dp.message(Funnel.phone)
async def funnel_phone_text(message: Message, state: FSMContext):
    digits = "".join(ch for ch in (message.text or "") if ch.isdigit())
    if len(digits) < 10:
        await message.answer("Похоже, номер неполный. Отправьте номер кнопкой или цифрами, например: +79001234567")
        return
    await funnel_phone_done(message, state, message.text)


async def funnel_phone_done(message: Message, state: FSMContext, phone: str):
    data = await state.get_data()
    source = data.get("source", "воронка")
    age = data.get("child_age", 0)
    child_class = data.get("child_class")
    parent_name = data.get("parent_name", "")

    save_user(
        user_id=message.chat.id,
        username=message.chat.username,
        full_name=parent_name,
        phone=phone,
        child_name="",
        child_age=age,
        marketing_consent=1,
        source=source,
        child_class=child_class,
    )

    await state.clear()

    await message.answer(
        f"🎉 Заявка принята, {parent_name}!\n\n"
        f"Менеджер свяжется с вами в течение <b>2 часов</b> по номеру {phone} "
        f"и подберёт удобное время.\n\n"
        f"Если не дозвонимся с первого раза — обязательно напишем в Telegram.",
        reply_markup=main_menu_kb(),
    )

    # Уведомление админам
    for admin_id in ADMIN_IDS:
        await bot.send_message(
            admin_id,
            f"📥 Заявка на созвон! Точка входа: «{source}»\n\n"
            f"Родитель: {parent_name}\n"
            f"Телефон: {phone}\n"
            f"Ребёнок: {age} лет" + (f", класс {child_class}" if child_class else "") + "\n"
            f"Telegram: @{message.chat.username or 'нет'}\n"
            f"ID: {message.chat.id}\n\n"
            f"⏰ Дедлайн звонка: +2 часа",
        )

    # Bitrix: контакт + сделка + задача Вике
    bitrix_create_lead(parent_name, phone, age, child_class, source, message.chat.username)

    # Напоминание M03: если созвон не отмечен через 2 часа — пингануть админа
    add_reminder(message.chat.id, "m03_admin", datetime.now() + timedelta(hours=2), payload=phone)


# ─── СТАРАЯ КОРОТКАЯ ЗАЯВКА (кнопка меню) — сохранена ─────────
@dp.message(F.text == "📋 Оставить заявку")
async def start_lead(message: Message, state: FSMContext):
    await state.set_state(LeadForm.parent_name)
    await message.answer(
        "Отлично! Давай познакомимся.\n\nКак тебя зовут? (имя родителя)",
        reply_markup=remove_kb(),
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
    await state.update_data(phone=message.contact.phone_number)
    await state.set_state(LeadForm.child_name)
    await message.answer("Отлично! Как зовут твоего ребёнка?", reply_markup=remove_kb())


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
    await state.update_data(child_age=int(message.text))
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


def marketing_consent_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, согласен", callback_data="consent:yes")],
        [InlineKeyboardButton(text="❌ Нет, не согласен", callback_data="consent:no")],
    ])


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
        source="menu",
    )
    await state.clear()

    await message.answer(
        f"🎉 Спасибо, {data.get('parent_name', '')}!\n\n"
        f"Заявка принята:\n"
        f"• Ребёнок: {data.get('child_name', '')}, {age} лет\n"
        f"• Телефон: {data.get('phone', '')}\n\n"
        "Менеджер свяжется с вами в течение 2 часов.",
        reply_markup=main_menu_kb(),
    )

    consent_text = "✅ Согласен на получение сообщений" if consent else "❌ Не согласен на получение сообщений"
    for admin_id in ADMIN_IDS:
        await bot.send_message(
            admin_id,
            f"📥 Новая заявка (кнопка меню)!\n\n"
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

    user_info = f"@{message.from_user.username}" if message.from_user.username else f"ID {message.from_user.id}"
    for admin_id in ADMIN_IDS:
        await bot.send_photo(
            admin_id,
            photo=photo_id,
            caption=(
                f"💳 Новая оплата от {user_info}\n\n"
                f"Родитель: {user[3] if user else '—'}\n"
                f"Телефон: {user[4] if user else '—'}\n"
                f"Ребёнок: {user[5] if user else '—'} лет\n"
                f"Точка входа: {user[9] if user and len(user) > 9 else '—'}"
            ),
            reply_markup=admin_approve_kb(message.from_user.id),
        )


@dp.message(PaymentForm.screenshot)
async def process_screenshot_invalid(message: Message):
    await message.answer("Пожалуйста, отправь фото (скриншот оплаты).")


# ─── ПОСЛЕ ПРОБНОГО: M10/M11/M12/M13/M20 ─────────────────────
@dp.callback_query(F.data.startswith("join:"))
async def cb_joined(callback: CallbackQuery):
    ok, user_id = callback.data.split(":")[1], int(callback.data.split(":")[2])
    await callback.answer()
    if ok == "1":
        await callback.message.edit_text("✅ Супер! Приятного занятия.")
        await bot.send_message(
            user_id,
            "🙌 <b>Спасибо, что были с нами!</b>\n\nКак вам занятие? Выберите, что важнее:",
            reply_markup=after_lesson_kb(user_id),
        )
        update_user(user_id, reached_m13=0)
        add_reminder(user_id, "m20_final", datetime.now() + timedelta(hours=30))
    else:
        await callback.message.edit_text("Поняли. Что именно не получилось?")
        await bot.send_message(
            user_id,
            "😔 Жаль, что не получилось подключиться. Что случилось?",
            reply_markup=miss_reason_kb(user_id),
        )


@dp.callback_query(F.data.startswith("miss:"))
async def cb_miss(callback: CallbackQuery):
    reason, user_id = int(callback.data.split(":")[1]), int(callback.data.split(":")[2])
    await callback.answer("Записали.")
    texts = {
        1: "Поняли! Тогда предлагаем перенести занятие на удобное время.",
        2: "Поняли, бывает. Поможем подключиться в следующий раз — пришлём инструкцию заранее.",
        3: "Поняли. Часто такое бывает перед первым занятием. Предлагаем попробовать ещё раз — обычно со второго раза дети раскрепощаются.",
        4: "Поняли! Усилим напоминания и за сутки, и за 15 минут пришлём ссылку ещё раз.",
        5: "Поняли. Напишите, пожалуйста, что именно помешало — постараемся помочь.",
    }
    await bot.send_message(
        user_id,
        f"{texts.get(reason, texts[5])}\n\nМенеджер свяжется с вами и подберёт новый слот "
        f"(занятия проходят в пятницу, субботу и воскресенье).",
        reply_markup=after_lesson_kb(user_id),
    )
    for admin_id in ADMIN_IDS:
        await bot.send_message(
            admin_id,
            f"⚠️ Неявка на пробное (ID {user_id}). Причина: {texts.get(reason, reason)}\n"
            f"Нужен созвон-повтор. Телефон: {(get_user(user_id) or [None]*5)[4]}",
        )


@dp.callback_query(F.data.startswith("next:"))
async def cb_next(callback: CallbackQuery):
    choice, user_id = int(callback.data.split(":")[1]), int(callback.data.split(":")[2])
    await callback.answer()
    user = get_user(user_id) or ()
    phone = user[4] if len(user) > 4 else "—"
    if choice == 1:
        await callback.message.edit_text("Отлично! Записали на разбор.")
        await bot.send_message(
            user_id,
            "💬 <b>Разбор вашего ребёнка</b>\n\nПсихолог разберёт поведение ребёнка на занятии: "
            "сильные стороны, зоны роста и конкретный план на 4 месяца.\n\n"
            "Менеджер перезвонит и предложит удобные слоты (будни вечером или выходные).",
        )
    elif choice == 2:
        await callback.message.edit_text("Отлично! Менеджер свяжется с вами.")
        await bot.send_message(
            user_id,
            "🚀 <b>Запись на курс</b>\n\nМенеджер перезвонит в течение 2 часов, расскажет про программу, "
            "цену и ближайшие потоки, и зафиксирует место за вашим ребёнком.",
        )
    else:
        await callback.message.edit_text("Записали!")
        await bot.send_message(
            user_id,
            "📚 <b>Материалы после пробного занятия</b>\n\nПришлём подборку: 3 упражнения на "
            "самостоятельность, которые можно делать дома за 10 минут в день.",
        )
    update_user(user_id, reached_m13=1)
    for admin_id in ADMIN_IDS:
        label = {1: "ХОЧЕТ РАЗБОР", 2: "ХОЧЕТ НА КУРС", 3: "ХОЧЕТ МАТЕРИАЛЫ"}[choice]
        await bot.send_message(
            admin_id,
            f"🔥 Горячий лид после пробного (ID {user_id}): {label}\nТелефон: {phone}",
        )


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
        await message.answer("Ты ещё не оставлял заявку в нашем боте.\n\nЕсли хочешь записаться — нажми /start")
        return
    if user[7] == 0:
        await message.answer("✅ Ты уже отписан от сообщений.")
        return
    rows = update_marketing_consent(message.from_user.id, 0)
    if rows > 0:
        await message.answer(
            "✅ Ты успешно отписан.\n\n"
            "Больше не будем присылать сообщения о курсах и акциях.\n\n"
            "Если передумаешь — оставь заявку снова через /start."
        )
        for admin_id in ADMIN_IDS:
            await bot.send_message(
                admin_id,
                f"🔕 Пользователь отписался:\n\nИмя: {user[3]}\nТелефон: {user[4]}\nID: {message.from_user.id}",
            )
    else:
        await message.answer("Что-то пошло не так. Напиши менеджеру напрямую.")


# ─── ADMIN ACTIONS (оплата) ────────────────────────────────────
@dp.callback_query(F.data.startswith("approve:"))
async def cb_approve(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    user_id = int(callback.data.split(":")[1])
    try:
        link = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID, member_limit=1, name=f"Access {user_id}")
        invite_url = link.invite_link
    except Exception as e:
        logging.error(f"Ошибка создания ссылки: {e}")
        await callback.answer("Ошибка! Проверь, что бот админ в канале", show_alert=True)
        return
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
        "❌ К сожалению, оплата не подтверждена.\nПожалуйста, свяжись с менеджером для уточнения.",
    )
    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n❌ ОТКЛОНЕНО", reply_markup=None)
    await callback.answer("Отклонено")


# ─── ADMIN COMMANDS ────────────────────────────────────────────
@dp.message(Command("probnoe"))
async def cmd_probnoe(message: Message):
    """/probnoe <user_id> <YYYY-MM-DD HH:MM> — записать на пробное, шлёт M02 и планирует M07–M10."""
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        _, uid_s, dt_s = message.text.split(maxsplit=2)
        user_id = int(uid_s)
        when = datetime.strptime(dt_s.strip(), "%Y-%m-%d %H:%M")
    except (ValueError, IndexError):
        await message.answer("Формат: /probnoe 123456789 2026-10-03 11:00")
        return
    user = get_user(user_id)
    if not user:
        await message.answer(f"Пользователь {user_id} не найден в базе (сначала заявка через бота).")
        return
    update_user(user_id, probnoe_at=when.isoformat(), status="probnoe")
    pretty = when.strftime("%d.%m.%Y в %H:%M")
    await bot.send_message(
        user_id,
        f"✅ <b>Записываем на пробное занятие!</b>\n\n"
        f"📅 Дата: {pretty}\n"
        f"⏳ Занятие идёт 60 минут, начало в {LESSON_TIME}\n\n"
        f"За сутки, за 2 часа и за 15 минут пришлём напоминание со ссылкой «Войти в занятие».\n"
        f"Если планы изменятся — просто ответьте в чат, перенесём.",
    )
    add_reminder(user_id, "m07_24h", when - timedelta(hours=24))
    add_reminder(user_id, "m08_2h", when - timedelta(hours=2))
    add_reminder(user_id, "m09_15m", when - timedelta(minutes=15))
    add_reminder(user_id, "m10_join", when + timedelta(minutes=10))
    await message.answer(
        f"✅ Пробное для {user_id} назначено на {pretty}.\n"
        f"Напоминания M07/M08/M09/M10 запланированы. Подтверждение M02 отправлено."
    )


@dp.message(Command("done"))
async def cmd_done(message: Message):
    """/done <user_id> — отметить, что созвон состоялся (гасит M03)."""
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        user_id = int(message.text.split()[1])
    except (ValueError, IndexError):
        await message.answer("Формат: /done 123456789")
        return
    update_user(user_id, call_done=1)
    await message.answer(f"✅ Созвон с {user_id} отмечен. Напоминание M03 не придёт.")


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*), source FROM users GROUP BY source")
        by_source = c.fetchall()
        c.execute("SELECT COUNT(*) FROM payments WHERE status='pending'")
        pending = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM users WHERE marketing_consent=1")
        marketing = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM reminders WHERE sent=0")
        planned = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM users WHERE probnoe_at IS NOT NULL")
        probnoe = c.fetchone()[0]
    text = "📊 Статистика:\n\n<b>По точкам входа:</b>\n"
    for cnt, src in by_source:
        text += f"• {src}: {cnt}\n"
    text += (f"\n🧭 Записано на пробное: {probnoe}"
             f"\n⏳ Ожидают проверки оплаты: {pending}"
             f"\n📬 Согласны на сообщения: {marketing}"
             f"\n🔔 Напоминаний в очереди: {planned}")
    await message.answer(text)


# ─── ПЛАНИРОВЩИК НАПОМИНАНИЙ ──────────────────────────────────
async def reminder_worker():
    while True:
        try:
            for rid, user_id, kind, payload in due_reminders():
                user = get_user(user_id)
                if not user:
                    mark_reminder_sent(rid)
                    continue
                try:
                    if kind == "m03_admin":
                        if user[10]:  # call_done — созвон состоялся, пинать не надо
                            continue
                        for admin_id in ADMIN_IDS:
                            await bot.send_message(
                                admin_id,
                                f"⏰ Прошло 2 часа, созвон не отмечен!\n\n"
                                f"Телефон: {user[4]}\nИмя: {user[3]}\n"
                                f"Точка входа: {user[9]}\n"
                                f"Отметить созвон: /done {user_id}",
                            )
                    elif kind == "m07_24h":
                        await bot.send_message(
                            user_id,
                            "⏳ <b>Напоминание: завтра пробное занятие!</b>\n\n"
                            "Проверьте: ребёнок знает, во сколько начало, и есть ли под рукой "
                            "тетрадь и ручка. Мы пришлём ссылку за 15 минут до старта.")
                    elif kind == "m08_2h":
                        await bot.send_message(
                            user_id,
                            "⏳ <b>Через 2 часа пробное занятие!</b>\n\n"
                            "Проверьте камеру и микрофон заранее — так не будет спешки перед стартом.")
                    elif kind == "m09_15m":
                        await bot.send_message(
                            user_id,
                            f"🚀 <b>Занятие начинается через 15 минут!</b>\n\n"
                            f"👉 <a href=\"{LESSON_LINK}\">ВОЙТИ В ЗАНЯТИЕ</a>\n\n"
                            f"Нажмите кнопку «Войти» подключившись.",
                            disable_web_page_preview=True)
                    elif kind == "m10_join":
                        await bot.send_message(
                            user_id,
                            "👋 Занятие уже началось! Удалось подключиться?",
                            reply_markup=joined_kb(user_id))
                    elif kind == "m20_final":
                        if user[12]:  # reached_m13 — уже выбрал следующий шаг
                            continue
                        await bot.send_message(
                            user_id,
                            "🤝 <b>Остались вопросы после пробного?</b>\n\n"
                            "Можем: записать на курс, перенести занятие или дать разбор "
                            "результатов вашего ребёнка от психолога. Просто ответьте на это сообщение "
                            "или нажмите кнопку ниже 👇",
                            reply_markup=after_lesson_kb(user_id))
                except Exception as e:
                    logging.error(f"reminder {kind} for {user_id}: {e}")
                finally:
                    mark_reminder_sent(rid)
        except Exception as e:
            logging.error(f"reminder_worker: {e}")
        await asyncio.sleep(20)


# ─── MAIN ──────────────────────────────────────────────────────
async def main():
    init_db()
    asyncio.create_task(reminder_worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
