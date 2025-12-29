# --- START OF FILE web_app.py ---

import asyncio
import secrets
import shutil
import uuid
import math
import string
import random
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, List
from pathlib import Path
import humanize
import aiofiles

from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, File, UploadFile, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from aiogram import Bot

# Импортируем наши модули
import database
import utils
from config import SECRET_KEY, UPLOAD_DIR, DB_CONFIG, BOOKS, PER_PAGE

# --- Настройка FastAPI ---

app = FastAPI(docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
templates = Jinja2Templates(directory="templates")

# --- Подготовка директорий ---
utils.ensure_upload_dir_exists()

if not os.path.exists("static"):
    os.makedirs("static")

app.mount("/static", StaticFiles(directory="static"), name="static")


# --- Вспомогательные функции для аутентификации ---

def generate_random_credentials():
    username = 'user_' + ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    password = ''.join(random.choices(string.ascii_letters + string.digits, k=12))
    return username, password

async def get_current_admin(request: Request):
    username = request.session.get('username')
    if not username:
        raise HTTPException(status_code=303, detail="Not authenticated", headers={"Location": "/login"})
    
    admin = await database.get_admin_by_username(username)
    if not admin:
        request.session.clear()
        raise HTTPException(status_code=303, detail="Not authenticated", headers={"Location": "/login"})
    
    return admin

async def require_main_admin(request: Request):
    admin = await get_current_admin(request)
    if admin['role'] != 'main_admin':
        raise HTTPException(status_code=403, detail="Access denied: main admin only")
    return admin

# --- Эндпоинты (маршруты) веб-панели ---

@app.on_event("startup")
async def on_startup():
    pass

# --- Маршруты для аутентификации ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    admin = await database.get_admin_by_username(username)
    
    if not admin or admin['password'] != password:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Неверное имя пользователя или пароль"
        })
    
    request.session['username'] = username
    request.session['role'] = admin['role']
    return RedirectResponse(url="/", status_code=303)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

@app.get("/", response_class=HTMLResponse)
async def admin_panel(request: Request, admin: dict = Depends(get_current_admin)):
    async with database.db_pool.acquire() as conn:
        stats = {
            'total_users': await conn.fetchval('SELECT COUNT(*) FROM users'),
            'total_hadiths': await conn.fetchval('SELECT COUNT(*) FROM hadiths'),
            'active_users': await conn.fetchval('SELECT COUNT(DISTINCT user_id) FROM progress'),
            'blocked_users': await conn.fetchval('SELECT COUNT(*) FROM users WHERE blocked = 1'),
            'pending_hadiths': await database.get_pending_hadiths_count()
        }
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "stats": stats,
        "admin": admin
    })

# --- Маршруты для работы с хадисами ---

@app.get("/hadiths", response_class=HTMLResponse)
async def hadiths_list(
    request: Request,
    page: int = 1,
    q: Optional[str] = None,
    book: Optional[str] = None,
    lang: Optional[str] = None,
    admin: dict = Depends(get_current_admin)
):
    result = await database.get_hadiths_list(
        page=page,
        per_page=PER_PAGE,
        search_query=q,
        book_filter=book,
        lang_filter=lang
    )
    total_pages = math.ceil(result['total_count'] / PER_PAGE)
    
    context = {
        "request": request,
        "hadiths": result['hadiths'],
        "books": BOOKS,
        "current_page": page,
        "total_pages": total_pages,
        "total_count": result['total_count'],
        "search_query": q or "",
        "book_filter": book or "",
        "lang_filter": lang or "",
        "admin": admin
    }
    return templates.TemplateResponse("hadiths.html", context)

@app.get("/hadiths-no-audio", response_class=HTMLResponse)
async def hadiths_without_audio_list(
    request: Request,
    page: int = 1,
    q: Optional[str] = None,
    admin: dict = Depends(get_current_admin)
):
    result = await database.get_hadiths_without_audio(
        page=page,
        per_page=10,
        search_query=q
    )
    total_pages = math.ceil(result['total_count'] / 10)
    
    context = {
        "request": request,
        "hadiths": result['hadiths'],
        "books": BOOKS,
        "search_query": q or "",
        "total_count": result['total_count'],
        "current_page": page,
        "total_pages": total_pages,
        "admin": admin
    }
    return templates.TemplateResponse("hadiths_no_audio.html", context)

@app.get("/hadiths/add", response_class=HTMLResponse)
async def add_hadith_form(request: Request, admin: dict = Depends(get_current_admin)):
    return templates.TemplateResponse("add_hadith.html", {
        "request": request,
        "books": BOOKS,
        "admin": admin
    })

@app.post("/hadiths/add")
async def add_hadith(request: Request, text: str = Form(...), language: str = Form(...), book: str = Form(...), admin: dict = Depends(get_current_admin)):
    if admin['role'] == 'main_admin':
        await database.save_hadith(text.strip(), language, book)
        return RedirectResponse(url="/hadiths", status_code=303)
    else:
        await database.create_pending_hadith(text.strip(), language, book, admin['username'])
        return RedirectResponse(url="/hadiths/add?success=pending", status_code=303)

@app.get("/hadiths/edit/{hadith_id}", response_class=HTMLResponse)
async def edit_hadith_form(request: Request, hadith_id: int, admin: dict = Depends(require_main_admin)):
    hadith = await database.get_hadith_by_id(hadith_id)
    if not hadith:
        raise HTTPException(status_code=404, detail="Хадис не найден")
    return templates.TemplateResponse("edit_hadith.html", {"request": request, "hadith": hadith, "admin": admin})

@app.post("/hadiths/edit/{hadith_id}")
async def edit_hadith(hadith_id: int, text: str = Form(...), admin: dict = Depends(require_main_admin)):
    await database.update_hadith(hadith_id, text.strip())
    return RedirectResponse(url="/hadiths", status_code=303)

@app.post("/hadiths/delete/{hadith_id}")
async def delete_hadith_endpoint(hadith_id: int, admin: dict = Depends(require_main_admin)):
    await database.delete_hadith(hadith_id)
    return RedirectResponse(url="/hadiths", status_code=303)

@app.post("/hadiths/upload-audio/{hadith_id}")
async def upload_hadith_audio(
    request: Request,
    hadith_id: int,
    audio_file: UploadFile = File(...),
    admin: dict = Depends(require_main_admin)
):
    """
    Загрузка аудио через обновленный utils.py.
    Исправляет Ошибку №8 (Audio/Voice type).
    """
    hadith = await database.get_hadith_by_id(hadith_id)
    if not hadith:
        raise HTTPException(status_code=404, detail="Хадис не найден")
    
    bot: Bot = request.app.state.bot
    # utils.process_hadith_audio теперь обрабатывает типы и БД
    success = await utils.process_hadith_audio(bot, hadith_id, audio_file)
    
    if success:
        return RedirectResponse(url="/hadiths-no-audio", status_code=303)
    else:
        raise HTTPException(status_code=500, detail="Ошибка обработки аудио. Возможно, файл слишком большой или формат не поддерживается.")

# --- Маршруты для пользователей и рассылки ---

@app.get("/users", response_class=HTMLResponse)
async def users_list(request: Request, page: int = 1, q: Optional[str] = None, admin: dict = Depends(require_main_admin)):
    tashkent_tz = ZoneInfo("Asia/Tashkent")
    result = await database.get_users_list(page=page, per_page=PER_PAGE, search_query=q)
    
    users = result['users']
    for user in users:
        ts = user.get('registered_at')
        if ts:
            utc_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("UTC"))
            user['registered_at_str'] = utc_dt.astimezone(tashkent_tz).strftime('%Y-%m-%d %H:%M:%S')
        else:
            user['registered_at_str'] = "N/A"
            
    return templates.TemplateResponse("users.html", {
        "request": request, 
        "users": users, 
        "search_query": q or "", 
        "admin": admin,
        "current_page": page,
        "total_pages": math.ceil(result['total_count'] / PER_PAGE),
        "total_count": result['total_count']
    })

@app.get("/users/stats/{user_id}", response_class=HTMLResponse)
async def user_stats_page(request: Request, user_id: str, admin: dict = Depends(require_main_admin)):
    user, stats = await database.get_user_detailed_stats_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    
    tashkent_tz = ZoneInfo("Asia/Tashkent")
    ts = user.get('registered_at')
    if ts:
        utc_dt = datetime.fromtimestamp(ts, tz=ZoneInfo("UTC"))
        user['registered_at_str'] = utc_dt.astimezone(tashkent_tz).strftime('%Y-%m-%d %H:%M:%S')
        user['registered_at_human'] = humanize.naturaltime(datetime.now(tz=ZoneInfo("UTC")) - utc_dt)
    else:
        user['registered_at_str'] = "N/A"
        user['registered_at_human'] = "неизвестно"
        
    return templates.TemplateResponse("user_stats.html", {"request": request, "user": user, "user_stats": stats, "admin": admin})

@app.post("/users/scan", response_class=RedirectResponse)
async def trigger_user_scan(request: Request, background_tasks: BackgroundTasks, admin: dict = Depends(require_main_admin)):
    bot: Bot = request.app.state.bot
    background_tasks.add_task(utils.perform_user_scan, bot)
    return RedirectResponse(url="/users", status_code=303)

@app.get("/users/message/{user_id}", response_class=HTMLResponse)
async def message_user_form(request: Request, user_id: str, admin: dict = Depends(require_main_admin)):
    user = await database.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return templates.TemplateResponse("send_user_message.html", {"request": request, "user": user, "admin": admin})

@app.post("/users/message/{user_id}")
async def message_user_send(request: Request, user_id: str, message: str = Form(...), admin: dict = Depends(require_main_admin)):
    bot: Bot = request.app.state.bot
    await utils.send_personal_message(bot, user_id, message.strip())
    return RedirectResponse(url="/users", status_code=303)

@app.get("/broadcast", response_class=HTMLResponse)
async def broadcast_form(request: Request, admin: dict = Depends(require_main_admin)):
    return templates.TemplateResponse("broadcast.html", {"request": request, "admin": admin})

@app.post("/broadcast_v2")
async def send_broadcast(
    request: Request,
    background_tasks: BackgroundTasks,
    message: str = Form(...),
    files: List[UploadFile] = File(default=[]),
    file_id: Optional[str] = Form(None),
    file_type: Optional[str] = Form(None), 
    admin: dict = Depends(require_main_admin)
):
    """
    Рассылка. Исправлена Ошибка №3 (Лимит 10 файлов).
    """
    clean_file_id = file_id.strip() if file_id else None
    
    # Фильтрация пустых файлов (браузер может отправлять пустой файл, если ничего не выбрано)
    valid_files = [f for f in files if f.filename]
    
    # СЕРВЕРНАЯ ПРОВЕРКА ЛИМИТА
    if len(valid_files) > 10:
        return templates.TemplateResponse("broadcast.html", {
            "request": request, 
            "admin": admin,
            "error": "❌ Ошибка! Нельзя загружать более 10 файлов для одного альбома. Telegram API не поддерживает альбомы > 10 элементов."
        })

    media_paths = []
    bot: Bot = request.app.state.bot
    
    if not clean_file_id and valid_files:
        for file in valid_files:
            file_ext = Path(file.filename).suffix
            temp_path = UPLOAD_DIR / f"{uuid.uuid4()}{file_ext}"
            
            async with aiofiles.open(temp_path, 'wb') as out_file:
                while content := await file.read(1024 * 1024):
                    await out_file.write(content)
                    
            media_paths.append(str(temp_path))

    # Задача в фон (utils.send_broadcast_message сама проверит размер фото)
    background_tasks.add_task(
        utils.send_broadcast_message,
        bot=bot,
        message_text=message.strip(),
        media_paths=media_paths,
        file_id=clean_file_id,
        file_type=file_type 
    )
    
    return templates.TemplateResponse("broadcast_result.html", {
        "request": request, 
        "background": True
    })

# --- Резервное копирование и остальное ---

@app.get("/backup", response_class=HTMLResponse)
async def backup_form(request: Request, admin: dict = Depends(require_main_admin)):
    return templates.TemplateResponse("backup.html", {"request": request, "admin": admin})

@app.get("/backup/export")
async def export_backup(admin: dict = Depends(require_main_admin)):
    filename = f"hadith_bot_backup_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.dump"
    dsn = f"postgresql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
    command = ["pg_dump", "-d", dsn, "-F", "c", "-b", "-v"]
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    
    async def stream_backup():
        while True:
            chunk = await process.stdout.read(8192)
            if not chunk: break
            yield chunk
    
    headers = {'Content-Disposition': f'attachment; filename="{filename}"'}
    return StreamingResponse(stream_backup(), media_type="application/octet-stream", headers=headers)

@app.post("/backup/import")
async def import_backup(request: Request, backup_file: UploadFile = File(...), admin: dict = Depends(require_main_admin)):
    if not backup_file.filename.endswith('.dump'):
        return templates.TemplateResponse("backup.html", {"request": request, "error": "Неверный формат файла.", "admin": admin})
        
    dsn = f"postgresql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
    command = ["pg_restore", "-d", dsn, "--clean", "--if-exists", "-v"]
    
    process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    
    while chunk := await backup_file.read(8192):
        process.stdin.write(chunk)
    await process.stdin.drain()
    process.stdin.close()
    
    stdout, stderr = await process.communicate()
    
    if process.returncode != 0:
        return templates.TemplateResponse("backup.html", {"request": request, "error": f"Ошибка восстановления: {stderr.decode()}", "admin": admin})
    
    return templates.TemplateResponse("backup.html", {"request": request, "success": "База восстановлена! Перезапустите бота.", "admin": admin})

@app.get("/participants", response_class=HTMLResponse)
async def participants_list(request: Request, admin: dict = Depends(require_main_admin)):
    participants = await database.get_all_admins()
    return templates.TemplateResponse("participants.html", {
        "request": request,
        "participants": participants,
        "admin": admin
    })

@app.post("/participants/create")
async def create_participant(request: Request, admin: dict = Depends(require_main_admin)):
    username, password = generate_random_credentials()
    await database.create_admin(username, password, 'participant', admin['username'])
    return RedirectResponse(url=f"/participants?new_user={username}&new_pass={password}", status_code=303)

@app.post("/participants/delete/{participant_id}")
async def delete_participant(participant_id: int, admin: dict = Depends(require_main_admin)):
    await database.delete_admin(participant_id)
    return RedirectResponse(url="/participants", status_code=303)

@app.get("/pending-hadiths", response_class=HTMLResponse)
async def pending_hadiths_list(request: Request, admin: dict = Depends(require_main_admin)):
    pending_hadiths = await database.get_pending_hadiths()
    return templates.TemplateResponse("pending_hadiths.html", {
        "request": request,
        "pending_hadiths": pending_hadiths,
        "admin": admin,
        "books": BOOKS
    })

@app.get("/pending-hadiths/edit/{hadith_id}", response_class=HTMLResponse)
async def edit_pending_hadith_form(request: Request, hadith_id: int, admin: dict = Depends(require_main_admin)):
    hadith = await database.get_pending_hadith_by_id(hadith_id)
    if not hadith:
        raise HTTPException(status_code=404, detail="Хадис не найден")
    return templates.TemplateResponse("edit_pending_hadith.html", {
        "request": request,
        "hadith": hadith,
        "books": BOOKS
    })

@app.post("/pending-hadiths/approve/{hadith_id}")
async def approve_pending_hadith_endpoint(hadith_id: int, edited_text: Optional[str] = Form(None), admin: dict = Depends(require_main_admin)):
    success = await database.approve_pending_hadith(hadith_id, edited_text)
    if success:
        return RedirectResponse(url="/pending-hadiths?success=approved", status_code=303)
    else:
        raise HTTPException(status_code=404, detail="Хадис не найден")

@app.post("/pending-hadiths/reject/{hadith_id}")
async def reject_pending_hadith_endpoint(hadith_id: int, admin: dict = Depends(require_main_admin)):
    await database.reject_pending_hadith(hadith_id)
    return RedirectResponse(url="/pending-hadiths?success=rejected", status_code=303)

@app.post("/pending-hadiths/update/{hadith_id}")
async def update_pending_hadith_endpoint(hadith_id: int, text: str = Form(...), admin: dict = Depends(require_main_admin)):
    await database.update_pending_hadith(hadith_id, text.strip())
    return RedirectResponse(url="/pending-hadiths", status_code=303)

@app.get("/api/last-hadith")
async def get_last_hadith(book: str, language: str, admin: dict = Depends(get_current_admin)):
    last_hadith = await database.get_last_hadith_by_book_and_language(book, language)
    if last_hadith:
        return {
            "success": True,
            "hadith": {
                "id": last_hadith['id'],
                "text": last_hadith['text'], 
                "hadith_number": last_hadith['hadith_number']
            }
        }
    else:
        return {
            "success": False,
            "message": "В этом сборнике пока нет хадисов."
        }

# --- END OF FILE web_app.py ---