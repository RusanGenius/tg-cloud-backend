import os
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from dotenv import load_dotenv
from supabase import create_client, Client

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command

# 1. Загрузка переменных
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- ЛОГИКА БОТА ---

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username
    try:
        data = {"id": user_id, "username": username}
        supabase.table("users").upsert(data).execute()
        await message.answer("Привет! Отправь мне файл, фото или видео.")
    except Exception as e:
        print(f"Ошибка БД: {e}")

# Обработка файлов с сохранением правильного типа
@dp.message(F.document | F.photo | F.video | F.audio)
async def handle_files(message: Message):
    user_id = message.from_user.id
    file_id = None
    file_name = "Без названия"
    file_type = "file" # По умолчанию
    file_size = 0

    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name or "document"
        file_size = message.document.file_size
        file_type = "file"
        
    elif message.photo:
        file_id = message.photo[-1].file_id
        file_name = f"photo_{message.date}.jpg"
        file_size = message.photo[-1].file_size
        file_type = "folder" # Хак: используем type='folder' для фото? НЕТ.
        # ВНИМАНИЕ: В нашей таблице constraint type IN ('file', 'folder').
        # Чтобы не переделывать базу данных, будем сохранять фото как 'file',
        # но в имени файла у нас есть .jpg, по нему и определим.
        file_type = "file" 

    elif message.video:
        file_id = message.video.file_id
        file_name = message.video.file_name or "video.mp4"
        file_size = message.video.file_size
        file_type = "file"

    if file_id:
        try:
            new_file = {
                "user_id": user_id,
                "name": file_name,
                "type": file_type, 
                "file_id": file_id,
                "size": file_size,
                "parent_id": None
            }
            supabase.table("items").insert(new_file).execute()
            await message.answer(f"✅ Сохранено: {file_name}")
        except Exception as e:
            print(f"Ошибка сохранения: {e}")
            await message.answer("Ошибка при сохранении в базу.")

# --- API СЕРВЕР ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запускаем бота в фоне
    polling_task = asyncio.create_task(dp.start_polling(bot))
    yield
    polling_task.cancel()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/files")
async def get_files(user_id: int, folder_id: str = None):
    query = supabase.table("items").select("*").eq("user_id", user_id)
    if folder_id and folder_id != "root":
        query = query.eq("parent_id", folder_id)
    else:
        query = query.is_("parent_id", "null")
    return query.execute().data

class DownloadRequest(BaseModel):
    user_id: int
    file_id: str
    file_name: str = "file" # Добавили имя файла, чтобы понять тип

@app.post("/api/download")
async def download_file(req: DownloadRequest):
    try:
        # Проверяем расширение файла
        is_photo = req.file_name.lower().endswith(('.jpg', '.jpeg', '.png'))
        is_video = req.file_name.lower().endswith(('.mp4', '.mov'))

        if is_photo:
            await bot.send_photo(chat_id=req.user_id, photo=req.file_id, caption="Вот твое фото 📸")
        elif is_video:
            await bot.send_video(chat_id=req.user_id, video=req.file_id, caption="Вот твое видео 🎥")
        else:
            await bot.send_document(chat_id=req.user_id, document=req.file_id, caption="Вот твой файл 📄")
            
        return {"status": "ok"}
    except Exception as e:
        print(f"Ошибка отправки: {e}")
        # Теперь мы возвращаем ошибку 500, чтобы сайт знал, что что-то пошло не так
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def root():
    return {"message": "Working"}