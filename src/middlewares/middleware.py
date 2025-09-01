from aiogram.types import Update
from aiogram.dispatcher.middlewares.base import BaseMiddleware
import psycopg2
from datetime import datetime
import pytz
from config import DB_CONFIG

class RegisterUserMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Update, data: dict):
        if not event.message:
            return await handler(event, data)  # Middleware davom etsin

        user = event.message.from_user
        user_id = user.id
        date = datetime.now(pytz.timezone("Asia/Tashkent")).date()
        lang_code = user.language_code if user.language_code else "uz"

        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()

        # **1️⃣ SQL Injection xavfsizligi uchun f-string emas, parametr ishlatilmoqda**
        cur.execute("SELECT user_id FROM public.accounts WHERE user_id = %s", (user_id,))
        if not cur.fetchone():  # Foydalanuvchi bazada bo‘lmasa
            cur.execute("DELETE FROM public.accounts WHERE user_id = %s", (user_id,))
            conn.commit()

            cur.execute(
                "INSERT INTO accounts (user_id, lang_code, date) VALUES (%s, %s, %s)",
                (user_id, lang_code, date)
            )
            conn.commit()

        cur.close()
        conn.close()

        return await handler(event, data)  # **2️⃣ Xatolik tuzatildi, middleware davom etadi**