
import logging
import asyncio
from datetime import datetime
from pyrogram import filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import ChatAdminRequired, UserAlreadyParticipant

from config import BACKUP_CHANNEL_LINK
from utility import (
    add_user,
    users_col,
    get_user_link,
    safe_api_call,
    is_user_subscribed,
    auto_delete_message,
    get_allowed_channels,
    queue_file_for_processing,
)
from app import bot

logger = logging.getLogger(__name__)

@bot.on_chat_member_updated()
async def on_chat_member_updated_handler(client, chat_member_updated):
    try:
        if chat_member_updated.new_chat_member and chat_member_updated.new_chat_member.status in [enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.MEMBER]:
            user_id = chat_member_updated.from_user.id
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
        user_link = await get_user_link(message.from_user)
        first_name = message.from_user.first_name or "there"
        username = message.from_user.username or None
        user_doc = await add_user(user_id)
        joined_date = user_doc.get("joined", "Unknown")
        joined_str = joined_date.strftime("%Y-%m-%d %H:%M") if isinstance(joined_date, datetime) else str(joined_date)

        # Log new users
#        if user_doc.get("_new"):
#            log_msg = (
#                f"👤 New user added:\n"
#                f"ID: <code>{user_id}</code>\n"
#                f"First Name: <b>{first_name}</b>\n"
#            )
#            if username:
#                log_msg += f"Username: @{username}\n"
#            await safe_api_call(
#                lambda: bot.send_message(LOG_CHANNEL_ID, log_msg, parse_mode=enums.ParseMode.HTML)
#            )

        # Blocked users
        if user_doc.get("blocked", False):
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
            [InlineKeyboardButton("⚙️ Configure Channel", callback_data="config_bot")]
        ]
        #if CF_DOMAIN:
            #buttons.append([InlineKeyboardButton("🕸️ Website", url=CF_DOMAIN)])

        if buttons:
            reply_markup = InlineKeyboardMarkup(buttons)
        else:
            reply_markup = None             

        welcome_text = (
            f"Hi <b>{first_name}</b> 🆔 <code>{user_id}</code> ! 👋\n\n"
            "Thanks for hopping in! 😄\n"
            "We will reach out to you soon.\n"
            "Sit tight — we’ll be in touch before you know it! 🚀"
        )

        reply_msg = await safe_api_call(lambda: message.reply_text(
            welcome_text,
            quote=True,
            reply_markup=reply_markup
        ))

        if reply_msg:
            bot.loop.create_task(auto_delete_message(message, reply_msg))

    except Exception as e:
        logger.error(f"⚠️ Error in start_handler: {e}")

@bot.on_message(filters.channel & (filters.document | filters.video | filters.audio | filters.photo))
async def channel_file_handler(client, message):
    try:
        allowed_channels = await get_allowed_channels()
        if message.chat.id not in allowed_channels:
            return

        asyncio.create_task(queue_file_for_processing(message))
        
    except Exception as e:
        logger.error(f"Error in channel_file_handler: {e}")

@bot.on_message(filters.group & filters.service)
async def delete_service_messages(client, message):
    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Failed to delete service message in chat {message.chat.id}: {e}")

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
            "Press the below button to configure channel for TGFLIX",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        logger.error(f"⚠️ Error in config_callback_handler: {e}")

@bot.on_chat_join_request()
async def approve_join_request_handler(client, join_request):
    try:
        await client.approve_chat_join_request(join_request.chat.id, join_request.from_user.id)
#        await safe_api_call(lambda: bot.send_message(LOG_CHANNEL_ID, f"✅ Approved join request for {join_request.from_user.mention} in {join_request.chat.title}"))
    except (ChatAdminRequired, UserAlreadyParticipant) as e:
        logger.warning(f"Could not approve join request: {e}")
    except Exception as e:
        logger.error(f"Failed to approve join request: {e}")
