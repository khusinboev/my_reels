from datetime import datetime, timedelta

import psycopg2
import pytz
from aiogram import Router, F
from aiogram.exceptions import AiogramError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, KeyboardButton as RButton, \
    KeyboardButtonRequestChat, ChatInviteLink
from aiogram.enums import ChatType
from aiogram.fsm.state import StatesGroup, State
from dateutil.relativedelta import relativedelta

from src.keyboards.buttons import AdminPanel
from config import sql, ADMIN_ID, DB_CONFIG, bot
from src.keyboards.keyboard_func import PanelFunc

admin_router = Router()

class Form(StatesGroup):
    ch_add = State()
    for_username = State()
    ch_delete = State()

    anons_forward = State()
    anons_simple = State()

    clear_base = State()


# Admin panelga kirish
@admin_router.message(Command("panel", "admin"), F.from_user.id.in_(ADMIN_ID), F.chat.type == ChatType.PRIVATE)#,
async def panel_handler(message: Message) -> None:
    await message.answer("panel", reply_markup=await AdminPanel.admin_menu())


markup = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[[KeyboardButton(text="🔙Orqaga qaytish")]])
@admin_router.message(F.text == "🔙Orqaga qaytish", F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def backs(message: Message, state: FSMContext):
    await message.reply("Orqaga qaytildi", reply_markup=await AdminPanel.admin_menu())
    await state.clear()


# Statistika
@admin_router.message(F.text == "📊Statistika", F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def new(message: Message):
    now = datetime.now(pytz.timezone("Asia/Tashkent")).date()

    # Oxirgi 3 oy: joriy oy va undan oldingi 2 ta oy
    current_month = now.replace(day=1)
    months = [current_month - relativedelta(months=i) for i in range(3)]

    conn = psycopg2.connect(**DB_CONFIG)
    cur = conn.cursor()

    # Jami foydalanuvchilar
    cur.execute("SELECT COUNT(*) FROM accounts")
    all_users = cur.fetchone()[0]

    # Oxirgi 3 oydagi jami foydalanuvchilar
    cur.execute("SELECT COUNT(*) FROM accounts WHERE date >= %s", (months[-1],))
    last_3_months = cur.fetchone()[0]

    # Har bir oy bo‘yicha statistikalar
    month_counts = {}
    for month in months:
        cur.execute(
            "SELECT COUNT(*) FROM accounts WHERE date >= %s AND date < %s",
            (month, month + relativedelta(months=1))
        )
        month_counts[month.strftime("%B")] = cur.fetchone()[0] or 0  # Oy nomlari

    # Oxirgi 7 kun statistikasi
    last_7_days = {}
    for i in range(7):
        date_str = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        cur.execute("SELECT COUNT(*) FROM accounts WHERE date = %s", (date_str,))
        last_7_days[date_str] = cur.fetchone()[0] or 0

    cur.close()
    conn.close()

    # Xabarni tayyorlash
    stats_text = (
        f"📊 *Foydalanuvchi Statistikasi:*\n\n"
        f"🔹 *Jami foydalanuvchilar:* {all_users}\n\n"
        f"📅 *Oxirgi 3 oy:* (Jami {last_3_months} ta)\n"
    )
    for month, count in month_counts.items():
        stats_text += f" - {month}: {count} ta\n"

    stats_text += "\n📆 *Oxirgi 7 kun:* (Jami {})\n".format(sum(last_7_days.values()))
    for day, count in last_7_days.items():
        stats_text += f" - {day}: {count} ta\n"

    await message.answer(stats_text, parse_mode="Markdown")


# Kanallar bo'limi
@admin_router.message(F.text == '🔧Kanallar', F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def new(msg: Message):
    await msg.answer("Tanlang", reply_markup=await AdminPanel.admin_channel())


@admin_router.message(F.text == "🔙Orqaga qaytish", F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID), Form.ch_add or Form.ch_delete)
async def backs(message: Message, state: FSMContext):
    await message.reply("Orqaga qaytildi", reply_markup=await AdminPanel.admin_channel())
    await state.clear()


@admin_router.message(F.text == "➕Kanal qo'shish", F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def channel_add(message: Message, state: FSMContext):
    keyboard = []
    keyboard.extend([
        [KeyboardButton(text="🔙Orqaga qaytish")]
    ])
    await bot.send_message(message.chat.id,
                            text="Kanal ulash bo'limi. \nBotga kanal ulashning 3 ta usuli bor:\n"
                                 "1. https://t.me/coder_admin kanal havolasini shu tartibda yuboring.\n"
                                 "2. @coder_admin username ni shu tartibda yuboring",
                            reply_markup=ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True),
                            parse_mode="html")
    await state.set_state(Form.ch_add)


@admin_router.message(Form.ch_add, F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def channel_add1(message: Message, state: FSMContext):
    if message.chat_shared:
        pass
        # try:
        #     chat = await bot.get_chat(message.chat_shared.chat_id)
        #     invite_link: ChatInviteLink = await bot.create_chat_invite_link(chat_id=chat.id, name="KINOMTV_UZ")
        #     link = invite_link.invite_link
        # except AiogramError:
        #     await state.clear()
        #     await bot.send_message(chat_id=message.chat.id,
        #                            text="Bot kanalga <b>admin emas!</b> Iltimos admin qilib keyin urinib ko'ring",
        #                            reply_markup=await AdminPanel.admin_channel(),
        #                            parse_mode="html")
        # else:
        #     channel_id = chat.id
        #     sql.execute(f"SELECT chat_id FROM public.mandatorys WHERE chat_id = {channel_id}")
        #     data = sql.fetchone()
        #     if data is None:
        #         await PanelFunc.channel_add(channel_id, link)
        #         await state.clear()
        #         await message.reply("Kanal qo'shildi🎉🎉", reply_markup=await AdminPanel.admin_channel())
        #
        #     else:
        #         await message.reply("Bu kanal avvaldan bor", reply_markup=await AdminPanel.admin_channel())
        #     await state.clear()
    elif "https://t.me/" in message.text:
        chat_link = "@"+message.text.split("https://t.me/", 1)[1]
        try:
            chat = await bot.get_chat(chat_link)
        except Exception as e:
            print(e)
            await state.clear()
            await bot.send_message(chat_id=message.chat.id,
                                   text="Bot kanalga <b>admin emas!</b> yoki havolani qayta ishlashda muammolar bo'lyapti. Iltimos havolani va adminlikni tekshirib qaytadan urining",
                                   reply_markup=await AdminPanel.admin_channel(),
                                   parse_mode="html")
        else:
            channel_id = chat.id
            sql.execute(f"SELECT chat_id FROM public.mandatorys WHERE chat_id = {channel_id}")
            data = sql.fetchone()
            if data is None:
                await message.reply("Kanal username qabul qilindi, endi taklif havolasini yuboring. U https://t.me/+ deb boshlanadi. Buni kanal havolalari bo'limida yaratasiz.", reply_markup=markup)
                await state.update_data(channel_id=str(channel_id))
                await state.set_state(Form.for_username)

            else:
                await message.reply("Bu kanal avvaldan bor, qaytadan yuboring", reply_markup=markup)
    elif message.text[0] == "@":
        chat_link = "@"+message.text[1:]
        try:
            chat = await bot.get_chat(chat_link)
        except Exception:
            await state.clear()
            await bot.send_message(chat_id=message.chat.id,
                                   text="Bot kanalga <b>admin emas!</b> yoki havolani qayta ishlashda muammolar bo'lyapti. Iltimos havolani va adminlikni tekshirib qaytadan urining",
                                   reply_markup=await AdminPanel.admin_channel(),
                                   parse_mode="html")
        else:
            channel_id = chat.id
            sql.execute(f"SELECT chat_id FROM public.mandatorys WHERE chat_id = {channel_id}")
            data = sql.fetchone()
            if data is None:
                await message.reply(
                    "Kanal username qabul qilindi, endi taklif havolasini yuboring. U https://t.me/+ deb boshlanadi. Buni kanal havolalari bo'limida yaratasiz.",
                    reply_markup=markup)
                await state.update_data(channel_id=str(channel_id))
                await state.set_state(Form.for_username)

            else:
                await message.reply("Bu kanal avvaldan bor, qaytadan yuboring", reply_markup=markup)
    else:
        await message.answer("Kanal <b>username</b> yuboring", reply_markup=markup, parse_mode="html")

@admin_router.message(Form.for_username, F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def channel_add1(message: Message, state: FSMContext):
    link = message.text
    if "https://t.me/" in link:
        data = await state.get_data()
        channel_id = data["channel_id"]
        await PanelFunc.channel_add(channel_id, link)
        await state.clear()
        await message.reply("Kanal qo'shildi🎉🎉", reply_markup=await AdminPanel.admin_channel())
    else:
        await message.answer(
            "Kanal taklif havolasini yuboring. U https://t.me/+ deb boshlanadi. Buni kanal havolalari bo'limida yaratasiz.",
            reply_markup=markup)


@admin_router.message(F.text == "❌Kanalni olib tashlash", F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def channel_delete(message: Message, state: FSMContext):
    await message.reply("O'chiriladigan kanalning userini yuboring.\nMisol uchun @coder_admin", reply_markup=markup)
    await state.set_state(Form.ch_delete)


@admin_router.message(Form.ch_delete, F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def channel_delete2(message: Message, state: FSMContext):
    all_details = await bot.get_chat(message.text)
    channel_id = all_details.id
    sql.execute(f"""SELECT chat_id FROM public.mandatorys WHERE chat_id = '{channel_id}'""")
    data = sql.fetchone()

    if data is None:
        await message.reply("Bunday kanal yo'q", reply_markup=await AdminPanel.admin_channel())
    else:
        if message.text[0] == '@':
            await PanelFunc.channel_delete(channel_id)
            await state.clear()
            await message.reply("Kanal muvaffaqiyatli o'chirildi", reply_markup=await AdminPanel.admin_channel())
        else:
            await message.reply("Kanal useri xato kiritildi\nIltimos userni @coder_admin ko'rinishida kiriting",
                                reply_markup=await AdminPanel.admin_channel())

    await state.clear()


@admin_router.message(F.text == "📋 Kanallar ro'yxati", F.chat.type == ChatType.PRIVATE, F.from_user.id.in_(ADMIN_ID))
async def channel_list(message: Message):
    if len(await PanelFunc.channel_list()) > 3:
        await message.answer(await PanelFunc.channel_list(), parse_mode='html')
    else:
        await message.answer("Hozircha kanallar yo'q")