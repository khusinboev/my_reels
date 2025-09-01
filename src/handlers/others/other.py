from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, CallbackQuery

from config import bot
from src.keyboards.buttons import UserPanels

other_router = Router()
# some code...

@other_router.message()
async def chosen_lang(message: Message, state: FSMContext):
    try:
        await message.delete()
        await state.clear()
    except: pass
    await message.answer("<b>salom</b>", parse_mode="html")


# Shu yerda keyingi bosqichni (fac5 va hokazo) davom ettirishingiz mumkin.
@other_router.callback_query()
async def handle_hello(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception as e:
        try:
            await bot.delete_message(chat_id=callback.from_user.id, message_id=callback.inline_message_id )
        except:
            pass