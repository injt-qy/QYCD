import os
import asyncio
import tempfile
from pathlib import Path
from collections import defaultdict
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =========================
# 配置
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

COOLDOWN = int(os.getenv("COOLDOWN", "30"))
MAX_RUNNING = int(os.getenv("MAX_RUNNING", "2"))

# Render 自动提供这个变量
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

# 建议在 Render 自己设置
WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    "sherlock-webhook-secret"
)

WEBHOOK_PATH = "/telegram/webhook"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN 未设置")

if not PUBLIC_URL:
    raise RuntimeError(
        "RENDER_EXTERNAL_URL 未设置，请手动设置 PUBLIC_URL"
    )


# =========================
# 状态
# =========================

last_query = defaultdict(lambda: datetime.min)
running_users = set()
running_count = 0


# =========================
# Telegram Application
# =========================

telegram_app = (
    Application.builder()
    .token(BOT_TOKEN)
    .build()
)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# =========================
# /start
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔎 Sherlock TG Bot\n\n"
        "查询公开用户名在社交平台上的匹配情况。\n\n"
        "使用：\n"
        "/search 用户名\n\n"
        "例如：\n"
        "/search test123\n\n"
        "其他：\n"
        "/help"
    )


# =========================
# /help
# =========================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "📖 使用帮助\n\n"
        "/search 用户名\n"
        "查询一个用户名。\n\n"
        "/stats\n"
        "管理员查看运行状态。\n\n"
        "⚠️ 一次只允许查询一个用户名。"
    )


# =========================
# Sherlock 查询
# =========================

async def run_sherlock(
    chat_id: int,
    username: str,
    context: ContextTypes.DEFAULT_TYPE,
):
    global running_count

    running_count += 1

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔎 开始查询\n\n"
                f"👤 用户名：{username}\n\n"
                f"⏳ Sherlock 正在检查，请稍候……"
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:

            output_file = Path(tmpdir) / "result.txt"

            command = [
                "sherlock",
                username,
                "--output",
                str(output_file),
                "--print-found",
            ]

            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            try:
                stdout, _ = await asyncio.wait_for(
                    process.communicate(),
                    timeout=300,
                )
            except asyncio.TimeoutError:
                process.kill()

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⏱ 查询超时\n\n"
                        f"用户名：{username}"
                    ),
                )
                return

            console_output = stdout.decode(
                "utf-8",
                errors="ignore"
            )

            if output_file.exists():
                result = output_file.read_text(
                    encoding="utf-8",
                    errors="ignore"
                )
            else:
                result = console_output

            result = result.strip()

            if not result:
                result = "没有找到结果。"

            header = (
                "🔎 Sherlock 查询完成\n\n"
                f"👤 用户名：{username}\n\n"
            )

            # Telegram 单条消息限制
            if len(header + result) <= 3900:

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=header + result,
                    disable_web_page_preview=True,
                )

            else:

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"✅ 查询完成\n\n"
                        f"👤 用户名：{username}\n"
                        f"📄 完整结果已作为文件发送。"
                    ),
                )

                with output_file.open("rb") as file:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=file,
                        filename=f"{username}_sherlock.txt",
                        caption="📄 Sherlock 完整结果",
                    )

    except Exception as e:

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "❌ 查询失败\n\n"
                f"{type(e).__name__}: "
                f"{str(e)[:500]}"
            ),
        )

    finally:
        running_count -= 1


# =========================
# /search
# =========================

async def search_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not update.message:
        return

    user_id = update.effective_user.id

    if not context.args:

        await update.message.reply_text(
            "❌ 请提供用户名。\n\n"
            "例如：\n"
            "/search test123"
        )
        return

    if len(context.args) != 1:

        await update.message.reply_text(
            "❌ 一次只能查询一个用户名。"
        )
        return

    username = context.args[0].strip()

    # 基本过滤
    if len(username) > 100:

        await update.message.reply_text(
            "❌ 用户名太长。"
        )
        return

    if username.startswith("-"):

        await update.message.reply_text(
            "❌ 无效用户名。"
        )
        return

    # 用户正在查询
    if user_id in running_users:

        await update.message.reply_text(
            "⏳ 你已经有一个查询正在进行。"
        )
        return

    # 全局并发限制
    if running_count >= MAX_RUNNING:

        await update.message.reply_text(
            "⏳ 当前查询人数较多，请稍后再试。"
        )
        return

    # 普通用户限速
    if not is_admin(user_id):

        now = datetime.now()

        elapsed = (
            now - last_query[user_id]
        ).total_seconds()

        if elapsed < COOLDOWN:

            remaining = int(
                COOLDOWN - elapsed
            )

            await update.message.reply_text(
                f"⏳ 查询太频繁，请 {remaining} 秒后再试。"
            )
            return

        last_query[user_id] = now

    running_users.add(user_id)

    # 创建后台任务
    async def worker():

        try:
            await run_sherlock(
                update.effective_chat.id,
                username,
                context,
            )
        finally:
            running_users.discard(user_id)

    asyncio.create_task(worker())

    await update.message.reply_text(
        f"✅ 已加入查询队列\n\n"
        f"👤 用户名：{username}\n\n"
        f"🔎 Sherlock 正在处理……"
    )


# =========================
# /stats
# =========================

async def stats_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    if not is_admin(user_id):

        await update.message.reply_text(
            "❌ 无权限。"
        )
        return

    await update.message.reply_text(
        "👑 Sherlock Bot 状态\n\n"
        f"运行中的查询：{running_count}\n"
        f"最大并发：{MAX_RUNNING}\n"
        f"用户冷却：{COOLDOWN} 秒\n"
        f"管理员：{len(ADMIN_IDS)}"
    )


# =========================
# 注册命令
# =========================

telegram_app.add_handler(
    CommandHandler("start", start)
)

telegram_app.add_handler(
    CommandHandler("help", help_command)
)

telegram_app.add_handler(
    CommandHandler("search", search_command)
)

telegram_app.add_handler(
    CommandHandler("stats", stats_command)
)


# =========================
# FastAPI
# =========================

@asynccontextmanager
async def lifespan(app: FastAPI):

    await telegram_app.initialize()
    await telegram_app.start()

    webhook_url = (
        PUBLIC_URL +
        WEBHOOK_PATH
    )

    await telegram_app.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=[
            "message"
        ],
    )

    print(
        "Telegram webhook:",
        webhook_url
    )

    yield

    await telegram_app.bot.delete_webhook()
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(
    title="Sherlock Telegram Bot",
    lifespan=lifespan,
)


# =========================
# 健康检查
# =========================

@app.get("/")
async def root():

    return {
        "status": "ok",
        "service": "sherlock-tgbot",
    }


@app.get("/health")
async def health():

    return {
        "status": "healthy",
        "running": running_count,
    }


# =========================
# Telegram Webhook
# =========================

@app.post(WEBHOOK_PATH)
async def telegram_webhook(
    request: Request
):

    secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    if secret != WEBHOOK_SECRET:

        raise HTTPException(
            status_code=403,
            detail="Forbidden",
        )

    data = await request.json()

    update = Update.de_json(
        data,
        telegram_app.bot,
    )

    await telegram_app.process_update(
        update
    )

    return JSONResponse(
        {"ok": True}
    )
