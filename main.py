import asyncio
import html
import logging
import os
from contextlib import asynccontextmanager

import httpx
import uvicorn
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from fastapi import FastAPI, HTTPException, Request

# ============ CREDENTIALS & CONFIG ============
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
API_URL      = os.getenv("API_URL", "https://shabbir.serv00.net/sex/vid65/api/user_tasks.php")
API_KEY      = os.getenv("API_KEY", "PUT_A_LONG_RANDOM_SECRET")
BOT_USERNAME = os.getenv("BOT_USERNAME", "fuclcrobot")
APP_SHORT    = os.getenv("APP_SHORT", "app")
WEBAPP_URL   = os.getenv("WEBAPP_URL", "https://YOURBLOG.blogspot.com/p/app.html")

# Render automatically injects RENDER_EXTERNAL_URL (e.g. https://your-app.onrender.com)
WEBHOOK_BASE   = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_PATH   = f"/webhook/{BOT_TOKEN.split(':')[0]}"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "wh_sec_982374982374")
PORT           = int(os.getenv("PORT", "10000"))
PING_INTERVAL  = int(os.getenv("PING_INTERVAL", "600"))  # 600s = 10 minutes
# ==============================================

GET_BTN = "🎬 Get Video"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
esc = lambda s: html.escape(str(s or ""))

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)
http: httpx.AsyncClient | None = None


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


async def deliver(uid: int, name: str, chat_id: int, chat_type: str, video_id: int | None = None, reply_to: int | None = None):
    params = {"uid": uid, "chat_type": chat_type, "chat_id": chat_id}
    if video_id:
        params["video_id"] = video_id

    r = await api("get_video", **params)
    try:
        if r.get("ok"):
            v = r["video"]
            cap = (
                f"🎬 <b>{esc(v['name'])}</b>\n"
                f"👁 {v['views']} views\n\n"
                f"🎟 Left today: <b>{r['left']}</b>\n"
                f"🔒 Only <b>{esc(name)}</b> can open this link."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="▶️ Watch video", url=watch_link(r["token"]))]
            ])
            if v.get("image"):
                try:
                    return await bot.send_photo(
                        chat_id=chat_id,
                        photo=v["image"],
                        caption=cap,
                        reply_markup=kb,
                        reply_to_message_id=reply_to,
                        allow_sending_without_reply=True,
                    )
                except TelegramBadRequest:
                    pass
            return await bot.send_message(
                chat_id=chat_id,
                text=cap,
                reply_markup=kb,
                reply_to_message_id=reply_to,
                allow_sending_without_reply=True,
            )

        err = r.get("error")
        if err == "need_tasks":
            rows = [
                [InlineKeyboardButton(
                    text=("📢 " if t["type"] == "telegram" else "🔗 ") + f"{t['title']} (+{t['reward']})",
                    url=t["url"],
                )]
                for t in r["tasks"]
            ]
            rows.append([InlineKeyboardButton(text="✅ Verify", callback_data=f"vf:{uid}:{video_id or 0}")])
            await bot.send_message(
                chat_id=chat_id,
                text="🔒 <b>Daily free limit reached.</b>\nComplete the tasks below to earn bonus videos, then tap <b>Verify</b>.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                reply_to_message_id=reply_to,
                allow_sending_without_reply=True,
            )
        elif err == "limit":
            await bot.send_message(
                chat_id=chat_id,
                text=f"⏳ Daily limit reached. Resets in <b>{fmt_time(r.get('reset_in', 0))}</b>.",
                reply_to_message_id=reply_to,
                allow_sending_without_reply=True,
            )
        elif err == "no_user":
            await bot.send_message(
                chat_id=chat_id,
                text="Send /start to me in private chat first.",
                reply_to_message_id=reply_to,
                allow_sending_without_reply=True,
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=r.get("message") or "Something went wrong.",
                reply_to_message_id=reply_to,
                allow_sending_without_reply=True,
            )
    except TelegramForbiddenError:
        await api("inactive", uid=uid)
    except TelegramAPIError as e:
        logging.warning("send failed: %s", e)


# ---------------- Handlers ----------------
@router.message(CommandStart())
async def start_handler(message: Message):
    u = message.from_user
    private = message.chat.type == ChatType.PRIVATE
    await api(
        "register",
        id=u.id,
        first_name=u.first_name or "",
        last_name=u.last_name or "",
        username=u.username or "",
        active=1 if private else 0,
    )
    if not private:
        return await message.reply("Use /video to get a video.")

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=GET_BTN)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=f"Tap {GET_BTN}",
    )
    await message.reply(
        f"👋 Hi <b>{esc(u.first_name)}</b>!\nTap <b>{GET_BTN}</b> below to receive a video.",
        reply_markup=kb,
    )


@router.message(Command("video", "get"))
@router.message(F.chat.type == ChatType.PRIVATE, F.text == GET_BTN)
async def video_handler(message: Message):
    u = message.from_user
    private = message.chat.type == ChatType.PRIVATE
    await api(
        "register",
        id=u.id,
        first_name=u.first_name or "",
        last_name=u.last_name or "",
        username=u.username or "",
        active=1 if private else 0,
    )
    await deliver(
        uid=u.id,
        name=u.first_name,
        chat_id=message.chat.id,
        chat_type="private" if private else "group",
        reply_to=message.message_id,
    )


@router.callback_query(F.data.startswith("vf:"))
async def verify_handler(query: CallbackQuery):
    _, uid_str, vid_str = query.data.split(":")
    uid, vid = int(uid_str), int(vid_str)
    if query.from_user.id != uid:
        return await query.answer("This button is not for you.", show_alert=True)

    r = await api("verify", uid=uid)
    if r.get("ok") and r.get("left", 0) > 0:
        await query.answer("✅ Verified!")
        try:
            await query.message.delete()
        except TelegramAPIError:
            pass
        ct = "private" if query.message.chat.type == ChatType.PRIVATE else "group"
        await deliver(
            uid=uid,
            name=query.from_user.first_name,
            chat_id=query.message.chat.id,
            chat_type=ct,
            video_id=vid or None,
        )
    else:
        await query.answer("❌ Not completed yet. Finish every task, then press Verify.", show_alert=True)


# ---------------- Self-Ping & FastAPI Lifecycle ----------------
async def self_ping_loop():
    if not WEBHOOK_BASE:
        return
    health_url = f"{WEBHOOK_BASE}/health"
    while True:
        await asyncio.sleep(PING_INTERVAL)
        try:
            r = await http.get(health_url, timeout=15)
            logging.info("Self-ping %s -> %s", health_url, r.status_code)
        except Exception as e:
            logging.warning("Self-ping failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http
    http = httpx.AsyncClient(timeout=30)
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="🎬 Videos", web_app=WebAppInfo(url=WEBAPP_URL))
    )
    ping_task = None
    if WEBHOOK_BASE:
        webhook_url = f"{WEBHOOK_BASE}{WEBHOOK_PATH}"
        await bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True,
            allowed_updates=dp.resolve_used_update_types(),
        )
        logging.info("Webhook set to %s", webhook_url)
        ping_task = asyncio.create_task(self_ping_loop())
    else:
        logging.warning("RENDER_EXTERNAL_URL not set; webhook & self-ping disabled.")

    yield

    if ping_task:
        ping_task.cancel()
    await http.aclose()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
@app.get("/health")
async def health():
    return {"status": "ok", "bot": BOT_USERNAME}


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret token")
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
