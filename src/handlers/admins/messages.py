import asyncio
import os
import aiofiles
import logging
from aiogram import Router, F, Bot
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, KeyboardButton, ReplyKeyboardMarkup, BufferedInputFile
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter
)
from config import ADMIN_ID, sql, bot
from src.keyboards.buttons import AdminPanel

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('broadcast.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

msg_router = Router()

# Semaphore to limit concurrent requests
semaphore = asyncio.Semaphore(20)

# Files for logging failed users
FAILED_USERS_FILE = "failed_users.txt"
TEST_FAILED_COPY_FILE = "test_failed_copy.txt"
TEST_FAILED_FORWARD_FILE = "test_failed_forward.txt"

# === STATES (FSM) === #
class MsgState(StatesGroup):
    forward_msg = State()
    send_msg = State()
    test_copy_msg = State()
    test_forward_msg = State()

# === BACK BUTTON === #
markup = ReplyKeyboardMarkup(
    resize_keyboard=True,
    keyboard=[[KeyboardButton(text="🔙Orqaga qaytish")]]
)

# === LOGGER: Write failed user to file === #
async def log_failed_user(user_id: int, filename: str):
    async with aiofiles.open(filename, mode="a") as f:
        await f.write(f"{user_id}\n")
    logger.info(f"Failed user {user_id} logged to {filename}")

# === SAFE SEND FUNCTIONS === #
async def send_copy_safe(user_id: int, message: Message, semaphore: asyncio.Semaphore, is_test: bool = False, test_filename: str = None):
    async with semaphore:
        for attempt in range(5):
            try:
                sent_msg = await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id
                )
                if is_test:
                    await bot.delete_message(chat_id=user_id, message_id=sent_msg.message_id)
                logger.info(f"Successfully sent copy to user {user_id}")
                await asyncio.sleep(0.2)  # Minimum delay for copy
                return True
            except TelegramRetryAfter as e:
                logger.warning(f"RetryAfter for user {user_id}: waiting {e.retry_after}s")
                await asyncio.sleep(e.retry_after + (2 ** attempt))
            except (TelegramForbiddenError, TelegramNotFound):
                logger.error(f"User {user_id} blocked or not found")
                await log_failed_user(user_id, test_filename if is_test else FAILED_USERS_FILE)
                await asyncio.sleep(0.2)
                return False
            except TelegramBadRequest as e:
                if "message to copy not found" in str(e).lower():
                    logger.error(f"Message to copy not found for user {user_id}")
                    await log_failed_user(user_id, test_filename if is_test else FAILED_USERS_FILE)
                    await asyncio.sleep(0.2)
                    return False
                if attempt < 4:
                    logger.warning(f"BadRequest for user {user_id}, attempt {attempt + 1}: {e}")
                    await asyncio.sleep(2 ** attempt)
                else:
                    logger.error(f"Failed to send copy to user {user_id}: {e}")
                    await log_failed_user(user_id, test_filename if is_test else FAILED_USERS_FILE)
                    await asyncio.sleep(0.2)
                    return False
            except Exception as e:
                logger.error(f"Unexpected error sending copy to {user_id} (attempt {attempt + 1}): {e}")
                if attempt < 4:
                    await asyncio.sleep(2 ** attempt)
                else:
                    await log_failed_user(user_id, test_filename if is_test else FAILED_USERS_FILE)
                    await asyncio.sleep(0.2)
                    return False
        logger.error(f"Failed to send copy to user {user_id} after 5 attempts")
        await asyncio.sleep(0.2)
        return False

async def send_forward_safe(user_id: int, message: Message, semaphore: asyncio.Semaphore, is_test: bool = False, test_filename: str = None):
    async with semaphore:
        for attempt in range(5):
            try:
                sent_msg = await bot.forward_message(
                    chat_id=user_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id
                )
                if is_test:
                    await bot.delete_message(chat_id=user_id, message_id=sent_msg.message_id)
                logger.info(f"Successfully sent forward to user {user_id}")
                await asyncio.sleep(0.5)  # Minimum delay for forward
                return True
            except TelegramRetryAfter as e:
                logger.warning(f"RetryAfter for user {user_id}: waiting {e.retry_after}s")
                await asyncio.sleep(e.retry_after + (2 ** attempt))
            except (TelegramForbiddenError, TelegramNotFound):
                logger.error(f"User {user_id} blocked or not found")
                await log_failed_user(user_id, test_filename if is_test else FAILED_USERS_FILE)
                await asyncio.sleep(0.5)
                return False
            except TelegramBadRequest as e:
                if attempt < 4:
                    logger.warning(f"BadRequest for user {user_id}, attempt {attempt + 1}: {e}")
                    await asyncio.sleep(2 ** attempt)
                else:
                    logger.error(f"Failed to send forward to user {user_id}: {e}")
                    await log_failed_user(user_id, test_filename if is_test else FAILED_USERS_FILE)
                    await asyncio.sleep(0.5)
                    return False
            except Exception as e:
                logger.error(f"Unexpected error sending forward to {user_id} (attempt {attempt + 1}): {e}")
                if attempt < 4:
                    await asyncio.sleep(2 ** attempt)
                else:
                    await log_failed_user(user_id, test_filename if is_test else FAILED_USERS_FILE)
                    await asyncio.sleep(0.5)
                    return False
        logger.error(f"Failed to send forward to user {user_id} after 5 attempts")
        await asyncio.sleep(0.5)
        return False

# === BROADCAST FUNCTION === #
async def broadcast(user_ids: list[int], message: Message, send_func, is_test: bool = False, test_filename: str = None):
    total = len(user_ids)
    success = 0
    failed = 0
    status_msg = await message.answer("📤 Yuborish boshlandi...")
    batch_size = 100
    update_interval = 1000  # Update every 1000 users
    # For more frequent updates, use update_interval = 100 and increase sleep to 1 second:
    # update_interval = 100

    # Clear the log file at the start if it exists
    filename = test_filename if is_test else FAILED_USERS_FILE
    if os.path.exists(filename):
        os.remove(filename)
        logger.info(f"Cleared log file: {filename}")

    for i in range(0, total, batch_size):
        batch = user_ids[i:i + batch_size]
        tasks = [send_func(uid, message, semaphore, is_test, test_filename) for uid in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if result is True:
                success += 1
            else:
                failed += 1

        # Update progress
        if (i + batch_size) % update_interval == 0 or (i + batch_size) >= total:
            try:
                await status_msg.edit_text(
                    f"📬 {'Sinov' if is_test else 'Xabar'} yuborilmoqda...\n\n"
                    f"✅ Yuborilgan: {success} ta\n"
                    f"❌ Yuborilmagan: {failed} ta\n"
                    f"📦 Jami: {total} ta\n"
                    f"📊 Progres: {min(i + batch_size, total)}/{total}"
                )
            except Exception as e:
                logger.error(f"Failed to update status message: {e}")

        # Sleep between batches (optional, as per-user delays are handled in send functions)
        await asyncio.sleep(0.1)
        logger.info(f"Processed batch {i//batch_size + 1}/{total//batch_size + 1}")

    # Final status message
    await message.answer(
        f"✅ {'Sinov' if is_test else 'Xabar'} yuborildi\n\n"
        f"📤 Yuborilgan: {success} ta\n"
        f"❌ Yuborilmagan: {failed} ta",
        reply_markup=await AdminPanel.admin_msg()
    )
    logger.info(f"Broadcast completed: {success} successful, {failed} failed, total: {total}")

    # Send the failed users file if it exists
    if os.path.exists(filename):
        async with aiofiles.open(filename, "rb") as f:
            data = await f.read()
            file = BufferedInputFile(data, filename)
            await message.answer_document(
                file,
                caption=f"❌ {'Sinov' if is_test else 'Xabar'} yuborishda xato bo‘lgan foydalanuvchilar"
            )
            logger.info(f"Sent failed users file: {filename}")

    return success, failed

# === DATABASE PAGINATION === #
async def get_user_ids_paginated(batch_size: int = 1000):
    offset = 0
    user_ids = []
    while True:
        sql.execute(f"SELECT user_id FROM public.accounts LIMIT {batch_size} OFFSET {offset}")
        rows = sql.fetchall()
        if not rows:
            break
        user_ids.extend([row[0] for row in rows])
        offset += batch_size
        logger.info(f"Fetched {len(rows)} user IDs at offset {offset}")
    return user_ids

# === HANDLERS === #
@msg_router.message(F.text == "✍Xabarlar", F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def panel_handler(message: Message) -> None:
    await message.answer("Xabarlar bo'limi!", reply_markup=await AdminPanel.admin_msg())
    logger.info(f"Admin {message.from_user.id} accessed messages panel")

@msg_router.message(F.text == "📨Forward xabar yuborish", F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def start_forward(message: Message, state: FSMContext):
    await message.answer("Forward yuboriladigan xabarni yuboring", reply_markup=markup)
    await state.set_state(MsgState.forward_msg)
    logger.info(f"Admin {message.from_user.id} started forward message")

@msg_router.message(MsgState.forward_msg, F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def send_forward_to_all(message: Message, state: FSMContext):
    await state.clear()
    user_ids = await get_user_ids_paginated()
    await broadcast(user_ids, message, send_forward_safe)
    logger.info(f"Admin {message.from_user.id} completed forward broadcast")

@msg_router.message(F.text == "📬Oddiy xabar yuborish", F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def start_text_send(message: Message, state: FSMContext):
    await message.answer("Yuborilishi kerak bo'lgan xabarni yuboring", reply_markup=markup)
    await state.set_state(MsgState.send_msg)
    logger.info(f"Admin {message.from_user.id} started copy message")

@msg_router.message(MsgState.send_msg, F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def send_text_to_all(message: Message, state: FSMContext):
    await state.clear()
    user_ids = await get_user_ids_paginated()
    await broadcast(user_ids, message, send_copy_safe)
    logger.info(f"Admin {message.from_user.id} completed copy broadcast")

@msg_router.message(F.text == "🧪Sinov: Copy yuborish", F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def test_copy_broadcast(message: Message, state: FSMContext):
    await message.answer("🧪 Sinov: Oddiy xabarni yuboring (copy), yuboriladi va darhol o‘chiriladi:")
    await state.set_state(MsgState.test_copy_msg)
    logger.info(f"Admin {message.from_user.id} started test copy broadcast")

@msg_router.message(MsgState.test_copy_msg, F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def handle_test_copy(message: Message, state: FSMContext):
    await state.clear()
    user_ids = await get_user_ids_paginated()
    await broadcast(user_ids, message, send_copy_safe, is_test=True, test_filename=TEST_FAILED_COPY_FILE)
    logger.info(f"Admin {message.from_user.id} completed test copy broadcast")

@msg_router.message(F.text == "🧪Sinov: Forward yuborish", F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def test_forward_broadcast(message: Message, state: FSMContext):
    await message.answer("🧪 Sinov: Forward xabar yuboring, darhol o‘chiriladi:")
    await state.set_state(MsgState.test_forward_msg)
    logger.info(f"Admin {message.from_user.id} started test forward broadcast")

@msg_router.message(MsgState.test_forward_msg, F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def handle_test_forward(message: Message, state: FSMContext):
    await state.clear()
    user_ids = await get_user_ids_paginated()
    await broadcast(user_ids, message, send_forward_safe, is_test=True, test_filename=TEST_FAILED_FORWARD_FILE)
    logger.info(f"Admin {message.from_user.id} completed test forward broadcast")

@msg_router.message(F.text == "🔙Orqaga qaytish", F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def back_to_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Orqaga qaytildi", reply_markup=await AdminPanel.admin_msg())
    logger.info(f"Admin {message.from_user.id} returned to menu")