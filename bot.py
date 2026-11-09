import asyncio
import json
import logging
import os
import re
from typing import Optional, Tuple

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart
from aiohttp import web
import aiosqlite
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import httpx

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "600"))
PORT = int(os.getenv("PORT", "8080"))
DB_FILE = "bot.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher()


# 1. МИКРО-СЕРВЕР ДЛЯ RENDER
async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="Bot is alive!", status=200)


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Встроенный веб-сервер слушает порт {PORT}")


# 2. РАБОТА С БАЗОЙ ДАННЫХ (SQLite)
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                target_price REAL NOT NULL,
                last_price REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )
        await db.commit()


async def add_track(
    user_id: int, url: str, title: str, target: float, current: float
) -> int:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            """
            INSERT INTO tracks (user_id, url, title, target_price, last_price)
            VALUES (?, ?, ?, ?, ?)
        """,
            (user_id, url, title, target, current),
        )
        await db.commit()
        return cursor.lastrowid


async def get_user_tracks(user_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM tracks WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ) as cursor:
            return await cursor.fetchall()


async def get_all_tracks():
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tracks") as cursor:
            return await cursor.fetchall()


async def update_track_price(track_id: int, new_price: float):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE tracks SET last_price = ? WHERE id = ?",
            (new_price, track_id),
        )
        await db.commit()


async def delete_track(track_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "DELETE FROM tracks WHERE id = ? AND user_id = ?",
            (track_id, user_id),
        )
        await db.commit()
        return cursor.rowcount > 0


# 3. ПАРСЕР ЦЕН
def _clean_price(raw_text: str) -> Optional[float]:
    clean = re.sub(r"[^\d.,]", "", raw_text.replace(" ", "")).replace(",", ".")
    if clean.count(".") > 1:
        parts = clean.split(".")
        clean = "".join(parts[:-1]) + "." + parts[-1]
    try:
        val = float(clean)
        return val if val > 0 else None
    except ValueError:
        return None


async def fetch_product_info(url: str) -> Tuple[Optional[str], Optional[float]]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=12.0, follow_redirects=True
        ) as client:
            res = await client.get(url)
            if res.status_code != 200:
                return None, None
            html = res.text
    except Exception as e:
        log.warning(f"Ошибка запроса к {url}: {e}")
        return None, None

    soup = BeautifulSoup(html, "html.parser")

    title = None
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    elif soup.title and soup.title.string:
        title = soup.title.string.strip()
    title = (title[:55] + "...") if title and len(title) > 55 else (title or "Товар")

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                offers = item.get("offers")
                if isinstance(offers, dict) and "price" in offers:
                    p = _clean_price(str(offers["price"]))
                    if p:
                        return title, p
                elif isinstance(offers, list) and offers:
                    p = _clean_price(str(offers[0].get("price", "")))
                    if p:
                        return title, p
        except Exception:
            continue

    og_price = soup.find(
        "meta", property=re.compile(r"price:amount$", re.IGNORECASE)
    )
    if og_price and og_price.get("content"):
        price = _clean_price(og_price["content"])
        if price:
            return title, price

    price_tag = soup.find(
        attrs={"class": re.compile(r"(price|cost|val__price)", re.IGNORECASE)}
    )
    if price_tag:
        price = _clean_price(price_tag.get_text())
        if price:
            return title, price

    return title, None


# 4. ОБРАБОТЧИКИ TELEGRAM
@dp.message(CommandStart())
async def cmd_start(msg: types.Message):
    await msg.answer(
        "👋 **Бот ЧёПочём на связи!**\n\n"
        "Отправь мне команду для отслеживания:\n"
        "`/track <ссылка> <желаемая_цена>`\n\n"
        "**Команды:**\n"
        "• `/list` — список товаров\n"
        "• `/del <ID>` — удалить товар\n\n"
        "💡 *Пример:*\n"
        "`/track https://example.com/item 4500`",
        parse_mode="Markdown",
    )


@dp.message(Command("track"))
async def cmd_track(msg: types.Message):
    parts = msg.text.strip().split()
    if len(parts) < 3:
        await msg.answer(
            "⚠️ Формат: `/track <ссылка> <желаемая_цена>`\n"
            "Пример: `/track https://site.ru/item 15000`",
            parse_mode="Markdown",
        )
        return

    url = parts[1]
    raw_target = parts[2]

    try:
        target_price = float(raw_target.replace(",", "."))
        if target_price <= 0:
            raise ValueError
    except ValueError:
        await msg.answer("❌ Укажи число для цены.")
        return

    wait_msg = await msg.answer("🔍 Считываю страницу и цену...")
    title, current_price = await fetch_product_info(url)

    if current_price is None:
        await wait_msg.edit_text(
            "❌ Не удалось считать цену со страницы. Сайт может требовать вход или блокировать ботов."
        )
        return

    track_id = await add_track(
        msg.from_user.id, url, title, target_price, current_price
    )
    await wait_msg.edit_text(
        f"✅ **Товар добавлен! (ID: {track_id})**\n\n"
        f"📦 **{title}
