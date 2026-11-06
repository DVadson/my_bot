import asyncio
import sqlite3
import json
import os
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

# Достаем ключи из окружения
TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY")

if not TOKEN or not DEEPSEEK_KEY:
    raise ValueError("Ошибка: Проверь файл .env! В нем должны быть BOT_TOKEN и DEEPSEEK_API_KEY")

DB_PATH = 'tasks.db'
TZ = ZoneInfo("Europe/Moscow")

bot = Bot(token=TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Клиент для работы с DeepSeek
ai_client = AsyncOpenAI(
    api_key=DEEPSEEK_KEY,
    base_url="https://api.deepseek.com"
)

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
    conn.commit()
    conn.close()

def add_task(user_id: int, text: str, remind_at: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('INSERT INTO tasks (user_id, text, remind_at) VALUES (?, ?, ?)',
                (user_id, text, remind_at))
    conn.commit()
    conn.close()

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

async def check_reminders():
    tasks = get_pending_tasks()
    for task_id, user_id, text in tasks:
        try:
            await bot.send_message(chat_id=user_id, text=f"⏰ Напоминание: {text}")
            delete_task(task_id, user_id)
        except Exception as e:
            print(f"Ошибка при отправке {task_id}: {e}")

# --- Клавиатура ---
def get_main_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📋 Список задач")
    builder.adjust(1)
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

@dp.message(Command('del'))
async def cmd_del(message: types.Message):
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("Напиши номер задачи, которую хочешь удалить. Например: /del 3")
        return
    
    task_id = int(args[1])
    delete_task(task_id, message.from_user.id)
    await message.answer(f"🗑 Задача {task_id} удалена (если она существовала).")

# --- Обработка кнопки и текста ---
@dp.message()
async def handle_text(message: types.Message):
    if message.text.startswith('/'):
        return

    if message.text == "📋 Список задач":
        await send_task_list(message.chat.id, message.from_user.id)
        return

    now_str = datetime.now(TZ).strftime('%Y-%m-%d %H:%M')
    
    prompt = f"""
    Ты — умный ассистент. Сейчас: {now_str} (по московскому времени).
    Пользователь пишет: "{message.text}"
    
    Твоя задача — понять, просит ли пользователь создать напоминание.
    
    Если он просит напомнить о чем-то (например, "напомни завтра в 10:00..."), ответь СТРОГО в формате JSON:
    {{"action": "remind", "task": "текст задачи", "remind_at": "ГГГГ-ММ-ДД ЧЧ:ММ"}}
    
    Если это просто вопрос или болтовня (например, "как дела?", "кто ты?"), ответь СТРОГО в формате JSON:
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
    init_db()
    scheduler.add_job(check_reminders, 'interval', seconds=30)
    scheduler.start()
    
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())