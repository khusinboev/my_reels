import asyncio
import contextlib
import logging
import re
import tempfile
from datetime import datetime
from pathlib import Path

from aiogram import Router, F, Bot
from aiogram.enums import ChatType
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, FSInputFile, InputMediaPhoto, InputMediaVideo
from aiogram.exceptions import TelegramBadRequest

from config import bot, ADMIN_ID, db, sql  # O'zingizning config faylingiz
from src.keyboards.keyboard_func import CheckData # O'zingizning keyboard faylingiz

# ----------------------- Logging -----------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("insta-bot")

# ----------------------- Regex -------------------------
# /p/, /reel/, /tv/, /stories/, /video/ kabi barcha formatlarni qamrab oladi
INSTAGRAM_URL_PATTERN = re.compile(
    r"(https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv|stories|video)/[A-Za-z0-9\-_]+)"
)

# ----------------------- Instaloader -------------------
import instaloader

# MUHIM: Bu yerga o'zingizning Instagram akkauntingiz ma'lumotlarini kiriting
# Bu akkaunt orqali bot Instagramga kiradi. Xavfsizlik uchun alohida akkaunt ochish tavsiya etiladi.
INSTAGRAM_USERNAME = "intelsoftmeta@gmail.com"  # O'zgartiring
INSTAGRAM_PASSWORD = "paro!123"  # O'zgartiring

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

# Bir marta bot ishga tushganda login qilib olamiz
try:
    log.info("Instagram akkauntiga kirilmoqda...")
    L.load_session_from_file(INSTAGRAM_USERNAME)
except FileNotFoundError:
    L.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
    L.save_session_to_file(INSTAGRAM_USERNAME)
log.info("Instagram akkauntiga muvaffaqiyatli kirildi.")


# ----------------------- Router ------------------------
user_router = Router()


# --- Database Functions (Caching) ---
async def cache_download(user_id: int, url: str, title: str, file_ids: list[str], media_types: list[str]):
    """Bir nechta faylni keshlaydigan funksiya"""
    # Ma'lumotlarni JSON formatida saqlash osonroq
    import json
    file_ids_json = json.dumps(file_ids)
    media_types_json = json.dumps(media_types)

    sql.execute(
        "INSERT INTO public.downloads (user_id, url, title, file_id, media_type, date) VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (url) DO UPDATE SET file_id = excluded.file_id, title = excluded.title, media_type = excluded.media_type, date = excluded.date",
        (user_id, url, title, file_ids_json, media_types_json, datetime.now()),
    )
    db.commit()

def get_cached_files(url: str):
    """Keshdan bir nechta faylni olish"""
    import json
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

# ... (sizning `check` funksiyangiz o'zgarishsiz qoladi) ...
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


# ------------------ Downloader (YANGILANGAN) -------------------------

async def download_instagram_media(url: str) -> tuple[list[Path], str, str]:
    """
    Instaloader'ni asinxron tarzda ishlatib, media yuklaydi.
    Bu botning boshqa ishlarga bloklanib qolishini oldini oladi.
    """
    def sync_download(temp_dir_str: str):
        temp_dir = Path(temp_dir_str)
        # URLdan shortcode'ni ajratib olamiz
        match = re.search(r"/(p|reel|reels|tv|stories|video)/([^/]+)", url)
        if not match:
            raise ValueError("URLdan shortcode topilmadi.")
        shortcode = match.group(2)

        try:
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            L.download_post(post, target=temp_dir)

            title = post.owner_username
            description = post.caption or ""

            # Keraksiz fayllarni filtrlaymiz va to'g'ri tartiblaymiz
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
             raise Exception("Bu kontentni ko'rish uchun akkauntga kirish talab etiladi.")
        except Exception as e:
            log.error(f"Instaloader'da kutilmagan xatolik: {e}")
            raise Exception("Instagramdan ma'lumot olishda noma'lum xatolik yuz berdi.")

    with tempfile.TemporaryDirectory() as temp_dir:
        # Sinxron funksiyani asinxron chaqiramiz
        files, title, description = await asyncio.to_thread(sync_download, temp_dir)
        # Vaqtinchalik fayllarni doimiy joyga ko'chiramiz, chunki `with` bloki tugashi bilan ular o'chib ketadi
        permanent_files = []
        permanent_dir = Path(tempfile.mkdtemp())
        for f in files:
            new_path = permanent_dir / f.name
            f.rename(new_path)
            permanent_files.append(new_path)

    return permanent_files, title, description

async def send_media_group(message: Message, files: list[Path], caption: str):
    """Bir nechta media faylni (karusel) yuboradi"""
    media_group = []
    sent_file_ids = []
    media_types = []

    for idx, path in enumerate(files):
        # Birinchi faylga caption qo'shamiz
        file_caption = caption if idx == 0 else ""
        if path.suffix.lower() in ('.jpg', '.jpeg', '.png'):
            media_group.append(InputMediaPhoto(media=FSInputFile(path), caption=file_caption, parse_mode="HTML"))
            media_types.append("photo")
        elif path.suffix.lower() == '.mp4':
            media_group.append(InputMediaVideo(media=FSInputFile(path), caption=file_caption, parse_mode="HTML"))
            media_types.append("video")

    if media_group:
        sent_messages = await message.answer_media_group(media=media_group)
        for sent_msg in sent_messages:
            if sent_msg.photo:
                sent_file_ids.append(sent_msg.photo[-1].file_id)
            elif sent_msg.video:
                sent_file_ids.append(sent_msg.video.file_id)

    return sent_file_ids, media_types


# ------------------ Main Handler (YANGILANGAN) -----------------------

@user_router.message(F.chat.type == ChatType.PRIVATE, F.text)
async def process_message(message: Message):
    user_id = message.from_user.id
    # check_status, channels = await CheckData.check_member(bot, user_id)

    # if not check_status:
    #     await message.answer(
    #         "❗️ Iltimos, botdan foydalanishdan oldin quyidagi kanallarga a’zo bo‘ling:",
    #         reply_markup=await CheckData.channels_btn(channels)
    #     )
    #     return

    url_match = INSTAGRAM_URL_PATTERN.search(message.text)
    if not url_match:
        await message.answer("❌ Iltimos, to‘g‘ri Instagram havolasini yuboring. Masalan: `https://www.instagram.com/reel/...`")
        return

    # Linkni normallashtirish
    url = url_match.group(0)

    # Keshni tekshirish
    cached = get_cached_files(url)
    if cached:
        file_ids, title, media_types = cached
        caption = f"🎬 <b>{title}</b>\n\n📥 Yuklab olindi: @{bot.id}" # Bot username'ni avtomatik olish
        try:
            if len(file_ids) == 1:
                if media_types[0] == "video":
                    await message.answer_video(video=file_ids[0], caption=caption, parse_mode="HTML")
                elif media_types[0] == "photo":
                    await message.answer_photo(photo=file_ids[0], caption=caption, parse_mode="HTML")
            else: # Karusel uchun kesh
                media_group = []
                for idx, file_id in enumerate(file_ids):
                    file_caption = caption if idx == 0 else ""
                    if media_types[idx] == "photo":
                        media_group.append(InputMediaPhoto(media=file_id, caption=file_caption, parse_mode="HTML"))
                    else:
                        media_group.append(InputMediaVideo(media=file_id, caption=file_caption, parse_mode="HTML"))
                await message.answer_media_group(media=media_group)
            return
        except TelegramBadRequest:
            log.warning(f"Keshdagi fayl ID topilmadi: {file_ids}. Qayta yuklanmoqda.")
            # Agar keshdagi file_id Telegram serverlaridan o'chirilgan bo'lsa, qayta yuklaymiz

    loading_msg = await message.answer("⏳ Yuklanmoqda, iltimos kuting...")
    temp_dir_to_clean = None

    try:
        files, title, description = await download_instagram_media(url)
        temp_dir_to_clean = files[0].parent if files else None

        if not files:
            raise Exception("Hech qanday media fayl topilmadi.")

        short_desc = (description[:200] + "...") if len(description) > 200 else description
        bot_username = (await bot.get_me()).username
        caption = f"🎬 <b>{title}</b>\n\n📝 {short_desc}\n\n📥 @{bot_username} orqali yuklab olindi"

        sent_file_ids = []
        media_types = []

        if len(files) == 1:
            path = files[0]
            if path.suffix.lower() in ('.jpg', '.jpeg', '.png'):
                sent = await message.answer_photo(photo=FSInputFile(path), caption=caption, parse_mode="HTML")
                if sent.photo:
                    sent_file_ids.append(sent.photo[-1].file_id)
                    media_types.append("photo")
            elif path.suffix.lower() == '.mp4':
                sent = await message.answer_video(video=FSInputFile(path), caption=caption, parse_mode="HTML")
                if sent.video:
                    sent_file_ids.append(sent.video.file_id)
                    media_types.append("video")
        else: # Karusel (media group)
            sent_file_ids, media_types = await send_media_group(message, files, caption)

        # Muvaffaqiyatli yuborilgan bo'lsa keshga saqlaymiz
        if sent_file_ids:
            await cache_download(user_id, url, title, sent_file_ids, media_types)

        await loading_msg.delete()

    except Exception as e:
        await loading_msg.edit_text(f"⚠️ Xatolik: {e}")
        log.error(f"URL ({url}) ni yuklashda xatolik: {e}")
        await bot.send_message(ADMIN_ID[0], f"Error: {e}\nURL: {url}")
    finally:
        # Vaqtinchalik fayllarni tozalash
        if temp_dir_to_clean:
            try:
                import shutil
                shutil.rmtree(temp_dir_to_clean)
            except Exception as e:
                log.error(f"Vaqtinchalik papkani ({temp_dir_to_clean}) o'chirishda xatolik: {e}")

        with contextlib.suppress(Exception):
            await loading_msg.delete()