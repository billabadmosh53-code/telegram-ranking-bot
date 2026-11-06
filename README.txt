MishraRobot V2.0 - Render deployment

FILES
- main.py: Telegram bot code
- requirements.txt: Python dependencies

RENDER SETTINGS
- Service type: Web Service
- Runtime: Python
- Build command: pip install -r requirements.txt
- Start command: python main.py
- Environment variable: BOT_TOKEN = fresh token from @BotFather
- Optional: OPENAI_API_KEY = your own OpenAI API key (only if /ai is needed)
- Optional: OPENAI_MODEL = gpt-4o-mini
- PORT is supplied by Render automatically; do not hardcode it.

IMPORTANT
1. Revoke the token exposed in the old source and use a newly generated token.
2. Never commit BOT_TOKEN or .env to GitHub.
3. Run exactly ONE active Render instance / worker using this token. Stop local copies and duplicate Render services to avoid Telegram 409 conflicts.
4. SQLite on Render's normal ephemeral filesystem can be lost on redeploy/restart. For persistent stats/warnings/settings, attach a Render persistent disk mounted at /var/data and set DATABASE_PATH=/var/data/mishrarobot.db. Confirm persistent disk availability/pricing for your Render plan.
5. Add the bot to the group as admin and grant permissions needed for deleting messages, restricting/banning users, pinning messages, and reading messages. Disable BotFather privacy mode if you need it to count ordinary group messages.
6. /kang resends/downloads the sticker file; it does not add the sticker to a Telegram sticker pack. /mmf supports photos and static stickers, not animated/video stickers.

Implemented commands include:
/top /ranking, /stats /mystats, /profile, /ship, /challenge,
/afk /back, /ban /unban /kick /mute /unmute, /warn /unwarn,
/pin /unpin /del /delete /purge, /lock /unlock,
/antispam on|off, /antilink on|off, /adminlog on|off,
/filter /filters /stopfilter, /setwelcome /welcome, /setgoodbye /goodbye,
/mmf, /kang, @admin, @all, and optional /ai.

Before production, test each moderation command in a private test group. The bot cannot moderate group admins and Telegram permissions / group settings may limit actions.
