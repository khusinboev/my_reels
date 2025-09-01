import asyncio
import contextlib
import logging
import re
import tempfile
from datetime import datetime
from pathlib import Path

from aiogram import Router, F
from aiogram.enums import ChatType
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.exceptions import TelegramBadRequest

from config import bot, ADMIN_ID, db, sql
from src.keyboards.keyboard_func import CheckData

# ----------------------- Logging -----------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("insta-bot")

# ----------------------- Regex -------------------------
# Faqat: /p/, /reel/, /tv/ (stories uchun login kerak)
INSTAGRAM_URL_PATTERN = re.compile(
    r"(https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/[A-Za-z0-9_\-/.?=&]+)"
)

# ----------------------- Router ------------------------
user_router = Router()


# ------------------ Caching ---------------------------
async def cache_download(user_id: int, url: str, title: str, file_id: str, media_type: str):
    sql.execute(
        "INSERT INTO public.downloads (user_id, url, title, file_id, media_type, date) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (url) DO UPDATE SET file_id = excluded.file_id, "
        "title = excluded.title, media_type = excluded.media_type, date = excluded.date",
        (user_id, url, title, file_id, media_type, datetime.now()),
    )
    db.commit()


def get_cached_file(url: str):
    sql.execute("SELECT file_id, title, media_type FROM public.downloads WHERE url = %s", (url,))
    row = sql.fetchone()
    return row if row else None


# ------------------ Commands ---------------------------
@user_router.message(CommandStart())
async def start_cmd(message: Message):
    await message.answer(
        "👋 Botimizga xush kelibsiz!\n\n"
        "Instagramdan video yoki foto yuklab olish uchun havolani yuboring.\n\n"
        "Yordam uchun /help buyrug'ini ishlating.\n\n@adkhambek_4",
        parse_mode="HTML"
    )


@user_router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "<b>Yordam:</b>\n"
        "- Instagram <i>post, reel, tv</i> havolasini yuboring.\n"
        "- Faqat <u>ochiq (public)</u> akkauntlardan yuklab olish mumkin.\n"
        "- Havolada xatolik bo‘lsa, qayta yuboring.\n\n"
        "- Admin: @adkhambek_4",
        parse_mode="HTML"
    )


@user_router.callback_query(F.data == "check", F.message.chat.type == ChatType.PRIVATE)
async def check(call: CallbackQuery):
    user_id = call.from_user.id
    try:
        check_status, channels = await CheckData.check_member(bot, user_id)
        if not check_status:
            await call.answer("Botdan foydalanish uchun barcha kanallarga a'zo bo‘ling.", show_alert=True)
            return

        await call.answer()
        await call.message.delete()
        await bot.send_message(user_id, "Botimizga xush kelibsiz! Instagram havolasini yuboring.", parse_mode="HTML")

    except Exception as e:
        await bot.send_message(ADMIN_ID[0], f"Error in check: {e}")


# ------------------ Download with yt-dlp (NO LOGIN, NO BLOCK) -------------------------
async def download_instagram(url: str, temp_dir: Path) -> tuple[list[Path], str, str]:
    import yt_dlp

    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'outtmpl': str(temp_dir / '%(title).50s.%(ext)s'),
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'retries': 3,
        'fragment_retries': 3,
        'socket_timeout': 15,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        },
        'extractor_args': {'instagram': ['skip=comments']},
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=True)
        except Exception as e:
            if "429" in str(e) or "rate limit" in str(e).lower():
                raise Exception("Instagram so'rov chegarasiga yetdi. Iltimos, 5 daqiqadan keyin qayta urinib ko'ring.")
            elif "Private account" in str(e):
                raise Exception("Bu akkaunt shaxsiy. Yuklab olib bo'lmaydi.")
            else:
                raise Exception(f"Yuklab olib bo'lmadi: {str(e)}")

    title = info.get('title') or info.get('alt_title') or "Instagram media"
    description = info.get('description', '')

    # FAQAT media fayllar (mp4, jpg, jpeg, png)
    files = [
        f for f in temp_dir.iterdir()
        if f.is_file() and f.suffix.lower() in ['.mp4', '.jpg', '.jpeg', '.png']
    ]
    return files, title, description


# ------------------ Main Handler -----------------------
@user_router.message(F.chat.type == ChatType.PRIVATE)
async def process_message(message: Message):
    user_id = message.from_user.id
    check_status, channels = await CheckData.check_member(bot, user_id)

    if not check_status:
        await message.answer(
            "❗ Iltimos, quyidagi kanallarga a’zo bo‘ling:",
            reply_markup=await CheckData.channels_btn(channels)
        )
        return

    if not message.text:
        await message.answer("Noma'lum kontent. Instagram havolasini yuboring.")
        return

    url_match = INSTAGRAM_URL_PATTERN.search(message.text)
    if not url_match:
        await message.answer("❌ Iltimos, to‘g‘ri Instagram havolasini yuboring.")
        return

    url = url_match.group(0).split("?")[0].rstrip("/")

    # Keshdan tekshirish (faqat bitta media uchun)
    cached = get_cached_file(url)
    if cached:
        file_id, title, media_type = cached
        caption = f"🎬 {title}\n\n📥 Yuklab olindi: @my_reels_robot"
        try:
            if media_type == "video":
                await message.answer_video(video=file_id, caption=caption)
            elif media_type == "photo":
                await message.answer_photo(photo=file_id, caption=caption)
        except TelegramBadRequest:
            pass  # Agar file_id eskirgan bo'lsa, qayta yuklaymiz
        return

    # Yuklanmoqda...
    loading_msg = await message.answer("⏳ Yuklanmoqda…")

    try:
        with tempfile.TemporaryDirectory() as temp_dir_str:
            temp_dir = Path(temp_dir_str)
            files, title, description = await download_instagram(url, temp_dir)

            if not files:
                raise Exception("Hech qanday media topilmadi.")

            short_desc = (description[:300] + "…") if description else ""
            caption = f"🎬 <b>{title}</b>\n\n📝 {short_desc}\n\n📥 Yuklab olindi: @my_reels_robot"

            sent_file_ids = []
            final_media_type = None

            for idx, file_path in enumerate(files):
                cur_caption = caption if idx == 0 else None

                if file_path.suffix.lower() in ['.jpg', '.jpeg', '.png']:
                    sent = await message.answer_photo(
                        photo=FSInputFile(file_path),
                        caption=cur_caption,
                        parse_mode="HTML"
                    )
                    if sent.photo:
                        sent_file_ids.append(sent.photo[-1].file_id)
                        final_media_type = "photo"

                elif file_path.suffix.lower() == '.mp4':
                    sent = await message.answer_video(
                        video=FSInputFile(file_path),
                        caption=cur_caption,
                        parse_mode="HTML"
                    )
                    if sent.video:
                        sent_file_ids.append(sent.video.file_id)
                        final_media_type = "video"

            # Faqat bitta fayl bo'lsa keshga saqlash
            if len(files) == 1 and sent_file_ids:
                await cache_download(user_id, url, title, sent_file_ids[0], final_media_type)

        await loading_msg.delete()

    except Exception as e:
        log.exception(f"Download error: {e}")
        error_msg = str(e) if "429" not in str(e) else "⏳ So'rovlar juda ko'p. Iltimos, 5 daqiqa kuting va qayta urinib ko'ring."
        await loading_msg.edit_text(f"⚠️ {error_msg}")
        await bot.send_message(ADMIN_ID[0], f"Xatolik: {e}\nURL: {url}")

    finally:
        with contextlib.suppress(Exception):
            await loading_msg.delete()