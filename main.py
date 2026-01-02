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
from aiogram.filters import CommandStart, CommandObject # Импорт для обработки /start аргументов
import aiohttp 

# --- КОНФИГУРАЦИЯ ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- ОБРАБОТЧИК /START (С Deep Linking) ---
@dp.message(CommandStart())
async def command_start(message: Message, command: CommandObject):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    
    # Регистрируем юзера
    try:
        supabase.table("users").upsert({"id": user_id, "username": username}).execute()
    except:
        pass

    args = command.args # Получаем аргументы после /start
    
    if args and args.startswith("file_"):
        # Формат: file_AgACAl... (Telegram File ID)
        requested_file_id = args.replace("file_", "")
        
        # Ищем файл в БД (проверка безопасности: есть ли такой файл вообще)
        # В этой версии доступ ПУБЛИЧНЫЙ (любой, у кого ссылка, получит файл)
        # Мы ищем по file_id, чтобы найти его тип и имя
        res = supabase.table("items").select("*").eq("file_id", requested_file_id).limit(1).execute()
        
        if res.data:
            file_data = res.data[0]
            await message.answer(f"📂 Вам отправили файл: <b>{file_data['name']}</b>", parse_mode="HTML")
            
            # Отправляем файл
            try:
                if file_data['name'].lower().endswith(('.jpg', '.jpeg', '.png')):
                    await message.answer_photo(file_data['file_id'])
                elif file_data['name'].lower().endswith(('.mp4', '.mov')):
                    await message.answer_video(file_data['file_id'])
                else:
                    await message.answer_document(file_data['file_id'])
            except Exception as e:
                await message.answer("Ошибка при отправке файла.")
        else:
            await message.answer("Файл не найден или был удален.")
            
    else:
        # Обычный старт
        await message.answer("Привет! Это твое облако ☁️\nНажми кнопку ниже, чтобы открыть.", 
                             reply_markup=None) # Здесь можно прикрепить Menu Button кодом, если нужно


# --- ОСТАЛЬНОЙ БОТ (ЗАГРУЗКА ФАЙЛОВ) ---
@dp.message(F.document | F.photo | F.video | F.audio)
async def handle_files(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    
    file_id = None
    file_name = "Без названия"
    file_size = 0

    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name or "doc"
        file_size = message.document.file_size
    elif message.photo:
        file_id = message.photo[-1].file_id
        file_name = f"img_{message.date}.jpg"
        file_size = message.photo[-1].file_size
    elif message.video:
        file_id = message.video.file_id
        file_name = message.video.file_name or "video.mp4"
        file_size = message.video.file_size

    if file_id:
        try:
            new_file = {
                "user_id": user_id,
                "name": file_name,
                "type": "file",
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

@app.get("/api/files")
async def get_files(user_id: int, folder_id: str = None, mode: str = 'strict'):
    query = supabase.table("items").select("*").eq("user_id", user_id)
    if mode == 'global':
        query = query.neq("type", "folder")
    elif folder_id and folder_id != "null" and folder_id != "root":
        query = query.eq("parent_id", folder_id)
    else:
        query = query.is_("parent_id", "null")
    query = query.order("type", desc=True).order("created_at", desc=True)
    return query.execute().data

class FolderRequest(BaseModel):
    user_id: int
    name: str
    parent_id: Optional[str] = None

@app.post("/api/create_folder")
async def create_folder(req: FolderRequest):
    try:
        parent = req.parent_id
        if parent == "null" or parent == "": parent = None
        new_folder = {"user_id": req.user_id, "name": req.name, "type": "folder", "parent_id": parent}
        supabase.table("items").insert(new_folder).execute()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class DeleteRequest(BaseModel):
    item_id: str

@app.post("/api/delete")
async def delete_item(req: DeleteRequest):
    try:
        item = supabase.table("items").select("type").eq("id", req.item_id).execute()
        if item.data and item.data[0]['type'] == 'folder':
            supabase.table("items").update({"parent_id": None}).eq("parent_id", req.item_id).execute()
        supabase.table("items").delete().eq("id", req.item_id).execute()
        return {"status": "deleted"}
    except Exception as e:
        print(f"Delete error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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

@app.get("/api/preview/{file_id}")
async def get_preview(file_id: str):
    try:
        file_info = await bot.get_file(file_id)
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200: raise HTTPException(status_code=404)
                content = await resp.read()
                return Response(content=content, media_type="image/jpeg")
    except Exception as e:
        raise HTTPException(status_code=404)

class MoveRequest(BaseModel):
    file_id: str
    folder_id: Optional[str] # Может быть None (корень)

@app.post("/api/move_file")
async def move_file(req: MoveRequest):
    try:
        supabase.table("items").update({"parent_id": req.folder_id}).eq("id", req.file_id).execute()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def root():
    return {"message": "Telegram Cloud v2.1 Working"}