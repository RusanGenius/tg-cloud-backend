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
import aiohttp # Для скачивания картинок

# --- КОНФИГУРАЦИЯ ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- БОТ (ЗАГРУЗКА ФАЙЛОВ) ---
@dp.message(F.document | F.photo | F.video | F.audio)
async def handle_files(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    
    # 1. Сначала сохраняем юзера, если его нет
    try:
        supabase.table("users").upsert({"id": user_id, "username": username}).execute()
    except:
        pass

    file_id = None
    file_name = "Без названия"
    file_type = "file"
    file_size = 0

    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name or "doc"
        file_size = message.document.file_size
    elif message.photo:
        # Берем среднее качество для превью (или последнее для оригинала)
        file_id = message.photo[-1].file_id
        file_name = f"img_{message.date}.jpg"
        file_size = message.photo[-1].file_size
    elif message.video:
        file_id = message.video.file_id
        file_name = message.video.file_name or "video.mp4"
        file_size = message.video.file_size

    if file_id:
        try:
            # Важно: Сохраняем в корень (parent_id = None)
            new_file = {
                "user_id": user_id,
                "name": file_name,
                "type": "file", # Папки - это folder, все остальное - file
                "file_id": file_id,
                "size": file_size,
                "parent_id": None 
            }
            supabase.table("items").insert(new_file).execute()
            await message.answer(f"💾 Сохранено: {file_name}")
        except Exception as e:
            print(e)
            await message.answer("Ошибка сохранения.")

# --- API СЕРВЕР ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(dp.start_polling(bot))
    yield
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. Получение списка файлов
# Обновленный эндпоинт получения файлов
@app.get("/api/files")
async def get_files(user_id: int, folder_id: str = None, mode: str = 'strict'):
    # mode='strict' -> показывать только то, что лежит конкретно тут (для Папок)
    # mode='global' -> показывать всё рекурсивно (для Галереи/Фильтров)
    
    query = supabase.table("items").select("*").eq("user_id", user_id)
    
    if mode == 'global':
        # Глобальный режим: Игнорируем parent_id, просто берем все файлы
        # Но папки в глобальном списке нам не нужны, только контент
        query = query.neq("type", "folder")
    
    elif folder_id and folder_id != "null" and folder_id != "root":
        # Внутри конкретной папки
        query = query.eq("parent_id", folder_id)
    else:
        # В корне (только то, что не рассортировано, или папки корня)
        query = query.is_("parent_id", "null")
        
    query = query.order("type", desc=True).order("created_at", desc=True)
    return query.execute().data

# 2. Создание папки (ИСПРАВЛЕННОЕ)
class FolderRequest(BaseModel):
    user_id: int
    name: str
    parent_id: Optional[str] = None # Явно разрешаем null

@app.post("/api/create_folder")
async def create_folder(req: FolderRequest):
    try:
        # Логика: если пришло "null" строкой или None -> записываем как None (SQL NULL)
        parent = req.parent_id
        if parent == "null" or parent == "":
            parent = None
        
        new_folder = {
            "user_id": req.user_id,
            "name": req.name,
            "type": "folder",
            "parent_id": parent
        }
        # .execute() возвращает результат, если ошибка - вылетит исключение
        supabase.table("items").insert(new_folder).execute()
        return {"status": "ok"}
    except Exception as e:
        print(f"Error creating folder: {e}")
        # Возвращаем ошибку 500, чтобы фронтенд понял, что беда
        raise HTTPException(status_code=500, detail=str(e))

# 3. Удаление элемента
class DeleteRequest(BaseModel):
    item_id: str

@app.post("/api/delete")
async def delete_item(req: DeleteRequest):
    try:
        # Сначала проверяем, папка это или нет
        # (Это дополнительный запрос, но для надежности полезно)
        item = supabase.table("items").select("type").eq("id", req.item_id).execute()
        
        if item.data and item.data[0]['type'] == 'folder':
            # Если это папка, сначала "освобождаем" файлы внутри неё
            # Делаем update: ставим parent_id = null всем файлам, которые лежали в этой папке
            supabase.table("items").update({"parent_id": None}).eq("parent_id", req.item_id).execute()

        # Теперь спокойно удаляем сам объект (файл или папку)
        supabase.table("items").delete().eq("id", req.item_id).execute()
        return {"status": "deleted"}
        
    except Exception as e:
        print(f"Delete error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 4. Скачивание файла (отправка в ТГ)
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
        raise HTTPException(status_code=500, detail=str(e))

# 5. ПРЕВЬЮ КАРТИНОК (ПРОКСИ)
@app.get("/api/preview/{file_id}")
async def get_preview(file_id: str):
    try:
        # 1. Спрашиваем у Телеграма путь к файлу
        file_info = await bot.get_file(file_id)
        file_path = file_info.file_path
        
        # 2. Формируем ссылку на скачивание
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        
        # 3. Скачиваем и тут же отдаем браузеру (стриминг)
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=404)
                content = await resp.read()
                return Response(content=content, media_type="image/jpeg")
    except Exception as e:
        # Если ошибка, возвращаем пустоту или дефолтную картинку
        print(f"Preview error: {e}")
        raise HTTPException(status_code=404)

# 6. ПЕРЕМЕЩЕНИЕ ФАЙЛОВ (Добавить файл в папку)
class MoveRequest(BaseModel):
    file_id: str     # ID записи в таблице items (UUID)
    folder_id: str   # ID папки, куда кладем

@app.post("/api/move_file")
async def move_file(req: MoveRequest):
    try:
        # Просто обновляем parent_id у файла
        supabase.table("items").update({"parent_id": req.folder_id}).eq("id", req.file_id).execute()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def root():
    return {"message": "Telegram Cloud v2.0 Working"}