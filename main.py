import os
import sys
import logging
import traceback
import asyncio
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from config import API_TOKEN, PORT, MY_CHAT_ID, VERSION
import db
from moex_api import load_instrument_names
from handlers import register_handlers, set_http_session, set_bot

# ---------- ЛОГИРОВАНИЕ ----------
logging.basicConfig(level=logging.INFO)

# ---------- ПЕРЕХВАТ КРИТИЧЕСКИХ ОШИБОК ИМПОРТА ----------
try:
    from config import API_TOKEN, PORT, MY_CHAT_ID, VERSION
    import db
    from moex_api import load_instrument_names
    from handlers import register_handlers, set_http_session, set_bot
except Exception as e:
    logging.critical("CRITICAL IMPORT ERROR", exc_info=True)
    sys.exit(1)

# ---------- ИНИЦИАЛИЗАЦИЯ ----------
if not API_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())  # <-- добавлено хранилище для FSM

# ---------- HEALTH‑СЕРВЕР ----------
async def health_handler(request):
    return web.Response(text="OK")

async def run_health_server():
    app = web.Application()
    app.router.add_get('/health', health_handler)
    app.router.add_get('/', health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"✅ Health‑сервер запущен на порту {PORT}")
    await asyncio.Event().wait()

# ---------- ГЛАВНАЯ ФУНКЦИЯ ----------
async def main():
    # Инициализация БД
    db.init_db()

    # Создание HTTP‑сессии
    http_session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
    set_http_session(http_session)  # передаём в handlers
    set_bot(bot)                    # передаём экземпляр бота

    # Загружаем названия инструментов
    await load_instrument_names(http_session)

    # Регистрируем обработчики
    register_handlers(dp)

    # Удаляем вебхук и запускаем поллинг
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("✅ Вебхук удалён")

    try:
        await bot.send_message(MY_CHAT_ID, f"🚀 Бот перезапущен и готов к работе! ver: {VERSION}")
    except Exception as e:
        logging.error(f"❌ Не удалось отправить уведомление о запуске: {e}")

    logging.info("✅ Запускаем polling...")
    polling_task = asyncio.create_task(dp.start_polling(bot))
    health_task = asyncio.create_task(run_health_server())

    done, pending = await asyncio.wait(
        [polling_task, health_task],
        return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    await http_session.close()
    logging.info("✅ HTTP сессия закрыта")

if __name__ == "__main__":
    asyncio.run(main())