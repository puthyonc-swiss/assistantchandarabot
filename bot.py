"""
bot.py
------
SwissKH Telegram Report Bot

WHAT THIS BOT DOES (step by step):
  1. You send a screenshot photo to the bot (private chat)
  2. Bot sends the photo to Gemini AI, asking it to read round/event info
  3. If something important is missing (like start time), bot asks you
  4. Bot shows you the draft report (photo + caption) for approval
  5. Bot shows checkbox buttons for each group (from config.py)
  6. You tap groups, then tap "Send" -> bot posts to those groups

SECRETS NEEDED (set these as environment variables, never hardcode them):
  TELEGRAM_BOT_TOKEN   - from @BotFather
  GEMINI_API_KEY       - from Google AI Studio
"""

import os
import logging
import asyncio

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from google import genai
from google.genai import types

import config  # our editable settings file (groups, link, event name)

# ── Logging (so we can see what's happening in Render's logs) ─────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Load secrets from environment variables (NEVER hardcoded) ─────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# RENDER_EXTERNAL_URL is set AUTOMATICALLY by Render itself for every Web
# Service (you don't need to type this in - Render fills it in for you).
# It looks like: https://your-service-name.onrender.com
# We need it to tell Telegram where to send messages (the webhook address).
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN environment variable.")
if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY environment variable.")
if not RENDER_EXTERNAL_URL:
    raise RuntimeError(
        "Missing RENDER_EXTERNAL_URL environment variable. "
        "This should be set automatically by Render for Web Services. "
        "If running locally for testing, set it manually to a placeholder."
    )

# One shared Gemini client for the whole bot
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# ── Conversation states ────────────────────────────────────────────────────
# These are just labels for "where we are" in the chat with the user.
WAITING_FOR_START_TIME, CHOOSING_GROUPS = range(2)

# Model choice: Flash-Lite is the cheapest Gemini model that still supports
# vision (reading images). Good fit for simple screenshot reading.
GEMINI_MODEL = "gemini-2.5-flash-lite"


# ───────────────────────────────────────────────────────────────────────────
# STEP 1: Receive the photo, send it to Gemini, read back what it found
# ───────────────────────────────────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Triggered when the user sends a photo to the bot."""

    await update.message.reply_text("📸 Got it! Reading the screenshot...")

    # Telegram sends multiple sizes of the same photo; the last one is the
    # largest/highest quality.
    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()

    # Save the photo info in this chat's memory so later steps can use it
    context.user_data["photo_bytes"] = bytes(photo_bytes)
    context.user_data["photo_file_id"] = update.message.photo[-1].file_id

    # Ask Gemini to read the screenshot
    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=bytes(photo_bytes), mime_type="image/jpeg"),
                (
                    "This is a screenshot from a petanque tournament management "
                    "system. Look for: which ROUND number is shown (e.g. Round 1, "
                    "Round 2), and any tournament/event name visible. "
                    "Reply in this exact format, nothing else:\n"
                    "ROUND: <round number or 'unknown'>\n"
                    "NOTES: <anything else relevant you noticed, one short line>"
                ),
            ],
        )
        gemini_text = response.text or ""
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        await update.message.reply_text(
            "⚠️ I couldn't read the photo (AI error). Please try sending it again."
        )
        return ConversationHandler.END

    # Very simple parsing of Gemini's reply (we keep this basic on purpose)
    round_value = "unknown"
    for line in gemini_text.splitlines():
        if line.strip().upper().startswith("ROUND:"):
            round_value = line.split(":", 1)[1].strip()

    context.user_data["round_value"] = round_value

    if round_value.lower() == "unknown" or round_value == "":
        # Gemini couldn't find the round - ask the user directly
        await update.message.reply_text(
            "🤔 I couldn't tell which round this is from the photo.\n"
            "What round is this? (e.g. just type: 1)"
        )
        return WAITING_FOR_START_TIME  # reuse this state to capture round instead
    else:
        await update.message.reply_text(
            f"✅ Looks like: Round {round_value}\n\n"
            "What time did this round start? (e.g. 12:00 PM)"
        )
        return WAITING_FOR_START_TIME


# ───────────────────────────────────────────────────────────────────────────
# STEP 2: Receive the start time (or missing round info) typed by the user
# ───────────────────────────────────────────────────────────────────────────
async def handle_start_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Triggered when the user replies with the start time (plain text)."""

    user_text = update.message.text.strip()

    # If we didn't know the round yet, this message IS the round number
    if context.user_data.get("round_value", "unknown").lower() == "unknown":
        context.user_data["round_value"] = user_text
        await update.message.reply_text(
            "Got it. What time did this round start? (e.g. 12:00 PM)"
        )
        return WAITING_FOR_START_TIME

    # Otherwise, this message is the start time
    context.user_data["start_time"] = user_text

    # Build the draft caption
    round_value = context.user_data.get("round_value", "?")
    caption = build_caption(round_value, user_text)
    context.user_data["caption"] = caption

    # Show the draft (photo + caption) back to the user for approval
    await update.message.reply_photo(
        photo=context.user_data["photo_file_id"],
        caption=caption,
    )

    # Show group checkboxes
    context.user_data["selected_groups"] = set()
    await update.message.reply_text(
        "👆 Here's the draft report. Select which group(s) to send it to:",
        reply_markup=build_group_keyboard(set()),
    )
    return CHOOSING_GROUPS


def build_caption(round_value: str, start_time: str) -> str:
    """Builds the final report text shown with the photo."""
    return (
        f"🏆 {config.EVENT_NAME}\n\n"
        f"📋 Round {round_value} started at {start_time}\n\n"
        f"📊 Track live scores here:\n{config.LIVE_SCORE_LINK}"
    )


# ───────────────────────────────────────────────────────────────────────────
# STEP 3: Build and handle the group selection buttons
# ───────────────────────────────────────────────────────────────────────────
def build_group_keyboard(selected_ids: set) -> InlineKeyboardMarkup:
    """Builds the checkbox-style button list from config.GROUPS."""
    rows = []
    for group in config.GROUPS:
        checked = "☑️" if group["id"] in selected_ids else "⬜"
        rows.append([
            InlineKeyboardButton(
                f"{checked} {group['name']}",
                callback_data=f"toggle:{group['id']}",
            )
        ])
    rows.append([InlineKeyboardButton("✅ Send to selected groups", callback_data="send_now")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


async def handle_group_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Triggered whenever a button is tapped during group selection."""
    query = update.callback_query
    await query.answer()  # required by Telegram, even if no popup needed

    selected = context.user_data.get("selected_groups", set())

    if query.data == "cancel":
        await query.edit_message_text("❌ Cancelled. No report was sent.")
        return ConversationHandler.END

    if query.data == "send_now":
        if not selected:
            await query.answer("Please select at least one group first.", show_alert=True)
            return CHOOSING_GROUPS

        await query.edit_message_text("📤 Sending...")

        sent_to = []
        failed = []
        for group in config.GROUPS:
            if group["id"] in selected:
                try:
                    await context.bot.send_photo(
                        chat_id=group["id"],
                        photo=context.user_data["photo_file_id"],
                        caption=context.user_data["caption"],
                    )
                    sent_to.append(group["name"])
                except Exception as e:
                    logger.error(f"Failed to send to {group['name']}: {e}")
                    failed.append(group["name"])

        result_lines = []
        if sent_to:
            result_lines.append("✅ Sent to: " + ", ".join(sent_to))
        if failed:
            result_lines.append("⚠️ Failed to send to: " + ", ".join(failed))

        await query.message.reply_text("\n".join(result_lines))
        return ConversationHandler.END

    # Otherwise, it's a "toggle:<id>" button
    if query.data.startswith("toggle:"):
        group_id = int(query.data.split(":", 1)[1])
        if group_id in selected:
            selected.remove(group_id)
        else:
            selected.add(group_id)
        context.user_data["selected_groups"] = selected

        await query.edit_message_reply_markup(
            reply_markup=build_group_keyboard(selected)
        )
        return CHOOSING_GROUPS


# ───────────────────────────────────────────────────────────────────────────
# Basic commands
# ───────────────────────────────────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Hi! Send me a screenshot of your round/pairing screen, "
        "and I'll help you write the Telegram report for it."
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ───────────────────────────────────────────────────────────────────────────
# Wire everything together
# ───────────────────────────────────────────────────────────────────────────
def main() -> None:
    application: Application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command),
            MessageHandler(filters.PHOTO, handle_photo),
        ],
        states={
            WAITING_FOR_START_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_start_time),
            ],
            CHOOSING_GROUPS: [
                CallbackQueryHandler(handle_group_button),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        # per_message=False is correct here because this conversation mixes
        # text-input states (WAITING_FOR_START_TIME) with button-tap states
        # (CHOOSING_GROUPS). You WILL see a one-time startup warning in the
        # logs about "CallbackQueryHandler will not be tracked for every
        # message" - that is expected and harmless for our use case (one
        # user, one conversation at a time). Ignore it.
        per_message=False,
    )

    application.add_handler(conv_handler)

    # ── Webhook setup (instead of polling) ─────────────────────────────────
    # Render gives every Web Service a random port via the PORT env variable -
    # we MUST read it dynamically, never hardcode a port number.
    port = int(os.environ.get("PORT", 8443))

    # This is the URL Telegram will send messages to. We use the bot token
    # in the path as a simple way to make sure random internet traffic can't
    # pretend to be Telegram (only someone who knows the token can hit this
    # exact URL correctly).
    webhook_url = f"{RENDER_EXTERNAL_URL}/{TELEGRAM_BOT_TOKEN}"

    logger.info(f"Bot starting in webhook mode on port {port}...")
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TELEGRAM_BOT_TOKEN,
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
