"""
Telegram video bot - aiogram 3, WEBHOOK mode, ready for Render.
No polling: Telegram pushes updates to  https://<your-service>.onrender.com/webhook/<secret>
The webhook is registered automatically on every start.
All settings come from environment variables (see render.yaml).
"""
import hashlib
import html
import logging
import os
import time

import httpx
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                           MenuButtonDefault, MenuButtonWebApp, Message, ReplyKeyboardMarkup,
                           ReplyParameters, WebAppInfo)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ================= SETTINGS (environment variables) =================
BOT_TOKEN    = os.environ["BOT_TOKEN"]
API_URL      = os.environ["API_URL"]                  # https://shabbir.serv00.net/sex/vid65/api/user_tasks.php
API_KEY      = os.environ["API_KEY"]                  # same as bot_key in config.php
BOT_USERNAME = os.environ.get("BOT_USERNAME", "fuclcrobot")
APP_SHORT    = os.environ.get("APP_SHORT", "app")     # Mini App short name from BotFather /newapp
WEBAPP_URL   = os.environ.get("WEBAPP_URL", "")       # your Blogger page
SHOW_MENU_BUTTON = os.environ.get("SHOW_MENU_BUTTON", "1") == "1"   # "0" removes the blue menu button too
BASE_URL     = (os.environ.get("WEBHOOK_BASE") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET") or hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:40]
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
PORT         = int(os.environ.get("PORT", "8080"))
GROUP_COOLDOWN = int(os.environ.get("GROUP_COOLDOWN", "20"))   # seconds between group replies per user
GET_BTN      = "🎬 Get Video"                         # text of the native button
# =====================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
router = Router()
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
_last_group = {}   # user_id -> last time we replied in a group (anti-spam)
http: httpx.AsyncClient = None
esc = lambda s: html.escape(str(s or ""))


async def api(action: str, **data) -> dict:
    try:
        r = await http.post(API_URL, params={"action": action}, data=data, headers={"X-Bot-Key": API_KEY})
        j = r.json()
        if not j.get("ok"):
            logging.error("API %s -> %s", action, r.text[:400])
        return j
    except Exception as e:
        logging.error("API %s failed: %s", action, e)
        return {"ok": False, "error": "api_down", "message": "Server is busy, try again shortly."}


def fmt_time(sec: int) -> str:
    h, m = sec // 3600, sec % 3600 // 60
    return f"{h}h {m}m" if h else f"{max(m, 1)}m"


def watch_link(token: str) -> str:
    return f"https://t.me/{BOT_USERNAME}/{APP_SHORT}?startapp={token}"


async def register(u, private: bool):
    await api("register", id=u.id, first_name=u.first_name or "", last_name=u.last_name or "",
              username=u.username or "", active=1 if private else 0)


async def deliver(bot: Bot, uid: int, name: str, chat_id: int, chat_type: str,
                  video_id: int = None, reply_to: int = None):
    rp = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
    params = dict(uid=uid, chat_type=chat_type, chat_id=chat_id)
    if video_id:
        params["video_id"] = video_id
    r = await api("get_video", **params)
    try:
        if r.get("ok"):
            v = r["video"]
            cap = (f"🎬 <b>{esc(v['name'])}</b>\n👁 {v['views']} views\n\n"
                   f"🎟 Left today: <b>{r['left']}</b>\n🔒 Only <b>{esc(name)}</b> can open this link.")
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="▶️ Watch video", url=watch_link(r["token"]))]])
            if v.get("image"):
                try:
                    return await bot.send_photo(chat_id, v["image"], caption=cap, reply_markup=kb, reply_parameters=rp)
                except TelegramBadRequest:
                    pass
            return await bot.send_message(chat_id, cap, reply_markup=kb, reply_parameters=rp)

        err = r.get("error")
        if err == "need_tasks":
            rows = [[InlineKeyboardButton(text=("📢 " if t["type"] == "telegram" else "🔗 ") + f"{t['title']} (+{t['reward']})", url=t["url"])]
                    for t in r["tasks"]]
            rows.append([InlineKeyboardButton(text="✅ Verify", callback_data=f"vf:{uid}:{video_id or 0}")])
            await bot.send_message(
                chat_id, "🔒 <b>Daily free limit reached.</b>\nComplete the tasks below to earn bonus videos, then tap <b>Verify</b>.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), reply_parameters=rp)
        elif err == "limit":
            await bot.send_message(chat_id, f"⏳ Daily limit reached. Resets in <b>{fmt_time(r.get('reset_in', 0))}</b>.", reply_parameters=rp)
        elif err == "no_user":
            await bot.send_message(chat_id, "Send /start to me in private chat first.", reply_parameters=rp)
        else:
            await bot.send_message(chat_id, esc(r.get("message") or "Something went wrong."), reply_parameters=rp)
    except TelegramForbiddenError:
        await api("inactive", uid=uid)
    except TelegramAPIError as e:
        logging.warning("send failed: %s", e)


# ---------------- handlers ----------------
@router.message(CommandStart())
async def start(m: Message, command: CommandObject, bot: Bot):
    u = m.from_user
    private = m.chat.type == ChatType.PRIVATE
    await register(u, private)
    if not private:
        return await m.answer("Use /video to get a video.")
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=GET_BTN)]], resize_keyboard=True,
                             is_persistent=True, input_field_placeholder="Tap 🎬 Get Video")
    await m.answer(f"👋 Hi <b>{esc(u.first_name)}</b>!\nTap <b>{GET_BTN}</b> below to receive a video.", reply_markup=kb)
    if command.args == "get":                      # came from the group's "Open inbox" button
        await deliver(bot, u.id, u.first_name, m.chat.id, "private")


@router.message(Command("video", "get"), F.chat.type == ChatType.PRIVATE)
@router.message(F.chat.type == ChatType.PRIVATE, F.text == GET_BTN)
async def get_video_private(m: Message, bot: Bot):
    u = m.from_user
    await register(u, True)
    await deliver(bot, u.id, u.first_name, m.chat.id, "private")


@router.message(Command("video", "get"), F.chat.type.in_(GROUPS))
@router.message(F.chat.type.in_(GROUPS), F.text == GET_BTN)
async def get_video_group(m: Message):
    """In a group: register the user, then point them to the bot inbox (video is sent privately)."""
    u = m.from_user
    if not u or u.is_bot:
        return
    await register(u, False)
    now = time.time()
    if now - _last_group.get(u.id, 0) < GROUP_COOLDOWN:      # silent anti-spam
        return
    _last_group[u.id] = now
    if len(_last_group) > 5000:
        _last_group.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📥 Open inbox", url=f"https://t.me/{BOT_USERNAME}?start=get")]])
    await m.reply(f"✅ <b>{esc(u.first_name)}</b>, you are registered.\nTap <b>Open inbox</b> to get your video 👇", reply_markup=kb)


@router.callback_query(F.data.startswith("vf:"))
async def on_verify(cb: CallbackQuery, bot: Bot):
    _, uid, vid = cb.data.split(":")
    uid, vid = int(uid), int(vid)
    if cb.from_user.id != uid:
        return await cb.answer("This button is not for you.", show_alert=True)
    r = await api("verify", uid=uid)
    if r.get("ok") and r.get("left", 0) > 0:
        await cb.answer("✅ Verified!")
        msg = cb.message
        try:
            await msg.delete()
        except TelegramAPIError:
            pass
        ct = "private" if msg.chat.type == ChatType.PRIVATE else "group"
        await deliver(bot, uid, cb.from_user.first_name, msg.chat.id, ct, vid or None)
    else:
        await cb.answer("❌ Not completed yet. Finish every task, then press Verify.", show_alert=True)


# ---------------- webhook server ----------------
async def on_startup(bot: Bot, dispatcher: Dispatcher):
    global http
    http = httpx.AsyncClient(timeout=30)
    if not BASE_URL:
        raise RuntimeError("Set WEBHOOK_BASE (Render sets RENDER_EXTERNAL_URL automatically for web services).")
    url = BASE_URL + WEBHOOK_PATH
    await bot.set_webhook(url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True,
                          allowed_updates=dispatcher.resolve_used_update_types())
    logging.info("Webhook set -> %s", BASE_URL + "/webhook/***")
    try:
        if SHOW_MENU_BUTTON and WEBAPP_URL:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🎬 Videos", web_app=WebAppInfo(url=WEBAPP_URL)))
        else:
            await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except TelegramAPIError as e:
        logging.warning("menu button: %s", e)


async def on_shutdown(bot: Bot):
    if http:
        await http.aclose()


def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    ok = lambda _: web.Response(text="ok")
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)          # Render health check
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
