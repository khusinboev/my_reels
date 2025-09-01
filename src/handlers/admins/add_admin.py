from aiogram import Router, F
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, KeyboardButtonRequestChat, KeyboardButton, ReplyKeyboardMarkup

from config import ADMIN_ID, bot, sql
from src.keyboards.buttons import AdminPanel
from src.keyboards.keyboard_func import PanelFunc

add_router = Router()

class AdminAdd(StatesGroup):
    admin_add = State()
    admin_delete = State()

markup = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[[KeyboardButton(text="🔙Orqaga qaytish")]])


# Kanallar bo'limi
@add_router.message(F.text == "🔧Adminlar👨‍💻", F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def new(msg: Message):
    await msg.answer("Tanlang", reply_markup=await AdminPanel.admin_add())


@add_router.message(F.text == "🔙Orqaga qaytish", F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID), AdminAdd.admin_add or AdminAdd.admin_delete)
async def backs(message: Message, state: FSMContext):
    await message.answer("Orqaga qaytildi", reply_markup=await AdminPanel.admin_add())
    await state.clear()


@add_router.message(F.text == "➕Admin qo'shish", F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def channel_add(message: Message, state: FSMContext):
    keyboard = []
    keyboard.extend([
        [KeyboardButton(text="🔙Orqaga qaytish")]
    ])
    await bot.send_message(message.chat.id,
                           text="Qo'shish kerak bo'lgan admin ID sini yuboring, buni @ShowJsonBot orqali bilishingiz mumkin",
                           reply_markup=ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True),
                           parse_mode="html")
    await state.set_state(AdminAdd.admin_add)


@add_router.message(AdminAdd.admin_add, F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def channel_add1(message: Message, state: FSMContext):
    if message.text.isdigit():
        admin_id = message.text
        sql.execute(f"SELECT user_id FROM public.admins WHERE user_id = {admin_id}")
        data = sql.fetchone()
        if data is None:
            await PanelFunc.admin_add(admin_id)
            await state.clear()
            await message.answer("Admin qo'shildi🎉🎉", reply_markup=await AdminPanel.admin_add())
        else:
            await message.answer("Bu admin avvaldan bor", reply_markup=await AdminPanel.admin_add())
        await state.clear()
    else:
        await message.answer("Admin ID xato kiritildi\nID faqat sonlardan iborat ko'rinishda kiriting",
                            reply_markup=markup)


@add_router.message(F.text == "❌Admin o'chirish", F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def channel_delete(message: Message, state: FSMContext):
    await message.answer("O'chiriladigan adminning IDsini yuboring.", reply_markup=markup)
    await state.set_state(AdminAdd.admin_delete)


@add_router.message(AdminAdd.admin_delete, F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def channel_delete2(message: Message, state: FSMContext):
    if message.text.isdigit():
        channel_id = message.text.upper()
        sql.execute(f"""SELECT user_id FROM public.admins WHERE user_id = '{channel_id}'""")
        data = sql.fetchone()
        if data is None:
            await message.answer("Bunday admin yo'q", reply_markup=await AdminPanel.admin_add())
        else:
            await PanelFunc.admin_delete(channel_id)
            await state.clear()
            await message.answer("Admin muvaffaqiyatli o'chirildi", reply_markup=await AdminPanel.admin_add())
        await state.clear()
    else:
        await message.answer("Admin ID xato kiritildi\nID faqat sonlardan iborat ko'rinishda kiriting",
                            reply_markup=markup)


@add_router.message(F.text == "📋 Adminlar ro'yxati", F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def channel_list(message: Message):
    if len(await PanelFunc.admin_list()) > 3:
        await message.answer(await PanelFunc.admin_list(), parse_mode='html')
    else:
        await message.answer("Hozircha Adminlar yo'q")