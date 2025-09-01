import asyncio
import logging
from aiogram import Bot, Dispatcher

from config import BOT_TOKEN, dp, bot
from src.db.init_db import create_all_base
from src.handlers.admins.add_admin import add_router
from src.handlers.admins.admin import admin_router
from src.handlers.admins.messages import msg_router
from src.handlers.others.channels import channel_router
from src.handlers.others.groups import group_router
from src.handlers.others.other import other_router
from src.handlers.users.users import user_router
from src.middlewares.middleware import RegisterUserMiddleware


async def on_startup() -> None:
    await create_all_base()


async def main():
    await on_startup()
    logging.basicConfig(level=logging.INFO)

    dp.update.middleware(RegisterUserMiddleware())

    #for admin
    dp.include_router(admin_router)
    dp.include_router(add_router)
    dp.include_router(msg_router)

    #for user
    dp.include_router(user_router)

    #for other
    dp.include_router(group_router)
    dp.include_router(channel_router)
    dp.include_router(other_router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())