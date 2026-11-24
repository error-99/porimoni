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
    ChatMemberUpdated,
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
BOT_TOKEN    = os.getenv("BOT_TOKEN", "8529860367:AAHJf08a27HjKLyCm4oVDD9InqLADXPTTVo")
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
PING_INTERVAL  = int(os.getenv("PING_INTERVAL", "600"))
# ==============================================

GET_BTN = "🎬 Get Video"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
esc = lambda s: html.escape(str(s or ""))

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)
http: httpx.AsyncClient | None = None
prompted_groups: set[int] = set()


def get_video_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=GET_BTN)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=f"Tap {GET_BTN}",
    )


def inbox_link(payload: str = "video") -> str:
    return f"https://t.me/{BOT_USERNAME}?start={payload}"


def inbox_kb(payload: str = "video") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📥 Open Inbox", url=inbox_link(payload))]]
    )


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


async def deliver_to_inbox(
    uid: int,
    name: str,
    video_id: int | None = None,
    source_chat_type: str = "private",
    source_chat_id: int | None = None,
) -> tuple[bool, str]:
    """Runs the exact inbox delivery flow for user `uid`. Returns (delivered_to_dm, status_code)."""
    params = {"uid": uid, "chat_type": source_chat_type, "chat_id": source_chat_id or uid}
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
                    await bot.send_photo(chat_id=uid, photo=v["image"], caption=cap, reply_markup=kb)
                    return True, "ok"
                except TelegramBadRequest:
                    pass
            await bot.send_message(chat_id=uid, text=cap, reply_markup=kb)
            return True, "ok"

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
                chat_id=uid,
                text="🔒 <b>Daily free limit reached.</b>\nComplete the tasks below to earn bonus videos, then tap <b>Verify</b>.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
            )
            return True, "need_tasks"

        if err == "limit":
            await bot.send_message(
                chat_id=uid,
                text=f"⏳ Daily limit reached. Resets in <b>{fmt_time(r.get('reset_in', 0))}</b>.",
            )
            return True, "limit"

        if err == "no_user":
            return False, "no_user"

        await bot.send_message(chat_id=uid, text=r.get("message") or "Something went wrong.")
        return True, err or "error"

    except TelegramForbiddenError:
        await api("inactive", uid=uid)
        return False, "blocked_or_no_dm"
    except TelegramAPIError as e:
        logging.warning("send to inbox failed: %s", e)
        return False, "tg_error"


async def handle_group_video_request(message: Message):
    u = message.from_user
    if not u or u.is_bot:
        return

    await api(
        "register",
        id=u.id,
        first_name=u.first_name or "",
        last_name=u.last_name or "",
        username=u.username or "",
        active=0,
    )

    sent_dm, status = await deliver_to_inbox(
        uid=u.id,
        name=u.first_name or "User",
        source_chat_type="group",
        source_chat_id=message.chat.id,
    )

    link = inbox_link("video")
    if sent_dm:
        text = (
            f"📩 <b>{esc(u.first_name)}</b>, check your inbox!\n"
            f"👉 <a href=\"{link}\">Open Inbox</a>"
        )
    else:
        text = (
            f"📩 <b>{esc(u.first_name)}</b>, I couldn't message your inbox yet. "
            f"Check your inbox and start the bot first:\n👉 <a href=\"{link}\">Open Inbox</a>"
        )

    await message.reply(
        text,
        reply_markup=inbox_kb("video"),
        allow_sending_without_reply=True,
    )


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
        return await message.reply(
            f"Tap <b>{GET_BTN}</b> below to receive a video in your inbox.",
            reply_markup=get_video_kb(),
            allow_sending_without_reply=True,
        )

    await message.reply(
        f"👋 Hi <b>{esc(u.first_name)}</b>!\nTap <b>{GET_BTN}</b> below to receive a video.",
        reply_markup=get_video_kb(),
    )

    # If user came from the group's inbox link (/start video), deliver immediately
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip() == "video":
        await deliver_to_inbox(uid=u.id, name=u.first_name or "User", source_chat_type="private")


@router.message(Command("video", "get"))
@router.message(F.chat.type == ChatType.PRIVATE, F.text == GET_BTN)
async def private_video_handler(message: Message):
    u = message.from_user
    await api(
        "register",
        id=u.id,
        first_name=u.first_name or "",
        last_name=u.last_name or "",
        username=u.username or "",
        active=1,
    )
    sent_dm, status = await deliver_to_inbox(
        uid=u.id,
        name=u.first_name or "User",
        source_chat_type="private",
        source_chat_id=message.chat.id,
    )
    if not sent_dm and status == "no_user":
        await message.reply("Send /start to me in private chat first.")


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text == GET_BTN)
async def group_button_video_handler(message: Message):
    await handle_group_video_request(message)


@router.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated):
    if event.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    new_status = event.new_chat_member.status
    if new_status in {"member", "administrator"}:
        prompted_groups.add(event.chat.id)
        try:
            await bot.send_message(
                chat_id=event.chat.id,
                text=f"🎬 Tap <b>{GET_BTN}</b> below to get a video in your inbox!",
                reply_markup=get_video_kb(),
            )
        except TelegramAPIError as e:
            logging.warning("group button attach failed on join: %s", e)


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def group_auto_attach_handler(message: Message):
    if message.new_chat_members:
        try:
            await message.reply(
                f"🎬 Tap <b>{GET_BTN}</b> below to get a video in your inbox!",
                reply_markup=get_video_kb(),
                allow_sending_without_reply=True,
            )
            prompted_groups.add(message.chat.id)
        except TelegramAPIError:
            pass
        return

    if message.chat.id not in prompted_groups:
        prompted_groups.add(message.chat.id)
        try:
            await message.reply(
                f"🎬 Tap <b>{GET_BTN}</b> below to get a video in your inbox!",
                reply_markup=get_video_kb(),
                allow_sending_without_reply=True,
            )
        except TelegramAPIError:
            pass


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
        await deliver_to_inbox(
            uid=uid,
            name=query.from_user.first_name or "User",
            video_id=vid or None,
            source_chat_type="private",
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
