import os
import asyncio
from typing import Optional, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from dotenv import load_dotenv
from supabase import create_client, Client

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, LabeledPrice, PreCheckoutQuery, ContentType
from aiogram.filters import CommandStart, CommandObject
import aiohttp 

# --- CONFIGURATION ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ADMIN_USERNAME = "astermaneiro"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- HELPER FUNCTIONS ---

def check_is_blocked(user_id: int):
    try:
        res = supabase.table("users").select("username, is_blocked").eq("id", user_id).single().execute()
        if res.data:
            # Admin cannot be blocked
            if res.data['username'] == ADMIN_USERNAME: return
            if res.data.get('is_blocked', False):
                raise HTTPException(status_code=403, detail="USER_BLOCKED")
    except HTTPException:
        raise
    except:
        pass

def get_folder_tree_text(user_id, folder_id, indent=0):
    items = supabase.table("items").select("*").eq("user_id", user_id).eq("parent_id", folder_id).execute().data
    items.sort(key=lambda x: (x['type'] != 'folder', x['name']))
    
    text = ""
    for i, item in enumerate(items, 1):
        prefix = "    " * indent
        if item['type'] == 'folder':
            text += f"{prefix}{i}. Папка «{item['name']}»:\n"
            text += get_folder_tree_text(user_id, item['id'], indent + 1)
        else:
            text += f"{prefix}{i}. {item['name']}\n"
    return text

async def copy_folder_recursive(source_folder_id, target_user_id, target_parent_id=None):
    """Recursively copies a folder to another user."""
    folder_res = supabase.table("items").select("*").eq("id", source_folder_id).single().execute()
    if not folder_res.data: return
    
    source_folder = folder_res.data
    new_folder_data = {
        "user_id": target_user_id,
        "name": source_folder['name'],
        "type": "folder",
        "parent_id": target_parent_id
    }
    new_folder = supabase.table("items").insert(new_folder_data).execute().data[0]
    
    items = supabase.table("items").select("*").eq("parent_id", source_folder_id).execute().data
    
    for item in items:
        if item['type'] == 'folder':
            await copy_folder_recursive(item['id'], target_user_id, new_folder['id'])
        else:
            new_file = {
                "user_id": target_user_id,
                "name": item['name'],
                "type": "file",
                "file_id": item['file_id'],
                "size": item['size'],
                "parent_id": new_folder['id']
            }
            supabase.table("items").insert(new_file).execute()

async def send_folder_contents(chat_id, folder_id):
    """Recursively sends files to a chat."""
    items = supabase.table("items").select("*").eq("parent_id", folder_id).execute().data
    items.sort(key=lambda x: (x['type'] != 'folder', x['name']))

    for item in items:
        if item['type'] == 'folder':
            await bot.send_message(chat_id, f"📂 <b>{item['name']}</b>", parse_mode="HTML")
            await send_folder_contents(chat_id, item['id'])
        else:
            try:
                if item['name'].lower().endswith(('.jpg', '.jpeg', '.png')):
                    await bot.send_photo(chat_id, item['file_id'], caption=item['name'])
                elif item['name'].lower().endswith(('.mp4', '.mov')):
                    await bot.send_video(chat_id, item['file_id'], caption=item['name'])
                else:
                    await bot.send_document(chat_id, item['file_id'], caption=item['name'])
                await asyncio.sleep(0.3) # Anti-flood delay
            except:
                pass


# --- PAYMENT LOGIC (STARS) ---

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def successful_payment(message: Message):
    payment_info = message.successful_payment
    await message.answer(f"⭐ Спасибо за поддержку! Получено звёзд: {payment_info.total_amount}")


# --- BOT HANDLERS ---

@dp.message(CommandStart())
async def command_start(message: Message, command: CommandObject):
    user_id = message.from_user.id
    
    # Logic to fix missing usernames
    username = message.from_user.username
    if not username:
        username = message.from_user.first_name or "User"
    
    try:
        supabase.table("users").upsert({"id": user_id, "username": username}).execute()
    except:
        pass

    args = command.args
    
    # 1. FILE SHARING
    if args and args.startswith("file_"):
        requested_uuid = args.replace("file_", "")
        try:
            res = supabase.table("items").select("*").eq("id", requested_uuid).limit(1).execute()
            if res.data:
                file_data = res.data[0]
                await message.answer(f"📂 Вам отправили файл: <b>{file_data['name']}</b>", parse_mode="HTML")
                if file_data['type'] == 'folder':
                     await message.answer("Это папка. Используйте ссылку для папки.")
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
    
    # 2. FOLDER SHARING
    elif args and args.startswith("folder_"):
        folder_uuid = args.replace("folder_", "")
        try:
            res = supabase.table("items").select("*").eq("id", folder_uuid).eq("type", "folder").limit(1).execute()
            if res.data:
                folder_data = res.data[0]
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="☁️ Сохранить в облако", callback_data=f"save_{folder_uuid}")],
                    [InlineKeyboardButton(text="📥 Выгрузить в чат", callback_data=f"send_{folder_uuid}")],
                    [InlineKeyboardButton(text="👀 Посмотреть содержимое", callback_data=f"view_{folder_uuid}")]
                ])
                await message.answer(
                    f"📁 Вам отправили папку «<b>{folder_data['name']}</b>» с файлами.", 
                    reply_markup=kb, 
                    parse_mode="HTML"
                )
            else:
                await message.answer("Папка не найдена или удалена.")
        except:
            await message.answer("Некорректная ссылка на папку.")
            
    else:
        await message.answer("Привет! Отправь мне файлы для сохранения или открой Mini App.", 
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                 [InlineKeyboardButton(text="Открыть Tg Cloud", web_app={"url": "https://tg-cloud-frontend.vercel.app"})] 
                             ]))

@dp.callback_query(F.data.startswith("save_"))
async def cb_save_folder(callback: CallbackQuery):
    folder_id = callback.data.split("_")[1]
    user_id = callback.from_user.id
    await callback.answer("Начинаю копирование...")
    try:
        await copy_folder_recursive(folder_id, user_id, None)
        await callback.message.answer("✅ Папка успешно сохранена в ваше облако!")
    except:
        await callback.message.answer("Ошибка при копировании.")

@dp.callback_query(F.data.startswith("send_"))
async def cb_send_folder(callback: CallbackQuery):
    folder_id = callback.data.split("_")[1]
    await callback.answer("Начинаю отправку файлов...")
    await callback.message.answer("⏳ Выгрузка файлов началась...")
    try:
        await send_folder_contents(callback.from_user.id, folder_id)
        await callback.message.answer("✅ Выгрузка завершена.")
    except:
        await callback.message.answer("Ошибка при отправке.")

@dp.callback_query(F.data.startswith("view_"))
async def cb_view_folder(callback: CallbackQuery):
    folder_id = callback.data.split("_")[1]
    await callback.answer()
    
    # Get folder info for the owner's user_id
    folder_res = supabase.table("items").select("user_id, name").eq("id", folder_id).single().execute()
    if not folder_res.data:
        await callback.message.answer("Папка не найдена.")
        return
        
    tree_text = get_folder_tree_text(folder_res.data['user_id'], folder_id, indent=0)
    msg_text = f"Папка «{folder_res.data['name']}»:\n\n{tree_text}" if tree_text else f"Папка «{folder_res.data['name']}» пуста."
    
    if len(msg_text) > 4000: msg_text = msg_text[:4000] + "\n..."
    await callback.message.answer(msg_text)

@dp.message(F.document | F.photo | F.video | F.audio)
async def handle_files(message: Message):
    user_id = message.from_user.id
    
    # Check for block
    try: check_is_blocked(user_id)
    except: await message.answer("⛔ Ваш аккаунт заблокирован администратором."); return

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

# --- API INITIALIZATION ---

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

# --- REQUEST MODELS ---

class AdminRequest(BaseModel):
    admin_id: int
    target_user_id: Optional[int] = None

class DeleteAllRequest(BaseModel):
    user_id: int

class FolderRequest(BaseModel):
    user_id: int
    name: str
    parent_id: Optional[str] = None

class RenameRequest(BaseModel):
    item_id: str
    new_name: str

class ItemRequest(BaseModel):
    item_id: str

class DownloadRequest(BaseModel):
    user_id: int
    file_id: str
    file_name: str
    recipient_id: Optional[int] = None

class MoveRequest(BaseModel):
    file_id: str
    folder_id: Optional[str]

class InvoiceRequest(BaseModel):
    amount: int
    title: str = "Поддержка автора"
    description: str = "Донат на развитие проекта"

# --- API ENDPOINTS: ADMIN ---

@app.post("/api/admin/users")
async def get_all_users(req: AdminRequest):
    # Check if user is admin
    admin = supabase.table("users").select("username").eq("id", req.admin_id).single().execute()
    if not admin.data or admin.data['username'] != ADMIN_USERNAME:
        raise HTTPException(403, "Access Denied")
    
    users = supabase.table("users").select("*").order("id", desc=True).execute().data
    # Admin is always on top
    users.sort(key=lambda u: u['username'] != ADMIN_USERNAME)
    return users

@app.post("/api/admin/block")
async def toggle_block_user(req: AdminRequest):
    admin = supabase.table("users").select("username").eq("id", req.admin_id).single().execute()
    if not admin.data or admin.data['username'] != ADMIN_USERNAME:
        raise HTTPException(403, "Access Denied")
    
    if req.target_user_id == req.admin_id: return {"status": "error"} # Cannot block self

    curr = supabase.table("users").select("is_blocked").eq("id", req.target_user_id).single().execute()
    new_status = not curr.data.get('is_blocked', False)
    
    supabase.table("users").update({"is_blocked": new_status}).eq("id", req.target_user_id).execute()
    return {"status": "ok", "is_blocked": new_status}

@app.post("/api/admin/delete_user")
async def delete_user_admin(req: AdminRequest):
    admin = supabase.table("users").select("username").eq("id", req.admin_id).single().execute()
    if not admin.data or admin.data['username'] != ADMIN_USERNAME:
        raise HTTPException(403, "Access Denied")
    
    if req.target_user_id == req.admin_id: return {"status": "error"}

    supabase.table("items").delete().eq("user_id", req.target_user_id).execute()
    supabase.table("users").delete().eq("id", req.target_user_id).execute()
    return {"status": "ok"}


# --- API ENDPOINTS: CLIENT ---

@app.get("/api/profile")
async def get_profile_stats(user_id: int):
    check_is_blocked(user_id)
    try:
        res = supabase.table("items").select("type, name, size").eq("user_id", user_id).execute()
        items = res.data
        total_files = 0; total_size_bytes = 0
        count_photos = 0; count_videos = 0; count_docs = 0; count_folders = 0
        
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
    check_is_blocked(user_id)
    query = supabase.table("items").select("*").eq("user_id", user_id)
    if mode == 'global': query = query.neq("type", "folder")
    elif mode == 'folders': query = query.eq("type", "folder")
    elif folder_id and folder_id != "null" and folder_id != "root": query = query.eq("parent_id", folder_id)
    else: query = query.is_("parent_id", "null")
    query = query.order("type", desc=True).order("created_at", desc=True)
    return query.execute().data

@app.post("/api/delete_all")
async def delete_all_data(req: DeleteAllRequest):
    check_is_blocked(req.user_id)
    try:
        supabase.table("items").delete().eq("user_id", req.user_id).execute()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/create_folder")
async def create_folder(req: FolderRequest):
    check_is_blocked(req.user_id)
    try:
        parent = req.parent_id
        if parent == "null" or parent == "": parent = None
        new_folder = {"user_id": req.user_id, "name": req.name, "type": "folder", "parent_id": parent}
        supabase.table("items").insert(new_folder).execute()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/rename")
async def rename_item(req: RenameRequest):
    try:
        supabase.table("items").update({"name": req.new_name}).eq("id", req.item_id).execute()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/delete")
async def delete_item(req: ItemRequest):
    """Normal deletion: if it's a folder, files are moved to the root."""
    try:
        item = supabase.table("items").select("type").eq("id", req.item_id).execute()
        if item.data and item.data[0]['type'] == 'folder':
            supabase.table("items").update({"parent_id": None}).eq("parent_id", req.item_id).execute()
        supabase.table("items").delete().eq("id", req.item_id).execute()
        return {"status": "deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/delete_folder_recursive")
async def delete_folder_recursive_api(req: ItemRequest):
    """Recursively deletes a folder with all its contents."""
    try:
        async def recursive_del(folder_id):
             children = supabase.table("items").select("id, type").eq("parent_id", folder_id).execute().data
             for child in children:
                 if child['type'] == 'folder':
                     await recursive_del(child['id'])
                 else:
                     supabase.table("items").delete().eq("id", child['id']).execute()
             supabase.table("items").delete().eq("id", folder_id).execute()

        await recursive_del(req.item_id)
        return {"status": "deleted_recursive"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/download")
async def download_file(req: DownloadRequest):
    target_id = req.recipient_id if req.recipient_id else req.user_id
    
    if target_id != req.user_id:
        try:
            admin_check = supabase.table("users").select("username").eq("id", target_id).single().execute()
            if not admin_check.data or admin_check.data['username'] != ADMIN_USERNAME:
                raise HTTPException(status_code=403, detail="Access Denied: Only admin can redirect downloads")
        except:
            raise HTTPException(status_code=403, detail="Access Denied")

    if target_id == req.user_id:
        check_is_blocked(req.user_id)

    try:
        is_photo = req.file_name.lower().endswith(('.jpg', '.jpeg', '.png'))
        is_video = req.file_name.lower().endswith(('.mp4', '.mov'))
        
        if is_photo: await bot.send_photo(target_id, req.file_id, caption="📸")
        elif is_video: await bot.send_video(target_id, req.file_id, caption="🎥")
        else: await bot.send_document(target_id, req.file_id, caption="📄")
        
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

@app.post("/api/move_file")
async def move_file(req: MoveRequest):
    try:
        supabase.table("items").update({"parent_id": req.folder_id}).eq("id", req.file_id).execute()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/generate_invoice")
async def generate_invoice(req: InvoiceRequest):
    try:
        link = await bot.create_invoice_link(
            title=req.title,
            description=req.description,
            payload="donate",
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Stars", amount=req.amount)]
        )
        return {"link": link}
    except Exception as e:
        print(f"Error generating invoice: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def root():
    return {"message": "Tg Cloud v3.0"}