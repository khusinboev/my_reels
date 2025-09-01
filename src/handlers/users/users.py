import asyncio
import contextlib
import logging
import re
import tempfile
import json
import shutil
import sys  # Botni to'xtatish uchun import qilindi
from datetime import datetime
from pathlib import Path

# Aiogram kutubxonalari
from aiogram import Router, F, Bot
from aiogram.enums import ChatType
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, FSInputFile, InputMediaPhoto, InputMediaVideo
from aiogram.exceptions import TelegramBadRequest

# Loyiha modullari (o'zingizning fayllaringiz)
from config import bot, ADMIN_ID, db, sql
from src.keyboards.keyboard_func import CheckData

# Instaloader kutubxonasi
import instaloader

# ----------------------- Logging -----------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
log = logging.getLogger("insta-bot")

# ----------------------- Regex -------------------------
# /p/, /reel/, /tv/, /stories/, /video/ kabi barcha formatlarni qamrab oladi
INSTAGRAM_URL_PATTERN = re.compile(
    r"(https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv|stories|video)/[A-Za-z0-9\-_]+)"
)

# ----------------------- Instaloader Sozlamalari -------------------

# MUHIM: Bu yerga FAQAT Instagram akkauntingizning USERNAME'ini kiriting.
# PAROL KERAK EMAS. Bot shu nomdagi sessiya faylini qidiradi.
INSTAGRAM_USERNAME = "intelsoftmeta@gmail.com"  # O'zgartiring!

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
# Bot endi serverda login qilishga urinmaydi, faqat tayyor sessiyani yuklaydi.
try:
    log.info(f"'{INSTAGRAM_USERNAME}' uchun saqlangan sessiya fayli yuklanmoqda...")
    # Sessiya fayli loyihaning asosiy papkasida bo'lishi kerak.
    # Masalan: /home/myreels/my_reels/YOUR_INSTAGRAM_USERNAME
    L.load_session_from_file(INSTAGRAM_USERNAME)
    log.info("Sessiya muvaffaqiyatli yuklandi. Bot ishga tayyor.")
except FileNotFoundError:
    log.critical(f"KRITIK XATO: Sessiya fayli ('{INSTAGRAM_USERNAME}') topilmadi!")
    log.critical("Iltimos, sessiya faylini lokal kompyuterda yaratib, serverdagi loyiha papkasiga yuklang.")
    log.critical("Bot sessiya faylisiz ishlay olmaydi va hozir to'xtatiladi.")
    # Botni to'xtatish, chunki u ishlay olmaydi.
    sys.exit("Sessiya fayli topilmadi. Bot to'xtatildi.")

# ----------------------- Router ------------------------
user_router = Router()


# --- Database Functions (Caching) ---
async def cache_download(user_id: int, url: str, title: str, file_ids: list[str], media_types: list[str]):
    """Bir nechta faylni JSON formatida keshlaydigan funksiya."""
    file_ids_json = json.dumps(file_ids)
    media_types_json = json.dumps(media_types)

    sql.execute(
        "INSERT INTO public.downloads (user_id, url, title, file_id, media_type, date) VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (url) DO UPDATE SET file_id = excluded.file_id, title = excluded.title, media_type = excluded.media_type, date = excluded.date",
        (user_id, url, title, file_ids_json, media_types_json, datetime.now()),
    )
    db.commit()


def get_cached_files(url: str):
    """Keshdan bir nechta faylni oladi."""
    sql.execute("SELECT file_id, title, media_type FROM public.downloads WHERE url=%s", (url,))
    row = sql.fetchone()
    if row:
        file_ids = json.loads(row[0])
        title = row[1]
        media_types = json.loads(row[2])
        return file_ids, title, media_types
    return None


# ------------------ Commands ---------------------------

@user_router.message(CommandStart())
async def start_cmd(message: Message):
    await message.answer(
        "👋 Botimizga xush kelibsiz!\n\n"
        "Instagramdan video, foto yoki karusel yuklab olish uchun havolani yuboring.\n\n"
        "Yordam uchun /help buyrug'ini ishlating.\n\n"
        "Dasturchi: @adkhambek_4",
        parse_mode="HTML"
    )


@user_router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "<b>Yordam:</b>\n"
        "- Instagram <i>post, reel, stories, karusel</i> havolasini yuboring.\n"
        "- Ochiq (public) va yopiq (private) akkauntlardan yuklab olish mumkin (agar bot akkaunti o'sha sahifaga a'zo bo'lsa).\n"
        "- Havolada xatolik bo‘lsa, qayta tekshirib yuboring.\n\n"
        "<b>Admin:</b> @adkhambek_4",
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

        with contextlib.suppress(TelegramBadRequest):
            await call.answer()
        await call.message.delete()
        await bot.send_message(
            chat_id=user_id,
            text="Botimizga xush kelibsiz! Instagram havolasini yuboring.",
            parse_mode="HTML"
        )
    except Exception as e:
        log.error(f"Error in check callback: {e}")
        await bot.send_message(ADMIN_ID[0], f"Error in check: {e}")


# ------------------ Downloader -------------------------

async def download_instagram_media(url: str) -> tuple[list[Path], str, str]:
    """
    Instaloader'ni asinxron tarzda ishlatib, media yuklaydi.
    Bu botning boshqa so'rovlarga javob berishini bloklab qo'ymaydi.
    """

    def sync_download(temp_dir_str: str):
        temp_dir = Path(temp_dir_str)
        match = re.search(r"/(p|reel|reels|tv|stories|video)/([^/]+)", url)
        if not match:
            raise ValueError("URLdan shortcode topilmadi.")
        shortcode = match.group(2)

        try:
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            L.download_post(post, target=temp_dir)

            title = post.owner_username
            description = post.caption or ""

            files = sorted(
                (f for f in temp_dir.iterdir() if f.is_file() and not f.name.endswith(('.txt', '.json', '.xz'))),
                key=lambda f: f.stat().st_mtime
            )
            return files, title, description
        except instaloader.exceptions.PrivateProfileNotFollowedException:
            raise Exception("Bu yopiq (private) akkaunt. Bot bu akkauntga a'zo emas.")
        except instaloader.exceptions.NotFoundException:
            raise Exception("Post topilmadi. Havola noto'g'ri yoki post o'chirilgan.")
        except instaloader.exceptions.LoginRequiredException:
            raise Exception(
                "Bu kontentni ko'rish uchun akkauntga kirish talab etiladi. Sessiya eskirgan bo'lishi mumkin.")
        except Exception as e:
            log.error(f"Instaloader'da kutilmagan xatolik: {e}")
            raise Exception("Instagramdan ma'lumot olishda noma'lum xatolik yuz berdi.")

    # Vaqtinchalik papka yaratamiz
    with tempfile.TemporaryDirectory() as temp_dir:
        files, title, description = await asyncio.to_thread(sync_download, temp_dir)
        # Fayllarni doimiyroq, lekin hali ham vaqtinchalik bo'lgan boshqa joyga ko'chiramiz,
        # chunki `with` bloki tugashi bilan `temp_dir` o'chib ketadi.
        permanent_files = []
        # Bu papkani keyinroq `finally` blokida o'zimiz o'chiramiz.
        permanent_dir = Path(tempfile.mkdtemp())
        for f in files:
            new_path = permanent_dir / f.name
            shutil.move(str(f), new_path)  # `move` ishonchliroq
            permanent_files.append(new_path)

    return permanent_files, title, description


async def send_media_group(message: Message, files: list[Path], caption: str):
    """Bir nechta media faylni (karusel) yuboradi"""
    media_group = []
    for idx, path in enumerate(files):
        file_caption = caption if idx == 0 else ""
        InputMedia = InputMediaPhoto if path.suffix.lower() in ('.jpg', '.jpeg', '.png') else InputMediaVideo
        media_group.append(InputMedia(media=FSInputFile(path), caption=file_caption, parse_mode="HTML"))

    sent_messages = await message.answer_media_group(media=media_group)
    sent_file_ids = [m.photo[-1].file_id if m.photo else m.video.file_id for m in sent_messages]
    media_types = ["photo" if m.photo else "video" for m in sent_messages]

    return sent_file_ids, media_types


# ------------------ Main Handler -----------------------

@user_router.message(F.chat.type == ChatType.PRIVATE, F.text)
async def process_message(message: Message):
    user_id = message.from_user.id
    check_status, channels = await CheckData.check_member(bot, user_id)

    if not check_status:
        await message.answer(
            "❗️ Iltimos, botdan foydalanishdan oldin quyidagi kanallarga a’zo bo‘ling:",
            reply_markup=await CheckData.channels_btn(channels)
        )
        return

    url_match = INSTAGRAM_URL_PATTERN.search(message.text)
    if not url_match:
        await message.answer("❌ Iltimos, to‘g‘ri Instagram havolasini yuboring.")
        return

    url = url_match.group(0).split("?")[0]

    # Keshni tekshirish
    cached = get_cached_files(url)
    if cached:
        file_ids, title, media_types = cached
        bot_info = await bot.get_me()
        caption = f"🎬 <b>{title}</b>\n\n📥 @{bot_info.username} orqali yuklab olindi"
        try:
            if len(file_ids) == 1:
                media_type = media_types[0]
                if media_type == "video":
                    await message.answer_video(video=file_ids[0], caption=caption, parse_mode="HTML")
                elif media_type == "photo":
                    await message.answer_photo(photo=file_ids[0], caption=caption, parse_mode="HTML")
            else:
                media_group = []
                for idx, (file_id, media_type) in enumerate(zip(file_ids, media_types)):
                    InputMedia = InputMediaPhoto if media_type == "photo" else InputMediaVideo
                    media_group.append(
                        InputMedia(media=file_id, caption=caption if idx == 0 else "", parse_mode="HTML"))
                await message.answer_media_group(media=media_group)
            return
        except TelegramBadRequest as e:
            log.warning(f"Keshdagi fayl ID topilmadi yoki yaroqsiz: {file_ids}. Xato: {e}. Qayta yuklanmoqda.")

    loading_msg = await message.answer("⏳ Yuklanmoqda, iltimos kuting...")
    temp_dir_to_clean = None

    try:
        files, title, description = await download_instagram_media(url)
        if files:
            temp_dir_to_clean = files[0].parent

        if not files:
            raise Exception("Hech qanday media fayl topilmadi.")

        bot_info = await bot.get_me()
        short_desc = (description[:200] + "...") if len(description) > 200 else description
        caption = f"🎬 <b>{title}</b>\n\n📝 {short_desc}\n\n📥 @{bot_info.username} orqali yuklab olindi"

        if len(files) == 1:
            path = files[0]
            media_type = "photo" if path.suffix.lower() in ('.jpg', '.jpeg', '.png') else "video"
            if media_type == "photo":
                sent = await message.answer_photo(photo=FSInputFile(path), caption=caption, parse_mode="HTML")
                sent_file_ids = [sent.photo[-1].file_id] if sent.photo else []
            else:
                sent = await message.answer_video(video=FSInputFile(path), caption=caption, parse_mode="HTML")
                sent_file_ids = [sent.video.file_id] if sent.video else []
            media_types = [media_type]
        else:
            sent_file_ids, media_types = await send_media_group(message, files, caption)

        if sent_file_ids:
            await cache_download(user_id, url, title, sent_file_ids, media_types)

    except Exception as e:
        await loading_msg.edit_text(f"⚠️ **Xatolik:**\n`{e}`")
        log.error(f"URL ({url}) ni yuklashda xatolik: {e}", exc_info=True)
        await bot.send_message(ADMIN_ID[0], f"Error: {e}\nURL: {url}")
    finally:
        with contextlib.suppress(TelegramBadRequest):
            await loading_msg.delete()
        if temp_dir_to_clean:
            try:
                shutil.rmtree(temp_dir_to_clean)
                log.info(f"Vaqtinchalik papka ({temp_dir_to_clean}) muvaffaqiyatli o'chirildi.")
            except Exception as e:
                log.error(f"Vaqtinchalik papkani ({temp_dir_to_clean}) o'chirishda xatolik: {e}")