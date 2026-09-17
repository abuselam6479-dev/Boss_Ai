import os
import time
import sqlite3
import threading
import base64
import json
import io
import traceback
import requests
import telebot

from google import genai
from google.genai import types
from docx import Document as DocxDocument

from telebot.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "").strip()
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", "0").strip()
try:
    ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW else 0
except ValueError:
    ADMIN_ID = 0

FREE_LIMIT = 15
MONTHLY_PRICE = 100
DB_FILE = "bossai.db"

bot = telebot.TeleBot(TOKEN, parse_mode=None)

CHANNEL_USERNAME = "@bossainews"
CHANNEL_JOIN_THRESHOLD = 10

CHAT_MODELS = {
    "DeepSeek": "deepseek/deepseek-chat",
    "GPT-4o": "openai/gpt-4o",
    "Claude": "anthropic/claude-3.5-sonnet",
    "Grok": "x-ai/grok-2-1212",
}

IMAGE_MODEL = "google/gemini-2.5-flash-image"
MUSIC_MODEL = "google/lyria-3-clip-preview"

active_documents = {}
MAX_DOC_CONTEXT_CHARS = 8000

user_mode = {}
telebirr_waiting = set()
memory_waiting = set()
doc_waiting = set()
broadcast_waiting = set()
channel_setup_waiting = set()
post_channel_waiting = set()

busy_users = set()
busy_lock = threading.Lock()
last_request = {}

def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def get_setting(key, default=None):
    conn = get_db()
    row = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (key,)
    ).fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key, value):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value)
    )
    conn.commit()
    conn.close()

def init_database():
    conn = get_db()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            free_used INTEGER DEFAULT 0,
            free_date TEXT,
            model TEXT DEFAULT 'GPT-4o',
            subscription_until INTEGER DEFAULT 0,
            referred_by INTEGER DEFAULT NULL,
            referrals INTEGER DEFAULT 0,
            paid_referrals INTEGER DEFAULT 0,
            created_at INTEGER,
            notes TEXT DEFAULT '',
            last_active INTEGER,
            reminder_sent_at INTEGER,
            total_messages INTEGER DEFAULT 0,
            channel_verified INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            created_at INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            status TEXT DEFAULT 'pending',
            created_at INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            rating TEXT,
            created_at INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    existing_columns = [
        row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()
    ]
    migrations = {
        "notes": "ALTER TABLE users ADD COLUMN notes TEXT DEFAULT ''",
        "last_active": "ALTER TABLE users ADD COLUMN last_active INTEGER",
        "reminder_sent_at": "ALTER TABLE users ADD COLUMN reminder_sent_at INTEGER",
        "total_messages": "ALTER TABLE users ADD COLUMN total_messages INTEGER DEFAULT 0",
        "channel_verified": "ALTER TABLE users ADD COLUMN channel_verified INTEGER DEFAULT 0",
    }
    for column, sql in migrations.items():
        if column not in existing_columns:
            conn.execute(sql)
    conn.commit()
    conn.close()

def current_date():
    return time.strftime("%Y-%m-%d")

def get_user(user_id, first_name="", username=""):
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if user is None:
        conn.execute(
            """
            INSERT INTO users (user_id, first_name, username, free_used, free_date, created_at, last_active)
            VALUES (?, ?, ?, 0, ?, ?, ?)
            """,
            (
                user_id,
                first_name or "",
                username or "",
                current_date(),
                int(time.time()),
                int(time.time())
            )
        )
        conn.commit()
        user = conn.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
    elif user["free_date"] != current_date():
        conn.execute(
            "UPDATE users SET free_used=0, free_date=? WHERE user_id=?",
            (current_date(), user_id)
        )
        conn.commit()
        user = conn.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

    conn.execute(
        """
        UPDATE users SET last_active=?, reminder_sent_at=NULL, first_name=?, username=? WHERE user_id=?
        """,
        (
            int(time.time()),
            first_name or user["first_name"] or "",
            username or user["username"] or "",
            user_id
        )
    )
    conn.commit()
    user = conn.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()
    conn.close()
    return user

def subscription_active(user):
    return bool(
        user["subscription_until"]
        and user["subscription_until"] > int(time.time())
    )

def get_subscription_price(user):
    if user["referrals"] >= 50 and user["paid_referrals"] >= 10:
        return 50
    if user["referrals"] >= 30:
        return 70
    return 100

def main_keyboard(user_id=None):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(
        KeyboardButton("💳 Payment Methods"),
        KeyboardButton("👥 Referral")
    )
    markup.row(
        KeyboardButton("🤖 Models"),
        KeyboardButton("🔄 Restart")
    )
    markup.row(
        KeyboardButton("❓ Help"),
        KeyboardButton("📊 My Account")
    )
    markup.row(
        KeyboardButton("🎨 Create Image"),
        KeyboardButton("🎵 Create Music")
    )
    markup.row(
        KeyboardButton("🎬 Create Video"),
        KeyboardButton("📄 Create Document")
    )
    markup.row(KeyboardButton("🧠 My Memory"))

    if user_id is not None and is_admin(user_id):
        markup.row(
            KeyboardButton("👑 Admin Panel"),
            KeyboardButton("📢 Broadcast")
        )
        markup.row(
            KeyboardButton("📣 Post to Channel")
        )
    return markup

def mode_keyboard():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton("🔙 Back to Chat"))
    return markup

def has_joined_channel(user_id):
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as error:
        print("Channel membership check failed:", error)
        return True

def channel_join_markup():
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            "📢 Join the Channel",
            url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"
        )
    )
    return markup

def enforce_channel_join(message, user):
    if (
        user["total_messages"] < CHANNEL_JOIN_THRESHOLD
        or user["channel_verified"]
    ):
        return True

    if has_joined_channel(user["user_id"]):
        conn = get_db()
        conn.execute(
            "UPDATE users SET channel_verified=1 WHERE user_id=?",
            (user["user_id"],)
        )
        conn.commit()
        conn.close()
        return True

    bot.reply_to(
        message,
        "🔔 To continue using it, please join our news channel first.",
        reply_markup=channel_join_markup()
    )
    return False

def save_message(user_id, role, content):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO messages (user_id, role, content, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, role, content, int(time.time()))
    )
    conn.commit()
    conn.close()

def get_history(user_id):
    conn = get_db()
    rows = conn.execute(
        """
        SELECT role, content
        FROM messages
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 10
        """,
        (user_id,)
    ).fetchall()
    conn.close()

    rows = list(reversed(rows))
    return [
        {"role": row["role"], "content": row["content"]}
        for row in rows
    ]

def system_prompt(notes="", doc_context=""):
    base = r"""
You are BOSSAI, a high-quality all-in-one AI assistant designed to serve a global audience across multiple languages seamlessly.

LANGUAGE & FLUENCY:
- Reply precisely in the same language the user uses.
- If the user writes in Amharic, the response MUST be in exceptionally natural, fluent, and high-quality Amharic—crafted like a native, educated speaker. Avoid literal or machine-translation styles. Use correct grammar, proper vocabulary, and polite standard forms (e.g., እርስዎ, ይችላሉ).
- For any other language (English, Arabic, etc.), maintain absolute native-level fluency, clarity, and precision.

ACCURACY & STYLE:
- Do not invent facts. Be direct, helpful, and friendly.
- Use clean plain text without excessive markdown symbols like **, ##.
"""

    if notes:
        base += (
            "\n\nUSER MEMORY:\n"
            "Use these saved details naturally when relevant:\n" + notes
        )
    if doc_context:
        base += (
            "\n\nDOCUMENT CONTEXT:\n"
            "The user has shared a document. Use this content when answering questions about it:\n\n" + doc_context
        )
    return base

def current_time_context():
    from zoneinfo import ZoneInfo
    from datetime import datetime

    now = datetime.now(ZoneInfo("Africa/Addis_Ababa"))
    formatted = now.strftime("%A, %B %d, %Y, %H:%M")
    return (
        "CURRENT DATE AND TIME:\n"
        f"Right now it is {formatted} (Ethiopia time, East Africa Time, UTC+3)."
    )

def call_gemini_with_retry(client, **kwargs):
    if "config" not in kwargs:
        kwargs["config"] = types.GenerateContentConfig(
            max_output_tokens=8192
        )

    try:
        return client.models.generate_content(**kwargs)
    except Exception as error:
        error_text = str(error)
        if "503" in error_text or "UNAVAILABLE" in error_text:
            time.sleep(3)
            return client.models.generate_content(**kwargs)
        if "429" in error_text or "RESOURCE_EXHAUSTED" in error_text:
            time.sleep(15)
            return client.models.generate_content(**kwargs)
        raise

def ask_openrouter(user_id, text):
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is missing.")

    user = get_user(user_id)
    model = user["model"]
    history = get_history(user_id)
    doc_context = active_documents.get(user_id, "")
    
    messages = [{
        "role": "system",
        "content": system_prompt(user["notes"] or "", doc_context)
    }]
    messages.extend(history)
    messages.append({"role": "user", "content": text})

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": CHAT_MODELS.get(model, CHAT_MODELS["GPT-4o"]),
            "messages": messages,
            "max_tokens": 1200
        },
        timeout=90
    )
    if not response.ok:
        raise RuntimeError(
            f"OpenRouter {response.status_code}: {response.text[:300]}"
        )
    data = response.json()
    return data["choices"][0]["message"]["content"]

def ask_gemini(user_id, text):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing.")

    user = get_user(user_id)
    history = get_history(user_id)
    doc_context = active_documents.get(user_id, "")
    
    conversation = ""
    for item in history:
        conversation += (
            item["role"] + ": " + item["content"] + "\n"
        )
    
    prompt = (
        system_prompt(user["notes"] or "", doc_context)
        + "\n\nPrevious conversation:\n"
        + conversation
        + "\n\nCurrent user message:\n"
        + text
    )

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = call_gemini_with_retry(
        client,
        model="gemini-2.5-flash",
        contents=prompt
    )
    if not response.text:
        raise RuntimeError("Gemini returned an empty response.")
    return response.text

def ask_openrouter_model(user_id, text, model_name):
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is missing.")

    history = get_history(user_id)
    messages = [{"role": "system", "content": system_prompt()}]
    messages.extend(history)
    messages.append({"role": "user", "content": text})

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": CHAT_MODELS[model_name],
            "messages": messages,
            "max_tokens": 1200
        },
        timeout=90
    )
    if not response.ok:
        raise RuntimeError(f"OpenRouter {model_name} {response.status_code}: {response.text[:300]}")
    data = response.json()
    content = data["choices"][0]["message"].get("content", "")
    if not content:
        raise RuntimeError(f"{model_name} returned an empty response.")
    return content

def ask_openai_compatible(user_id, text, base_url, api_key, model_name):
    if not api_key:
        raise RuntimeError(f"{model_name} API key is missing.")

    history = get_history(user_id)
    messages = [{"role": "system", "content": system_prompt()}]
    messages.extend(history)
    messages.append({"role": "user", "content": text})

    response = requests.post(
        base_url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        json={
            "model": model_name,
            "messages": messages,
            "max_tokens": 1200
        },
        timeout=90
    )
    if not response.ok:
        raise RuntimeError(f"{model_name} {response.status_code}: {response.text[:300]}")
    data = response.json()
    content = data["choices"][0]["message"].get("content", "")
    if not content:
        raise RuntimeError(f"{model_name} returned an empty response.")
    return content

def ask_groq(user_id, text):
    return ask_openai_compatible(
        user_id, text,
        "https://api.groq.com/openai/v1/chat/completions",
        GROQ_API_KEY,
        "llama-3.3-70b-versatile"
    )

def ask_github_models(user_id, text):
    return ask_openai_compatible(
        user_id, text,
        "https://models.github.ai/inference/chat/completions",
        GITHUB_TOKEN,
        "openai/gpt-4o"
    )

def ask_cerebras(user_id, text):
    return ask_openai_compatible(
        user_id, text,
        "https://api.cerebras.ai/v1/chat/completions",
        CEREBRAS_API_KEY,
        "llama3.3-70b"
    )

def ask_openrouter_free(user_id, text):
    return ask_openai_compatible(
        user_id, text,
        "https://openrouter.ai/api/v1/chat/completions",
        OPENROUTER_API_KEY,
        "openrouter/free"
    )

def ask_ai(user_id, text):
    fallback_chain = [
        "Gemini", "Groq", "GitHub Models", "Cerebras", "OpenRouterFree",
        "Claude", "Grok", "DeepSeek", "GPT-4o"
    ]
    errors = []

    for model_name in fallback_chain:
        try:
            if model_name == "Gemini":
                if not GEMINI_API_KEY:
                    raise RuntimeError("GEMINI_API_KEY is missing.")
                return ask_gemini(user_id, text)
            if model_name == "Groq":
                return ask_groq(user_id, text)
            if model_name == "GitHub Models":
                return ask_github_models(user_id, text)
            if model_name == "Cerebras":
                return ask_cerebras(user_id, text)
            if model_name == "OpenRouterFree":
                return ask_openrouter_free(user_id, text)
            return ask_openrouter_model(user_id, text, model_name)
        except Exception as error:
            errors.append(f"{model_name}: {str(error)[:350]}")
            continue

    raise RuntimeError(
        "All AI providers are temporarily unavailable. Details: " + " | ".join(errors)
    )

def notify_admin_error(context, user_id, error):
    if ADMIN_ID == 0:
        return
    try:
        bot.send_message(
            ADMIN_ID,
            "⚠️ BOSSAI Error\n\n"
            f"Context: {context}\n"
            f"User ID: {user_id}\n"
            f"Error: {str(error)[:400]}"
        )
    except Exception as notify_error:
        print("Could not notify admin:", notify_error)

def notify_admin(text):
    if ADMIN_ID == 0:
        return
    try:
        bot.send_message(ADMIN_ID, text)
    except Exception as error:
        print("Could not notify admin:", error)

def typing_loop(chat_id, stop_event):
    while not stop_event.is_set():
        try:
            bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        stop_event.wait(4)

def clean_formatting(text):
    text = text.replace("**", "")
    text = text.replace("###", "")
    text = text.replace("##", "")
    text = text.replace("* ", "- ")
    return text

def send_long_message(message, text, feedback_markup=None):
    if not text:
        text = "Sorry, I could not generate a response."

    text = clean_formatting(text)
    chunks = [
        text[i:i + 4000] for i in range(0, len(text), 4000)
    ]
    if len(chunks) == 1:
        bot.reply_to(
            message,
            chunks[0],
            reply_markup=feedback_markup
        )
        return

    bot.reply_to(message, chunks[0])
    for chunk in chunks[1:-1]:
        bot.send_message(message.chat.id, chunk)
    bot.send_message(
        message.chat.id,
        chunks[-1],
        reply_markup=feedback_markup
    )

def send_welcome(message, extra_note=""):
    name = message.from_user.first_name or "there"

    text = (
        f"👋 Welcome {name} to BOSSAI!\n\n"
        "🤖 Your all-in-one global AI assistant — GPT-4o, Claude, DeepSeek, Grok, "
        "and Gemini in a single bot.\n\n"
        f"🆓 Free: {FREE_LIMIT} messages per day\n"
        f"⭐ Unlimited: {MONTHLY_PRICE} ETB/month\n\n"
        f"📢 Join {CHANNEL_USERNAME} for updates and new features."
    )
    bot.send_message(
        message.chat.id,
        text + extra_note,
        reply_markup=main_keyboard(message.from_user.id)
    )

@bot.message_handler(commands=["start"])
def start(message):
    user_id = message.from_user.id

    conn = get_db()
    already_existed = conn.execute(
        "SELECT 1 FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone() is not None
    conn.close()

    user = get_user(
        user_id,
        message.from_user.first_name,
        message.from_user.username
    )
    user_mode.pop(user_id, None)

    referral_note = ""
    if message.text:
        parts = message.text.split(maxsplit=1)
        if (
            len(parts) == 2
            and parts[1].strip().startswith("ref_")
        ):
            try:
                referrer_id = int(parts[1].strip()[4:])
                if (
                    referrer_id != user_id
                    and user["referred_by"] is None
                ):
                    conn = get_db()
                    referrer = conn.execute(
                        "SELECT user_id FROM users WHERE user_id=?",
                        (referrer_id,)
                    ).fetchone()
                    if referrer:
                        conn.execute(
                            """
                            UPDATE users SET referred_by=? WHERE user_id=? AND referred_by IS NULL
                            """,
                            (referrer_id, user_id)
                        )
                        conn.execute(
                            """
                            UPDATE users SET referrals=referrals+1 WHERE user_id=?
                            """,
                            (referrer_id,)
                        )
                        conn.commit()
                        referral_note = (
                            "\n\n🎉 You joined through a referral link. Welcome!"
                        )
                    conn.close()
            except (ValueError, IndexError):
                pass

    send_welcome(message, referral_note)

@bot.message_handler(commands=["help"])
def help_command(message):
    bot.send_message(
        message.chat.id,
        "BOSSAI Help\n\n"
        "Chat: Send your question directly in any language.\n"
        f"Free: {FREE_LIMIT} messages per day.\n"
        f"Unlimited: {MONTHLY_PRICE} ETB/month.\n"
        "Support: @Silent_Survivorr"
    )

@bot.message_handler(func=lambda m: m.text == "❓ Help")
def help_button(message):
    help_command(message)

def show_payment_menu(message):
    user = get_user(message.from_user.id)
    price = get_subscription_price(user)

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            f"💳 Telebirr — {price} ETB/month",
            callback_data="telebirr"
        )
    )
    bot.send_message(
        message.chat.id,
        "Choose your payment method:",
        reply_markup=markup
    )

@bot.message_handler(commands=["menu"])
def menu_command(message):
    show_payment_menu(message)

@bot.message_handler(func=lambda m: m.text == "💳 Payment Methods")
def payment_button(message):
    show_payment_menu(message)

@bot.callback_query_handler(
    func=lambda call: call.data in ["telebirr", "payoneer", "paypal"]
)
def payment_callback(call):
    bot.answer_callback_query(call.id)

    if call.data == "telebirr":
        user = get_user(call.from_user.id)
        price = get_subscription_price(user)
        telebirr_waiting.add(call.from_user.id)
        bot.send_message(
            call.message.chat.id,
            f"Telebirr Payment\n\n"
            f"Amount: {price} ETB/month\n\n"
            "Receiver: Hussein\n"
            "Telebirr: 0964990206\n\n"
            "After payment, send your payment receipt screenshot here."
        )

@bot.message_handler(content_types=["photo"])
def photo_handler(message):
    user_id = message.from_user.id

    if user_id in telebirr_waiting:
        telebirr_waiting.discard(user_id)
        handle_payment_receipt(message)
        return

    user = get_user(
        user_id,
        message.from_user.first_name,
        message.from_user.username
    )
    if not enforce_channel_join(message, user):
        return

    handle_vision_photo(message)

def handle_payment_receipt(message):
    if ADMIN_ID == 0:
        bot.reply_to(
            message,
            "Receipt received. Admin verification is not configured yet."
        )
        return

    user = get_user(
        message.from_user.id,
        message.from_user.first_name,
        message.from_user.username
    )
    price = get_subscription_price(user)

    conn = get_db()
    cursor = conn.execute(
        """
        INSERT INTO payments (user_id, amount, status, created_at)
        VALUES (?, ?, 'pending', ?)
        """,
        (message.from_user.id, price, int(time.time()))
    )
    payment_id = cursor.lastrowid
    conn.commit()
    conn.close()

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton(
            "✅ Approve",
            callback_data=f"approve:{payment_id}:{message.from_user.id}"
        ),
        InlineKeyboardButton(
            "❌ Reject",
            callback_data=f"reject:{payment_id}:{message.from_user.id}"
        )
    )

    caption = (
        "Payment Receipt\n\n"
        f"Payment ID: {payment_id}\n"
        f"User ID: {message.from_user.id}\n"
        f"Amount: {price} ETB\n"
        "Status: Pending"
    )
    bot.send_photo(
        ADMIN_ID,
        message.photo[-1].file_id,
        caption=caption,
        reply_markup=markup
    )
    bot.reply_to(
        message,
        "Your receipt has been sent for verification. Please wait for approval."
    )

def handle_vision_photo(message):
    user_id = message.from_user.id
    user = get_user(
        user_id,
        message.from_user.first_name,
        message.from_user.username
    )

    if not subscription_active(user):
        if user["free_used"] >= FREE_LIMIT:
            bot.reply_to(
                message,
                f"You have used all {FREE_LIMIT} free messages today."
            )
            return

    stop_event = threading.Event()
    typing_thread = threading.Thread(
        target=typing_loop,
        args=(message.chat.id, stop_event),
        daemon=True
    )
    typing_thread.start()

    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        file_bytes = bot.download_file(file_info.file_path)

        client = genai.Client(api_key=GEMINI_API_KEY)
        image_part = types.Part.from_bytes(
            data=file_bytes,
            mime_type="image/jpeg"
        )
        response = call_gemini_with_retry(
            client,
            model="gemini-2.5-flash",
            contents=[system_prompt(user["notes"] or ""), image_part]
        )
        send_long_message(message, response.text)
    except Exception as error:
        notify_admin_error("Vision", user_id, error)
        bot.reply_to(message, "Sorry, service temporarily unavailable.")
    finally:
        stop_event.set()

@bot.callback_query_handler(
    func=lambda call: (
        call.data.startswith("approve:")
        or call.data.startswith("reject:")
    )
)
def payment_decision(call):
    if call.from_user.id != ADMIN_ID:
        return

    parts = call.data.split(":")
    action, payment_id, user_id = parts[0], int(parts[1]), int(parts[2])

    conn = get_db()
    if action == "approve":
        until = int(time.time()) + 30 * 24 * 60 * 60
        conn.execute("UPDATE payments SET status='approved' WHERE id=?", (payment_id,))
        conn.execute("UPDATE users SET subscription_until=? WHERE user_id=?", (until, user_id))
        conn.commit()
        bot.send_message(user_id, "Payment approved! Your subscription is active.", reply_markup=main_keyboard(user_id))
    else:
        conn.execute("UPDATE payments SET status='rejected' WHERE id=?", (payment_id,))
        conn.commit()
        bot.send_message(user_id, "Payment receipt rejected.")
    conn.close()

@bot.message_handler(func=lambda m: m.text == "👥 Referral")
def referral(message):
    user = get_user(message.from_user.id)
    bot_username = bot.get_me().username
    referral_link = f"https://t.me/{bot_username}?start=ref_{message.from_user.id}"
    bot.send_message(message.chat.id, f"Referral Link:\n{referral_link}\n\nYour referrals: {user['referrals']}")

@bot.message_handler(func=lambda m: m.text == "🤖 Models")
def models(message):
    markup = InlineKeyboardMarkup()
    for model in CHAT_MODELS:
        markup.add(InlineKeyboardButton(model, callback_data=f"model:{model}"))
    markup.add(InlineKeyboardButton("Gemini", callback_data="model:Gemini"))
    bot.send_message(message.chat.id, "Choose a model:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("model:"))
def model_callback(call):
    bot.answer_callback_query(call.id)
    model = call.data.split(":", 1)[1]
    conn = get_db()
    conn.execute("UPDATE users SET model=? WHERE user_id=?", (model, call.from_user.id))
    conn.commit()
    conn.close()
    bot.send_message(call.message.chat.id, f"Model changed to {model}.")

@bot.message_handler(func=lambda m: m.text == "🧠 My Memory")
def memory_button(message):
    memory_waiting.add(message.from_user.id)
    bot.reply_to(message, "Send me anything you want me to remember.")

def process_memory_input(message):
    user_id = message.from_user.id
    memory_waiting.discard(user_id)
    text = (message.text or "").strip()
    conn = get_db()
    conn.execute("UPDATE users SET notes=? WHERE user_id=?", (text, user_id))
    conn.commit()
    conn.close()
    bot.reply_to(message, "🧠 Memory updated.")

@bot.message_handler(func=lambda m: m.text == "📊 My Account")
def account(message):
    user = get_user(message.from_user.id)
    bot.send_message(message.chat.id, f"Model: {user['model']}\nReferrals: {user['referrals']}")

@bot.message_handler(func=lambda m: m.text == "🔄 Restart")
def restart(message):
    conn = get_db()
    conn.execute("DELETE FROM messages WHERE user_id=?", (message.from_user.id,))
    conn.commit()
    conn.close()
    send_welcome(message, "\n\n🔄 Conversation cleared.")

@bot.message_handler(content_types=["text"])
def chat(message):
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return

    user_id = message.from_user.id
    if text == "🔙 Back to Chat":
        user_mode.pop(user_id, None)
        bot.send_message(message.chat.id, "🔙 Back to chat.", reply_markup=main_keyboard(user_id))
        return

    user = get_user(user_id, message.from_user.first_name, message.from_user.username)
    if not enforce_channel_join(message, user):
        return

    if user_id in memory_waiting:
        process_memory_input(message)
        return

    try:
        save_message(user_id, "user", text)
        answer = ask_ai(user_id, text)
        save_message(user_id, "assistant", answer)
        send_long_message(message, answer)
    except Exception as error:
        notify_admin_error("Chat", user_id, error)
        bot.reply_to(message, "Sorry, service temporarily unavailable.")

def main():
    init_database()
    print("BOSSAI is running...")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)

if __name__ == "__main__":
    main()
