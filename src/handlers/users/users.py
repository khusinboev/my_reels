# /home/myreels/my_reels/src/handlers/users/users.py

import asyncio
import contextlib
import logging
import re
import tempfile
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Aiogram kutubxonalari
from aiogram import Router, F, Bot
from aiogram.enums import ChatType
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, FSInputFile, InputMediaPhoto, InputMediaVideo
from aiogram.exceptions import TelegramBadRequest

# Loyiha modullari (sizning fayllaringiz)
from config import bot, ADMIN_ID, db, sql, INSTAGRAM_USERNAME # INSTAGRAM_USERNAME config'dan import qilindi
from src.keyboards.keyboard_func import CheckData

# Instaloader kutubxonasi
import instaloader

# ----------------------- Logging -----------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
log = logging.getLogger("insta-bot")

# ----------------------- Regex -------------------------
INSTAGRAM_URL_PATTERN = re.compile(
    r"(https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv|stories|video)/[A-Za-z0-9\-_]+)"
)

# ----------------------- Instaloader Sozlamalari -------------------
L = instaloader.Instaloader(
    download_pictures=True,
    download_videos=True,
    download_video_thumbnails=False,
    download_geotags=False,
    download_comments=False,
    save_metadata=False,
    compress_json=False,
    filename_pattern="{shortcode}"
)

# --- Sessiyani yuklash logikasi ---
try:
    log.info(f"'{INSTAGRAM_USERNAME}' uchun saqlangan sessiya fayli yuklanmoqda...")
    # Sessiya fayli loyihaning asosiy papkasida (`main.py` yonida) bo'lishi kerak.
    L.load_session_from_file(INSTAGRAM_USERNAME)
    log.info("Sessiya muvaffaqiyatli yuklandi. Bot ishga tayyor.")
except FileNotFoundError:
    log.critical(f"KRITIK XATO: Sessiya fayli ('{INSTAGRAM_USERNAME}') topilmadi!")
    log.critical("Iltimos, sessiya faylini lokal kompyuterda yaratib, serverdagi loyiha papkasiga yuklang.")
    sys.exit("Sessiya fayli topilmadi. Bot to'xtatildi.")

# ----------------------- Router ------------------------
user_router = Router()

# ... (Database Functions, Commands, Check callback... - Bular avvalgi javobdagi bilan bir xil, o'zgarishsiz) ...
# --- Database Functions (Caching) ---
async def cache_download(user_id: int, url: str, title: str, file_ids: list[str], media_types: list[str]):
    file_ids_json = json.dumps(file_ids)
    media_types_json = json.dumps(media_types)
    sql.execute(
        "INSERT INTO public.downloads (user_id, url, title, file_id, media_type, date) VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (url) DO UPDATE SET file_id = excluded.file_id, title = excluded.title, media_type = excluded.media_type, date = excluded.date",
        (user_id, url, title, file_ids_json, media_types_json, datetime.now()),
    )
    db.commit()

def get_cached_files(url: str):
    sql.execute("SELECT file_id, title, media_type FROM public.downloads WHERE url=%s", (url,))
    row = sql.fetchone()
    if row:
        return json.loads(row[0]), row[1], json.loads(row[2])
    return None

# ------------------ Commands ---------------------------
@user_router.message(CommandStart())
async def start_cmd(message: Message):
    await message.answer(
        "👋 Botimizga xush kelibsiz!\n\n"
        "Instagramdan video, foto yoki karusel yuklab olish uchun havolani yuboring.\n\n"
        "Yordam uchun /help buyrug'ini ishlating.\n\n"
        "Dasturchi: @adkhambek_4", parse_mode="HTML"
    )

@user_router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "<b>Yordam:</b>\n"
        "- Instagram <i>post, reel, stories, karusel</i> havolasini yuboring.\n"
        "- Ochiq (public) va yopiq (private) akkauntlardan yuklab olish mumkin (agar bot akkaunti o'sha sahifaga a'zo bo'lsa).\n\n"
        "<b>Admin:</b> @adkhambek_4", parse_mode="HTML"
    )

@user_router.callback_query(F.data == "check", F.message.chat.type == ChatType.PRIVATE)
async def check(call: CallbackQuery):
    user_id = call.from_user.id
    try:
        check_status, channels = await CheckData.check_member(bot, user_id)
        if not check_status:
            return await call.answer("Botdan foydalanish uchun barcha kanallarga a'zo bo‘ling.", show_alert=True)
        await call.answer()
        await call.message.delete()
        await bot.send_message(chat_id=user_id, text="Kanallarga a'zo bo'ldingiz! Endi Instagram havolasini yuboring.", parse_mode="HTML")
    except Exception as e:
        log.error(f"Error in check callback: {e}")
        await bot.send_message(ADMIN_ID[0], f"Error in check: {e}")

# ------------------ Downloader (Asinxron va Xatoliklarga chidamli) -------------------------
async def download_instagram_media(url: str) -> tuple[list[Path], str, str]:
    def sync_download(temp_dir_str: str):
        temp_dir = Path(temp_dir_str)
        match = re.search(r"/(p|reel|reels|tv|stories|video)/([^/]+)", url)
        if not match: raise ValueError("URLdan shortcode topilmadi.")
        shortcode = match.group(2)

        try:
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            L.download_post(post, target=temp_dir)
            title = post.owner_username
            description = post.caption or ""
            files = sorted([f for f in temp_dir.iterdir() if f.is_file() and not f.name.endswith(('.txt', '.json', '.xz'))], key=lambda f: f.stat().st_mtime)
            return files, title, description
        except instaloader.exceptions.PrivateProfileNotFollowedException: raise Exception("Bu yopiq (private) akkaunt. Bot bu akkauntga a'zo emas.")
        except instaloader.exceptions.NotFoundException: raise Exception("Post topilmadi. Havola noto'g'ri yoki post o'chirilgan.")
        except instaloader.exceptions.LoginRequiredException: raise Exception("Bu kontentni ko'rish uchun akkauntga kirish talab etiladi. Sessiya eskirgan bo'lishi mumkin.")
        except Exception as e:
            log.error(f"Instaloader'da kutilmagan xatolik: {e}")
            raise Exception("Instagramdan ma'lumot olishda noma'lum xatolik yuz berdi.")

    permanent_dir = Path(tempfile.mkdtemp())
    with tempfile.TemporaryDirectory() as temp_dir:
        files, title, description = await asyncio.to_thread(sync_download, temp_dir)
        permanent_files = []
        for f in files:
            new_path = permanent_dir / f.name
            shutil.move(str(f), new_path)
            permanent_files.append(new_path)
    return permanent_files, title, description

async def send_media_group(message: Message, files: list[Path], caption: str):
    media_group = []
    for idx, path in enumerate(files):
        InputMedia = InputMediaPhoto if path.suffix.lower() in ('.jpg', '.jpeg', '.png') else InputMediaVideo
        media_group.append(InputMedia(media=FSInputFile(path), caption=caption if idx == 0 else "", parse_mode="HTML"))
    sent_messages = await message.answer_media_group(media=media_group)
    sent_file_ids = [m.photo[-1].file_id if m.photo else m.video.file_id for m in sent_messages]
    media_types = ["photo" if m.photo else "video" for m in sent_messages]
    return sent_file_ids, media_types

# ------------------ Main Handler (To'liq yangilangan) -----------------------
@user_router.message(F.chat.type == ChatType.PRIVATE, F.text)
async def process_message(message: Message):
    user_id = message.from_user.id
    check_status, _ = await CheckData.check_member(bot, user_id)
    if not check_status:
        # CheckData.channels_btn o'rniga sizning kodingizda nima bo'lsa, shuni ishlatish kerak
        # Misol uchun: `await get_channels_markup()`
        # Hozircha sizning kodingiz bo'yicha qoldiraman:
        from src.keyboards.keyboard import channels_btn
        return await message.answer("❗️ Iltimos, botdan foydalanishdan oldin quyidagi kanallarga a’zo bo‘ling:", reply_markup=await channels_btn())

    url_match = INSTAGRAM_URL_PATTERN.search(message.text)
    if not url_match:
        return await message.answer("❌ Iltimos, to‘g‘ri Instagram havolasini yuboring.")

    url = url_match.group(0).split("?")[0]
    bot_info = await bot.get_me()

    if cached := get_cached_files(url):
        file_ids, title, media_types = cached
        caption = f"🎬 <b>{title}</b>\n\n📥 @{bot_info.username} orqali yuklab olindi"
        try:
            if len(file_ids) == 1:
                await bot.send_chat_action(message.chat.id, 'upload_video' if media_types[0] == 'video' else 'upload_photo')
                if media_types[0] == "video": await message.answer_video(video=file_ids[0], caption=caption, parse_mode="HTML")
                else: await message.answer_photo(photo=file_ids[0], caption=caption, parse_mode="HTML")
            else:
                await bot.send_chat_action(message.chat.id, 'upload_document')
                media_group = [ (InputMediaPhoto if mt == 'photo' else InputMediaVideo)(media=fid, caption=caption if i == 0 else "", parse_mode="HTML") for i, (fid, mt) in enumerate(zip(file_ids, media_types)) ]
                await message.answer_media_group(media=media_group)
            return
        except TelegramBadRequest as e:
            log.warning(f"Keshdagi fayl ID yaroqsiz: {e}. Qayta yuklanmoqda.")

    loading_msg = await message.answer("⏳ Yuklanmoqda, iltimos kuting...")
    temp_dir_to_clean = None
    try:
        files, title, description = await download_instagram_media(url)
        if files: temp_dir_to_clean = files[0].parent
        if not files: raise Exception("Hech qanday media fayl topilmadi.")

        short_desc = (description[:200] + "...") if len(description) > 200 else description
        caption = f"🎬 <b>{title}</b>\n\n📝 {short_desc}\n\n📥 @{bot_info.username} orqali yuklab olindi"

        if len(files) == 1:
            path = files[0]
            media_type = "photo" if path.suffix.lower() in ('.jpg', '.jpeg', '.png') else "video"
            await bot.send_chat_action(message.chat.id, 'upload_photo' if media_type == 'photo' else 'upload_video')
            if media_type == "photo": sent = await message.answer_photo(FSInputFile(path), caption=caption, parse_mode="HTML")
            else: sent = await message.answer_video(FSInputFile(path), caption=caption, parse_mode="HTML")
            sent_file_ids = [sent.photo[-1].file_id if sent.photo else sent.video.file_id]
            media_types = [media_type]
        else:
            await bot.send_chat_action(message.chat.id, 'upload_document')
            sent_file_ids, media_types = await send_media_group(message, files, caption)

        if sent_file_ids:
            await cache_download(user_id, url, title, sent_file_ids, media_types)

    except Exception as e:
        await loading_msg.edit_text(f"⚠️ **Xatolik:**\n`{e}`", parse_mode="Markdown")
        log.error(f"URL ({url}) ni yuklashda xatolik: {e}", exc_info=True)
        await bot.send_message(ADMIN_ID[0], f"Error: {e}\nURL: {url}")
    finally:
        await contextlib.suppress(TelegramBadRequest)
        await loading_msg.delete()
        if temp_dir_to_clean:
            try:
                shutil.rmtree(temp_dir_to_clean)
            except Exception as e:
                log.error(f"Vaqtinchalik papkani o'chirishda xatolik: {e}")