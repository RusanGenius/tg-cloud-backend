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
from aiogram.filters import CommandStart, CommandObject
import aiohttp 

# --- КОНФИГУРАЦИЯ ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- БОТ ---
@dp.message(CommandStart())
async def command_start(message: Message, command: CommandObject):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    
    try:
        supabase.table("users").upsert({"id": user_id, "username": username}).execute()
    except:
        pass

    args = command.args
    if args and args.startswith("file_"):
        requested_uuid = args.replace("file_", "")
        try:
            res = supabase.table("items").select("*").eq("id", requested_uuid).limit(1).execute()
            if res.data:
                file_data = res.data[0]
                await message.answer(f"📂 Вам отправили файл: <b>{file_data['name']}</b>", parse_mode="HTML")
                if file_data['type'] == 'folder':
                     await message.answer("Шеринг папок в разработке.")
                     return
                try:
                    f_id = file_data['file_id']
                    name = file_data['name'].lower()
                    if name.endswith(('.jpg', '.jpeg', '.png')):
                        await message.answer_photo(f_id)
                    elif name.endswith(('.mp4', '.mov')):
                        await message.answer_video(f_id)
                    else:
                        await message.answer_document(f_id)
                except:
                    await message.answer("Ошибка отправки.")
            else:
                await message.answer("Файл не найден.")
        except:
             await message.answer("Некорректная ссылка.")
    else:
        await message.answer("Привет! Это твое облако ☁️")

@dp.message(F.document | F.photo | F.video | F.audio)
async def handle_files(message: Message):
    user_id = message.from_user.id
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

# --- API ---
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

# --- ЭНДПОИНТЫ ---

@app.get("/api/profile")
async def get_profile_stats(user_id: int):
    try:
        res = supabase.table("items").select("type, name, size").eq("user_id", user_id).execute()
        items = res.data
        total_files = 0
        total_size_bytes = 0
        count_photos = 0
        count_videos = 0
        count_docs = 0
        count_folders = 0
        
        for i in items:
            total_size_bytes += (i['size'] or 0)
            if i['type'] == 'folder': count_folders += 1
            else:
                total_files += 1
                name = i['name'].lower()
                if name.endswith(('.jpg', '.jpeg', '.png')): count_photos += 1
                elif name.endswith(('.mp4', '.mov')): count_videos += 1
                else: count_docs += 1
        
        total_size_mb = round(total_size_bytes / (1024 * 1024), 2)
        return {
            "total_files": total_files,
            "total_size_mb": total_size_mb,
            "counts": {"photos": count_photos, "videos": count_videos, "docs": count_docs, "folders": count_folders}
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail="Stats error")

@app.get("/api/files")
async def get_files(user_id: int, folder_id: str = None, mode: str = 'strict'):
    query = supabase.table("items").select("*").eq("user_id", user_id)
    if mode == 'global': query = query.neq("type", "folder")
    elif mode == 'folders': query = query.eq("type", "folder")
    elif folder_id and folder_id != "null" and folder_id != "root": query = query.eq("parent_id", folder_id)
    else: query = query.is_("parent_id", "null")
    query = query.order("type", desc=True).order("created_at", desc=True)
    return query.execute().data

# НОВОЕ: Удаление ВСЕХ данных пользователя
class DeleteAllRequest(BaseModel):
    user_id: int

@app.post("/api/delete_all")
async def delete_all_data(req: DeleteAllRequest):
    try:
        # Просто удаляем всё, где user_id совпадает
        supabase.table("items").delete().eq("user_id", req.user_id).execute()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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

class RenameRequest(BaseModel):
    item_id: str
    new_name: str

@app.post("/api/rename")
async def rename_item(req: RenameRequest):
    try:
        supabase.table("items").update({"name": req.new_name}).eq("id", req.item_id).execute()
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
        if is_photo: await bot.send_photo(req.user_id, req.file_id, caption="📸")
        elif is_video: await bot.send_video(req.user_id, req.file_id, caption="🎥")
        else: await bot.send_document(req.user_id, req.file_id, caption="📄")
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
    folder_id: Optional[str]

@app.post("/api/move_file")
async def move_file(req: MoveRequest):
    try:
        supabase.table("items").update({"parent_id": req.folder_id}).eq("id", req.file_id).execute()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def root():
    return {"message": "Tg Cloud v2.5 Lang & DeleteAll"}