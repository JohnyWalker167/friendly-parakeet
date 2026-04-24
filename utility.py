
import re
import aiohttp
import asyncio
import random
import base64
import uuid
import os
import logging
import imgbbpy
from datetime import datetime, timezone, timedelta
from pyrogram.errors import (FloodWait, UserNotParticipant, UserIsBlocked,
                              InputUserDeactivated, PeerIdInvalid, UserIsBot,
                              ChatAdminRequired)
from pyrogram import enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, User
from db import (
    allowed_channels_col,
    users_col,
    tokens_col,
    auth_users_col,
    otp_col,
    files_col,
    tmdb_col
)
from config import *
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from mutagen.id3 import ID3, APIC
from mutagen import File as MutagenFile
from cache import cache, invalidate_cache


async def upload_to_imgbb(image_url):
    """
    Downloads an image, uploads it to imgbb, and returns the new URL.
    """
    if not image_url:
      raise ValueError("Image URL cannot be empty.")

    temp_file_path = f"/tmp/{uuid.uuid4()}.jpg"
    client = None
    try:
        # 1. Download the image
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as response:
                if response.status != 200:
                    raise ValueError(f"Failed to download image from URL: Status {response.status}")
                with open(temp_file_path, "wb") as f:
                    while True:
                        chunk = await response.content.read(1024)
                        if not chunk:
                            break
                        f.write(chunk)

        # 2. Upload the local file
        client = imgbbpy.AsyncClient(IMGBB_API_KEY)
        image = await client.upload(file=temp_file_path, name=f"{uuid.uuid4()}")
        # After image = await client.upload(file=temp_file_path)
        return {
                "url": image.url,
                "delete_url": getattr(image, "delete_url", None)
        }

    except Exception as e:
        logger.error(f"Error during imgbb upload process: {e}")
        raise ValueError(f"Failed to upload image to imgbb: {e}")

    finally:
        # 3. Clean up resources
        if client:
            await client.close()
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)

# =========================
# Constants & Globals
# =========================

TOKEN_VALIDITY_SECONDS = 24 * 60 * 60  # 24 hours
AUTO_DELETE_SECONDS = 2 * 60

logger = logging.getLogger(__name__)

def build_search_pipeline(query, search_field, match_query=None, skip=0, limit=12):
    """
    Builds a flexible Atlas Search aggregation pipeline.

    Args:
        query (str): The search query string.
        search_field (str): The name of the field to search (e.g., 'title' or 'file_name').
        match_query (dict, optional): Additional filters for the $match stage. Defaults to None.
        skip (int, optional): The number of documents to skip for pagination. Defaults to 0.
        limit (int, optional): The number of documents to return. Defaults to 12.

    Returns:
        list: The aggregation pipeline.
    """
    if not query:
        return []

    # Atlas Search stage optimized for storage (using 'text' operator)
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

    # Match stage for additional filters
    match_stage = {"$match": match_query} if match_query else None

    # Projection stage to shape the output and include the search score
    project_stage = {
        "$project": {
            "score": {"$meta": "searchScore"},
            "_id": 1,
            "file_name": 1,
            "title": 1,
            "file_size": 1,
            "file_format": 1,
            "file_title": 1,
            "file_artist": 1,
            "message_id": 1,
            "channel_id": 1,
            "poster_url": 1,
            "poster_path": 1,
            "tmdb_id": 1,
            "tmdb_type": 1,
            "year": 1,
            "rating": 1,
            "plot": 1,
            "genres": 1,
            "cast": 1,
            "directors": 1,
            "poster_delete_url": 1
        }
    }

    # Sorting by relevance (score) first
    sort_stage = {
        "$sort": {
            "score": -1
        }
    }

    # Facet stage for pagination and total count
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


async def authorize_user(user_id, otp):
    """Authorize a user for 24 hours."""
    expiry = datetime.now(timezone.utc) + timedelta(seconds=TOKEN_VALIDITY_SECONDS)
    await auth_users_col.update_one(
        {"user_id": user_id},
        {"$set": {"expiry": expiry, "file_count": 0, "otp": otp}},
        upsert=True
    )

async def is_user_authorized(user_id, otp):
    """Check if a user is authorized."""
    doc = await auth_users_col.find_one({"user_id": user_id, "otp": otp})
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
    
# =========================
# Token Utilities
# =========================

async def generate_token(user_id):
    """Generate a new access token for a user."""
    token_id = str(uuid.uuid4())
    expiry = datetime.now(timezone.utc) + timedelta(seconds=TOKEN_VALIDITY_SECONDS)
    await tokens_col.insert_one({
        "token_id": token_id,
        "user_id": user_id,
        "expiry": expiry,
        "created_at": datetime.now(timezone.utc)
    })
    return token_id

async def is_token_valid(token_id, user_id):
    """Check if a token is valid for a user."""
    token = await tokens_col.find_one({"token_id": token_id, "user_id": user_id})
    if not token:
        return False
    expiry = token["expiry"]
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry < datetime.now(timezone.utc):
        await tokens_col.delete_one({"_id": token["_id"]})
        return False
    return True

async def get_token_link(token_id, bot_username):
    """Generate a Telegram deep link for a token."""
    return f"https://telegram.dog/{bot_username}?start=token_{token_id}"

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

def generate_telegram_link(bot_username, channel_id, message_id):
    """Generate a base64-encoded Telegram deep link for a file."""
    raw = f"{channel_id}_{message_id}".encode()
    b64 = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"https://telegram.dog/{bot_username}?start=file_{b64}" 

def generate_c_link(channel_id, message_id):
    # channel_id must be like -1001234567890
    return f"https://t.me/c/{str(channel_id)[4:]}/{message_id}"

def extract_channel_and_msg_id(link):
    # Only support t.me/c/(-?\d+)/(\d+)
    match = re.search(r"t\.me/c/(-?\d+)/(\d+)", link)
    if match:
        channel_id = int("-100" + match.group(1)) if not match.group(1).startswith("-100") else int(match.group(1))
        msg_id = int(match.group(2))
        return channel_id, msg_id
    raise ValueError("Invalid Telegram message link format. Only /c/ links are supported.")

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
    for unit in ['B','KB','MB','GB','TB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"

def remove_extension(caption):
    try:
        # Remove the extension and everything after it
        cleaned_caption = re.sub(r'\.(mkv|mp4|webm).*$', '', caption, flags=re.IGNORECASE)
        return cleaned_caption
    except Exception as e:
        logger.error(e)
        return None
    
def remove_unwanted(caption):
    try:
        # Match and keep everything up to and including the extension
        match = re.match(r'^(.*?\.(mkv|mp4|webm))', caption, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        return caption  # Return original if no match
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

async def delete_after_delay(client, channel_id, message_id, delay=AUTO_DELETE_SECONDS):
    await asyncio.sleep(delay)
    try:
        await safe_api_call(lambda: client.delete_messages(channel_id, message_id))
    except Exception as e:
        logger.error(f"Failed to auto delete message: {e}")

async def auto_delete_message(user_message, bot_message):
    try:        
        await asyncio.sleep(AUTO_DELETE_SECONDS)
        await safe_api_call(lambda: user_message.delete())
        await safe_api_call(lambda: bot_message.delete())
    except Exception as e:
        pass


async def extract_tmdb_link(tmdb_url):
    movie_pattern = r'themoviedb\.org\/movie\/(\d+)'
    tv_pattern = r'themoviedb\.org\/tv\/(\d+)'
    collection_pattern = r'themoviedb\.org\/collection\/(\d+)'
    if re.search(movie_pattern, tmdb_url):
        tmdb_type = 'movie'
        tmdb_id = int(re.search(movie_pattern, tmdb_url).group(1))
    elif re.search(tv_pattern, tmdb_url):
        tmdb_type = 'tv'
        tmdb_id = int(re.search(tv_pattern, tmdb_url).group(1)) 
    elif re.search(collection_pattern, tmdb_url):
        tmdb_type = 'collection'
        tmdb_id = int(re.search(collection_pattern, tmdb_url).group(1)) 
    else:
        raise ValueError("Invalid TMDB link. Must be a movie, tv, or collection link.")
    return tmdb_type, tmdb_id
        
# =========================
# Queue System for File Processing
# =========================

file_queue = asyncio.PriorityQueue()

def get_queue_size():
    """Returns the current size of the file processing queue."""
    return file_queue.qsize()

async def handle_duplicate_file(bot, file_info, log_duplicate: bool):
    """Checks for duplicate files and logs if requested."""
    existing = await files_col.find_one({"file_name": file_info["file_name"]})

    if existing:
        if log_duplicate:
            telegram_link = generate_c_link(
                file_info["channel_id"], file_info["message_id"]
            )
            await asyncio.sleep(3)
            await safe_api_call(
                lambda: bot.send_message(
                    LOG_CHANNEL_ID,
                    f"⚠️ Duplicate File.\nLink: {telegram_link}",
                    parse_mode=enums.ParseMode.HTML,
                )
            )
        return True
    return False

async def process_audio_file(bot, message, file_info):
    """Processes audio files: downloads, gets thumbnail, sends info, and cleans up."""
    try:
        audio_path = await bot.download_media(message)
        thumb_path = await get_audio_thumbnail(audio_path)
        if thumb_path:
            # Upload the local file
            client = imgbbpy.AsyncClient(IMGBB_API_KEY)
            image = await client.upload(file=thumb_path)
            url = image.url
            delete_url = image.delete_url
            # Update file_info
            file_info["poster_url"] = url
            if delete_url:
                file_info['poster_delete_url'] = delete_url  
            os.remove(thumb_path)
        os.remove(audio_path)
    except Exception as e:
        logger.error(f"Error processing audio file: {e}")
    finally:
        # 3. Clean up resources
        if client:
            await client.close()

async def file_queue_worker(bot):
    from tmdb import process_tmdb_info
    while True:
        _priority, item = await file_queue.get()
        file_info, _, message, log_duplicate, is_no_tmdb = item
        try:
            if await handle_duplicate_file(bot, file_info, log_duplicate):
                continue

            tmdb_info = None
            if is_no_tmdb:
                file_info["is_no_tmdb"] = True
            elif message.audio:
                await process_audio_file(bot, message, file_info)
            else:
                # Process TMDB info for video or document files
                tmdb_info = await process_tmdb_info(bot, file_info)

            # Upsert file_info after processing
            await upsert_file_info(file_info)

            # If a file was successfully linked to a TMDB entry, update the timestamp
            if tmdb_info:
                tmdb_id, tmdb_type = tmdb_info
                await tmdb_col.update_one(
                    {"tmdb_id": tmdb_id, "tmdb_type": tmdb_type},
                    {"$set": {"last_file_added_at": datetime.now(timezone.utc)}}
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

async def delete_expired_tokens():
    """
    Delete expired tokens from tokens_col using 'expiry' field.
    """
    now = datetime.now(timezone.utc)
    result = await tokens_col.delete_many({"expiry": {"$lt": now}})
    logger.info(f"Deleted {result.deleted_count} expired tokens.")

async def generate_otp(user_id):
    """
    Generate a 6-digit OTP and an otp_token, and store them in otp_col.
    If a valid OTP already exists for the user, return it.
    """
    existing_otp = await otp_col.find_one({"user_id": user_id})
    if existing_otp:
        expiry = existing_otp["expiry"]
        if isinstance(expiry, datetime) and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        if expiry > datetime.now(timezone.utc):
            return existing_otp["otp"], existing_otp["otp_token"]

    otp = str(random.randint(100000, 999999))
    otp_token = str(uuid.uuid4())
    expiry = datetime.now(timezone.utc) + timedelta(hours=24)

    await otp_col.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "otp": otp,
                "otp_token": otp_token,
                "expiry": expiry
            }
        },
        upsert=True
    )
    return otp, otp_token

async def verify_otp(user_id, otp):
    """
    Verify the OTP for a given user_id.
    If valid, authorize the user and delete the OTP.
    """
    doc = await otp_col.find_one({"user_id": user_id, "otp": otp})
    if not doc:
        return False

    expiry = doc["expiry"]
    if isinstance(expiry, datetime) and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    if expiry < datetime.now(timezone.utc):
        await otp_col.delete_one({"_id": doc["_id"]})
        return False

    await authorize_user(user_id, otp)
    await otp_col.delete_one({"_id": doc["_id"]})
    return True

async def delete_expired_otps():
    """
    Delete expired OTPs from otp_col.
    """
    now = datetime.now(timezone.utc)
    result = await otp_col.delete_many({"expiry": {"$lt": now}})
    if result.deleted_count > 0:
        logger.info(f"Deleted {result.deleted_count} expired OTPs.")

async def periodic_expiry_cleanup(interval_seconds=3600 * 24):
    """
    Periodically delete expired auth users, tokens, and OTPs.
    """
    while True:
        await delete_expired_auth_users()
        await delete_expired_tokens()
        await delete_expired_otps()
        await asyncio.sleep(interval_seconds)


def remove_redandent(filename):
    """
    Remove common username patterns from a filename while preserving the content title.

    Args:
        filename (str): The input filename

    Returns:
        str: Filename with usernames removed
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

async def get_audio_thumbnail(audio_path, output_dir="downloads"):
    audio = MutagenFile(audio_path)
    thumbnail_path = os.path.join(output_dir, "audio_thumbnail.jpg")

    if isinstance(audio, MP3):
        if audio.tags and isinstance(audio.tags, ID3):
            for tag in audio.tags.values():
                if isinstance(tag, APIC):
                    with open(thumbnail_path, "wb") as img_file:
                        img_file.write(tag.data)
                    return thumbnail_path
    elif isinstance(audio, FLAC):
        if audio.pictures:
            with open(thumbnail_path, "wb") as img_file:
                img_file.write(audio.pictures[0].data)
            return thumbnail_path
    elif isinstance(audio, MP4):
        if audio.tags and 'covr' in audio.tags:
            cover = audio.tags['covr'][0]
            with open(thumbnail_path, "wb") as img_file:
                img_file.write(cover)
            return thumbnail_path
    
    return None
