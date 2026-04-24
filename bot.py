
import asyncio
import uvicorn
import logging

from app import bot
from db import files_col
from utility import file_queue_worker, periodic_expiry_cleanup
from fast_api import api
from config import LOG_CHANNEL_ID
from handlers import owner, user

async def main():
    """
    Starts the bot and FastAPI server.
    """
    index_names = [index['name'] async for index in files_col.list_indexes()]
    if "file_name_text" not in index_names:
        await files_col.create_index([("file_name", "text")])

    await bot.start()

    bot.loop.create_task(start_fastapi())
    bot.loop.create_task(file_queue_worker(bot))
    bot.loop.create_task(periodic_expiry_cleanup())

    try:
        me = await bot.get_me()
        user_name = me.username or "Bot"
        await bot.send_message(LOG_CHANNEL_ID, f"✅ @{user_name} started and FastAPI server running.")
        logging.info("Bot started and FastAPI server running.")
    except Exception as e:
        print(f"Failed to send startup message to log channel: {e}")

async def start_fastapi():
    """
    Starts the FastAPI server using Uvicorn.
    """
    try:
        config = uvicorn.Config(api, host="0.0.0.0", port=8000, loop="asyncio", log_level="warning")
        server = uvicorn.Server(config)
        await server.serve()
    except KeyboardInterrupt:
        pass
        logging.info("FastAPI server stopped.")

if __name__ == "__main__":
    try:
        bot.loop.run_until_complete(main())
        bot.loop.run_forever()
    except KeyboardInterrupt:
        bot.stop()
        tasks = asyncio.all_tasks(loop=bot.loop)
        for task in tasks:
            task.cancel()
        bot.loop.stop()
        logging.info("Bot stopped.")
