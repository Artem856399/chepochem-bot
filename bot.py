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


# 1. СЕРВЕР ДЛЯ RENDER
async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Веб-сервер запущен на порту {PORT}")


# 2. БАЗА ДАННЫХ
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


# 3. СПЕЦИАЛЬНЫЙ ПАРСЕР WILDBERRIES (Через прямое API)
async def fetch_wb_info(url: str) -> Tuple[Optional[str], Optional[float]]:
    # Вытаскиваем артикул цифрами из ссылки
    match = re.search(r"/catalog/(\d+)/", url) or re.search(r"nm=(\d+)", url)
    if not match:
        match = re.search(r"/(\d{6,})/", url)
    if not match:
        return None, None

    article = match.group(1)
    api_url = f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&spp=30&nm={article}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "*/*",
    }

    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=10.0, follow_redirects=True
        ) as client:
            res = await client.get(api_url)
            if res.status_code != 200:
                return None, None

            data = res.json()
            products = data.get("data", {}).get("products", [])
            if not products:
                return None, None

            prod = products[0]
            title = prod.get("name") or f"Товар WB {article}"

            # В API Wildberries цена хранится в копейках (де Fond на 100)
            price = None
            sizes = prod.get("sizes", [])
            if sizes and isinstance(sizes[0], dict):
                p_obj = sizes[0].get("price", {})
                if isinstance(p_obj, dict) and "total" in p_obj:
                    price = p_obj["total"] / 100.0

            if price is None and "salePriceU" in prod:
                price = prod["salePriceU"] / 100.0
            elif price is None and "priceU" in prod:
                price = prod["priceU"] / 100.0

            return title, price
    except Exception as e:
        log.warning(f"Ошибка API WB: {e}")
        return None, None


# 4. ОБЩИЙ ПАРСЕР ДЛЯ ОСТАЛЬНЫХ САЙТОВ
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
    # Если это Wildberries — сразу идём по прямому API
    if "wildberries.ru" in url or "wb.ru" in url:
        wb_title, wb_price = await fetch_wb_info(url)
        if wb_price is not None:
            return wb_title, wb_price

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
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
    title = (title[:50] + "...") if title and len(title) > 50 else (title or "Товар")

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


# 5. КОМАНДЫ БОТА
@dp.message(CommandStart())
async def cmd_start(msg: types.Message):
    reply = (
        "👋 Бот ЧёПочём на связи!\n\n"
        "Отправь команду:\n"
        "/track <ссылка> <желаемая_цена>\n\n"
        "Другие команды:\n"
        "• /list — список товаров\n"
        "• /del <ID> — удалить товар\n\n"
        "Пример:\n"
        "/track https://www.wildberries.ru/catalog/178008725/detail.aspx 2000"
    )
    await msg.answer(reply)


@dp.message(Command("track"))
async def cmd_track(msg: types.Message):
    parts = msg.text.strip().split()
    if len(parts) < 3:
        await msg.answer(
            "⚠️ Формат: /track <ссылка> <цена>\nПример: /track https://... 1500"
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

    wait_msg = await msg.answer("🔍 Проверяю ссылку...")
    title, current_price = await fetch_product_info(url)

    if current_price is None:
        await wait_msg.edit_text(
            "❌ Не удалось считать цену. Сайт защищён от парсинга."
        )
        return

    track_id = await add_track(
        msg.from_user.id, url, title, target_price, current_price
    )
    text = (
        f"✅ Товар добавлен! (ID: {track_id})\n\n"
        f"📦 {title}\n"
        f"💵 Текущая: {current_price:,.2f} ₽\n"
        f"🎯 Ждем: {target_price:,.2f} ₽\n\n"
        f"Как цена упадет — сразу пришлю сообщение!"
    )
    await wait_msg.edit_text(text)


@dp.message(Command("list"))
async def cmd_list(msg: types.Message):
    tracks = await get_user_tracks(msg.from_user.id)
    if not tracks:
        await msg.answer(
            "У тебя пока нет товаров в списке. Добавь через /track."
        )
        return

    text = "📋 Твои товары:\n\n"
    for t in tracks:
        text += (
            f"🆔 {t['id']} | {t['title']}\n"
            f"• Текущая: {t['last_price']:,.2f} ₽\n"
            f"• Цель: {t['target_price']:,.2f} ₽\n"
            f"• Ссылка: {t['url']}\n"
            f"• Удалить: /del {t['id']}\n\n"
        )
    await msg.answer(text, disable_web_page_preview=True)


@dp.message(Command("del"))
async def cmd_del(msg: types.Message):
    parts = msg.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.answer("⚠️ Укажи ID товара. Пример: /del 1")
        return

    track_id = int(parts[1])
    success = await delete_track(track_id, msg.from_user.id)
    if success:
        await msg.answer(f"🗑 Товар #{track_id} удален.")
    else:
        await msg.answer(f"❌ Товар #{track_id} не найден.")


# 6. ФОНОВЫЙ МОНИТОРИНГ
async def monitor_worker():
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL)
            tracks = await get_all_tracks()
            if not tracks:
                continue

            for t in tracks:
                _, new_price = await fetch_product_info(t["url"])
                if new_price is None:
                    continue

                if new_price != t["last_price"]:
                    await update_track_price(t["id"], new_price)

                if new_price <= t["target_price"]:
                    try:
                        msg_text = (
                            f"🔥 ЧЁ ПОЧЁМ: ЦЕНА УПАЛА!\n\n"
                            f"📦 {t['title']}\n"
                            f"📉 Новая цена: {new_price:,.2f} ₽ (была: {t['last_price']:,.2f} ₽)\n"
                            f"🎯 Твоя цель: {t['target_price']:,.2f} ₽\n\n"
                            f"Ссылка: {t['url']}"
                        )
                        await bot.send_message(
                            chat_id=t["user_id"], text=msg_text
                        )
                    except Exception as err:
                        log.error(f"Ошибка отправки: {err}")

                await asyncio.sleep(2)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"Сбой мониторинга: {e}")


# 7. СТАРТ
async def main():
    if not TOKEN or ":" not in TOKEN:
        raise ValueError("Токен BOT_TOKEN не задан!")

    await init_db()
    await start_web_server()

    worker_task = asyncio.create_task(monitor_worker())
    log.info("Бот успешно запущен!")

    try:
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
