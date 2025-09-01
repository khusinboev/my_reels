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
from src.utils.cookie_refresher import refresh_cookies

# ----------------------- Logging -----------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("insta-bot")

# ----------------------- Regex -------------------------
# /p/, /reel/, /tv/, /stories/
INSTAGRAM_URL_PATTERN = re.compile(
    r"(https?://(?:www\.)?instagram\.com/"
    r"(?:p|reel|tv|stories)/[A-Za-z0-9_\-/.?=&]+)"
)

# ----------------------- Router ------------------------
user_router = Router()

INSTAGRAM_USERNAME = "your_username"
INSTAGRAM_PASSWORD = "your_password"

async def download_instagram(url: str, temp_dir: Path, progress_cb=None) -> tuple[list[Path], str, str]:
    import yt_dlp

    def hook(d):
        if d.get("status") == "downloading" and progress_cb:
            percent = d.get("_percent_str", "0%").strip()
            progress_cb(percent)

    opts = {
        "quiet": True,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": str(temp_dir / "%(title).50s.%(ext)s"),
        "noplaylist": False,
        "playlist_items": "1-10",
        "progress_hooks": [hook],
        "cookies": "/home/myreels/cookies.txt",
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        # Agar cookie yaroqsiz bo‘lsa, yangilash
        if "login required" in str(e).lower():
            await refresh_cookies(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        else:
            raise

    title = info.get("title", "Instagram media")
    description = info.get("description", "")

    files = sorted(f for f in temp_dir.iterdir() if f.is_file() and not f.name.startswith('.'))
    return files, title, description


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
            await call.answer(
                text="Botdan foydalanish uchun barcha kanallarga a'zo bo‘ling.",
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
    """Download Instagram media using yt-dlp."""
    import yt_dlp

    def hook(d):
        if d.get("status") == "downloading" and progress_cb:
            percent = d.get("_percent_str", "0%").strip()
            progress_cb(percent)

    opts = {
        "quiet": True,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": str(temp_dir / "%(title).50s.%(ext)s"),
        "noplaylist": False,  # Allow downloading multiple if carousel
        "playlist_items": "1-10",  # Limit to first 10 if many
        "progress_hooks": [hook],
        "cookies": "/home/myreels/cookies.txt",  # 🔑 cookie fayl yo‘li
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title", "Instagram media")
        description = info.get("description", "")

    # Collect downloaded files
    files = sorted(f for f in temp_dir.iterdir() if f.is_file() and not f.name.startswith('.'))
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

    # Normalize link
    url = url_match.group(0)
    url = url.split("?")[0].rstrip("/")

    # Check cache (only for single media)
    cached = get_cached_file(url)
    if cached:
        file_id, title, media_type = cached
        caption = f"🎬 {title}\n\n📥 Yuklab olindi: @my_reels_robot"
        if media_type == "video":
            await message.answer_video(video=file_id, caption=caption)
        elif media_type == "photo":
            await message.answer_photo(photo=file_id, caption=caption)
        return

    # Loading message
    loading_msg = await message.answer("⏳ Yuklanmoqda… 0%")

    loop = asyncio.get_running_loop()
    progress = {"percent": "0%"}

    async def update_progress(new_text: str):
        try:
            await loading_msg.edit_text(new_text)
        except TelegramBadRequest as e:
            if "message is not modified" in e.message:
                pass
            else:
                log.warning(f"Edit error: {e}")
        except Exception as e:
            log.warning(f"Unexpected edit error: {e}")

    def progress_cb(percent):
        if percent != progress["percent"]:
            progress["percent"] = percent
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(
                    update_progress(f"⏳ Yuklanmoqda… {percent}")
                )
            )

    try:
        with tempfile.TemporaryDirectory() as temp_dir_str:
            temp_dir = Path(temp_dir_str)
            files, title, description = await download_instagram(url, temp_dir, progress_cb)

            if not files:
                raise Exception("Hech qanday media yuklanmadi.")

            short_desc = (description[:300] + "…") if description else ""
            caption = f"🎬 <b>{title}</b>\n\n📝 {short_desc}\n\n📥 Yuklab olindi: @my_reels_robot"

            sent_file_ids = []
            for idx, path in enumerate(files):
                cur_caption = caption if idx == 0 else None
                media_type = None
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
                        parse_mode="HTML"
                    )
                    if sent.video:
                        sent_file_ids.append(sent.video.file_id)
                        media_type = "video"

            # Cache only if single media
            if len(files) == 1 and sent_file_ids:
                await cache_download(user_id, url, title, sent_file_ids[0], media_type)

        await loading_msg.delete()

    except TelegramBadRequest as e:
        await loading_msg.edit_text("⚠️ Telegram yuklashni rad etdi. Keyinroq urinib ko‘ring.")
        await bot.send_message(ADMIN_ID[0], f"BadRequest: {e}\nURL: {url}")
    except Exception as e:
        await loading_msg.edit_text("⚠️ Yuklashda xatolik yuz berdi.")
        await bot.send_message(ADMIN_ID[0], f"Error: {e}\nURL: {url}")
    finally:
        with contextlib.suppress(Exception):
            await loading_msg.delete()
