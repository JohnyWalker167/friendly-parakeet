
import logging
import asyncio
from datetime import datetime
from pyrogram import filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import ChatAdminRequired, UserAlreadyParticipant, UserIsBlocked, InputUserDeactivated, PeerIdInvalid, UserIsBot

from config import BACKUP_CHANNEL_LINK, UPDATE_CHANNEL_ID, BOT_USERNAME, OWNER_ID, LOG_CHANNEL_ID, MY_DOMAIN
from utility import (
    add_user,
    users_col,
    safe_api_call,
    is_user_subscribed,
    auto_delete_message,
    verify_token,
    is_user_authorized,
    generate_token,
    shorten_url,
    human_readable_size,
    check_file_limit,
    increment_file_count,
    files_col,
    auth_users_col,
    build_search_pipeline,
    queue_file_for_processing, 
    extract_channel_and_msg_id
)
from app import bot
from tmdb import search_tmdb, get_info
from bson.objectid import ObjectId
from db import allowed_channels_col
from cache import cache

logger = logging.getLogger(__name__)

broadcasting = False

@bot.on_chat_member_updated()
async def on_chat_member_updated_handler(client, chat_member_updated):
    try:
        # Get bot's own ID
        me = client.me or await client.get_me()
        
        # We only care if the member being updated is the bot itself
        if not (chat_member_updated.new_chat_member and chat_member_updated.new_chat_member.user.id == me.id):
            return

        # Check if the bot was NOT previously a member/admin
        was_not_member = not chat_member_updated.old_chat_member or chat_member_updated.old_chat_member.status in [
            enums.ChatMemberStatus.LEFT,
            enums.ChatMemberStatus.BANNED,
        ]
        
        # If the bot is now a member or administrator
        if chat_member_updated.new_chat_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.MEMBER]:
            if was_not_member:
                user_id = chat_member_updated.from_user.id if chat_member_updated.from_user else None
                if not user_id:
                    return
                
                if chat_member_updated.from_user.is_bot:
                    return

                chat_id = chat_member_updated.chat.id
                
                # We only update if it's a channel or group
                if chat_member_updated.chat.type in [enums.ChatType.CHANNEL, enums.ChatType.GROUP, enums.ChatType.SUPERGROUP]:
                    await users_col.update_one(
                        {"user_id": user_id},
                        {"$set": {"channel_id": chat_id}},
                        upsert=False # We assume user is already in DB from /start
                    )
                    
                    try:
                        await client.send_message(
                            user_id,
                            f"✅ Successfully configured <b>{chat_member_updated.chat.title}</b> (<code>{chat_id}</code>) as your destination channel!"
                        )
                    except Exception as e:
                        logger.warning(f"Could not send confirmation message to user {user_id}: {e}")
    except Exception as e:
        logger.error(f"Error in on_chat_member_updated_handler: {e}")

@bot.on_message(filters.command("start") & filters.private)
async def start_handler(client, message):
    try:
        user_id = message.from_user.id
        first_name = message.from_user.first_name or "there"
        user_doc = await add_user(user_id)

        # Blocked users
        if user_doc.get("blocked", False):
            return

        # Check for token in start command
        if len(message.command) > 1:
            token = message.command[1]
            if await verify_token(user_id, token):
                await message.reply_text("✅ Verification successful! You now have access for 24 hours.")
            else:
                reply = await message.reply_text("❌ Invalid or expired verification token.")
                bot.loop.create_task(auto_delete_message(message, reply))
            return

        # --- Check subscription ---
        if not await is_user_subscribed(client, user_id):
            reply = await safe_api_call(lambda: message.reply_text(
                text="Please join our updates channel to continue 😊",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔔 Join Updates", url=f"{BACKUP_CHANNEL_LINK}")]]
                )
            ))
            bot.loop.create_task(auto_delete_message(message, reply))
            return

        # --- Authorized or new user ---
        buttons = [
            [InlineKeyboardButton("⚙️ Configure", callback_data="config_bot")]
        ]

        welcome_text = (
            f"Hi <b>{first_name}</b> ! 👋\n\n"
            "Thanks for hopping in! 😄\n"
            "Send me a query to search for"
        )

        reply_msg = await safe_api_call(lambda: message.reply_text(
            welcome_text,
            quote=True,
            reply_markup=InlineKeyboardMarkup(buttons)
        ))
        bot.loop.create_task(auto_delete_message(message, reply_msg))
    except Exception as e:
        logger.error(f"⚠️ Error in start_handler: {e}")

@bot.on_message(filters.channel & (filters.document | filters.video | filters.audio | filters.photo))
async def channel_file_handler(client, message):
    try:
        channel_id = message.chat.id
        channel_doc = await allowed_channels_col.find_one({"channel_id": channel_id})
        if not channel_doc:
            return

        is_no_tmdb = channel_doc.get("is_no_tmdb", False)
        asyncio.create_task(queue_file_for_processing(message, is_no_tmdb=is_no_tmdb))
        
    except Exception as e:
        logger.error(f"Error in channel_file_handler: {e}")

@bot.on_callback_query(filters.regex("config_bot"))
async def config_callback_handler(client, query):
    try:
        me = client.me or await client.get_me()
        bot_username = me.username
        buttons = [
            [
                InlineKeyboardButton("Add to Channel", url=f"https://t.me/{bot_username}?startchannel=true&admin=post_messages"),
                InlineKeyboardButton("Add to Group", url=f"https://t.me/{bot_username}?startgroup=true&admin=post_messages")
            ]
        ]
        await query.message.edit_text(
            "Press the below button to configure channel for the bot",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        logger.error(f"⚠️ Error in config_callback_handler: {e}")
        

@bot.on_chat_join_request()
async def approve_join_request_handler(client, join_request):
    try:
        if join_request.chat.id != UPDATE_CHANNEL_ID:
            return
        await client.approve_chat_join_request(join_request.chat.id, join_request.from_user.id)
    except (ChatAdminRequired, UserAlreadyParticipant) as e:
        logger.warning(f"Could not approve join request: {e}")
    except Exception as e:
        logger.error(f"Failed to approve join request: {e}")

async def get_search_results(query_text, page=1, channel_id=None):
    sanitized_search = bot.sanitize_query(query_text)
    skip = (page - 1) * 10
    match_query = {"channel_id": channel_id} if channel_id else None
    pipeline = build_search_pipeline(sanitized_search, 'file_name', match_query=match_query, skip=skip, limit=10)
    search_result = await files_col.aggregate(pipeline).to_list(length=None)
    
    files = search_result[0]['results'] if search_result and 'results' in search_result[0] else []
    total_count = search_result[0]['totalCount'][0]['total'] if search_result and 'totalCount' in search_result[0] and search_result[0]['totalCount'] else 0
    
    return files, total_count

async def get_filter_keyboard():
    buttons = []
    # Fetch all allowed channels
    async for channel in allowed_channels_col.find():
        channel_id = channel.get("channel_id")
        channel_name = channel.get("channel_name", f"Channel {channel_id}")
        buttons.append([InlineKeyboardButton(channel_name, callback_data=f"apply_filter_{channel_id}")])
    
    # Add "All Channels" option
    buttons.append([InlineKeyboardButton("🌐 All Channels", callback_data="apply_filter_all")])
    # Add a "Back" button to return to search results
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="search_page_1")])
    
    return InlineKeyboardMarkup(buttons)

def get_search_keyboard(files, query_text, current_page, total_count):
    buttons = []
    for f in files:
        file_size = human_readable_size(f.get("file_size"))
        buttons.append([InlineKeyboardButton(f"{f.get('file_name')} ({file_size})", callback_data=f"send_file_{f['_id']}")])
    
    pagination_buttons = []
    if current_page > 1:
        pagination_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"search_page_{current_page-1}"))
    
    pagination_buttons.append(InlineKeyboardButton("🔍 Filters", callback_data="search_filters"))

    if (current_page * 10) < total_count:
        pagination_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"search_page_{current_page+1}"))
    
    buttons.append(pagination_buttons)
    
    return InlineKeyboardMarkup(buttons)

@bot.on_message(filters.private & filters.text & ~filters.command(["start", "add", "rm", "index", "stats", "restart", "del", "broadcast", "log"]))
async def search_message_handler(client, message):
    user_id = message.from_user.id
    query_text = message.text.strip()

    if message.from_user and message.from_user.is_bot:
        return
    
    if not await is_user_authorized(user_id):
        token = await generate_token(user_id)
        start_link = f"https://t.me/{BOT_USERNAME}?start={token}"
        short_link = await shorten_url(start_link)
        reply = await message.reply_text(
            "⚠️ Verification Required\n\nPlease verify your account to use search.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Verify Now", url=short_link)]])
        )

        bot.loop.create_task(auto_delete_message(message, reply))
        return

    # Owner TMDB search
    if user_id == OWNER_ID and query_text.startswith("tmdb "):
        tmdb_query = query_text[5:]
        results = await search_tmdb(tmdb_query)
        if not results:
            reply = await message.reply_text("❌ No TMDB results found.")
            bot.loop.create_task(auto_delete_message(message, reply))
            return
        
        buttons = []
        for res in results:
            buttons.append([InlineKeyboardButton(f"🎬 {res['title']} ({res['year']})", callback_data=f"send_tmdb_{res['media_type']}_{res['id']}")])
        
        reply = await message.reply_text(f"TMDB Results for: {tmdb_query}", reply_markup=InlineKeyboardMarkup(buttons))
        bot.loop.create_task(auto_delete_message(message, reply))
        return

    # Regular file search
    cache[f"query_{user_id}"] = query_text
    cache.pop(f"filter_{user_id}", None) # Reset filter for new search

    files, total_count = await get_search_results(query_text)
    if not files:
        reply = await message.reply_text(f"❌ No results found for: {query_text}")
        bot.loop.create_task(auto_delete_message(message, reply))
        return
    
    keyboard = get_search_keyboard(files, query_text, 1, total_count)
    reply = await message.reply_text(f"Search results for: <b>{query_text}</b>\nFilter: <b>All Channels</b>\nTotal found: {total_count}", reply_markup=keyboard)
    bot.loop.create_task(auto_delete_message(message, reply))

@bot.on_callback_query(filters.regex(r"^search_filters$"))
async def search_filter_handler(client, query):
    await query.answer()
    keyboard = await get_filter_keyboard()
    await query.message.edit_text(
        "Select a channel to filter your search results:",
        reply_markup=keyboard
    )

@bot.on_callback_query(filters.regex(r"^apply_filter_"))
async def apply_filter_handler(client, query):
    user_id = query.from_user.id
    filter_type = query.data.split("_")[2]
    
    query_text = cache.get(f"query_{user_id}")
    if not query_text:
        await query.answer("Session expired, please search again.", show_alert=True)
        return

    channel_id = None if filter_type == "all" else int(filter_type)
    cache[f"filter_{user_id}"] = channel_id
    
    await query.answer(f"Filter applied: {'All Channels' if not channel_id else 'Selected Channel'}")
    
    files, total_count = await get_search_results(query_text, page=1, channel_id=channel_id)
    if not files:
        await query.message.edit_text(
            f"❌ No results found for: <b>{query_text}</b> with the selected filter.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Filters", callback_data="search_filters")]])
        )
        return

    keyboard = get_search_keyboard(files, query_text, 1, total_count)
    
    filter_name = "All Channels"
    if channel_id:
        channel_doc = await allowed_channels_col.find_one({"channel_id": channel_id})
        filter_name = channel_doc.get("channel_name", "Selected Channel") if channel_doc else "Selected Channel"

    await query.message.edit_text(
        f"Search results for: <b>{query_text}</b>\nFilter: <b>{filter_name}</b>\nTotal found: {total_count}",
        reply_markup=keyboard
    )

@bot.on_callback_query(filters.regex(r"^search_page_"))
async def search_pagination_handler(client, query):
    user_id = query.from_user.id
    page = int(query.data.split("_")[2])
    query_text = cache.get(f"query_{user_id}")
    channel_id = cache.get(f"filter_{user_id}")
    
    if not query_text:
        await query.answer("Session expired, please search again.", show_alert=True)
        return

    await query.answer("Loading..")

    files, total_count = await get_search_results(query_text, page=page, channel_id=channel_id)
    keyboard = get_search_keyboard(files, query_text, page, total_count)
    
    filter_name = "All Channels"
    if channel_id:
        channel_doc = await allowed_channels_col.find_one({"channel_id": channel_id})
        filter_name = channel_doc.get("channel_name", "Selected Channel") if channel_doc else "Selected Channel"

    await query.message.edit_text(
        f"Search results for: <b>{query_text}</b>\nFilter: <b>{filter_name}</b>\nTotal found: {total_count}",
        reply_markup=keyboard
    )

@bot.on_callback_query(filters.regex(r"^send_tmdb_"))
async def send_tmdb_callback_handler(client, callback_query):
    if callback_query.from_user.id != OWNER_ID:
        await callback_query.answer("Unauthorized", show_alert=True)
        return
    
    data = callback_query.data.split("_")
    tmdb_type = data[2]
    tmdb_id = data[3]
    
    await callback_query.answer("Processing TMDB info...")
    info = await get_info(tmdb_type, tmdb_id)
    if info and "message" in info and not info["message"].startswith("Error"):
        text = info["message"]
        poster_url = info.get("poster_url")
        
        try:
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🎥 Trailer", url=info["trailer_url"])]]
            ) if info.get("trailer_url") else None

            if poster_url:
                await client.send_photo(UPDATE_CHANNEL_ID, poster_url, caption=text, reply_markup=keyboard)
            else:
                await client.send_message(UPDATE_CHANNEL_ID, text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Error sending TMDB info: {e}")
            await callback_query.answer(f"Failed to send: {e}", show_alert=True)
    else:
        await callback_query.answer("Failed to get info from TMDB", show_alert=True)

@bot.on_callback_query(filters.regex(r"^send_file_"))
async def send_file_callback_handler(client, callback_query):
    user_id = callback_query.from_user.id
    file_id = callback_query.data.split("_")[2]
    
    if not await is_user_authorized(user_id):
        await callback_query.answer("Verification expired. Please re-verify.", show_alert=True)
        return

    if not await check_file_limit(user_id):
        await callback_query.answer("Daily limit reached!", show_alert=True)
        return

    user_data = await users_col.find_one({"user_id": user_id})
    user_channel_id = user_data.get("channel_id") if user_data else None

    if not user_channel_id:
        await callback_query.answer("Please configure your channel first! /start", show_alert=True)
        return

    try:
        file = await files_col.find_one({"_id": ObjectId(file_id)})
        if not file:
            await callback_query.answer("File not found!", show_alert=True)
            return

        from_channel_id = file.get("channel_id")
        message_id = file.get("message_id")
        filename = file.get("file_name")
        
        encoded_link = bot.encode_file_link(from_channel_id, message_id, user_id)
        play_url = f"{MY_DOMAIN}/player/{encoded_link}"
        
        await bot.copy_message(
            chat_id=user_channel_id,
            from_chat_id=from_channel_id,
            message_id=message_id,
            caption=f"<b>{filename}</b>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎬 Play", url=play_url)]
            ])
        )
        
        await increment_file_count(user_id)
        await callback_query.answer("File sent successfully!", show_alert=False)        
    except Exception as e:
        logger.error(f"Error sending file: {e}")
        await callback_query.answer("Failed to send file. Check bot permissions in your channel.", show_alert=True)

@bot.on_message(filters.command("add") & filters.private & filters.user(OWNER_ID))
async def add_channel_handler(client, message):
    if len(message.command) < 3:
        await message.reply_text("Usage: /add channel_id channel_name [notmdb]")
        return
    try:
        is_no_tmdb = False
        command_parts = message.command[2:]
        if command_parts and command_parts[-1].lower() == "notmdb":
            is_no_tmdb = True
            command_parts = command_parts[:-1]
        
        channel_id = int(message.command[1])
        channel_name = " ".join(command_parts)
        
        update_data = {
            "channel_id": channel_id,
            "channel_name": channel_name,
            "is_no_tmdb": is_no_tmdb
        }
        
        await allowed_channels_col.update_one(
            {"channel_id": channel_id},
            {"$set": update_data},
            upsert=True
        )
        msg = f"✅ Channel {channel_id} ({channel_name}) added to allowed channels."
        if is_no_tmdb:
            msg += "\nTMDB processing: <b>Disabled</b>"
        await message.reply_text(msg)
    except Exception as e:
        await message.reply_text(f"An error occurred: {e}")

@bot.on_message(filters.command("rm") & filters.private & filters.user(OWNER_ID))
async def remove_channel_handler(client, message):
    if len(message.command) != 2:
        await message.reply_text("Usage: /rm channel_id")
        return
    try:
        channel_id = int(message.command[1])
        result = await allowed_channels_col.delete_one({"channel_id": channel_id})
        if result.deleted_count:
            await message.reply_text(f"✅ Channel {channel_id} removed.")
        else:
            await message.reply_text("❌ Channel not found.")
    except Exception as e:
        await message.reply_text(f"An error occurred: {e}")

@bot.on_message(filters.command("index") & filters.private & filters.user(OWNER_ID))
async def index_channel_files(client, message):
    if len(message.command) < 3:
        await message.reply_text("Usage: /index <start_link> <end_link>")
        return
    try:
        start_link, end_link = message.command[1], message.command[2]
        start_channel_id, start_msg_id = extract_channel_and_msg_id(start_link)
        end_channel_id, end_msg_id = extract_channel_and_msg_id(end_link)
        
        if start_channel_id != end_channel_id:
             await message.reply_text("Start and end links must be from the same channel.")
             return
             
        channel_id = start_channel_id
        start_id = min(start_msg_id, end_msg_id)
        end_id = max(start_msg_id, end_msg_id)
        
        reply = await message.reply_text(f"🔁 Indexing files from {start_id} to {end_id}...")
        safe_api_call
        count = 0
        batch_size = 50
        for batch_start in range(start_id, end_id + 1, batch_size):
            batch_end = min(batch_start + batch_size - 1, end_id)
            ids = list(range(batch_start, batch_end + 1))
            messages = await safe_api_call(lambda: client.get_messages(channel_id, ids))
            
            if not messages:
                continue

            new_files_in_batch = 0
            for msg in messages:
                if msg and (msg.document or msg.video or msg.audio):
                    await queue_file_for_processing(msg, channel_id=channel_id)
                    count += 1
                    new_files_in_batch += 1
            
            if new_files_in_batch > 0:
                try:
                    await reply.edit_text(f"🔁 Indexing... {count} files queued.")
                except Exception:
                    pass
            await asyncio.sleep(2) # Slight delay between batches
        
        await reply.edit_text(f"✅ Indexing completed! {count} files queued.")
    except Exception as e:
        await message.reply_text(f"An error occurred: {e}")

@bot.on_message(filters.command("broadcast") & filters.chat(LOG_CHANNEL_ID))
async def broadcast_handler(client, message):
    global broadcasting
    if message.reply_to_message:
        if broadcasting:
            await message.reply_text("already broadcasting")
            return
        users = await users_col.find({}, {"_id": 0, "user_id": 1}).to_list(length=None)
        total_users = len(users)
        sent_count = 0
        failed_count = 0
        removed_count = 0
        broadcasting = True

        status_message = await safe_api_call(lambda: message.reply_text(
            f"📢 Broadcast in progress...\n\n"
            f"👥 Total Users: {total_users}\n"
            f"✅ Sent: {sent_count}\n"
            f"❌ Failed: {failed_count}\n"
            f"🗑️ Removed: {removed_count}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Cancel", callback_data="cancel_broadcast")]]
            )
        ))

        for i, user in enumerate(users):
            if not broadcasting:
                await status_message.edit_text("📢 <b>Broadcast cancelled.</b>")
                break
            try:
                msg = message.reply_to_message
                await asyncio.sleep(3)
                await safe_api_call(lambda: msg.copy(user["user_id"]))
                sent_count += 1
            except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid, UserIsBot):
                await users_col.delete_one({"user_id": user["user_id"]})
                removed_count += 1
            except Exception as e:
                failed_count += 1
                logger.error(f"Error broadcasting to {user['user_id']}: {e}")

            if i % 10 == 0:
                await asyncio.sleep(3)
                await safe_api_call(lambda: status_message.edit_text(
                    f"📢 Broadcast in progress...\n\n"
                    f"👥 Total Users: {total_users}\n"
                    f"✅ Sent: {sent_count}\n"
                    f"❌ Failed: {failed_count}\n"
                    f"🗑️ Removed: {removed_count}",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("Cancel", callback_data="cancel_broadcast")]]
                    )
                ))
        else:
            await safe_api_call(lambda: status_message.edit_text(
                f"✅ Broadcast finished!\n\n"
                f"👥 Total Users: {total_users}\n"
                f"✅ Sent: {sent_count}\n"
                f"❌ Failed: {failed_count}\n"
                f"🗑️ Removed: {removed_count}"
            ))

        broadcasting = False


@bot.on_callback_query(filters.regex("cancel_broadcast"))
async def cancel_broadcast_handler(client, query):
    global broadcasting
    if broadcasting:
        broadcasting = False
        await query.answer("Cancelling broadcast...", show_alert=True)
    else:
        await query.answer("No broadcast in progress.", show_alert=True)

@bot.on_message(filters.command("stats") & filters.private & filters.user(OWNER_ID))
async def stats_command(client, message):
    total_users = await users_col.count_documents({})
    total_auth_users = await auth_users_col.count_documents({})
    total_files = await files_col.count_documents({})
    
    # Get file count per channel
    pipeline = [
        {"$group": {"_id": "$channel_id", "count": {"$sum": 1}}}
    ]
    channel_counts = await files_col.aggregate(pipeline).to_list(length=None)
    
    channel_stats_text = ""
    for stat in channel_counts:
        channel_id = stat["_id"]
        count = stat["count"]
        channel_doc = await allowed_channels_col.find_one({"channel_id": channel_id})
        channel_name = channel_doc.get("channel_name", f"Unknown ({channel_id})") if channel_doc else f"Unknown ({channel_id})"
        channel_stats_text += f"  - {channel_name}: {count}\n"

    text = (
        f"📊 <b>Bot Stats</b>\n\n"
        f"🔐 Authorized Users: {total_auth_users} / {total_users}\n"
        f"📁 Total Files: {total_files}\n\n"
        f"📺 <b>Channel-wise Files:</b>\n"
        f"{channel_stats_text if channel_stats_text else '  None'}"
    )
    await message.reply_text(text)

@bot.on_message(filters.command("restart") & filters.private & filters.user(OWNER_ID))
async def restart_bot(client, message):
    import sys
    import os
    await message.delete()
    os.system("python3 update.py")
    os.execl(sys.executable, sys.executable, "bot.py")

@bot.on_message(filters.command("del") & filters.private & filters.user(OWNER_ID))
async def delete_command(client, message):
    try:
        args = message.command
        if not (2 <= len(args) <= 3):
            await message.reply_text("<b>Usage:</b> /del <link> [end_link]")
            return

        if len(args) == 2:
            user_input = args[1].strip()
            try:
                channel_id, msg_id = extract_channel_and_msg_id(user_input)
                result = await files_col.delete_one({"channel_id": channel_id, "message_id": msg_id})
                if result.deleted_count > 0:
                    await message.reply_text(f"Deleted file with message ID {msg_id}.")
                else:
                    await message.reply_text(f"No file record found.")
            except ValueError:
                await message.reply_text("Invalid link.")
        
        elif len(args) == 3:
            start_link = args[1].strip()
            end_link = args[2].strip()
            try:
                channel_id, start_msg_id = extract_channel_and_msg_id(start_link)
                _, end_msg_id = extract_channel_and_msg_id(end_link)
                start_id = min(start_msg_id, end_msg_id)
                end_id = max(start_msg_id, end_msg_id)
                result = await files_col.delete_many({
                    "channel_id": channel_id,
                    "message_id": {"$gte": start_id, "$lte": end_id}
                })
                await message.reply_text(f"Deleted {result.deleted_count} files.")
            except ValueError as e:
                await message.reply_text(f"Error: {e}")

    except Exception as e:
        logger.error(f"Error in delete_command: {e}")
        await message.reply_text(f"An error occurred: {e}")

@bot.on_message(filters.command("log") & filters.private & filters.user(OWNER_ID))
async def send_log_file(client, message: Message):
    log_file = "bot_log.txt"
    try:
        if not os.path.exists(log_file):
            await safe_api_call(lambda: message.reply_text("Log file not found."))
            return
        reply = await safe_api_call(lambda: client.send_document(message.chat.id, log_file, caption="Here is the log file."))
        bot.loop.create_task(auto_delete_message(message, reply))
    except Exception as e:
        logger.error(f"Failed to send log file: {e}")
