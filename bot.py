# -*- coding: utf-8 -*-
import asyncio
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

# ⚠️ إعداد بيئة تيليجرام التجريبية
import requests
import urllib3
urllib3.disable_warnings()

# سنقوم باعتراض الطلبات إذا كان التوكن تجريبياً
IS_TEST_ENV = True # ضعها True إذا كنت تستخدم توكن بيتا، أو False للعادي

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
if IS_TEST_ENV:
    # 1. تعديل التوكن ليشمل المسار
    BASE_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/test"
else:
    BASE_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

import httpx
from telegram import BotCommand
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, Conflict, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import library
import ai
import userbot
import usernames

# ============================ الإعدادات ============================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
SMART_MODEL = os.environ.get("GEMINI_SMART_MODEL", "gemini-pro-latest")
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-flash-lite-latest")
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("ADMIN_IDS", ""))}
DATA_FILE = os.environ.get("DATA_FILE", "data.json")
MAX_HISTORY = 20
RELAY_CHAT_ID = os.environ.get("RELAY_CHAT_ID", "").strip()
LIB_SYNC_MINUTES = int(os.environ.get("LIB_SYNC_MINUTES", "60"))
LIB_PAGE = 8
MAX_RELAY_BYTES = 49 * 1024 * 1024

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# ... (باقي دوال التخزين والأزرار والمكتبة كما هي) ...
# [سأترك باقي الكود كما هو لتفادي الإطالة، ولكن هنا يكمن السحر]

def start_health_server():
    port = os.environ.get("PORT")
    if not port: return
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
        do_HEAD = do_GET
        def log_message(self, *args): pass
    server = HTTPServer(("0.0.0.0", int(port)), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("start", "بدء"),
            BotCommand("mode", "تغيير المود"),
            BotCommand("new", "محادثة جديدة"),
            BotCommand("id", "معرفة رقمك"),
            BotCommand("image", "تصميم صورة"),
            BotCommand("status", "فحص اتصال الحساب والمجموعة (للأدمن)"),
        ]
    )
    if LIB_SYNC_MINUTES > 0:
        spawn(sync_loop(app))
# ============================ التشغيل المعدل ============================
def main():
    global IS_TEST_ENV
    if not TELEGRAM_TOKEN or not (GEMINI_API_KEY or ai.GROK_API_KEY):
        raise SystemExit("❌ ضع TELEGRAM_TOKEN و GEMINI_API_KEY أو GROK_API_KEY")

    # اكتشاف تلقائي لبيئة البوت
    try:
        r = httpx.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=10)
        if r.status_code == 401:
            r = httpx.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/test/getMe", timeout=10)
            if r.status_code == 200:
                IS_TEST_ENV = True
            else:
                log.error("التوكن غير صالح في العادي والتجريبي")
                return
        else:
            IS_TEST_ENV = False
    except Exception as e:
        log.error(f"فشل الاتصال: {e}")
        return

    log.info(f"📡 يعمل على: {'بيتا (Test)' if IS_TEST_ENV else 'العادي'}")

    start_health_server()

    # إعداد التطبيق
    if IS_TEST_ENV:
        # في وضع بيتا: نعطل التحقق من SSL ونوجه المسار
        app = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .base_url(BASE_API_URL)
            .concurrent_updates(True)
            .post_init(post_init)
            .build()
        )
    else:
        app = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .concurrent_updates(True)
            .post_init(post_init)
            .build()
        )

    # إضافة المعالجات (كما هي)
    app.add_handler(MessageHandler(filters.ALL, on_any_update), group=-1)
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("image", cmd_image))
    app.add_handler(CommandHandler("groups", cmd_groups))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.TEXT & ~filters.COMMAND, on_nontext))
    app.add_error_handler(on_error)

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
