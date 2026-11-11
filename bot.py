import asyncio
import sqlite3
import json
import os
import boto3
import imaplib
import email
from email.header import decode_header
from datetime import datetime
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from openai import AsyncOpenAI
from dotenv import load_dotenv

# Загружаем переменные из файла .env
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
ADMIN_ID = 802266675  # Твой Telegram ID

if not TOKEN or not DEEPSEEK_KEY:
    raise ValueError("Ошибка: Проверь файл .env! В нем должны быть BOT_TOKEN и DEEPSEEK_API_KEY")

DB_PATH = 'tasks.db'
TZ = ZoneInfo("Europe/Moscow")

# --- S3 НАСТРОЙКИ ---
S3_URL = "https://s3.twcstorage.ru"
S3_BUCKET = "mybot-storage"
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "VLSPTFLZ4APPVRLT1V3E")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "3e8Te5EzCc3ZItqQWFbb4hI47fRNzPsClVNLtLdG")

s3_client = boto3.client(
    's3',
    endpoint_url=S3_URL,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    region_name='ru-1'
)

bot = Bot(token=TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

ai_client = AsyncOpenAI(
    api_key=DEEPSEEK_KEY,
    base_url="https://api.deepseek.com"
)

def download_db():
    try:
        s3_client.download_file(S3_BUCKET, 'tasks.db', DB_PATH)
        print("База скачана из S3.")
    except Exception as e:
        print(f"База не найдена в S3: {e}")

def upload_db():
    try:
        s3_client.upload_file(DB_PATH, S3_BUCKET, 'tasks.db')
        print("База сохранена в S3.")
    except Exception as e:
        print(f"Ошибка загрузки базы в S3: {e}")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            text TEXT,
            remind_at TEXT
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS seen_emails (
            message_id TEXT PRIMARY KEY
        )
    ''')
    conn.commit()
    conn.close()

def is_email_seen(message_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT 1 FROM seen_emails WHERE message_id = ?', (message_id,))
    res = cur.fetchone()
    conn.close()
    return res is not None

def mark_email_seen(message_id: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('INSERT OR IGNORE INTO seen_emails (message_id) VALUES (?)', (message_id,))
    conn.commit()
    conn.close()
    upload_db()

def add_task(user_id: int, text: str, remind_at: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('INSERT INTO tasks (user_id, text, remind_at) VALUES (?, ?, ?)',
                (user_id, text, remind_at))
    conn.commit()
    conn.close()
    upload_db()

def get_pending_tasks():
    now_str = datetime.now(TZ).strftime('%Y-%m-%d %H:%M')
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT id, user_id, text FROM tasks WHERE remind_at <= ?', (now_str,))
    rows = cur.fetchall()
    conn.close()
    return rows

def delete_task(task_id: int, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('DELETE FROM tasks WHERE id = ? AND user_id = ?', (task_id, user_id))
    conn.commit()
    conn.close()
    upload_db()

async def check_reminders():
    tasks = get_pending_tasks()
    for task_id, user_id, text in tasks:
        try:
            await bot.send_message(chat_id=user_id, text=f"⏰ Напоминание: {text}")
            delete_task(task_id, user_id)
        except Exception as e:
            print(f"Ошибка при отправке {task_id}: {e}")

# --- ПРОВЕРКА ПОЧТЫ ---
def decode_mime_words(s):
    if not s:
        return ""
    return ''.join(word.decode(encoding or 'utf-8') if isinstance(word, bytes) else word for word, encoding in decode_header(s))

async def check_email(chat_id: int, silent=False):
    try:
        mail = imaplib.IMAP4_SSL("imap.yandex.ru")
        mail.login(EMAIL_USER, EMAIL_PASS)
        mail.select("inbox")
        
        status, messages = mail.search(None, "UNSEEN")
        if status != "OK":
            if not silent: await bot.send_message(chat_id=chat_id, text="Не удалось проверить почту.")
            mail.logout()
            return
            
        mail_ids = messages[0].split()
        if not mail_ids:
            if not silent: await bot.send_message(chat_id=chat_id, text="Новых писем нет.")
            mail.logout()
            return

        if not silent:
            await bot.send_message(chat_id=chat_id, text=f"Найдено {len(mail_ids)} новых писем. Обрабатываю...")
        
        new_emails_count = 0
        for i in mail_ids[-5:]:
            status, msg_data = mail.fetch(i, "(RFC822)")
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    
                    message_id = msg.get("Message-ID", "")
                    if message_id and is_email_seen(message_id):
                        continue 

                    sender = decode_mime_words(msg.get("From", "Неизвестный отправитель"))
                    subject = decode_mime_words(msg.get("Subject", "Без темы"))
                    
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                                break
                    else:
                        body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                    
                    prompt = f"""
                    Отправитель: {sender}
                    Тема: {subject}
                    Текст письма: {body[:2000]}
                    
                    Сделай краткое изложение этого письма на русском языке (максимум 2-3 предложения).
                    """
                    
                    try:
                        ai_response = await ai_client.chat.completions.create(
                            model="deepseek-chat",
                            messages=[{"role": "user", "content": prompt}],
                            temperature=0.3
                        )
                        summary = ai_response.choices[0].message.content.strip()
                        
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"📧 **Отправитель:** {sender}\n**Суть:** {summary}",
                            parse_mode="Markdown"
                        )
                        new_emails_count += 1
                    except Exception as e:
                        await bot.send_message(chat_id=chat_id, text=f"📧 {sender}: (Ошибка ИИ при пересказе)")
                        print(f"Ошибка DeepSeek: {e}")
                    
                    if message_id:
                        mark_email_seen(message_id)
                    mail.store(i, '+FLAGS', '\\Seen')

        if silent and new_emails_count == 0:
            pass 

        mail.logout()
    except Exception as e:
        print(f"Ошибка почты: {e}")
        if not silent: await bot.send_message(chat_id=chat_id, text="❌ Ошибка при подключении к почте. Проверь логи.")

# --- Клавиатура ---
def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📋 Список задач")
    builder.button(text="📬 Проверить почту")
    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)

async def send_task_list(chat_id: int, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('SELECT id, text, remind_at FROM tasks WHERE user_id = ? ORDER BY remind_at', (user_id,))
    rows = cur.fetchall()
    conn.close()
    
    if not rows:
        await bot.send_message(chat_id=chat_id, text="Список пуст.")
        return
    
    lines = [f"ID {task_id}: {text} — {remind_at}" for task_id, text, remind_at in rows]
    await bot.send_message(chat_id=chat_id, text="Твои задачи:\n" + "\n".join(lines))

# --- Команды ---
@dp.message(Command('start'))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я твой умный помощник.\n\n"
        "Просто напиши мне, что и когда напомнить. Например:\n"
        "«Напомни завтра в 18:30 купить хлеб»\n\n"
        "Или пользуйся кнопками ниже.",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command('list'))
async def cmd_list(message: types.Message):
    await send_task_list(message.chat.id, message.from_user.id)

@dp.message(Command('mail'))
async def cmd_mail(message: types.Message):
    await check_email(message.chat.id)

@dp.message(Command('del'))
async def cmd_del(message: types.Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Напиши номер задачи, которую хочешь удалить. Например: /del 3")
        return
    
    task_id = int(args[1])
    delete_task(task_id, message.from_user.id)
    await message.answer(f"🗑 Задача {task_id} удалена.")

# --- Обработка кнопок и текста ---
@dp.message()
async def handle_text(message: types.Message):
    if message.text.startswith('/'):
        return

    if message.text == "📋 Список задач":
        await send_task_list(message.chat.id, message.from_user.id)
        return

    if message.text == "📬 Проверить почту":
        await check_email(message.chat.id)
        return

    now_str = datetime.now(TZ).strftime('%Y-%m-%d %H:%M')
    
    prompt = f"""
    Ты — умный ассистент. Сейчас: {now_str} (по московскому времени).
    Пользователь пишет: "{message.text}"
    
    Твоя задача — понять, просит ли пользователь создать напоминание.
    
    Если он просит напомнить о чем-то, ответь СТРОГО в формате JSON:
    {{"action": "remind", "task": "текст задачи", "remind_at": "ГГГГ-ММ-ДД ЧЧ:ММ"}}
    
    Если это просто вопрос или болтовня, ответь СТРОГО в формате JSON:
    {{"action": "chat", "reply": "твой текстовый ответ пользователю"}}
    
    Не добавляй никаких других полей и разметки!
    """

    try:
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        
        response = await ai_client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": "Ты полезный ассистент, отвечающий только валидным JSON."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        
        result = json.loads(response.choices[0].message.content)
        
        if result.get("action") == "chat":
            await message.answer(result.get("reply", "Я вас не понял."))
            return
            
        if result.get("action") == "remind":
            task_text = result.get("task", "Без названия")
            remind_at = result.get("remind_at")
            
            datetime.strptime(remind_at, '%Y-%m-%d %H:%M')
            
            add_task(message.from_user.id, task_text, remind_at)
            await message.answer(f"✅ Записал: «{task_text}»\n⏰ Когда: {remind_at}")
            return
            
        raise ValueError("Неизвестный формат ответа")
        
    except Exception as e:
        print(f"Ошибка ИИ: {e}")
        await message.answer("❌ Не смог понять задачу. Попробуй переформулировать.")

async def main():
    download_db()
    init_db()
    upload_db()
    
    # Запускаем фоновую проверку почты каждые 10 минут (600 секунд)
    scheduler.add_job(check_email, 'interval', seconds=600, args=[ADMIN_ID], kwargs={'silent': True})
    
    scheduler.add_job(check_reminders, 'interval', seconds=30)
    scheduler.start()
    
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
