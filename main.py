import io
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from PIL import Image, ImageDraw, ImageFont
import telebot
from telebot import types

# === CONFIGURATION ===
TOKEN = "8396894464:AAGnd8hSvW7tfZvErlxDl8wp_H5xIzQ80Do"
bot = telebot.TeleBot(TOKEN, parse_mode="Markdown")

# === IN-MEMORY DATABASES ===
user_scores = {}  
user_warnings = {}  
group_ships = {}  
afk_users = {}  
group_filters = {}  
user_stats = {}  
group_locks = {}    
group_antispam = {} 


# === HEALTH CHECK SERVER (For Render Keep-Alive) ===
class HealthCheckHandler(BaseHTTPRequestHandler):

  def do_GET(self):
    self.send_response(200)
    self.end_headers()
    self.wfile.write(b"MishraRobot is active and running!")

  def log_message(self, format, *args):
    return


def run_server():
  port = int(os.environ.get("PORT", 10000))
  server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
  server.serve_forever()


# === HELPER FUNCTIONS ===
def get_user_id_from_message(message):
  if message.reply_to_message:
    return message.reply_to_message.from_user.id
  args = message.text.split()
  if len(args) > 1:
    target = args[1]
    if target.isdigit():
      return int(target)
    elif target.startswith("@"):
      username = target[1:]
      for uid, udata in user_stats.get(message.chat.id, {}).items():
        if udata.get("username") == username:
          return uid
  return None


def is_user_admin(chat_id, user_id):
  try:
    member = bot.get_chat_member(chat_id, user_id)
    return member.status in ["creator", "administrator"]
  except:
    return False


# === START & HELP ===
@bot.message_handler(commands=["start", "help", "panel"])
def send_welcome(message):
  help_text = (
      "🌸 **MISHRA ROBOT — PANEL**\n\n"
      "📊 **Stats & Fun:**\n"
      "• `/top` or `/ranking` — Group leaderboard\n"
      "• `/mystats` — Your message count\n"
      "• `/ship` — 24-hour group matchmaking\n"
      "• `/afk [reason]` — Set AFK status\n"
      "• `/back` — Remove AFK status\n\n"
      "🛡️ **Moderation & Protection (Admins):**\n"
      "• `/ban`, `/unban`, `/kick`, `/mute`, `/unmute`\n"
      "• `/warn`, `/unwarn`, `/pin`, `/unpin`\n"
      "• `/del` or `/delete` — Delete replied message\n"
      "• `/purge` — Reply to a message to purge up to it\n"
      "• `/lock`, `/unlock` — Lock/Unlock group chat\n"
      "• `/filter WORD text` — Add word filter\n"
      "• `/filters` — List filters\n"
      "• `/stopfilter WORD` — Remove filter\n"
      "• `/antispam on|off` — Toggle antispam\n\n"
      "🎭 **Media & Tools:**\n"
      "• `/mmf` — Large bold meme text on photo/sticker\n"
      "• `/kang` — Save sticker\n"
      "• `@admin` / `@all [message]` — Tag admins or everyone"
  )
  bot.send_message(message.chat.id, help_text)


# === STATS & FUN COMMANDS ===
@bot.message_handler(commands=["ranking", "top"])
def show_ranking(message):
  chat_id = message.chat.id
  if chat_id in user_scores and user_scores[chat_id]:
    sorted_users = sorted(
        user_scores[chat_id].values(), key=lambda x: x["count"], reverse=True
    )
    text = "🏆 **Group Leaderboard (Top 10):**\n\n"
    for i, u in enumerate(sorted_users[:10], 1):
      medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
      text += f"{medal} {u['name']} — {u['count']} messages\n"
  else:
    text = "📊 No message ranking data recorded yet!"
  bot.send_message(message.chat.id, text)


@bot.message_handler(commands=["mystats", "stats"])
def show_mystats(message):
  chat_id = message.chat.id
  user_id = message.from_user.id
  if chat_id in user_scores and user_id in user_scores[chat_id]:
    u = user_scores[chat_id][user_id]
    text = (
        f"📊 **Your Stats:**\n👤 Name: {u['name']}\n💬 Messages:"
        f" {u['count']}\n⚠️ Warnings:"
        f" {user_warnings.get(chat_id, {}).get(user_id, 0)}"
    )
  else:
    text = "📊 No stats recorded for you yet!"
  bot.send_message(message.chat.id, text)


@bot.message_handler(commands=["ship"])
def group_ship(message):
  chat_id = message.chat.id
  today = datetime.now().strftime("%Y-%m-%d")

  if (
      chat_id in group_ships
      and group_ships[chat_id]["date"] == today
      and chat_id in user_scores
      and len(user_scores[chat_id]) >= 2
  ):
    bot.send_message(
        chat_id,
        f"💖 **Today's Couple of the Day:**\n{group_ships[chat_id]['text']}\n_(Next"
        " ship unlocks tomorrow!)_",
    )
    return

  if chat_id not in user_scores or len(user_scores[chat_id]) < 2:
    bot.send_message(
        chat_id,
        "❌ Not enough active users in this group to perform matchmaking!",
    )
    return

  users = list(user_scores[chat_id].values())
  p1 = random.choice(users)
  p2 = random.choice(users)
  while p1 == p2 and len(users) > 1:
    p2 = random.choice(users)

  percentage = random.randint(30, 100)
  love_bar = "█" * (percentage // 10) + "░" * (10 - (percentage // 10))
  ship_msg = (
      f"💘 **Matchmaking Result:**\n"
      f"@{p1['name']} 💖 @{p2['name']}\n"
      f"Compatibility: **{percentage}%**\n`{love_bar}`"
  )

  group_ships[chat_id] = {"date": today, "text": ship_msg}
  bot.send_message(chat_id, ship_msg)


# === AFK SYSTEM ===
@bot.message_handler(commands=["afk"])
def set_afk(message):
  chat_id = message.chat.id
  user_id = message.from_user.id
  reason = (
      message.text.split(maxsplit=1)[1]
      if len(message.text.split()) > 1
      else "AFK"
  )
  if chat_id not in afk_users:
    afk_users[chat_id] = {}
  afk_users[chat_id][user_id] = {"reason": reason, "time": time.time()}
  bot.reply_to(message, f"💤 {message.from_user.first_name} is now AFK: {reason}")


@bot.message_handler(commands=["back"])
def remove_afk(message):
  chat_id = message.chat.id
  user_id = message.from_user.id
  if chat_id in afk_users and user_id in afk_users[chat_id]:
    del afk_users[chat_id][user_id]
    bot.reply_to(message, f"👋 Welcome back, {message.from_user.first_name}!")


# === MEDIA: MMF (Large Bold Text) ===
@bot.message_handler(commands=["mmf"])
def mmf_command(message):
  if not message.reply_to_message or not (
      message.reply_to_message.photo or message.reply_to_message.sticker
  ):
    bot.reply_to(
        message,
        "❌ Reply to a photo or sticker with `/mmf Top Text` or `/mmf Top ; Bottom Text`!",
    )
    return

  parts = message.text.split(maxsplit=1)
  if len(parts) < 2:
    bot.reply_to(message, "❌ Please provide text! Example: `/mmf Ghummi karne ja rhe`")
    return

  text_input = parts[1]
  top_text = ""
  bottom_text = ""

  if ";" in text_input:
    split_t = text_input.split(";", 1)
    top_text = split_t[0].strip()
    bottom_text = split_t[1].strip()
  else:
    top_text = text_input.strip()

  file_info = None
  if message.reply_to_message.photo:
    file_info = bot.get_file(message.reply_to_message.photo[-1].file_id)
  elif message.reply_to_message.sticker:
    if message.reply_to_message.sticker.is_animated:
      bot.reply_to(
          message,
          "❌ Animated stickers are not supported for custom text rendering.",
      )
      return
    file_info = bot.get_file(message.reply_to_message.sticker.file_id)

  downloaded_file = bot.download_file(file_info.file_path)

  try:
    img = Image.open(io.BytesIO(downloaded_file)).convert("RGBA")
    W, H = img.size
    draw = ImageDraw.Draw(img)

    try:
      font_size = max(int(H / 8), 24)
      font = ImageFont.truetype("arial.ttf", font_size)
    except:
      font = ImageFont.load_default()

    def draw_text_with_outline(xy, text):
      x, y = xy
      for adj_x in range(-3, 4):
        for adj_y in range(-3, 4):
          draw.text((x + adj_x, y + adj_y), text.upper(), font=font, fill="black")
      draw.text((x, y), text.upper(), font=font, fill="white")

    if top_text:
      bbox = draw.textbbox((0, 0), top_text.upper(), font=font)
      w = bbox[2] - bbox[0]
      draw_text_with_outline(((W - w) / 2, 10), top_text)

    if bottom_text:
      bbox = draw.textbbox((0, 0), bottom_text.upper(), font=font)
      w = bbox[2] - bbox[0]
      h = bbox[3] - bbox[1]
      draw_text_with_outline(((W - w) / 2, H - h - 15), bottom_text)

    output = io.BytesIO()
    output.name = "sticker.webp"
    img.save(output, format="WEBP")
    output.seek(0)
    bot.send_sticker(message.chat.id, output)
  except Exception as e:
    bot.reply_to(message, f"❌ Error processing image/sticker: {e}")


# === MEDIA: KANG ===
@bot.message_handler(commands=["kang"])
def kang_sticker(message):
  if not message.reply_to_message or not message.reply_to_message.sticker:
    bot.reply_to(message, "❌ Reply to any sticker to kang it!")
    return

  sticker = message.reply_to_message.sticker
  try:
    file_info = bot.get_file(sticker.file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    
    if sticker.is_animated:
      bot.send_document(message.chat.id, (f"{sticker.file_unique_id}.tgs", downloaded_file), caption="📦 Kanged Animated Sticker")
    elif sticker.is_video:
      bot.send_document(message.chat.id, (f"{sticker.file_unique_id}.webm", downloaded_file), caption="📦 Kanged Video Sticker")
    else:
      output = io.BytesIO(downloaded_file)
      output.name = "kanged.webp"
      bot.send_sticker(message.chat.id, output)
  except Exception as e:
    bot.reply_to(message, f"❌ Failed to kang sticker: {e}")


# === DELETE & PURGE SYSTEM ===
@bot.message_handler(commands=["del", "delete"])
def delete_message(message):
  if not is_user_admin(message.chat.id, message.from_user.id):
    bot.reply_to(message, "❌ Only admins can delete messages!")
    return
  if not message.reply_to_message:
    bot.reply_to(message, "❌ Reply to a message to delete it!")
    return
  try:
    bot.delete_message(message.chat.id, message.reply_to_message.message_id)
    bot.delete_message(message.chat.id, message.message_id)
  except Exception as e:
    bot.reply_to(message, f"❌ Failed to delete message: {e}")


@bot.message_handler(commands=["purge"])
def purge_messages(message):
  if not is_user_admin(message.chat.id, message.from_user.id):
    bot.reply_to(message, "❌ Only admins can purge messages!")
    return
  if not message.reply_to_message:
    bot.reply_to(message, "❌ Reply to the starting message you want to purge from!")
    return
  
  start_id = message.reply_to_message.message_id
  end_id = message.message_id
  deleted_count = 0

  for msg_id in range(start_id, end_id + 1):
    try:
      bot.delete_message(message.chat.id, msg_id)
      deleted_count += 1
    except:
      pass

  status_msg = bot.send_message(message.chat.id, f"🧹 Purged {deleted_count} messages.")
  time.sleep(3)
  try:
    bot.delete_message(message.chat.id, status_msg.message_id)
  except:
    pass


# === LOCK / UNLOCK / FILTERS & PROTECTION ===
@bot.message_handler(commands=["lock", "unlock"])
def toggle_lock(message):
  chat_id = message.chat.id
  user_id = message.from_user.id
  if not is_user_admin(chat_id, user_id):
    bot.reply_to(message, "❌ Only admins can lock/unlock the group!")
    return
  
  command = message.text.split()[0].replace("/", "").lower()
  try:
    if command == "lock":
      perm = types.ChatPermissions(can_send_messages=False)
      bot.set_chat_permissions(chat_id, perm)
      bot.reply_to(message, "🔒 Group has been locked! Members cannot send messages.")
    else:
      perm = types.ChatPermissions(
          can_send_messages=True,
          can_send_media_messages=True,
          can_send_other_messages=True,
          can_add_web_page_previews=True
      )
      bot.set_chat_permissions(chat_id, perm)
      bot.reply_to(message, "🔓 Group has been unlocked! Members can now chat.")
  except Exception as e:
    bot.reply_to(message, f"❌ Failed to change lock status: {e}")


@bot.message_handler(commands=["filter"])
def add_filter(message):
  chat_id = message.chat.id
  user_id = message.from_user.id
  if not is_user_admin(chat_id, user_id):
    bot.reply_to(message, "❌ Only admins can add filters!")
    return
  
  args = message.text.split(maxsplit=2)
  if len(args) < 3:
    bot.reply_to(message, "❌ Usage: `/filter WORD response text`")
    return
  
  keyword = args[1].lower()
  response = args[2]
  
  if chat_id not in group_filters:
    group_filters[chat_id] = {}
  group_filters[chat_id][keyword] = response
  bot.reply_to(message, f"✅ Filter for `{keyword}` added successfully!")


@bot.message_handler(commands=["filters"])
def list_filters(message):
  chat_id = message.chat.id
  if chat_id in group_filters and group_filters[chat_id]:
    text = "📁 **Active Group Filters:**\n\n"
    for kw in group_filters[chat_id].keys():
      text += f"• `{kw}`\n"
    bot.send_message(chat_id, text)
  else:
    bot.send_message(chat_id, "📁 No active filters set in this group.")


@bot.message_handler(commands=["stopfilter"])
def remove_filter(message):
  chat_id = message.chat.id
  user_id = message.from_user.id
  if not is_user_admin(chat_id, user_id):
    bot.reply_to(message, "❌ Only admins can remove filters!")
    return
  
  args = message.text.split()
  if len(args) < 2:
    bot.reply_to(message, "❌ Usage: `/stopfilter WORD`")
    return
  
  keyword = args[1].lower()
  if chat_id in group_filters and keyword in group_filters[chat_id]:
    del group_filters[chat_id][keyword]
    bot.reply_to(message, f"✅ Filter `{keyword}` removed.")
  else:
    bot.reply_to(message, "❌ Filter not found.")


@bot.message_handler(commands=["antispam"])
def toggle_antispam(message):
  chat_id = message.chat.id
  user_id = message.from_user.id
  if not is_user_admin(chat_id, user_id):
    bot.reply_to(message, "❌ Only admins can configure antispam!")
    return
  
  args = message.text.split()
  if len(args) < 2:
    bot.reply_to(message, "❌ Usage: `/antispam on` or `/antispam off`")
    return
  
  status = args[1].lower()
  if status == "on":
    group_antispam[chat_id] = True
    bot.reply_to(message, "🛡️ Antispam protection enabled.")
  else:
    group_antispam[chat_id] = False
    bot.reply_to(message, "🛡️ Antispam protection disabled.")


# === ADMIN MODERATION COMMANDS ===
@bot.message_handler(commands=["ban", "unban", "kick", "mute", "unmute", "warn", "unwarn", "pin", "unpin"])
def moderation_actions(message):
  chat_id = message.chat.id
  user_id = message.from_user.id

  if not is_user_admin(chat_id, user_id):
    bot.reply_to(message, "❌ You must be an administrator to use this command!")
    return

  command = message.text.split()[0].replace("/", "").lower()

  if command == "pin":
    if message.reply_to_message:
      bot.pin_chat_message(chat_id, message.reply_to_message.message_id)
      bot.reply_to(message, "📌 Message pinned successfully!")
    else:
      bot.reply_to(message, "❌ Reply to a message to pin it!")
    return

  if command == "unpin":
    bot.unpin_chat_message(chat_id)
    bot.reply_to(message, "📌 Unpinned latest message!")
    return

  target_id = get_user_id_from_message(message)
  if not target_id:
    bot.reply_to(message, "❌ Specify a user via reply, username, or user ID.")
    return

  try:
    if command == "ban":
      bot.ban_chat_member(chat_id, target_id)
      bot.reply_to(message, f"🔨 User `{target_id}` has been banned.")
    elif command == "unban":
      bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
      bot.reply_to(message, f"🔓 User `{target_id}` has been unbanned.")
    elif command == "kick":
      bot.ban_chat_member(chat_id, target_id, until_date=int(time.time() + 30))
      bot.reply_to(message, f"👢 User `{target_id}` has been kicked.")
    elif command == "mute":
      permission = types.ChatPermissions(can_send_messages=False)
      bot.restrict_chat_member(chat_id, target_id, permissions=permission)
      bot.reply_to(message, f"🔇 User `{target_id}` has been muted.")
    elif command == "unmute":
      permission = types.ChatPermissions(
          can_send_messages=True,
          can_send_media_messages=True,
          can_send_other_messages=True,
          can_add_web_page_previews=True,
      )
      bot.restrict_chat_member(chat_id, target_id, permissions=permission)
      bot.reply_to(message, f"🔊 User `{target_id}` has been unmuted.")
    elif command == "warn":
      if chat_id not in user_warnings:
        user_warnings[chat_id] = {}
      user_warnings[chat_id][target_id] = user_warnings[chat_id].get(target_id, 0) + 1
      count = user_warnings[chat_id][target_id]
      if count >= 3:
        bot.ban_chat_member(chat_id, target_id)
        bot.reply_to(message, f"⚠️ User reached 3 warnings and has been **banned**!")
      else:
        bot.reply_to(message, f"⚠️ Warning issued to user (`{count}/3`).")
    elif command == "unwarn":
      if chat_id in user_warnings and target_id in user_warnings[chat_id]:
        user_warnings[chat_id][target_id] = max(0, user_warnings[chat_id][target_id] - 1)
        bot.reply_to(message, f"✅ Warning removed. Current warnings: {user_warnings[chat_id][target_id]}/3")
  except Exception as e:
    bot.reply_to(message, f"❌ Action failed. Error: {e}")


# === TAGGING SYSTEM (@admin / @all) ===
@bot.message_handler(func=lambda msg: msg.text and (msg.text.lower().startswith("@admin") or msg.text.lower().startswith("@all")))
def handle_tags(message):
  chat_id = message.chat.id
  text = message.text.strip()
  
  if text.lower().startswith("@admin"):
    try:
      admins = bot.get_chat_administrators(chat_id)
      admin_mentions = []
      for admin in admins:
        if not admin.user.is_bot:
          name = admin.user.first_name
          admin_mentions.append(f"[{name}](tg://user?id={admin.user.id})")
      
      custom_msg = text[6:].strip()
      output = "🚨 **Attention Admins:** " + (" ".join(admin_mentions))
      if custom_msg:
        output += f"\n💬 **Reason:** {custom_msg}"
      bot.reply_to(message, output)
    except Exception as e:
      bot.reply_to(message, f"❌ Could not fetch admins: {e}")

  elif text.lower().startswith("@all"):
    user_id = message.from_user.id
    if not is_user_admin(chat_id, user_id):
      bot.reply_to(message, "❌ Only administrators can use `@all` tag!")
      return
    
    custom_msg = text[4:].strip()
    output = "📢 **Attention Everyone!**"
    if custom_msg:
      output += f"\n\n{custom_msg}"
    bot.reply_to(message, output)


# === MESSAGE & AFK MONITORING (Middleware) ===
@bot.message_handler(
    func=lambda message: True,
    content_types=[
        "text",
        "sticker",
        "animation",
        "photo",
        "video",
        "document",
        "audio",
        "voice",
    ],
)
def global_message_handler(message):
  if message.chat.type in ["group", "supergroup"]:
    chat_id = message.chat.id
    user_id = message.from_user.id
    user_name = message.from_user.first_name
    username = message.from_user.username
    msg_text = message.text or message.caption or ""

    # 1. Filter Check
    if chat_id in group_filters:
      for kw, resp in group_filters[chat_id].items():
        if kw in msg_text.lower():
          bot.reply_to(message, resp)
          break

    # 2. Store User Stats & Names
    if chat_id not in user_stats:
      user_stats[chat_id] = {}
    user_stats[chat_id][user_id] = {"name": user_name, "username": username}

    # 3. Check AFK Return
    if chat_id in afk_users and user_id in afk_users[chat_id]:
      afk_info = afk_users[chat_id].pop(user_id)
      duration = int(time.time() - afk_info["time"])
      bot.reply_to(message, f"👋 Welcome back {user_name}! You were away for {duration} seconds.")

    # 4. Check AFK Mentions in Reply
    if message.reply_to_message:
      replied_user_id = message.reply_to_message.from_user.id
      if chat_id in afk_users and replied_user_id in afk_users[chat_id]:
        afk_info = afk_users[chat_id][replied_user_id]
        bot.reply_to(message, f"💤 User is currently AFK: {afk_info['reason']}")

    # 5. Message Counting for Leaderboard
   
