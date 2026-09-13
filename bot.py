import os
import asyncio
import tempfile
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

COOLDOWN = int(os.getenv("COOLDOWN", "30"))
MAX_RUNNING = int(os.getenv("MAX_RUNNING", "2"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not configured")

app = FastAPI()
telegram_app = None
semaphore = asyncio.Semaphore(MAX_RUNNING)

user_last_search = {}
running_count = 0


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔎 枪宴 海外用户名查询机器人\n\n"
        "使用方法：\n"
        "/search 用户名\n\n"
        "例如：\n"
        "/search name\n\n"
        "查询结果来自 枪宴社工。"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 使用帮助\n\n"
        "/search 用户名 - 查询用户名\n"
        "/stats - 查看机器人状态\n"
        "/help - 查看帮助"
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 Sherlock Bot\n\n"
        f"当前运行：{running_count}/{MAX_RUNNING}\n"
        f"查询冷却：{COOLDOWN} 秒"
    )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global running_count

    if not update.message:
        return

    user = update.effective_user
    user_id = user.id

    if not context.args:
        await update.message.reply_text(
            "❌ 请提供用户名\n\n"
            "例如：\n"
            "/search sherlock"
        )
        return

    username = context.args[0].strip()

    if len(username) > 100:
        await update.message.reply_text("❌ 用户名不能超过 100 个字符")
        return

    if username.startswith("-"):
        await update.message.reply_text("❌ 无效用户名")
        return

    # 管理员不受冷却限制
    if user_id not in ADMIN_IDS:
        now = asyncio.get_running_loop().time()
        last = user_last_search.get(user_id, 0)

        if now - last < COOLDOWN:
            remaining = int(COOLDOWN - (now - last)) + 1
            await update.message.reply_text(
                f"⏳ 请 {remaining} 秒后再查询。"
            )
            return

        user_last_search[user_id] = now

    if semaphore.locked() and running_count >= MAX_RUNNING:
        await update.message.reply_text(
            "⏳ 当前查询任务较多，请稍后再试。"
        )
        return

    await update.message.reply_text(
        f"🔎 正在查询：`{username}`\n\n"
        "⏳ Sherlock 正在检查各个平台，请稍候……",
        parse_mode="Markdown",
    )

    asyncio.create_task(
        run_search(update, username)
    )


async def run_search(update: Update, username: str):
    global running_count

    async with semaphore:
        running_count += 1

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                output_file = Path(temp_dir) / "result.txt"

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
                    await process.communicate()

                    await update.message.reply_text(
                        "⏰ 查询超时，Sherlock 本次运行超过 5 分钟。"
                    )
                    return

                text = stdout.decode(
                    "utf-8",
                    errors="ignore"
                ).strip()

                if output_file.exists():
                    file_text = output_file.read_text(
                        encoding="utf-8",
                        errors="ignore"
                    ).strip()

                    if file_text:
                        text = file_text

                if not text:
                    text = "没有获得查询结果。"

                header = (
                    f"🔎 查询结果\n"
                    f"👤 用户名：{username}\n\n"
                    f"官方：@injt8\n"
                )

                result = header + text

                if len(result) <= 3900:
                    await update.message.reply_text(result)
                else:
                    result_file = Path(temp_dir) / f"{username}.txt"
                    result_file.write_text(
                        result,
                        encoding="utf-8"
                    )

                    with result_file.open("rb") as f:
                        await update.message.reply_document(
                            document=f,
                            filename=f"{username}.txt",
                            caption=f"🔎 {username} 查询结果",
                        )

        except Exception as e:
            await update.message.reply_text(
                f"❌ 查询失败：\n`{str(e)[:1000]}`",
                parse_mode="Markdown",
            )

        finally:
            running_count -= 1


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Sherlock Telegram Bot"
    }


@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "running": running_count,
        "max_running": MAX_RUNNING,
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    global telegram_app

    if WEBHOOK_SECRET:
        secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        )

        if secret != WEBHOOK_SECRET:
            raise HTTPException(
                status_code=403,
                detail="Invalid secret"
            )

    data = await request.json()
    update = Update.de_json(
        data,
        telegram_app.bot
    )

    await telegram_app.process_update(update)

    return {"ok": True}


@app.on_event("startup")
async def startup():
    global telegram_app

    telegram_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

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

    await telegram_app.initialize()
    await telegram_app.start()

    render_url = os.getenv(
        "RENDER_EXTERNAL_URL",
        ""
    ).rstrip("/")

    if not render_url:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL is missing"
        )

    webhook_url = (
        f"{render_url}/telegram/webhook"
    )

    await telegram_app.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET or None,
        drop_pending_updates=True,
    )


@app.on_event("shutdown")
async def shutdown():
    global telegram_app

    if telegram_app:
        await telegram_app.bot.delete_webhook()
        await telegram_app.stop()
        await telegram_app.shutdown()
