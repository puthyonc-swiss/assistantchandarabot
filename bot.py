"""
bot.py
------
SwissKH Telegram Report Bot

WHAT THIS BOT DOES (step by step):
  1. You send one or more screenshot photos to the bot (private chat) -
     send them as an album, or one at a time, either works the same way.
     One status message updates in place showing the running count, with
     "Done" and "Add more" buttons.
  2. When you tap Done, bot asks: Manual or AI read?
     - AI read: sends ONLY THE FIRST photo to Gemini AI to detect the
       round number (ORIGINAL/unchanged behavior), then asks start time.
     - Manual: you pick the round/stage from Khmer buttons, type the
       start time, then pick time limit and score limit from buttons.
  3. Bot shows you the draft report (ALL photos + one caption) for approval
  4. Bot shows checkbox buttons for each group (from config.py)
  5. You tap groups, then tap "Send" -> bot posts ALL photos (as one
     album) + the caption to those groups. Session then resets so the
     next photo you send starts a brand new report.

SECRETS NEEDED (set these as environment variables, never hardcode them):
  TELEGRAM_BOT_TOKEN   - from @BotFather
  GEMINI_API_KEY       - from Google AI Studio
"""

import os
import logging
import asyncio
import time

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
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
from google.genai import errors as genai_errors

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
(
    COLLECTING_PHOTOS,
    CHOOSING_MODE,
    CHOOSING_MANUAL_ROUND,
    WAITING_FOR_MANUAL_TIME,
    CHOOSING_TIME_LIMIT,
    CHOOSING_SCORE_LIMIT,
    WAITING_FOR_START_TIME,
    CHOOSING_GROUPS,
) = range(8)

# ── Manual mode options (Khmer labels) ─────────────────────────────────────
# Round/stage names for the Manual flow. Order matters - this is the order
# buttons appear in (2 per row).
MANUAL_ROUND_OPTIONS = [
    "ជុំទី 1",
    "ជុំទី 2",
    "ជុំទី 3",
    "ជុំទី 4",
    "ជុំទី 5",
    "វគ្គ 1/16 ផ្តាច់ព្រ័ត្រ",
    "វគ្គ 1/8 ផ្តាច់ព្រ័ត្រ",
    "វគ្គ 1/4 ផ្តាច់ព្រ័ត្រ",
    "វគ្គពាក់កណ្តាលផ្តាច់ព្រ័ត្រ",
    "វគ្គផ្តាច់ព្រ័ត្រ",
]

# Time limit options for the Manual flow (1 per row).
MANUAL_TIME_LIMIT_OPTIONS = [
    "30 នាទី + 1សេវ៉ា",
    "40 នាទី + 1សេវ៉ា",
    "50 នាទី + 1សេវ៉ា",
    "60 នាទី + 1សេវ៉ា",
]

# Score limit options for the Manual flow.
MANUAL_SCORE_LIMIT_OPTIONS = [
    "លេង 11 ពិន្ទុ",
    "លេង 13 ពិន្ទុ",
]

# Rounds that get AUTO-FILLED time limit + score (skip those 2 questions).
# Indices 0-7 = ជុំទី 1,2,3,4,5 + វគ្គ 1/16, 1/8, 1/4 (confirmed with Chandara).
# Indices 8-9 (half-final, final) are NOT in this set - they keep asking
# manually via buttons, unchanged.
AUTO_FILL_ROUND_INDICES = set(range(0, 8))

# Default values used for auto-filled rounds. These can still be changed
# afterward via the "✏️ Edit time & score" button on the draft screen.
AUTO_FILL_TIME_LIMIT_LABEL = "40 នាទី + 1សេវ៉ា"
AUTO_FILL_SCORE_LIMIT_LABEL = "លេង 11 ពិន្ទុ"

# Model choice: Flash-Lite is the cheapest Gemini model that still supports
# vision (reading images). Good fit for simple screenshot reading.
GEMINI_MODEL = "gemini-2.5-flash-lite"

# Retry settings for when Gemini's servers are temporarily overloaded
# (HTTP 503 "Service Unavailable" / "high demand" errors). These are NOT
# bugs in our code - Google's servers occasionally reject requests for a
# few seconds during traffic spikes, and retrying after a short wait
# usually succeeds.
GEMINI_MAX_RETRIES = 3
GEMINI_RETRY_DELAY_SECONDS = 3


# ───────────────────────────────────────────────────────────────────────────
# STEP 1: Collect photos as they arrive (can be 1 or many, sent as an
# album or one at a time - same handling either way)
# ───────────────────────────────────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Triggered every time the user sends a photo. Just stores it - does
    NOT call Gemini yet. Gemini reading happens once the user taps
    '✅ Done selecting photos' (see handle_done_collecting).

    Instead of sending a new status message for every photo (which would
    spam the chat), we send ONE status message on the first photo, then
    EDIT that same message's text each time a new photo arrives, so it
    always shows the current count in place."""

    # Make sure we start with a clean list the first time we enter this
    # state (e.g. a brand new report), but keep appending on later photos.
    if "photos" not in context.user_data:
        context.user_data["photos"] = []

    # Telegram sends multiple sizes of the same photo; the last one is the
    # largest/highest quality.
    largest_photo = update.message.photo[-1]
    context.user_data["photos"].append(largest_photo.file_id)

    count = len(context.user_data["photos"])
    status_text = f"📸 {count} photo(s) received."
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Done", callback_data="done_collecting"),
            InlineKeyboardButton("➕ Add more", callback_data="add_more_tapped"),
        ]
    ])

    status_message_id = context.user_data.get("status_message_id")

    if status_message_id is None:
        # First photo of this report - send the status message and
        # remember its ID so later photos can edit it.
        sent_message = await update.message.reply_text(status_text, reply_markup=keyboard)
        context.user_data["status_message_id"] = sent_message.message_id
    else:
        # A later photo - edit the existing status message in place.
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_message_id,
                text=status_text,
                reply_markup=keyboard,
            )
        except Exception as e:
            # If editing fails for any reason (e.g. message too old, or
            # was deleted), fall back to sending a fresh status message
            # rather than silently losing the count display.
            logger.warning(f"Could not edit status message, sending new one: {e}")
            sent_message = await update.message.reply_text(status_text, reply_markup=keyboard)
            context.user_data["status_message_id"] = sent_message.message_id

    return COLLECTING_PHOTOS


async def handle_add_more_tapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Triggered when the user taps '➕ Add more'. This button is just a
    gentle reminder/prompt - it does NOT change anything. Sending photos
    directly (without tapping this) already works the same way."""
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("📤 Please upload your photo.")
    return COLLECTING_PHOTOS


async def handle_done_collecting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Triggered when the user taps '✅ Done selecting photos'.
    Asks whether to use Manual entry or AI read for the round/time info."""

    query = update.callback_query
    await query.answer()

    photos = context.user_data.get("photos", [])
    if not photos:
        await query.edit_message_text(
            "⚠️ No photos found - please send at least one photo first."
        )
        clear_session_data(context)
        return ConversationHandler.END

    await query.edit_message_text(
        f"📸 Got {len(photos)} photo(s).\n\nWhich one do you want to work?",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✍️ Manual", callback_data="mode_manual"),
                InlineKeyboardButton("🤖 AI read", callback_data="mode_ai"),
            ]
        ]),
    )
    return CHOOSING_MODE


async def handle_ai_read_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Triggered when the user taps '🤖 AI read'.
    Takes the FIRST photo collected and sends it to Gemini to read the
    round number. The other photos (if any) are kept for later, when we
    post the final report - all photos go out together as an album.
    This is the ORIGINAL/unchanged AI-read behavior - just moved here from
    what used to be handle_done_collecting."""

    query = update.callback_query
    await query.answer()

    photos = context.user_data.get("photos", [])

    await query.edit_message_text(
        f"📸 Got {len(photos)} photo(s). Reading the first one..."
    )

    first_photo_file_id = photos[0]
    photo_file = await context.bot.get_file(first_photo_file_id)
    photo_bytes = await photo_file.download_as_bytearray()

    # Ask Gemini to read the screenshot
    # We retry automatically if Gemini's servers are temporarily overloaded
    # (503 errors) - this is common and usually resolves within seconds.
    gemini_text = ""
    last_error = None

    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
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
            last_error = None
            break  # success - stop retrying

        except genai_errors.ServerError as e:
            # 5xx errors (e.g. 503 "high demand") - worth retrying
            last_error = e
            logger.warning(
                f"Gemini server error on attempt {attempt}/{GEMINI_MAX_RETRIES}: {e}"
            )
            if attempt < GEMINI_MAX_RETRIES:
                await asyncio.sleep(GEMINI_RETRY_DELAY_SECONDS)

        except Exception as e:
            # Anything else (bad request, auth error, etc.) - retrying won't
            # help, so fail immediately instead of wasting time.
            last_error = e
            logger.error(f"Gemini error (not retrying): {e}")
            break

    if last_error is not None:
        logger.error(f"Gemini call failed after retries: {last_error}")
        count = len(photos)
        await query.message.reply_text(
            "⚠️ I couldn't read the photo right now (the AI service is busy).\n"
            f"Your {count} photo(s) are still saved - just tap Done to try again, "
            "or send more photos first.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Done", callback_data="done_collecting"),
                    InlineKeyboardButton("➕ Add more", callback_data="add_more_tapped"),
                ]
            ]),
        )
        return COLLECTING_PHOTOS

    # Very simple parsing of Gemini's reply (we keep this basic on purpose)
    round_value = "unknown"
    for line in gemini_text.splitlines():
        if line.strip().upper().startswith("ROUND:"):
            round_value = line.split(":", 1)[1].strip()

    context.user_data["round_value"] = round_value

    if round_value.lower() == "unknown" or round_value == "":
        # Gemini couldn't find the round - ask the user directly
        await query.message.reply_text(
            "🤔 I couldn't tell which round this is from the photo.\n"
            "What round is this? (e.g. just type: 1)"
        )
        return WAITING_FOR_START_TIME  # reuse this state to capture round instead
    else:
        await query.message.reply_text(
            f"✅ Looks like: Round {round_value}\n\n"
            "What time did this round start? (e.g. 12:00 PM)"
        )
        return WAITING_FOR_START_TIME


# ───────────────────────────────────────────────────────────────────────────
# STEP 2 (AI path): Receive the start time (or missing round info) typed
# by the user
# ───────────────────────────────────────────────────────────────────────────
async def handle_start_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Triggered when the user replies with the start time (plain text).
    This is part of the AI-read path only - unchanged from before."""

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

    # Build the draft caption (OLD/unchanged format for the AI path)
    round_value = context.user_data.get("round_value", "?")
    caption = build_caption(round_value, user_text)

    return await show_draft_and_group_buttons(update.message, context, caption)


async def show_draft_and_group_buttons(message, context: ContextTypes.DEFAULT_TYPE, caption: str, is_manual: bool = False) -> int:
    """Shared by BOTH the AI-read and Manual paths: stores the final
    caption, shows the draft (all photos + caption) for approval, then
    shows the group checkboxes. message can be a real Message object or
    any object with reply_media_group/reply_text (e.g. query.message).
    is_manual=True adds the '✏️ Edit time & score' button (Manual path
    only - AI-read path has no time-limit/score-limit to edit)."""

    context.user_data["caption"] = caption
    context.user_data["is_manual_report"] = is_manual

    # Telegram only shows the caption under the FIRST photo in an album -
    # that's a Telegram limitation, not something we can change.
    photos = context.user_data.get("photos", [])
    media_group = [
        InputMediaPhoto(media=file_id, caption=caption if i == 0 else None)
        for i, file_id in enumerate(photos)
    ]
    await message.reply_media_group(media=media_group)

    # Show group checkboxes
    context.user_data["selected_groups"] = set()
    await message.reply_text(
        "👆 Here's the draft report. Select which group(s) to send it to:",
        reply_markup=build_group_keyboard(set(), show_edit_time_score=is_manual),
    )
    return CHOOSING_GROUPS


def build_caption(round_value: str, start_time: str) -> str:
    """Builds the final report text for the AI-read path (OLD/unchanged
    format)."""
    return (
        f"🏆 {config.EVENT_NAME}\n\n"
        f"📋 Round {round_value} started at {start_time}\n\n"
        f"📊 Track live scores here:\n{config.LIVE_SCORE_LINK}"
    )


# ───────────────────────────────────────────────────────────────────────────
# Manual path: round selection -> start time -> time limit -> score limit
# ───────────────────────────────────────────────────────────────────────────
def build_manual_round_keyboard() -> InlineKeyboardMarkup:
    """Builds the round/stage selection buttons, 2 per row."""
    rows = []
    for i in range(0, len(MANUAL_ROUND_OPTIONS), 2):
        pair = MANUAL_ROUND_OPTIONS[i:i + 2]
        rows.append([
            InlineKeyboardButton(label, callback_data=f"manual_round:{idx}")
            for idx, label in zip(range(i, i + len(pair)), pair)
        ])
    return InlineKeyboardMarkup(rows)


def build_manual_time_limit_keyboard() -> InlineKeyboardMarkup:
    """Builds the time-limit selection buttons, 1 per row."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"manual_timelimit:{idx}")]
        for idx, label in enumerate(MANUAL_TIME_LIMIT_OPTIONS)
    ])


def build_manual_score_limit_keyboard() -> InlineKeyboardMarkup:
    """Builds the score-limit selection buttons."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"manual_scorelimit:{idx}")]
        for idx, label in enumerate(MANUAL_SCORE_LIMIT_OPTIONS)
    ])


async def handle_manual_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Triggered when the user taps '✍️ Manual'. Shows the round/stage
    selection buttons."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "តើជុំទីប៉ុន្មាន? (Which round?)",
        reply_markup=build_manual_round_keyboard(),
    )
    return CHOOSING_MANUAL_ROUND


async def handle_manual_round_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Triggered when a round/stage button is tapped. Stores the chosen
    label and asks for the start time (text input, same style as the AI
    path's time question). Also remembers whether this round is one of
    the AUTO-FILL rounds (ជុំទី 1-5, វគ្គ 1/16, 1/8, 1/4), so the next
    step knows whether to skip the time-limit/score-limit questions."""
    query = update.callback_query
    await query.answer()

    idx = int(query.data.split(":", 1)[1])
    round_label = MANUAL_ROUND_OPTIONS[idx]
    context.user_data["manual_round_label"] = round_label
    context.user_data["manual_is_auto_fill_round"] = idx in AUTO_FILL_ROUND_INDICES

    await query.edit_message_text(
        f"✅ {round_label}\n\n"
        "What time did this round start? (e.g. 12:00 PM)"
    )
    return WAITING_FOR_MANUAL_TIME


async def handle_manual_time_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Triggered when the user types the start time for the Manual path.

    Branches here:
      - AUTO-FILL rounds (ជុំទី 1-5, វគ្គ 1/16, 1/8, 1/4): skip the
        time-limit/score-limit questions, auto-set them, go straight
        to the draft.
      - Other rounds (half-final, final): continue to the time-limit
        buttons as before (unchanged behavior).
    """
    user_text = update.message.text.strip()
    context.user_data["manual_start_time"] = user_text

    if context.user_data.get("manual_is_auto_fill_round"):
        context.user_data["manual_time_limit_label"] = AUTO_FILL_TIME_LIMIT_LABEL
        context.user_data["manual_score_limit_label"] = AUTO_FILL_SCORE_LIMIT_LABEL
        return await finalize_manual_caption_and_show_draft(update.message, context)

    await update.message.reply_text(
        "What is the time limit?",
        reply_markup=build_manual_time_limit_keyboard(),
    )
    return CHOOSING_TIME_LIMIT


async def handle_manual_time_limit_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Triggered when a time-limit button is tapped."""
    query = update.callback_query
    await query.answer()

    idx = int(query.data.split(":", 1)[1])
    time_limit_label = MANUAL_TIME_LIMIT_OPTIONS[idx]
    context.user_data["manual_time_limit_label"] = time_limit_label

    await query.edit_message_text(
        f"✅ {time_limit_label}\n\nWhat is the score to play to?",
        reply_markup=build_manual_score_limit_keyboard(),
    )
    return CHOOSING_SCORE_LIMIT


async def handle_manual_score_limit_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Triggered when a score-limit button is tapped. This is the LAST
    manual input step - builds the final caption and shows the draft."""
    query = update.callback_query
    await query.answer()

    idx = int(query.data.split(":", 1)[1])
    score_limit_label = MANUAL_SCORE_LIMIT_OPTIONS[idx]
    context.user_data["manual_score_limit_label"] = score_limit_label

    await query.edit_message_text(f"✅ {score_limit_label}")
    return await finalize_manual_caption_and_show_draft(query.message, context)


def build_manual_caption(round_label: str, start_time: str, time_limit_label: str, score_limit_label: str) -> str:
    """Builds the final report text for the MANUAL path.
    Layout confirmed with Chandara:
      - Each line has a meaning-based emoji (not literal sport icons,
        since there's no pétanque emoji in Unicode) - EXCEPT score limit,
        which has no emoji (confirmed)
      - Start time gets its OWN row
      - Time limit + score limit stay together on the row below it
    """
    return (
        f"🏆 {config.EVENT_NAME}\n\n"
        f"🎯 {round_label}\n\n"
        f"⏰ ចាប់ផ្តើមប្រកួតម៉ោង {start_time}\n"
        f"⏱️ {time_limit_label} | {score_limit_label}\n\n"
        f"📊 តាមដានពិន្ទុតាមគេហទំព័រខាងក្រោមនេះ\n"
        f"{config.LIVE_SCORE_LINK}"
    )


async def finalize_manual_caption_and_show_draft(message, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Shared final step for the Manual path: builds the caption from
    whatever round/time/time-limit/score-limit values are currently
    stored, then shows the draft. Used by BOTH:
      - the auto-fill path (skips straight here after start time)
      - the normal manual path (after picking time-limit + score-limit)
      - the "Edit time & score" re-edit flow (after re-picking both)
    """
    caption = build_manual_caption(
        round_label=context.user_data["manual_round_label"],
        start_time=context.user_data["manual_start_time"],
        time_limit_label=context.user_data["manual_time_limit_label"],
        score_limit_label=context.user_data["manual_score_limit_label"],
    )
    return await show_draft_and_group_buttons(message, context, caption, is_manual=True)


def clear_session_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Wipes all data for the current report so the NEXT report starts
    completely fresh. Called after a report is successfully sent,
    cancelled, or ends in an unrecoverable error - so old photos from a
    finished report never accidentally mix into a new one."""
    context.user_data.clear()


# ───────────────────────────────────────────────────────────────────────────
# STEP 3: Build and handle the group selection buttons
# ───────────────────────────────────────────────────────────────────────────
def build_group_keyboard(selected_ids: set, show_edit_time_score: bool = False) -> InlineKeyboardMarkup:
    """Builds the checkbox-style button list from config.GROUPS.
    show_edit_time_score adds an extra '✏️ Edit time & score' button above
    the group checkboxes - only relevant for Manual-path reports (the
    AI-read path never has a time-limit/score-limit to edit)."""
    rows = []

    if show_edit_time_score:
        rows.append([InlineKeyboardButton("✏️ Edit time & score", callback_data="edit_time_score")])

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


async def handle_edit_time_score_tapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Triggered when '✏️ Edit time & score' is tapped on the draft screen
    (Manual path only). Re-shows the time-limit buttons; after picking
    both time-limit and score-limit again, returns to the draft with the
    updated caption (reuses the same handlers as the normal manual flow)."""
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "What is the time limit?",
        reply_markup=build_manual_time_limit_keyboard(),
    )
    return CHOOSING_TIME_LIMIT


async def handle_group_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Triggered whenever a button is tapped during group selection."""
    query = update.callback_query
    await query.answer()  # required by Telegram, even if no popup needed

    selected = context.user_data.get("selected_groups", set())

    if query.data == "cancel":
        await query.edit_message_text("❌ Cancelled. No report was sent.")
        clear_session_data(context)
        return ConversationHandler.END

    if query.data == "send_now":
        if not selected:
            await query.answer("Please select at least one group first.", show_alert=True)
            return CHOOSING_GROUPS

        await query.edit_message_text("📤 Sending...")

        photos = context.user_data.get("photos", [])
        caption = context.user_data["caption"]
        media_group = [
            InputMediaPhoto(media=file_id, caption=caption if i == 0 else None)
            for i, file_id in enumerate(photos)
        ]

        sent_to = []
        failed = []
        for group in config.GROUPS:
            if group["id"] in selected:
                try:
                    await context.bot.send_media_group(
                        chat_id=group["id"],
                        media=media_group,
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
        clear_session_data(context)
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
    # Clear any leftover data from a previous report, so a new /start
    # always begins a clean session.
    clear_session_data(context)
    await update.message.reply_text(
        "👋 Hi! Send me one or more screenshots of your round/pairing screen "
        "(you can send several at once), then tap Done when finished."
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_session_data(context)
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
            COLLECTING_PHOTOS: [
                MessageHandler(filters.PHOTO, handle_photo),  # more photos arriving
                CallbackQueryHandler(handle_done_collecting, pattern="^done_collecting$"),
                CallbackQueryHandler(handle_add_more_tapped, pattern="^add_more_tapped$"),
            ],
            CHOOSING_MODE: [
                CallbackQueryHandler(handle_manual_chosen, pattern="^mode_manual$"),
                CallbackQueryHandler(handle_ai_read_chosen, pattern="^mode_ai$"),
            ],
            CHOOSING_MANUAL_ROUND: [
                CallbackQueryHandler(handle_manual_round_selected, pattern="^manual_round:"),
            ],
            WAITING_FOR_MANUAL_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_time_entered),
            ],
            CHOOSING_TIME_LIMIT: [
                CallbackQueryHandler(handle_manual_time_limit_selected, pattern="^manual_timelimit:"),
            ],
            CHOOSING_SCORE_LIMIT: [
                CallbackQueryHandler(handle_manual_score_limit_selected, pattern="^manual_scorelimit:"),
            ],
            WAITING_FOR_START_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_start_time),
            ],
            CHOOSING_GROUPS: [
                CallbackQueryHandler(handle_edit_time_score_tapped, pattern="^edit_time_score$"),
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
