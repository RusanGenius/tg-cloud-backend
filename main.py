import os
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from dotenv import load_dotenv
from supabase import create_client, Client

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, BotCommand
from aiogram.filters import Command

# 1. Загружаем ключи из файла .env
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Проверка, что ключи на месте
if not BOT_TOKEN or not SUPABASE_URL:
    print("ОШИБКА: Не найдены ключи в .env файле!")

# 2. Инициализация Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 3. Инициализация Бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- ЛОГИКА БОТА ---

# Команда /start
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    
    # Пытаемся сохранить юзера в БД. Если он есть, ничего страшного (upsert)
    try:
        data = {"id": user_id, "username": username}
        supabase.table("users").upsert(data).execute()
        await message.answer("Привет! Я твое облако. Отправь мне файл, и я сохраню его.")
    except Exception as e:
        print(f"Ошибка БД: {e}")
        await message.answer("Ошибка подключения к базе данных.")

# Обработка файлов (Документы, Фото, Видео, Аудио)
@dp.message(F.document | F.photo | F.video | F.audio)
async def handle_files(message: Message):
    user_id = message.from_user.id
    file_id = None
    file_name = "Без названия"
    file_type = "file"
    file_size = 0

    # Определяем тип файла и берем ID
    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name or "document"
        file_size = message.document.file_size
    elif message.photo:
        # У фото несколько размеров, берем самый большой (последний)
        file_id = message.photo[-1].file_id
        file_name = f"photo_{message.date}.jpg"
        file_size = message.photo[-1].file_size
    elif message.video:
        file_id = message.video.file_id
        file_name = message.video.file_name or "video.mp4"
        file_size = message.video.file_size

    if file_id:
        try:
            # Сохраняем информацию о файле в таблицу items
            new_file = {
                "user_id": user_id,
                "name": file_name,
                "type": "file",
                "file_id": file_id,
                "size": file_size,
                "parent_id": None # Пока кидаем все в корень
            }
            supabase.table("items").insert(new_file).execute()
            await message.answer(f"✅ Файл '{file_name}' сохранен в облако!")
        except Exception as e:
            print(f"Ошибка сохранения: {e}")
            await message.answer("Не удалось сохранить файл в базу.")

# --- ЛОГИКА API (Веб-сервер для сайта) ---

# Функция запуска бота вместе с сервером
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Бот запускается...")
    # Запускаем поллинг в фоне
    polling_task = asyncio.create_task(dp.start_polling(bot))
    yield
    print("Бот останавливается...")
    polling_task.cancel()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

# Разрешаем запросы с любого сайта (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. Получить список файлов
@app.get("/api/files")
async def get_files(user_id: int, folder_id: str = None):
    # Если folder_id пустой, ищем файлы в корне (где parent_id is null)
    query = supabase.table("items").select("*").eq("user_id", user_id)
    
    if folder_id and folder_id != "root":
        query = query.eq("parent_id", folder_id)
    else:
        query = query.is_("parent_id", "null")
        
    response = query.execute()
    return response.data

# 2. Скачать файл (отправить его юзеру в личку)
class DownloadRequest(BaseModel):
    user_id: int
    file_id: str

@app.post("/api/download")
async def download_file(req: DownloadRequest):
    try:
        # Бот отправляет файл пользователю в чат
        await bot.send_document(chat_id=req.user_id, document=req.file_id)
        return {"status": "ok", "message": "File sent to Telegram chat"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Тестовая страница, чтобы проверить, работает ли сервер
@app.get("/")
async def root():
    return {"message": "Telegram Cloud API is working!"}