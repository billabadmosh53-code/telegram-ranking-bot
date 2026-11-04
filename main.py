import io
import os
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from PIL import Image, ImageDraw, ImageFont
import telebot
from telebot import types

TOKEN = '9270651831:AAF1rFK0q3DF_d61B9HmcDwCbj1i0FbQ5DM'
bot = telebot.TeleBot(TOKEN)


class HealthCheckHandler(BaseHTTPRequestHandler):

  def do_GET(self):
    self.send_response(200)
    self.end_headers()
    self.wfile.write(b'Bot is active!')

  def log_message(self, format, *args):
    return


def run_server():
  port = int(os.environ.get('PORT', 10000))
  server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
  server.serve_forever()


user_scores = {}
afk_users = {}
pending_promotions = {}
user_warnings = {}
group_ships = {}  # 24-hour ship cache {chat_id: {'u1': ..., 'u2': ..., 'pct': ..., 'time': ...}}


@bot.message_handler(commands=['start'])
def send_welcome(message):
  markup = types.InlineKeyboardMarkup()
  btn1 = types.InlineKeyboardButton('📊 Your Stats', callback_data='stats')
  btn2 = types.InlineKeyboardButton('🏆 Top Ranking', callback_data='top')
  markup.add(btn1, btn2)
  bot.send_message(
      message.chat.id,
      '👋 Welcome! Add me to a group to track rankings, manage admins, make'
      ' meme stickers, and more!',
      reply_markup=markup,
  )


@bot.message_handler(commands=['help'])
def send_help(message):
  help_text = (
      '🤖 **Bot Help Menu:**\n\n📊 **Ranking & Fun:**\n• `/ranking` or `/top` -'
      ' Global leaderboard\n• `/mystats` - Check your stats\n• `/afk [reason]`'
      ' - Set AFK status\n• `/ship` - Group Matchmaking (Lock for 24h)\n•'
      ' `/mmf text` - Classic Meme Sticker\n• `/kang` - Save sticker as Photo'
      ' or Video\n\n🛡️ **Admin & Moderation Commands:**\n• `/promote` - Choose'
      ' admin rights via 4 buttons\n• `/demote` - Remove admin rights\n•'
      ' `/ban` - Ban user (Reply or User ID)\n• `/unban [user_id]` - Unban user\n•'
      ' `/kick` - Kick user\n• `/mute` - Mute user (Reply or User ID)\n•'
      ' `/unmute` - Unmute user\n• `/warn` - Warn user\n• `/unwarn` - Remove'
      ' warnings\n• `/pin` / `/unpin` - Pin/Unpin message'
  )
  bot.send_message(message.chat.id, help_text, parse_mode='Markdown')


def is_user_admin(chat_id, user_id):
  try:
    member = bot.get_chat_member(chat_id, user_id)
    return member.status in ['creator', 'administrator']
  except:
    return False


def get_target_from_message(message):
  chat_id = message.chat.id
  if message.reply_to_message:
    return (
        message.reply_to_message.from_user.id,
        message.reply_to_message.from_user.first_name,
    )

  text = message.text or ''
  parts = text.split(maxsplit=1)
  if len(parts) < 2:
    return None, None

  query = parts[1].strip()

  clean_q = re.sub(r'[^0-9]', '', query)
  if clean_q and clean_q.isdigit():
    try:
      uid = int(clean_q)
      member = bot.get_chat_member(chat_id, uid)
      return member.user.id, member.user.first_name
    except:
      pass

  clean_name_query = re.sub(r'[^a-zA-Z0-9]', '', query).lower()
  if chat_id in user_scores:
    for uid, udata in user_scores[chat_id].items():
      db_name = re.sub(r'[^a-zA-Z0-9]', '', udata['name']).lower()
      db_uname = (
          re.sub(r'[^a-zA-Z0-9]', '', udata['username']).lower()
          if udata['username']
          else ''
      )
      if clean_name_query in db_uname or clean_name_query in db_name:
        return uid, udata['name']

  return None, None


@bot.message_handler(commands=['promote'])
def promote_user(message):
  chat_id = message.chat.id
  if not is_user_admin(chat_id, message.from_user.id):
    bot.send_message(chat_id, '⚠️ Aap Admin nahi hain!')
    return

  target_id, target_name = get_target_from_message(message)
  if not target_id:
    bot.send_message(
        chat_id,
        '⚠️ Kripya user ke message par reply karein ya User ID dein!',
        parse_mode='Markdown',
    )
    return

  pending_promotions[message.from_user.id] = {
      'chat_id': chat_id,
      'target_id': target_id,
      'target_name': target_name,
  }

  markup = types.InlineKeyboardMarkup(row_width=1)
  markup.add(
      types.InlineKeyboardButton(
          '👑 Full Powerful Admin (All Rights)', callback_data='promo_full'
      ),
      types.InlineKeyboardButton(
          '🛡️ Normal Moderator (Ban, Info, Delete, Pin)',
          callback_data='promo_normal',
      ),
      types.InlineKeyboardButton(
          '⚡ Custom Admin (Delete & Pin)', callback_data='promo_light'
      ),
      types.InlineKeyboardButton(
          '🔒 Restricted Admin (No Rights)', callback_data='promo_restricted'
      ),
  )
  bot.send_message(
      chat_id,
      f'⚙️ **{target_name}** ke liye kis tarah ke admin rights dena chahte hain?',
      reply_markup=markup,
      parse_mode='Markdown',
  )


@bot.callback_query_handler(func=lambda call: call.data.startswith('promo_'))
def promote_callback(call):
  user_id = call.from_user.id
  if user_id not in pending_promotions:
    bot.answer_callback_query(call.id, '⚠️ Session expired!', show_alert=True)
    return

  data = pending_promotions.pop(user_id)
  chat_id = data['chat_id']
  target_id = data['target_id']
  target_name = data['target_name']

  try:
    if call.data == 'promo_full':
      bot.promote_chat_member(
          chat_id,
          target_id,
          can_manage_chat=True,
          can_post_messages=True,
          can_edit_messages=True,
          can_delete_messages=True,
          can_manage_video_chats=True,
          can_restrict_members=True,
          can_promote_members=True,
          can_change_info=True,
          can_invite_users=True,
          can_pin_messages=True,
      )
      desc = '👑 Full Powerful Admin'
    elif call.data == 'promo_normal':
      bot.promote_chat_member(
          chat_id,
          target_id,
          can_manage_chat=True,
          can_delete_messages=True,
          can_restrict_members=True,
          can_change_info=True,
          can_invite_users=True,
          can_pin_messages=True,
          can_promote_members=False,
      )
      desc = '🛡️ Normal Moderator'
    elif call.data == 'promo_light':
      bot.promote_chat_member(
          chat_id,
          target_id,
          can_manage_chat=True,
          can_delete_messages=True,
          can_pin_messages=True,
          can_restrict_members=False,
          can_change_info=False,
          can_invite_users=False,
          can_promote_members=False,
      )
      desc = '⚡ Custom Admin (Delete & Pin)'
    else:
      bot.promote_chat_member(
          chat_id,
          target_id,
          can_manage_chat=True,
          can_delete_messages=False,
          can_restrict_members=False,
          can_promote_members=False,
          can_change_info=False,
          can_invite_users=False,
          can_pin_messages=False,
      )
      desc = '🔒 Restricted Admin'

    bot.edit_message_text(
        f'✅ **{target_name}** ko successfully **{desc}** bana diya gaya hai! 🚀',
        chat_id,
        call.message.message_id,
        parse_mode='Markdown',
    )
  except Exception as e:
    bot.edit_message_text(
        f'❌ Error: {str(e)}', chat_id, call.message.message_id
    )


@bot.message_handler(commands=['demote'])
def demote_user(message):
  chat_id = message.chat.id
  if not is_user_admin(chat_id, message.from_user.id):
    return
  target_id, target_name = get_target_from_message(message)
  if not target_id:
    bot.send_message(
        chat_id, '⚠️ Kripya user ke message par reply karein ya User ID dein!'
    )
    return
  try:
    bot.promote_chat_member(
        chat_id,
        target_id,
        can_manage_chat=False,
        can_delete_messages=False,
        can_restrict_members=False,
        can_promote_members=False,
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
    )
    bot.send_message(
        chat_id,
        f'📉 **{target_name}** ke admin rights hata diye gaye hain.',
        parse_mode='Markdown',
    )
  except Exception as e:
    bot.send_message(chat_id, f'❌ Error: {str(e)}')


@bot.message_handler(commands=['ban'])
def ban_user(message):
  chat_id = message.chat.id
  if not is_user_admin(chat_id, message.from_user.id):
    return
  target_id, target_name = get_target_from_message(message)
  if not target_id:
    bot.send_message(
        chat_id, '⚠️ Kripya user ke message par reply karein ya User ID dein!'
    )
    return
  try:
    bot.ban_chat_member(chat_id, target_id)
    bot.send_message(
        chat_id,
        f'🔨 **{target_name}** ko ban kar diya gaya hai!',
        parse_mode='Markdown',
    )
  except Exception as e:
    bot.send_message(chat_id, f'❌ Error: {str(e)}')


@bot.message_handler(commands=['unban'])
def unban_user(message):
  chat_id = message.chat.id
  if not is_user_admin(chat_id, message.from_user.id):
    return
  parts = message.text.split(maxsplit=1)
  if len(parts) < 2:
    bot.send_message(
        chat_id, '⚠️ User ID dein: `/unban [user_id]`', parse_mode='Markdown'
    )
    return
  try:
    bot.unban_chat_member(chat_id, int(parts[1]), only_if_banned=True)
    bot.send_message(
        chat_id,
        f'🔓 User ID `{parts[1]}` ko unban kar diya gaya hai.',
        parse_mode='Markdown',
    )
  except Exception as e:
    bot.send_message(chat_id, f'❌ Error: {str(e)}')


@bot.message_handler(commands=['kick'])
def kick_user(message):
  chat_id = message.chat.id
  if not is_user_admin(chat_id, message.from_user.id):
    return
  target_id, target_name = get_target_from_message(message)
  if not target_id:
    bot.send_message(
        chat_id, '⚠️ Kripya user ke message par reply karein ya User ID dein!'
    )
    return
  try:
    bot.ban_chat_member(chat_id, target_id)
    bot.unban_chat_member(chat_id, target_id)
    bot.send_message(
        chat_id,
        f'👢 **{target_name}** ko kick kar diya gaya hai!',
        parse_mode='Markdown',
    )
  except Exception as e:
    bot.send_message(chat_id, f'❌ Error: {str(e)}')


@bot.message_handler(commands=['mute'])
def mute_user(message):
  chat_id = message.chat.id
  if not is_user_admin(chat_id, message.from_user.id):
    return
  target_id, target_name = get_target_from_message(message)
  if not target_id:
    bot.send_message(
        chat_id, '⚠️ Kripya user ke message par reply karein ya User ID dein!'
    )
    return
  try:
    bot.restrict_chat_member(chat_id, target_id, can_send_messages=False)
    bot.send_message(
        chat_id,
        f'🔇 **{target_name}** ko mute kar diya gaya hai!',
        parse_mode='Markdown',
    )
  except Exception as e:
    bot.send_message(chat_id, f'❌ Error: {str(e)}')


@bot.message_handler(commands=['unmute'])
def unmute_user(message):
  chat_id = message.chat.id
  if not is_user_admin(chat_id, message.from_user.id):
    return
  target_id, target_name = get_target_from_message(message)
  if not target_id:
    bot.send_message(
        chat_id, '⚠️ Kripya user ke message par reply karein ya User ID dein!'
    )
    return
  try:
    bot.restrict_chat_member(
        chat_id,
        target_id,
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )
    bot.send_message(
        chat_id,
        f'🔊 **{target_name}** ko unmute kar diya gaya hai!',
        parse_mode='Markdown',
    )
  except Exception as e:
    bot.send_message(chat_id, f'❌ Error: {str(e)}')


@bot.message_handler(commands=['warn'])
def warn_user(message):
  chat_id = message.chat.id
  if not is_user_admin(chat_id, message.from_user.id):
    return
  target_id, target_name = get_target_from_message(message)
  if not target_id:
    bot.send_message(
        chat_id, '⚠️ Kripya user ke message par reply karein ya User ID dein!'
    )
    return

  if chat_id not in user_warnings:
    user_warnings[chat_id] = {}
  user_warnings[chat_id][target_id] = (
      user_warnings[chat_id].get(target_id, 0) + 1
  )
  warns = user_warnings[chat_id][target_id]

  bot.send_message(
      chat_id,
      f'⚠️ **{target_name}** ko warn kiya gaya hai! (Warnings: {warns}/3)',
      parse_mode='Markdown',
  )
  if warns >= 3:
    try:
      bot.ban_chat_member(chat_id, target_id)
      bot.send_message(
          chat_id,
          f'🚨 **{target_name}** ne 3 warnings poori kar li hain, isliye ban'
          ' kar diya gaya hai!',
      )
    except:
      pass


@bot.message_handler(commands=['unwarn'])
def unwarn_user(message):
  chat_id = message.chat.id
  if not is_user_admin(chat_id, message.from_user.id):
    return
  target_id, target_name = get_target_from_message(message)
  if not target_id:
    bot.send_message(chat_id, '⚠️ Kripya user ke message par reply karein!')
    return
  if chat_id in user_warnings and target_id in user_warnings[chat_id]:
    user_warnings[chat_id][target_id] = max(
        0, user_warnings[chat_id][target_id] - 1
    )
    bot.send_message(
        chat_id,
        f'🔄 **{target_name}** ki ek warning hata di gayi hai. (Total:'
        f' {user_warnings[chat_id][target_id]}/3)',
        parse_mode='Markdown',
    )
  else:
    bot.send_message(chat_id, f'✅ **{target_name}** par koi warning nahi hai.')


@bot.message_handler(commands=['pin'])
def pin_message(message):
  chat_id = message.chat.id
  if not is_user_admin(chat_id, message.from_user.id):
    return
  if not message.reply_to_message:
    bot.send_message(
        chat_id, '⚠️ Kripya pin karne ke liye message par reply karein!'
    )
    return
  try:
    bot.pin_chat_message(chat_id, message.reply_to_message.message_id)
    bot.send_message(chat_id, '📌 Message successfully pin kar diya gaya hai!')
  except Exception as e:
    bot.send_message(chat_id, f'❌ Error: {str(e)}')


@bot.message_handler(commands=['unpin'])
def unpin_message(message):
  chat_id = message.chat.id
  if not is_user_admin(chat_id, message.from_user.id):
    return
  try:
    bot.unpin_chat_message(chat_id)
    bot.send_message(chat_id, '📌 Message successfully unpin kar diya gaya hai!')
  except Exception as e:
    bot.send_message(chat_id, f'❌ Error: {str(e)}')


@bot.message_handler(commands=['afk'])
def set_afk(message):
  chat_id = message.chat.id
  user_id = message.from_user.id
  user_name = message.from_user.first_name
  if chat_id not in afk_users:
    afk_users[chat_id] = {}
  if user_id in afk_users[chat_id]:
    bot.send_message(chat_id, f'Hey {user_name}, you are already AFK.')
    return
  parts = message.text.split(maxsplit=1) if message.text else []
  reason = parts[1] if len(parts) > 1 else 'Away from keyboard'
  afk_users[chat_id][user_id] = {
      'name': user_name,
      'username': message.from_user.username,
      'reason': reason,
      'time': time.time(),
  }
  bot.send_message(
      chat_id,
      f'✅ **AFK Enabled** for {user_name}.\n📝 Reason: {reason}',
      parse_mode='Markdown',
  )


@bot.message_handler(commands=['ship'])
def ship_users(message):
  chat_id = message.chat.id
  if chat_id not in user_scores or len(user_scores[chat_id]) < 2:
    bot.send_message(chat_id, '⚠️ Kam se kam 2 active users hone chahiye!')
    return

  current_time = time.time()
  # Check if a ship pair already exists and is less than 24 hours old
  if chat_id in group_ships:
    ship_data = group_ships[chat_id]
    if current_time - ship_data['time'] < 86400:  # 86400 seconds = 24 hours
      u1 = ship_data['u1']
      u2 = ship_data['u2']
      pct = ship_data['pct']
      bot.send_message(
          chat_id,
          f'💘 **Matchmaking (Locked for 24h):**\n✨ {u1} ❤️ {u2}\n📊 Compatibility:'
          f' **{pct}%**',
          parse_mode='Markdown',
      )
      return

  # Generate new ship pair
  users = list(user_scores[chat_id].values())
  u1_obj, u2_obj = random.choice(users), random.choice(users)
  while u1_obj == u2_obj and len(users) > 1:
    u2_obj = random.choice(users)

  u1 = u1_obj['name']
  u2 = u2_obj['name']
  pct = random.randint(30, 100)

  # Save to cache with current timestamp
  group_ships[chat_id] = {'u1': u1, 'u2': u2, 'pct': pct, 'time': current_time}

  bot.send_message(
      chat_id,
      f'💘 **Matchmaking:**\n✨ {u1} ❤️ {u2}\n📊 Compatibility: **{pct}%**',
      parse_mode='Markdown',
  )


@bot.message_handler(commands=['mmf'])
def mmf_command(message):
  chat_id = message.chat.id
  if not message.reply_to_message:
    bot.send_message(
        chat_id,
        '⚠️ Kripya photo ya sticker par reply karke `/mmf text` likhein!',
    )
    return
  r = message.reply_to_message
  file_id = (
      r.photo[-1].file_id
      if r.photo
      else (
          r.sticker.file_id
          if (r.sticker and not r.sticker.is_animated and not r.sticker.is_video)
          else None
      )
  )
  if not file_id:
    bot.send_message(chat_id, '⚠️ Supported media nahi hai!')
    return

  text_parts = message.text.split(maxsplit=1) if message.text else []
  meme_text = text_parts[1].strip().upper() if len(text_parts) > 1 else 'MEME'

  try:
    file_info = bot.get_file(file_id)
    img = Image.open(io.BytesIO(bot.download_file(file_info.file_path))).convert(
        'RGBA'
    )
    canvas = Image.new('RGBA', (512, 512), (0, 0, 0, 0))

    img.thumbnail((512, 420))
    w, h = img.size
    canvas.paste(img, ((512 - w) // 2, 60), img)

    draw = ImageDraw.Draw(canvas)
    try:
      font = ImageFont.truetype('arial.ttf', 75)
    except:
      font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), meme_text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (512 - tw) / 2
    y = 5

    for ax in range(-4, 5):
      for ay in range(-4, 5):
        draw.text((x + ax, y + ay), meme_text, font=font, fill='black')
    draw.text((x, y), meme_text, font=font, fill='white')

    output = io.BytesIO()
    canvas.save(output, format='WEBP')
    output.seek(0)
    bot.send_sticker(chat_id, output)
    bot.send_message(chat_id, '✅ Meme sticker created.')
  except Exception as e:
    bot.send_message(chat_id, f'❌ Error: {str(e)}')


@bot.message_handler(commands=['kang'])
def save_sticker_command(message):
  chat_id = message.chat.id
  if not message.reply_to_message or not message.reply_to_message.sticker:
    bot.send_message(
        chat_id, '⚠️ Kripya kisi sticker par reply karke `/kang` likhein!'
    )
    return

  sticker = message.reply_to_message.sticker
  try:
    if sticker.is_video or sticker.is_animated:
      bot.send_video(chat_id, sticker.file_id, caption='🎥 Saved Video!')
    else:
      file_info = bot.get_file(sticker.file_id)
      downloaded_file = bot.download_file(file_info.file_path)
      img = Image.open(io.BytesIO(downloaded_file)).convert('RGB')
      output = io.BytesIO()
      img.save(output, format='PNG')
      output.seek(0)
      bot.send_photo(chat_id, output, caption='📸 Saved Sticker as Photo!')
  except Exception as e:
    bot.send_message(chat_id, f'❌ Error saving sticker: {str(e)}')


@bot.message_handler(commands=['ranking', 'top'])
def show_ranking(message):
  chat_id = message.chat.id
  if chat_id in user_scores and user_scores[chat_id]:
    sorted_users = sorted(
        user_scores[chat_id].values(), key=lambda x: x['count'], reverse=True
    )
    text = '🏆 **Global Ranking:**\n\n'
    for i, u in enumerate(sorted_users[:10], 1):
      text += (
          f"{'🥇' if i==1 else '🥈' if i==2 else '🥉' if i==3 else f'{i}.'}"
          f" {u['name']} — {u['count']} msgs\n"
      )
  else:
    text = 'No message data yet!'
  bot.send_message(message.chat.id, text, parse_mode='Markdown')


@bot.message_handler(commands=['mystats', 'stats'])
def show_mystats(message):
  chat_id, user_id = message.chat.id, message.from_user.id
  if chat_id in user_scores and user_id in user_scores[chat_id]:
    u = user_scores[chat_id][user_id]
    rank = sorted(
        user_scores[chat_id].items(), key=lambda x: x[1]['count'], reverse=True
    )
    user_rank = next(
        (i for i, (uid, _) in enumerate(ra
