import asyncio
import contextlib
import logging
import re
import tempfile
import subprocess
import json
import functools
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple
import aiohttp
import time
from concurrent.futures import ThreadPoolExecutor
import shutil  # Qo'shildi: Topda import

from aiogram import Router, F
from aiogram.enums import ChatType
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, FSInputFile, InputMediaPhoto, InputMediaVideo
from aiogram.exceptions import TelegramBadRequest

from config import bot, ADMIN_ID, db, sql, INSTA_USERNAME, INSTA_PASSWORD
from src.keyboards.keyboard_func import CheckData

# ----------------------- Logging -----------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("insta-bot")
COOKIE_FILE_PATH = "/home/myreels/my_reels/instagram_cookies.txt"
# ----------------------- Constants ----------------------
INSTAGRAM_URL_PATTERN = re.compile(
    r"(https?://(?:www\.)?instagram\.com/"
    r"(?:p|reel|reels|tv|stories|highlights|s)/[A-Za-z0-9_\-/.?=&]+)"  # Regex kengaytirildi
)

# Cache expiry: 7 days
CACHE_EXPIRY_DAYS = 7
MAX_RETRIES = 3
RETRY_DELAY = 2
MAX_CONCURRENT_DOWNLOADS = 2

# User agents for requests
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]

# ----------------------- Router ------------------------
user_router = Router()
executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)


# ----------------------- Database Operations -----------
async def cache_download(user_id: int, url: str, title: str, file_ids: List[str], media_types: List[str]):
    """Cache downloaded media with multiple file support"""
    try:
        # Convert lists to JSON strings for storage
        file_ids_json = json.dumps(file_ids)
        media_types_json = json.dumps(media_types)

        sql.execute(
            "INSERT INTO public.downloads (user_id, url, title, file_id, media_type, date) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (url) DO UPDATE SET file_id = excluded.file_id, title = excluded.title, media_type = excluded.media_type, date = excluded.date",
            (user_id, url, title, file_ids_json, media_types_json, datetime.now()),
        )
        db.commit()
        log.info(f"Cached download for URL: {url}")
    except Exception as e:
        log.error(f"Cache save error: {e}")


def get_cached_file(url: str) -> Optional[Tuple[List[str], str, List[str]]]:
    """Get cached file with expiry check"""
    try:
        sql.execute(
            "SELECT file_id, title, media_type, date FROM public.downloads WHERE url=%s",
            (url,)
        )
        row = sql.fetchone()
        if row:
            cached_date = row[3]
            if datetime.now() - cached_date < timedelta(days=CACHE_EXPIRY_DAYS):
                # Parse JSON strings back to lists
                file_ids = json.loads(row[0]) if isinstance(row[0], str) else [row[0]]
                media_types = json.loads(row[2]) if isinstance(row[2], str) else [row[2]]
                return file_ids, row[1], media_types
            else:
                # Remove expired cache
                sql.execute("DELETE FROM public.downloads WHERE url=%s", (url,))
                db.commit()
    except Exception as e:
        log.error(f"Cache retrieve error: {e}")
    return None


# ----------------------- Downloaders -------------------

class InstagramDownloader:
    def __init__(self):
        self.session = None

    async def create_session(self):
        """Create aiohttp session with proper headers"""
        if not self.session:
            headers = {
                'User-Agent': USER_AGENTS[0],
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
            }
            timeout = aiohttp.ClientTimeout(total=60)
            self.session = aiohttp.ClientSession(headers=headers, timeout=timeout)

    async def close_session(self):
        if self.session:
            await self.session.close()
            self.session = None

    async def download_with_ytdlp(self, url: str, temp_dir: Path) -> Tuple[List[Path], str, str]:
        """Download using yt-dlp"""
        try:
            output_template = str(temp_dir / '%(title)s.%(ext)s')

            cmd = [
                'yt-dlp',
                '--no-warnings',
                '--extract-flat', 'false',
                '--write-info-json',
                '--output', output_template,
                url
            ]
            if os.path.exists(COOKIE_FILE_PATH):
                cmd.extend(['--cookies', COOKIE_FILE_PATH])  # Cookie faylini qo'shish

            result = await asyncio.get_event_loop().run_in_executor(
                executor,
                functools.partial(
                    subprocess.run,
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding='utf-8'  # Qo'shildi: Encoding
                )
            )

            if result.returncode == 0:
                files = sorted([f for f in temp_dir.iterdir()
                                if f.is_file() and not f.name.endswith(('.json', '.txt'))])

                if files:
                    # Try to extract title from info.json
                    info_files = list(temp_dir.glob('*.info.json'))
                    title = "Instagram Media"
                    description = ""

                    if info_files:
                        try:
                            with open(info_files[0], 'r', encoding='utf-8') as f:
                                info = json.load(f)
                                title = info.get('title', 'Instagram Media')
                                description = info.get('description', '')
                        except:
                            pass

                    return files, title, description

            raise Exception(f"yt-dlp failed: {result.stderr}")

        except Exception as e:
            log.error(f"yt-dlp error: {e}")
            raise

    async def download_with_gallerydl(self, url: str, temp_dir: Path) -> Tuple[List[Path], str, str]:
        """Download using gallery-dl"""
        try:
            config = {
                'extractor': {
                    'instagram': {
                        'directory': [str(temp_dir)],
                        'filename': '{category}_{id}.{extension}'
                    }
                }
            }
            if os.path.exists(COOKIE_FILE_PATH):
                config['extractor']['instagram']['cookies'] = COOKIE_FILE_PATH  # Cookie faylini qo'shish

            config_file = temp_dir / 'config.json'
            with open(config_file, 'w') as f:
                json.dump(config, f)

            cmd = [
                'gallery-dl',
                '--config', str(config_file),
                url
            ]

            result = await asyncio.get_event_loop().run_in_executor(
                executor,
                functools.partial(  # Fix: partial qo'shildi
                    subprocess.run,
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding='utf-8'  # Qo'shildi
                )
            )

            if result.returncode == 0:
                files = sorted([f for f in temp_dir.iterdir()
                                if f.is_file() and not f.name.endswith(('.json', '.txt'))])

                if files:
                    return files, "Instagram Media", ""  # TODO: Metadata dan title olish mumkin

            raise Exception(f"gallery-dl failed: {result.stderr}")

        except Exception as e:
            log.error(f"gallery-dl error: {e}")
            raise

    async def download_with_instaloader(self, url: str, temp_dir: Path) -> Tuple[List[Path], str, str]:
        """Download using instaloader with improved error handling"""

        def _download():
            import instaloader

            # Configure instaloader with optimized settings
            L = instaloader.Instaloader(
                download_pictures=True,
                download_videos=True,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                filename_pattern="{shortcode}",
                max_connection_attempts=3,
                request_timeout=30,
                rate_controller=None
            )
            # --- YANGI QISM: Instaloaderga login qilish ---
            SESSION_FILE = temp_dir / f"{INSTA_USERNAME}.session"  # Har bir foydalanuvchi uchun alohida sessiya

            if not INSTA_USERNAME or not INSTA_PASSWORD:
                log.warning("INSTA_USERNAME or INSTA_PASSWORD not set for Instaloader. May fail.")
                # Agar login ma'lumotlari yo'q bo'lsa, anonim urinishni davom ettirish
            else:
                try:
                    L.load_session_from_file(INSTA_USERNAME, filename=SESSION_FILE)
                    log.info(f"Instaloader session loaded for {INSTA_USERNAME}.")
                except FileNotFoundError:
                    log.info(f"Instaloader session file not found for {INSTA_USERNAME}. Logging in...")
                    L.login(INSTA_USERNAME, INSTA_PASSWORD)
                    L.save_session_to_file(SESSION_FILE)  # Sessiyani saqlash
                    log.info(f"Instaloader logged in and session saved for {INSTA_USERNAME}.")
                except Exception as e:
                    log.error(f"Instaloader login/session error: {e}")
                    # Login xatosi bo'lsa, keyingi metodga o'tish yoki xato qaytarish
                    raise Exception(f"Instaloader login failed: {e}")
            # --- YANGI QISM TUGADI ---
            # Add some delay to avoid rate limits
            time.sleep(2)

            # Extract shortcode from URL
            shortcode_match = re.search(r'/([A-Za-z0-9_-]+)/?(?:\?.*)?$', url)
            if not shortcode_match:
                raise Exception("Cannot extract shortcode from URL")

            shortcode = shortcode_match.group(1)

            try:
                post = instaloader.Post.from_shortcode(L.context, shortcode)
                L.download_post(post, target=temp_dir)

                title = f"{post.owner_username} - {(post.caption[:50] + '...') if post.caption else 'Instagram media'}"
                description = post.caption or ""

                files = sorted([f for f in temp_dir.iterdir()
                                if f.is_file() and not f.name.endswith(('.txt', '.json', '.xz'))
                                and not f.name.startswith('.')])

                return files, title, description

            except Exception as e:
                error_msg = str(e).lower()
                if any(phrase in error_msg for phrase in ['login required', '403', '401', 'private', 'not found']):
                    if 'login required' in error_msg or '403' in error_msg:
                        raise Exception("Content is private or login required")
                    elif '401' in error_msg:
                        raise Exception("Rate limited or unauthorized")
                    elif 'not found' in error_msg:
                        raise Exception("Content not found")
                raise Exception(f"Instaloader error: {e}")

        try:
            result = await asyncio.get_event_loop().run_in_executor(executor, _download)
            return result
        except Exception as e:
            log.error(f"Instaloader error: {e}")
            raise

    async def download_instagram(self, url: str, temp_dir: Path) -> Tuple[List[Path], str, str]:
        """Download Instagram content using multiple methods with fallback"""
        methods = [
            ("yt-dlp", self.download_with_ytdlp),
            ("gallery-dl", self.download_with_gallerydl),
            ("instaloader", self.download_with_instaloader),
        ]

        last_error = None

        for method_name, method in methods:
            for attempt in range(MAX_RETRIES):
                try:
                    log.info(f"Trying {method_name} (attempt {attempt + 1})")
                    await self.create_session()

                    result = await method(url, temp_dir)

                    if result[0]:  # If files were downloaded
                        log.info(f"Successfully downloaded with {method_name}")
                        return result

                except Exception as e:
                    last_error = e
                    log.warning(f"{method_name} attempt {attempt + 1} failed: {e}")

                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))

                await self.close_session()

        # If all methods failed
        error_msg = f"All download methods failed. Last error: {last_error}"
        log.error(error_msg)
        raise Exception(error_msg)


# ----------------------- Global downloader instance ----
downloader = InstagramDownloader()


# ----------------------- Commands -----------------------

@user_router.message(CommandStart())
async def start_cmd(message: Message):
    await message.answer(
        "🎬 <b>Instagram Downloader Bot</b>\n\n"
        "📱 Instagram'dan video va rasmlarni yuklab olish uchun havolani yuboring\n\n"
        "✨ <b>Qo'llab-quvvatlanadigan formatlar:</b>\n"
        "• Post, Reel, IGTV, Stories\n"
        "• Video va rasmlar\n"
        "• Bir nechta media fayllar\n\n"
        "ℹ️ Yordam: /help\n"
        "👨‍💻 Admin: @adkhambek_4",
        parse_mode="HTML"
    )


@user_router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "📖 <b>Foydalanish qo'llanmasi:</b>\n\n"
        "1️⃣ Instagram post, reel, IGTV yoki stories havolasini yuboring\n"
        "2️⃣ Bot avtomatik ravishda yuklab oladi\n"
        "3️⃣ Yuklangan fayllar sizga yuboriladi\n\n"
        "⚠️ <b>Muhim eslatmalar:</b>\n"
        "• Faqat ochiq (public) akkauntlar\n"
        "• Maxsus akkauntlar yuklanmaydi\n"
        "• Katta fayllar biroz vaqt olishi mumkin\n\n"
        "🔄 <b>Qo'llab-quvvatlanadigan havolalar:</b>\n"
        "• instagram.com/p/...\n"
        "• instagram.com/reel/...\n"
        "• instagram.com/tv/...\n"
        "• instagram.com/stories/...\n\n"
        "💬 Savol bo'lsa: @adkhambek_4",
        parse_mode="HTML"
    )


@user_router.message(Command("stats"))
async def stats_cmd(message: Message):
    """Show download statistics"""
    user_id = message.from_user.id
    try:
        # Get user's download count
        sql.execute("SELECT COUNT(*) FROM public.downloads WHERE user_id=%s", (user_id,))
        user_downloads = sql.fetchone()[0]

        # Get total downloads
        sql.execute("SELECT COUNT(*) FROM public.downloads")
        total_downloads = sql.fetchone()[0]

        await message.answer(
            f"📊 <b>Statistika:</b>\n\n"
            f"👤 Sizning yuklanmalaringiz: <b>{user_downloads}</b>\n"
            f"🌍 Jami yuklanmalar: <b>{total_downloads}</b>\n"
            f"🚀 Bot versiyasi: <b>2.1 Fixed</b>",  # Versiya update
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer("❌ Statistikani olishda xatolik")


@user_router.callback_query(F.data == "check", F.message.chat.type == ChatType.PRIVATE)
async def check(call: CallbackQuery):
    user_id = call.from_user.id
    try:
        check_status, channels = await CheckData.check_member(bot, user_id)
        if not check_status:
            await call.answer(
                text="❗ Botdan foydalanish uchun barcha kanallarga a'zo bo'ling.",
                show_alert=True
            )
            return

        with contextlib.suppress(Exception):
            await call.answer("✅ Tekshiruv muvaffaqiyatli!")

        await call.message.delete()
        await bot.send_message(
            chat_id=user_id,
            text="🎉 <b>Xush kelibsiz!</b>\n\n"
                 "Instagram havolasini yuboring va yuklab olishni boshlaylik!",
            parse_mode="HTML"
        )

    except Exception as e:
        log.error(f"Check callback error: {e}")
        await bot.send_message(ADMIN_ID[0], f"❌ Check callback error: {e}")


# ----------------------- Main Handler ------------------

async def send_media_files(message: Message, files: List[Path], title: str, description: str) -> List[str]:
    """Send media files to user and return file IDs"""
    sent_file_ids = []
    media_types = []

    # Prepare caption
    short_desc = (description[:200] + "...") if len(description) > 200 else description
    caption = f"🎬 <b>{title}</b>\n\n📝 {short_desc}\n\n📥 @my_reels_robot" if short_desc else f"🎬 <b>{title}</b>\n\n📥 @my_reels_robot"

    try:
        if len(files) == 1:
            # Single file
            file_path = files[0]
            if file_path.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp'):
                sent = await message.answer_photo(
                    photo=FSInputFile(file_path),
                    caption=caption,
                    parse_mode="HTML"
                )
                if sent.photo:
                    sent_file_ids.append(sent.photo[-1].file_id)
                    media_types.append("photo")
            elif file_path.suffix.lower() in ('.mp4', '.avi', '.mov', '.mkv'):
                sent = await message.answer_video(
                    video=FSInputFile(file_path),
                    caption=caption,
                    parse_mode="HTML"
                )
                if sent.video:
                    sent_file_ids.append(sent.video.file_id)
                    media_types.append("video")

        elif len(files) <= 10:
            # Multiple files (up to 10) - use media group
            media_list = []
            for i, file_path in enumerate(files[:10]):
                if file_path.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp'):
                    media_list.append(InputMediaPhoto(
                        media=FSInputFile(file_path),
                        caption=caption if i == 0 else None,
                        parse_mode="HTML" if i == 0 else None
                    ))
                elif file_path.suffix.lower() in ('.mp4', '.avi', '.mov', '.mkv'):
                    media_list.append(InputMediaVideo(
                        media=FSInputFile(file_path),
                        caption=caption if i == 0 else None,
                        parse_mode="HTML" if i == 0 else None
                    ))

            if media_list:
                sent_messages = await message.answer_media_group(media=media_list)
                for msg in sent_messages:
                    if msg.photo:
                        sent_file_ids.append(msg.photo[-1].file_id)
                        media_types.append("photo")
                    elif msg.video:
                        sent_file_ids.append(msg.video.file_id)
                        media_types.append("video")

        else:
            # More than 10 files - send individually with minimal captions
            for i, file_path in enumerate(files):
                file_caption = f"🎬 {title} ({i + 1}/{len(files)})" if i < 5 else None

                if file_path.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp'):
                    sent = await message.answer_photo(
                        photo=FSInputFile(file_path),
                        caption=file_caption
                    )
                    if sent.photo:
                        sent_file_ids.append(sent.photo[-1].file_id)
                        media_types.append("photo")
                elif file_path.suffix.lower() in ('.mp4', '.avi', '.mov', '.mkv'):
                    sent = await message.answer_video(
                        video=FSInputFile(file_path),
                        caption=file_caption
                    )
                    if sent.video:
                        sent_file_ids.append(sent.video.file_id)
                        media_types.append("video")

                # Add small delay for large batches (Fix: Har 3 tadan keyin delay)
                if i > 0 and i % 3 == 0:
                    await asyncio.sleep(1)

    except Exception as e:
        log.error(f"Error sending media files: {e}")
        raise

    return sent_file_ids, media_types


@user_router.message(F.chat.type == ChatType.PRIVATE)
async def process_message(message: Message):
    user_id = message.from_user.id
    temp_dir = None

    try:
        # Check membership
        check_status, channels = await CheckData.check_member(bot, user_id)
        if not check_status:
            await message.answer(
                "❗ <b>Kanalga a'zo bo'ling</b>\n\n"
                "Botdan foydalanish uchun quyidagi kanallarga a'zo bo'ling:",
                reply_markup=await CheckData.channels_btn(channels),
                parse_mode="HTML"
            )
            return

        # Validate message
        if not message.text:
            await message.answer(
                "📝 Iltimos, Instagram havolasini matn ko'rinishida yuboring.\n\n"
                "Masalan: https://instagram.com/reel/ABC123..."
            )
            return

        # Extract Instagram URL
        url_match = INSTAGRAM_URL_PATTERN.search(message.text)
        if not url_match:
            await message.answer(
                "❌ <b>Noto'g'ri havola</b>\n\n"
                "Iltimos, to'g'ri Instagram havolasini yuboring:\n"
                "• Post: instagram.com/p/...\n"
                "• Reel: instagram.com/reel/...\n"
                "• IGTV: instagram.com/tv/...\n"
                "• Story: instagram.com/stories/...",
                parse_mode="HTML"
            )
            return

        # Normalize URL
        url = url_match.group(0).split("?")[0].rstrip("/")
        log.info(f"Processing URL: {url} for user: {user_id}")

        # Check cache first
        cached = get_cached_file(url)
        if cached:
            file_ids, title, media_types = cached
            log.info(f"Found cached content for {url}")

            try:
                caption = f"🎬 <b>{title}</b>\n\n📥 @my_reels_robot (Cache)"

                for file_id, media_type in zip(file_ids, media_types):
                    if media_type == "video":
                        await message.answer_video(video=file_id, caption=caption, parse_mode="HTML")
                    elif media_type == "photo":
                        await message.answer_photo(photo=file_id, caption=caption, parse_mode="HTML")
                    caption = None  # Only add caption to first file
                return
            except TelegramBadRequest:
                # Cache is invalid, proceed with fresh download
                log.warning("Cached file is invalid, downloading fresh")

        # Show loading message with progress
        loading_msg = await message.answer("🔄 <b>Yuklanmoqda...</b>\n⏱️ Iltimos kuting", parse_mode="HTML")

        # Download with timeout
        download_start = time.time()

        try:
            # YANGI: doimiy videos papkasi va har bir yuklash uchun noyob papka yaratish
            videos_dir = Path("videos")
            videos_dir.mkdir(parents=True, exist_ok=True)

            # unique per-request folder: userID_timestamp
            temp_dir = videos_dir / f"{user_id}_{int(time.time())}"
            temp_dir.mkdir(parents=True, exist_ok=True)

            # Update loading message
            await loading_msg.edit_text(
                "🔄 <b>Yuklanmoqda...</b>\n📡 Instagram'dan ma'lumot olinmoqda",
                parse_mode="HTML"
            )

            # Download files into temp_dir
            files, title, description = await downloader.download_instagram(url, temp_dir)

            if not files:
                raise Exception("Hech qanday media fayl yuklanmadi")

            # Update loading message
            await loading_msg.edit_text(
                "🔄 <b>Yuklanmoqda...</b>\n📤 Telegram'ga yuborilmoqda",
                parse_mode="HTML"
            )

            # Send files to user
            try:
                sent_file_ids, media_types = await send_media_files(message, files, title, description)
            except Exception as send_exc:
                log.error(f"Xatolik — fayllarni jo'natishda: {send_exc}")
                await loading_msg.edit_text(
                    "⚠️ <b>Fayllarni yuborishda xatolik yuz berdi.</b>\n\n"
                    "Adminga xabar berildi.",
                    parse_mode="HTML"
                )
                await bot.send_message(ADMIN_ID[0], f"Send error: {send_exc}\nURL: {url}\nUser: {user_id}")
                raise send_exc

            # Cache the results (agar yuborish muvaffaqiyat bo'lsa)
            if sent_file_ids:
                await cache_download(user_id, url, title, sent_file_ids, media_types)

            download_time = round(time.time() - download_start, 1)
            log.info(f"Successfully processed {url} in {download_time}s")

            # Send completion message
            await loading_msg.edit_text(
                f"✅ <b>Muvaffaqiyatli yuklandi!</b>\n"
                f"⏱️ Vaqt: {download_time}s\n"
                f"📁 Fayllar: {len(files)}",
                parse_mode="HTML"
            )

            # Auto-delete loading message after 3 seconds
            await asyncio.sleep(3)
            with contextlib.suppress(Exception):
                await loading_msg.delete()

        except TelegramBadRequest as e:
            error_msg = "⚠️ <b>Yuklashda muammo</b>\n\n" \
                        "Fayl Telegram tomonidan qabul qilinmadi.\n" \
                        "Sabab: Fayl hajmi yoki formati mos emas."
            await loading_msg.edit_text(error_msg, parse_mode="HTML")
            await bot.send_message(ADMIN_ID[0], f"TG BadRequest: {e}\nURL: {url}")
            raise

        except Exception as e:
            # existing user-friendly error mapping
            error_msg = str(e).lower()

            if "private" in error_msg or "login required" in error_msg:
                user_error = "🔒 <b>Shaxsiy akkaunt</b>\n\n" \
                             "Bu kontent shaxsiy akkauntda joylashgan.\n" \
                             "Faqat ochiq akkauntlardan yuklay olamiz."
            elif "not found" in error_msg:
                user_error = "❌ <b>Kontent topilmadi</b>\n\n" \
                             "Bu havola mavjud emas yoki o'chirilgan."
            elif "rate limit" in error_msg or "401" in error_msg:
                user_error = "⏳ <b>Vaqtincha cheklash</b>\n\n" \
                             "Instagram tomonidan vaqtincha cheklash.\n" \
                             "Bir necha daqiqadan so'ng urinib ko'ring."
            else:
                user_error = "⚠️ <b>Yuklashda xatolik</b>\n\n" \
                             "Havola noto'g'ri yoki kontent mavjud emas.\n" \
                             "Boshqa havola bilan urinib ko'ring."

            await loading_msg.edit_text(user_error, parse_mode="HTML")

            # Log detailed error for admin
            log.error(f"Download error for {url}: {e}")
            await bot.send_message(
                ADMIN_ID[0],
                f"❌ <b>Download Error</b>\n"
                f"URL: {url}\n"
                f"User: {user_id}\n"
                f"Error: {e}",
                parse_mode="HTML"
            )
            raise

    except Exception as e:
        # Outer unexpected error handler (saqlab qolinsin)
        log.error(f"Unexpected error in process_message: {e}")
        with contextlib.suppress(Exception):
            await message.answer(
                "🚫 <b>Kutilmagan xatolik</b>\n\n"
                "Botda texnik muammo yuz berdi.\n"
                "Keyinroq urinib ko'ring yoki admin bilan bog'laning: @adkhambek_4",
                parse_mode="HTML"
            )

    finally:
        # Har doim: vaqtincha papkani o'chirish (fayllar serverda qolmasin)
        try:
            if temp_dir and temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)
                log.info(f"Removed temp dir: {temp_dir}")
        except Exception as cleanup_exc:
            log.warning(f"Temp dir cleanup failed: {cleanup_exc}")