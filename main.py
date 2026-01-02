import os
import asyncio
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from dotenv import load_dotenv
from supabase import create_client, Client

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command, CommandObject
import aiohttp

# --- КОНФИГУРАЦИЯ ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Проверка ключей
if not BOT_TOKEN or not SUPABASE_URL:
    print("CRITICAL ERROR: Keys not found in .env")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- ЛОГИКА БОТА ---

@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    
    # 1. Сохраняем пользователя (upsert)
    try:
        supabase.table("users").upsert({"id": user_id, "username": username}).execute()
    except Exception as e:
        print(f"User DB Error: {e}")

    # 2. Проверяем аргументы запуска (Deep Linking) для шеринга файлов
    # Пример ссылки: t.me/BotName?start=file_123e4567-e89b...
    args = command.args
    
    if args and args.startswith("file_"):
        file_db_id = args.replace("file_", "")
        
        try:
            # Ищем файл в базе по ID (игнорируем владельца, так как это публичный доступ)
            data = supabase.table("items").select("*").eq("id", file_db_id).execute()
            
            if data.data:
                file_info = data.data[0]
                # Формируем красивую подпись
                me = await bot.get_me()
                caption = f"📂 <b>{file_info['name']}</b>\nПоделились через @{me.username}"
                
                # Отправляем в зависимости от типа
                if file_info['type'] == 'folder':
                     await message.answer("Этой ссылкой поделились папкой. Просмотр папок по ссылке пока недоступен.")
                elif '.jpg' in file_info['name'].lower() or '.png' in file_info['name'].lower():
                    await bot.send_photo(user_id, file_info['file_id'], caption=caption, parse_mode="HTML")
                elif '.mp4' in file_info['name'].lower() or '.mov' in file_info['name'].lower():
                    await bot.send_video(user_id, file_info['file_id'], caption=caption, parse_mode="HTML")
                else:
                    await bot.send_document(user_id, file_info['file_id'], caption=caption, parse_mode="HTML")
            else:
                await message.answer("❌ Файл не найден или был удален владельцем.")
                
        except Exception as e:
            print(f"Sharing error: {e}")
            await message.answer("Ошибка при получении файла.")
            
    else:
        # Обычный запуск
        await message.answer("Привет! Я твое личное облако ☁️.\n\n"
                             "1. Отправь мне файлы, фото или видео.\n"
                             "2. Нажми кнопку ниже, чтобы открыть Облако.", 
                             parse_mode="HTML")

# Обработка входящих файлов
@dp.message(F.document | F.photo | F.video | F.audio)
async def handle_files(message: Message):
    user_id = message.from_user.id
    
    # Пытаемся сохранить юзера, если вдруг его нет
    try:
        supabase.table("users").upsert({"id": user_id, "username": message.from_user.username}).execute()
    except:
        pass

    file_id = None
    file_name = "Без названия"
    file_type = "file" # По умолчанию
    file_size = 0

    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name or "doc"
        file_size = message.document.file_size
    elif message.photo:
        file_id = message.photo[-1].file_id
        file_name = f"photo_{message.date}.jpg"
        file_size = message.photo[-1].file_size
    elif message.video:
        file_id = message.video.file_id
        file_name = message.video.file_name or "video.mp4"
        file_size = message.video.file_size
    elif message.audio:
        file_id = message.audio.file_id
        file_name = message.audio.file_name or "audio.mp3"
        file_size = message.audio.file_size

    if file_id:
        try:
            # Сохраняем в корень (parent_id = None)
            new_file = {
                "user_id": user_id,
                "name": file_name,
                "type": "file",
                "file_id": file_id,
                "size": file_size,
                "parent_id": None 
            }
            supabase.table("items").insert(new_file).execute()
            await message.answer(f"💾 Сохранено: <b>{file_name}</b>", parse_mode="HTML")
        except Exception as e:
            print(f"Save error: {e}")
            await message.answer("Ошибка сохранения в базу данных.")

# --- API СЕРВЕР (FastAPI) ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запуск бота в фоновом режиме
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

# 1. Получение списка файлов (с поддержкой mode='global')
@app.get("/api/files")
async def get_files(user_id: int, folder_id: str = None, mode: str = 'strict'):
    # mode='strict' -> показывать только то, что лежит конкретно в folder_id (для Папок)
    # mode='global' -> показывать все файлы рекурсивно, игнорируя папки (для Галереи)
    
    query = supabase.table("items").select("*").eq("user_id", user_id)
    
    if mode == 'global':
        # Глобальный режим: Игнорируем parent_id, просто берем все файлы
        # Но сами папки в глобальном списке нам не нужны, только контент
        query = query.neq("type", "folder")
    
    elif folder_id and folder_id != "null" and folder_id != "root":
        # Внутри конкретной папки
        query = query.eq("parent_id", folder_id)
    else:
        # В корне (только то, что не рассортировано, или папки корня)
        query = query.is_("parent_id", "null")
        
    # Сортировка: сначала папки, потом новые файлы
    query = query.order("type", desc=True).order("created_at", desc=True)
    
    result = query.execute()
    return result.data

# 2. Создание папки
class FolderRequest(BaseModel):
    user_id: int
    name: str
    parent_id: Optional[str] = None

@app.post("/api/create_folder")
async def create_folder(req: FolderRequest):
    try:
        # Обработка null строк
        parent = req.parent_id
        if parent == "null" or parent == "":
            parent = None
        
        new_folder = {
            "user_id": req.user_id,
            "name": req.name,
            "type": "folder",
            "parent_id": parent
        }
        supabase.table("items").insert(new_folder).execute()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 3. Удаление элемента
class DeleteRequest(BaseModel):
    item_id: str

@app.post("/api/delete")
async def delete_item(req: DeleteRequest):
    try:
        # Проверяем тип удаляемого элемента
        item = supabase.table("items").select("type").eq("id", req.item_id).execute()
        
        if item.data and item.data[0]['type'] == 'folder':
            # Если это папка, отвязываем файлы (переносим в корень)
            supabase.table("items").update({"parent_id": None}).eq("parent_id", req.item_id).execute()

        # Удаляем сам элемент
        supabase.table("items").delete().eq("id", req.item_id).execute()
        return {"status": "deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 4. Скачивание (отправка в ЛС)
class DownloadRequest(BaseModel):
    user_id: int
    file_id: str
    file_name: str

@app.post("/api/download")
async def download_file(req: DownloadRequest):
    try:
        is_photo = req.file_name.lower().endswith(('.jpg', '.jpeg', '.png'))
        is_video = req.file_name.lower().endswith(('.mp4', '.mov'))

        if is_photo:
            await bot.send_photo(req.user_id, req.file_id, caption="📸")
        elif is_video:
            await bot.send_video(req.user_id, req.file_id, caption="🎥")
        else:
            await bot.send_document(req.user_id, req.file_id, caption="📄")
        return {"status": "ok"}
    except Exception as e:
        print(f"Download Error: {e}")
        raise HTTPException(status_code=500, detail="Bot blocked or network error")

# 5. Перемещение файла (Добавление в папку)
class MoveRequest(BaseModel):
    file_id: str
    folder_id: str

@app.post("/api/move_file")
async def move_file(req: MoveRequest):
    try:
        supabase.table("items").update({"parent_id": req.folder_id}).eq("id", req.file_id).execute()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 6. Прокси для картинок (Превью)
@app.get("/api/preview/{file_id}")
async def get_preview(file_id: str):
    try:
        file_info = await bot.get_file(file_id)
        file_path = file_info.file_path
        
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=404)
                content = await resp.read()
                return Response(content=content, media_type="image/jpeg")
    except Exception as e:
        # Если ошибка (например файл слишком большой для бота или просрочен)
        raise HTTPException(status_code=404)

@app.get("/")
async def root():
    return {"message": "TG Cloud API is Live"}