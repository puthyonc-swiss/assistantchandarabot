"""
config.py
---------
Edit THIS file to add/remove Telegram groups, or change the live score link.
Do NOT put secrets (bot token, Gemini API key) here — those go in
environment variables on Render (see render_env_example.txt).
"""

# ── Live score link (shown in every report) ──────────────────────────────
LIVE_SCORE_LINK = "https://example.com/your-live-score-link"  # <-- replace with your real link

# ── Telegram groups the bot can post to ───────────────────────────────────
# "id" = the group's Telegram chat ID (a negative number, e.g. -1001234567890)
# "name" = label shown on the button (emoji optional, just for clarity)
#
# How to get a group's chat ID (we'll do this together once the bot is live):
#   1. Add the bot to the group
#   2. Send any message in the group
#   3. Visit: https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
#   4. Look for "chat":{"id": ... } in the response
GROUPS = [
    {"name": "📢 Main Group", "id": -1000000000001},   # <-- replace with real ID
    {"name": "🏆 VIP Group", "id": -1000000000002},    # <-- replace with real ID
    {"name": "📊 Public Channel", "id": -1000000000003},  # <-- replace with real ID
]

# ── Event name shown in reports (you can hardcode it, or we can make it
#    ask you each time later — keeping it simple for now) ─────────────────
EVENT_NAME = "Petanque SwissKH Tournament"
