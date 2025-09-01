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
INSTAGRAM_URL_PATTERN = re.compile(
    r"(https?://(?:www\.)?instagram\.com/"
    r"(?:p|reel|tv|stories)/[A-Za-z0-9_\-/.?=&]+)"
)

# ----------------------- Router ------------------------
user_router = Router()


async def cache_download(user_id: int, url: str, title: str, file_id: str, media_type: str):
    sql.execute(
        "INSERT INTO public.downloads (user_id, url, title, file_id, media_type, date) VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (url) DO UPDATE SET file_id = excluded.file_id, title = excluded.title, media_type = excluded.media_type, date = excluded.date",
        (user_id, url, title, file_id, media_type, datetime.now()),
    )
    db.commit()


def get_cached_file(url: str):
    sql.execute("SELECT file_id, title, media_type FROM public.downloads WHERE url=%s", (url,))
    row = sql.fetchone()
    if row:
        return row[0], row[1], row[2]
    return None


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
        "- Instagram <i>post, reel, tv, stories</i> havolasini yuboring.\n"
        "- Faqat <u>ochiq (public)</u> akkauntlardan yuklab olish mumkin.\n"
        "- Havolada xatolik bo'lsa, qayta yuboring.\n\n"
        "- Admin: @adkhambek_4",
        parse_mode="HTML"
    )


@user_router.callback_query(F.data == "check", F.message.chat.type == ChatType.PRIVATE)
async def check(call: CallbackQuery):
    user_id = call.from_user.id
    try:
        check_status, channels = await CheckData.check_member(bot, user_id)
        if not check_status:
            await call.answer(
                text="Botdan foydalanish uchun barcha kanallarga a'zo bo'ling.",
                show_alert=True
            )
            return

        with contextlib.suppress(Exception):
            await call.answer()
        await call.message.delete()
        await bot.send_message(
            chat_id=user_id,
            text="Botimizga xush kelibsiz! Instagram havolasini yuboring.",
            parse_mode="HTML"
        )

    except Exception as e:
        await bot.send_message(ADMIN_ID[0], f"Error in check: {e}")
        await bot.forward_message(
            chat_id=ADMIN_ID[0],
            from_chat_id=call.message.chat.id,
            message_id=call.message.message_id
        )


# ------------------ Downloader -------------------------

async def download_instagram(url: str, temp_dir: Path, progress_cb=None) -> tuple[list[Path], str, str]:
    import yt_dlp

    def hook(d):
        if d.get("status") == "downloading" and progress_cb:
            percent = d.get("_percent_str", "0%").strip()
            progress_cb(percent)

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'continue_download': False,
        'format': 'best[ext=mp4]/best',
        'outtmpl': str(temp_dir / '%(title).50s.%(ext)s'),
        'noplaylist': True,
        'ignoreerrors': True,
        'extract_flat': False,
        'force_overwrites': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get('title', 'Instagram media')
            description = info.get('description', '')

        files = sorted(f for f in temp_dir.iterdir() if f.is_file() and not f.name.startswith('.'))
        return files, title, description

    except Exception as e:
        log.error(f"Download error: {e}")
        raise


# ------------------ Main Handler -----------------------

@user_router.message(F.chat.type == ChatType.PRIVATE)
async def process_message(message: Message):
    user_id = message.from_user.id
    check_status, channels = await CheckData.check_member(bot, user_id)

    if not check_status:
        await message.answer(
            "❗ Iltimos, quyidagi kanallarga a'zo bo'ling:",
            reply_markup=await CheckData.channels_btn(channels)
        )
        return

    if not message.text:
        await message.answer("Noma'lum kontent. Instagram havolasini yuboring.")
        return

    url_match = INSTAGRAM_URL_PATTERN.search(message.text)
    if not url_match:
        await message.answer("❌ Iltimos, to'g'ri Instagram havolasini yuboring.")
        return

    url = url_match.group(0)
    url = url.split("?")[0].rstrip("/")

    cached = get_cached_file(url)
    if cached:
        file_id, title, media_type = cached
        caption = f"🎬 {title}\n\n📥 Yuklab olindi: @my_reels_robot"
        if media_type == "video":
            await message.answer_video(video=file_id, caption=caption)
        elif media_type == "photo":
            await message.answer_photo(photo=file_id, caption=caption)
        return

    loading_msg = await message.answer("⏳ Yuklanmoqda… 0%")
    progress = {"percent": "0%"}

    async def update_progress(new_text: str):
        try:
            await loading_msg.edit_text(new_text)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                log.warning(f"Edit error: {e}")
        except Exception as e:
            log.warning(f"Unexpected edit error: {e}")

    def progress_cb(percent):
        if percent != progress["percent"]:
            progress["percent"] = percent
            asyncio.create_task(update_progress(f"⏳ Yuklanmoqda… {percent}"))

    try:
        with tempfile.TemporaryDirectory() as temp_dir_str:
            temp_dir = Path(temp_dir_str)
            files, title, description = await download_instagram(url, temp_dir, progress_cb)

            if not files:
                raise Exception("Hech qanday media yuklanmadi.")

            short_desc = (description[:300] + "…") if description else ""
            caption = f"🎬 <b>{title}</b>\n\n📝 {short_desc}\n\n📥 Yuklab olindi: @my_reels_robot"

            sent_file_ids = []
            media_type = None

            for idx, path in enumerate(files):
                file_size = path.stat().st_size
                if file_size > 50 * 1024 * 1024:  # 50MB limit
                    continue

                cur_caption = caption if idx == 0 else None

                if path.suffix.lower() in ('.jpg', '.jpeg', '.png'):
                    sent = await message.answer_photo(
                        photo=FSInputFile(path),
                        caption=cur_caption,
                        parse_mode="HTML"
                    )
                    if sent.photo:
                        sent_file_ids.append(sent.photo[-1].file_id)
                        media_type = "photo"
                elif path.suffix.lower() == '.mp4':
                    sent = await message.answer_video(
                        video=FSInputFile(path),
                        caption=cur_caption,
                        parse_mode="HTML",
                        width=1920,
                        height=1080,
                        duration=0
                    )
                    if sent.video:
                        sent_file_ids.append(sent.video.file_id)
                        media_type = "video"

            if len(files) == 1 and sent_file_ids:
                await cache_download(user_id, url, title, sent_file_ids[0], media_type)

        await loading_msg.delete()

    except Exception as e:
        await loading_msg.edit_text("⚠️ Yuklashda xatolik yuz berdi. Iltimos, keyinroq urunib ko'ring.")
        log.error(f"Download error: {e}")
        await bot.send_message(ADMIN_ID[0], f"Error: {e}\nURL: {url}")
