
import re
import aiohttp
import asyncio
import random
import base64
import uuid
import os
import logging
from datetime import datetime, timezone, timedelta
from pyrogram.errors import (FloodWait, UserNotParticipant, UserIsBlocked,
                              InputUserDeactivated, PeerIdInvalid, UserIsBot,
                              ChatAdminRequired)
from pyrogram import enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, User
from db import (
    allowed_channels_col,
    users_col,
    auth_users_col,
    otp_col,
    files_col
)
from config import *
from cache import cache, invalidate_cache


# =========================
# Constants & Globals
# =========================

TOKEN_VALIDITY_SECONDS = 24 * 60 * 60  # 24 hours
AUTO_DELETE_SECONDS = 2 * 60

logger = logging.getLogger(__name__)

# =========================
# Channel & User Utilities
# =========================

async def get_allowed_channels():
    return [
        doc["channel_id"]
        async for doc in allowed_channels_col.find({}, {"_id": 0, "channel_id": 1})
    ]

async def add_user(user_id):
    """
    Add a user to users_col only if not already present.
    Stores user_id, joined_date (UTC), and blocked status.
    Returns the user document with an extra key '_new' (True if newly added).
    """
    user_doc = await users_col.find_one({"user_id": user_id})
    
    if not user_doc:
        user_doc = {
            "user_id": user_id,
            "joined": datetime.now(timezone.utc),
            "blocked": False
        }

        await users_col.insert_one(user_doc)

        user_doc["_new"] = True
    else:
        user_doc["_new"] = False
    
    return user_doc


async def authorize_user(user_id, token):
    """Authorize a user for 24 hours."""
    expiry = datetime.now(timezone.utc) + timedelta(seconds=TOKEN_VALIDITY_SECONDS)
    await auth_users_col.update_one(
        {"user_id": user_id},
        {"$set": {"expiry": expiry, "token": token}},
        upsert=True
    )

async def is_user_authorized(user_id):
    """Check if a user is authorized."""
    if user_id == OWNER_ID:
        return True
    doc = await auth_users_col.find_one({"user_id": user_id})
    if not doc:
        return False
    expiry = doc["expiry"]
    if isinstance(expiry, str):
        try:
            expiry = datetime.fromisoformat(expiry)
        except Exception:
            return False
    if isinstance(expiry, datetime) and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry < datetime.now(timezone.utc):
        return False
    return True

async def get_user_link(user: User) -> str:
    try:
        user_id = user.id if hasattr(user, 'id') else None
        first_name = user.first_name if hasattr(user, 'first_name') else "Unknown"
    except Exception as e:
        logger.info(f"{e}")
        user_id = None
        first_name = "Unknown"
    
    if user_id:
        return f'<a href=tg://user?id={user_id}>{first_name}</a>'
    else:
        return first_name

async def get_user_firstname(user_id: int) -> str:
    """Gets a user's first name from the bot's API."""
    from app import bot
    try:
        if user_id == OWNER_ID:
          return "ADMIN"
        user = await bot.get_users(user_id)
        return user.first_name
    except Exception as e:
        logger.error(f"Error getting user's first name: {e}")
        return "Anonymous"
    
async def is_user_subscribed(client, user_id):
        """Check if a user is subscribed to backup channel."""
        if not BACKUP_CHANNEL_LINK:
            return True  # No backup channel configured, consider all subscribed
        try:
            member = await client.get_chat_member(UPDATE_CHANNEL_ID, user_id)
            return not member.status == 'kicked'
        except UserNotParticipant:
            return False
        except ChatAdminRequired:
            return False
        except Exception as e:
            logger.error(f"{e}")
            return False
# =========================
# Link & URL Utilities
# =========================

async def shorten_url(url):
    if url in cache:
        return cache[url]
    try:
        api_url = f"https://{SHORTERNER_URL}/api"
        params = {
            "api": f"{URLSHORTX_API_TOKEN}",
            "url": url,
            "format": "text"
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, params=params) as response:
                if response.status == 200:
                    short_url = (await response.text()).strip()
                    cache[url] = short_url
                    return short_url
                else:
                    res_text = await response.text()
                    logger.error(
                        f"URL shortening failed. Status code: {response.status}, Response: {res_text}")
                    return url
    except Exception as e:
        logger.error(f"URL shortening failed: {e}")
        return url
    
# =========================
# File Utilities
# =========================
async def upsert_file_info(file_info):
    """Insert or update file info, avoiding duplicates."""
    await files_col.update_one(
        {"channel_id": file_info["channel_id"], "message_id": file_info["message_id"]},
        {"$set": file_info},
        upsert=True
    )

def extract_file_info(message, channel_id=None):
    """Extract file info from a Pyrogram message."""
    caption_name = message.caption.strip() if message.caption else None
    file_info = {
        "channel_id": channel_id if channel_id is not None else message.chat.id,
        "message_id": message.id,
        "file_name": None,
        "file_size": None,
        "file_format": None,
    }
    if message.document:
        file_info["file_name"] = caption_name or message.document.file_name
        file_info["file_size"] = message.document.file_size
        file_info["file_format"] = message.document.mime_type
    elif message.video:
        file_info["file_name"] = caption_name or (message.video.file_name or "video.mp4")
        file_info["file_size"] = message.video.file_size
        file_info["file_format"] = message.video.mime_type
    elif message.audio:
        file_info["file_name"] = caption_name or (message.audio.file_name or "audio.mp3")
        file_info["file_size"] = message.audio.file_size
        file_info["file_format"] = message.audio.mime_type
        file_info["file_title"] = message.audio.title
        file_info["file_artist"] = message.audio.performer
    elif message.photo:
        file_info["file_name"] = caption_name or "photo.jpg"
        file_info["file_size"] = getattr(message.photo, "file_size", None)
        file_info["file_format"] = "image/jpeg"

    if file_info["file_name"]:
         file_info["file_name"] = remove_extension(
            re.sub(r"[',]", "", file_info["file_name"].replace("&", "and")).split("\n")[0]
        )
    return file_info

def human_readable_size(size):
    if size is None: return "Unknown"
    for unit in ['B','KB','MB','GB','TB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"

def remove_extension(caption):
    try:
        # Remove the extension and everything after it
        cleaned_caption = re.sub(r'\.(mkv|mp4|webm|mp3|flac|wav).*$', '', caption, flags=re.IGNORECASE)
        return cleaned_caption
    except Exception as e:
        logger.error(e)
        return None

# =========================
# Async/Bot Utilities
# =========================
async def safe_api_call(coro_factory, max_retries=3):
    """Utility wrapper to add delay and retry for flood waits."""
    retries = 0
    while retries < max_retries:
        try:
            return await coro_factory()
        except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid, UserIsBot) as e:
            raise e
        except FloodWait as e:
            retries += 1
            if retries < max_retries:
                sleep_duration = e.value * 1.2
                logger.warning(f"FloodWait: Sleeping for {sleep_duration:.2f} seconds before retrying. Attempt {retries}/{max_retries}")
                await asyncio.sleep(sleep_duration)
            else:
                logger.error(f"FloodWait limit reached after {max_retries} attempts. Giving up. {e}")
                return None
        except Exception as e:
            logger.error(f"An error occurred during an API call: {e}")
            return None
    return None

async def auto_delete_message(user_message, bot_message):
    try:        
        await asyncio.sleep(AUTO_DELETE_SECONDS)
        if user_message: await safe_api_call(lambda: user_message.delete())
        if bot_message: await safe_api_call(lambda: bot_message.delete())
    except Exception as e:
        pass

# =========================
# Queue System for File Processing
# =========================

file_queue = asyncio.PriorityQueue()

async def file_queue_worker(bot):
    while True:
        _priority, item = await file_queue.get()
        file_info, _, message, log_duplicate, is_no_tmdb = item
        try:
            # Upsert file_info
            await files_col.update_one(
                {"channel_id": file_info["channel_id"], "message_id": file_info["message_id"]},
                {"$set": file_info},
                upsert=True
            )

        except Exception as e:
            logger.error(f"❌ Error saving file: {e}")
        finally:
            file_queue.task_done()
            invalidate_cache()

# =========================
# Unified File Queueing
# =========================

async def queue_file_for_processing(
    message, channel_id=None, reply_func=None, log_duplicates=True, is_no_tmdb=False
):
    try:
        file_info = extract_file_info(message, channel_id=channel_id)
        if file_info["file_name"]:
            item = (file_info, reply_func, message, log_duplicates, is_no_tmdb)
            await file_queue.put((message.id, item))
    except Exception as e:
        if reply_func:
            await safe_api_call(lambda: reply_func(f"❌ Error queuing file: {e}"))

async def delete_expired_auth_users():
    """
    Delete expired auth users from auth_users_col using 'expiry' field.
    """
    now = datetime.now(timezone.utc)
    result = await auth_users_col.delete_many({"expiry": {"$lt": now}})
    logger.info(f"Deleted {result.deleted_count} expired auth users.")

async def delete_expired_otps():
    """
    Delete expired OTPs from otp_col. (Now renamed to tokens)
    """
    now = datetime.now(timezone.utc)
    result = await otp_col.delete_many({"expiry": {"$lt": now}})
    if result.deleted_count > 0:
        logger.info(f"Deleted {result.deleted_count} expired tokens.")

async def periodic_expiry_cleanup(interval_seconds=3600 * 24):
    """
    Periodically delete expired auth users and tokens.
    """
    while True:
        await delete_expired_auth_users()
        await delete_expired_otps()
        await asyncio.sleep(interval_seconds)


def remove_redandent(filename):
    """
    Remove common username patterns from a filename while preserving the content title.
    """
    filename = filename.replace("\n", "\\n")

    patterns = [
        r"^@[\w\.-]+?(?=_)",
        r"_@[A-Za-z]+_|@[A-Za-z]+_|[\[\]\s@]*@[^.\s\[\]]+[\]\[\s@]*",
        r"^[\w\.-]+?(?=_Uploads_)",
        r"^(?:by|from)[\s_-]+[\w\.-]+?(?=_)",
        r"^\[[\w\.-]+?\][\s_-]*",
        r"^\([\w\.-]+?\)[\s_-]*",
    ]

    result = filename
    for pattern in patterns:
        match = re.search(pattern, result)
        if match:
            result = re.sub(pattern, " ", result)
            break

    result = re.sub(r"^[_\s-]+|[_\s-]+$", " ", result)

    return result

async def generate_token(user_id):
    token = str(uuid.uuid4())
    expiry = datetime.now(timezone.utc) + timedelta(hours=24)

    await otp_col.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "token": token,
                "expiry": expiry
            }
        },
        upsert=True
    )
    return token

async def verify_token(user_id, token):
    doc = await otp_col.find_one({"user_id": user_id, "token": token})
    if not doc:
        return False

    expiry = doc["expiry"]
    if isinstance(expiry, datetime) and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    if expiry < datetime.now(timezone.utc):
        await otp_col.delete_one({"_id": doc["_id"]})
        return False

    await authorize_user(user_id, token)
    await otp_col.delete_one({"_id": doc["_id"]})
    return True

async def check_file_limit(user_id: int):
    if user_id == OWNER_ID:
        return True

    auth_user = await auth_users_col.find_one({"user_id": user_id})
    file_count = auth_user.get("file_count", 0) if auth_user else 0

    if file_count >= MAX_FILES_PER_SESSION:
        return False
    return True

async def increment_file_count(user_id: int):
    if user_id == OWNER_ID:
        return

    await auth_users_col.update_one(
        {"user_id": user_id},
        {"$inc": {"file_count": 1}},
        upsert=True
    )

def build_search_pipeline(query, search_field, match_query=None, skip=0, limit=12):
    """
    Builds a flexible Atlas Search aggregation pipeline.
    """
    if not query:
        return []

    search_stage = {
        "$search": {
            "index": "default",
            "text": {
                "query": query.strip(),
                "path": search_field,
                "fuzzy": {
                    "maxEdits": 2,
                    "prefixLength": 3
                }
            }
        }
    }

    match_stage = {"$match": match_query} if match_query else None

    project_stage = {
        "$project": {
            "score": {"$meta": "searchScore"},
            "_id": 1,
            "file_name": 1,
            "file_size": 1,
            "file_format": 1,
            "message_id": 1,
            "channel_id": 1
        }
    }

    sort_stage = {
        "$sort": {
            "score": -1
        }
    }

    facet_stage = {
        "$facet": {
            "results": [
                sort_stage,
                {"$skip": skip},
                {"$limit": limit}
            ],
            "totalCount": [
                {"$count": "total"}
            ]
        }
    }
    
    pipeline = [search_stage]
    if match_stage:
        pipeline.append(match_stage)

    pipeline.append(project_stage)
    pipeline.append(facet_stage)

    return pipeline

def extract_channel_and_msg_id(link):
    match = re.search(r"t\.me/c/(-?\d+)/(\d+)", link)
    if match:
        channel_id = int("-100" + match.group(1)) if not match.group(1).startswith("-100") else int(match.group(1))
        msg_id = int(match.group(2))
        return channel_id, msg_id
    raise ValueError("Invalid Telegram message link format. Only /c/ links are supported.")
