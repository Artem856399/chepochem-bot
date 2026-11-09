import asyncio
from dataclasses import dataclass
import html as _html
import json
import logging
import os
import re
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import quote, urlencode, urljoin, urlparse

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart
from aiohttp import web
import aiosqlite
from dotenv import load_dotenv
import httpx

load_dotenv()

# ============================================================================
#  КОНФИГУРАЦИЯ
# ============================================================================
TOKEN = os.getenv("BOT_TOKEN")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "600"))
PORT = int(os.getenv("PORT", "8080"))
DB_FILE = "bot.db"

SCRAPINGANT_KEY = os.getenv("SCRAPINGANT_KEY", "").strip()
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
PARSER_PROXY = os.getenv("PARSER_PROXY", "").strip()

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
OZON_APP_UA = "ozonapp_android/17.45.0+2277"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("chepochem")

bot = Bot(token=TOKEN)
dp = Dispatcher()


# ============================================================================
#  1. МИКРО-ВЕБ-СЕРВЕР (Для пинга Render)
# ============================================================================
async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="Bot is running!", status=200)


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Веб-сервер запущен на порту {PORT}")


# ============================================================================
#  2. БАЗА ДАННЫХ (aiosqlite)
# ============================================================================
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


# ============================================================================
#  3. МОДУЛЬ ПАРСИНГА (Wildberries, Ozon, General)
# ============================================================================
@dataclass
class ProductInfo:
    url: str
    shop: str
    title: Optional[str] = None
    price: Optional[float] = None
    old_price: Optional[float] = None
    currency: str = "RUB"
    in_stock: Optional[bool] = None
    image: Optional[str] = None
    source: str = ""


def _page_headers(referer: Optional[str] = None) -> Dict[str, str]:
    h = {
        "User-Agent": UA_DESKTOP,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin" if referer else "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }
    if referer:
        h["Referer"] = referer
    return h


def _api_headers(origin: str) -> Dict[str, str]:
    h = _page_headers()
    h.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Origin": origin,
            "Referer": origin + "/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
    )
    for k in ("Upgrade-Insecure-Requests", "Sec-Fetch-User", "Cache-Control"):
        h.pop(k, None)
    return h


def _make_client() -> httpx.AsyncClient:
    kwargs: Dict[str, Any] = dict(
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )
    if PARSER_PROXY:
        try:
            return httpx.AsyncClient(proxy=PARSER_PROXY, **kwargs)
        except TypeError:
            return httpx.AsyncClient(proxies=PARSER_PROXY, **kwargs)
    return httpx.AsyncClient(**kwargs)


_BLOCK_MARKERS = (
    "antibot",
    "captcha",
    "доступ ограничен",
    "access denied",
    "are you a robot",
    "just a moment",
    "challenge-platform",
    "/abt/",
    "подтвердите, что вы не робот",
)


def _looks_blocked(resp: httpx.Response) -> bool:
    if resp.status_code in (403, 429, 451, 498):
        return True
    if len(resp.text) < 30000:
        low = resp.text[:4000].lower()
        return any(m in low for m in _BLOCK_MARKERS)
    return False


async def _request(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: Dict[str, str],
    stage: str,
    params: Optional[dict] = None,
    retries: int = 2,
    follow: bool = True,
    timeout: Optional[float] = None,
) -> Optional[httpx.Response]:
    short = url if len(url) < 140 else url[:140] + "…"
    for attempt in range(retries + 1):
        try:
            r = await client.get(
                url,
                headers=headers,
                params=params,
                follow_redirects=follow,
                timeout=timeout if timeout else httpx.USE_CLIENT_DEFAULT,
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            logger.warning(
                "[%s] сетевая ошибка (%d/%d) %s: %r",
                stage,
                attempt + 1,
                retries + 1,
                short,
                e,
            )
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            return None

        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            await asyncio.sleep(2 * (attempt + 1))
            continue

        return r
    return None


async def _resolve_redirects(
    client: httpx.AsyncClient, url: str, max_hops: int = 6
) -> str:
    for hop in range(max_hops):
        try:
            r = await client.get(
                url, headers=_page_headers(), follow_redirects=False
            )
        except Exception:
            break
        loc = r.headers.get("location")
        if r.status_code in (301, 302, 303, 307, 308) and loc:
            url = urljoin(url, loc)
            continue
        break
    return url


def _to_price(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    s = re.sub(r"[^\d,.]", "", str(v))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        f = float(s)
        return f if f > 0 else None
    except ValueError:
        return None


def _json_from_text(text: str) -> Optional[dict]:
    t = text.strip()
    for candidate in (t, _html.unescape(t)):
        try:
            d = json.loads(candidate)
            return d if isinstance(d, dict) else None
        except ValueError:
            pass
        i, j = candidate.find("{"), candidate.rfind("}")
        if i != -1 and j > i:
            try:
                d = json.loads(candidate[i : j + 1])
                return d if isinstance(d, dict) else None
            except ValueError:
                pass
    return None


async def _gateway_get(
    client: httpx.AsyncClient, target: str, render: bool, stage: str
) -> Optional[str]:
    if SCRAPINGANT_KEY:
        r = await _request(
            client,
            "https://api.scrapingant.com/v2/general",
            params={
                "url": target,
                "x-api-key": SCRAPINGANT_KEY,
                "browser": "true" if render else "false",
                "proxy_country": "RU",
            },
            headers={"Accept": "*/*"},
            stage=stage + "/scrapingant",
            retries=1,
            timeout=75.0,
        )
    elif SCRAPERAPI_KEY:
        r = await _request(
            client,
            "https://api.scraperapi.com/",
            params={
                "api_key": SCRAPERAPI_KEY,
                "url": target,
                "country_code": "ru",
                "render": "true" if render else "false",
            },
            headers={"Accept": "*/*"},
            stage=stage + "/scraperapi",
            retries=1,
            timeout=75.0,
        )
    else:
        return None
    if r is None or r.status_code != 200:
        return None
    return r.text


# --- Универсальный разбор JSON-LD / Meta ---
_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)


def _iter_nodes(node: Any) -> Iterator[dict]:
    if isinstance(node, list):
        for x in node:
            yield from _iter_nodes(x)
    elif isinstance(node, dict):
        yield node
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from _iter_nodes(v)


def _product_from_ld(ld_objects: List[Any]) -> Optional[dict]:
    for obj in ld_objects:
        for n in _iter_nodes(obj):
            t = n.get("@type")
            if t != "Product" and (not isinstance(t, list) or "Product" not in t):
                continue
            offers = n.get("offers")
            offer_list = (
                offers
                if isinstance(offers, list)
                else [offers]
                if isinstance(offers, dict)
                else []
            )
            price = None
            currency = None
            for o in offer_list:
                if not isinstance(o, dict):
                    continue
                spec = o.get("priceSpecification")
                if isinstance(spec, list) and spec:
                    spec = spec[0]
                p = (
                    _to_price(o.get("price"))
                    or _to_price(o.get("lowPrice"))
                    or (
                        _to_price(spec.get("price"))
                        if isinstance(spec, dict)
                        else None
                    )
                )
                if p and (price is None or p < price):
                    price = p
                    currency = o.get("priceCurrency") or currency
            return {
                "title": n.get("name"),
                "price": price,
                "currency": currency or "RUB",
            }
    return None


def _meta(html: str, key: str) -> Optional[str]:
    k = re.escape(key)
    for pat in (
        rf'<meta[^>]+(?:property|name|itemprop)=["\']{k}["\'][^>]*content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name|itemprop)=["\']{k}["\']',
    ):
        m = re.search(pat, html, re.I)
        if m:
            return _html.unescape(m.group(1)).strip()
    return None


def _parse_generic_html(html: str, url: str, shop: str) -> Optional[ProductInfo]:
    ld_objects = []
    for m in _LD_RE.finditer(html):
        try:
            ld_objects.append(json.loads(_html.unescape(m.group(1).strip())))
        except ValueError:
            continue
    data = _product_from_ld(ld_objects) or {}
    title = data.get("title") or _meta(html, "og:title")
    price = data.get("price")
    currency = data.get("currency") or "RUB"

    if price is None:
        for key in (
            "product:price:amount",
            "og:price:amount",
            "product:sale_price:amount",
            "price",
        ):
            price = _to_price(_meta(html, key))
            if price:
                break

    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        title = _html.unescape(m.group(1)).strip() if m else None

    if price is None:
        return None

    return ProductInfo(
        url=url,
        shop=shop,
        title=title,
        price=price,
        currency=currency,
        source="generic-html",
    )


# --- Парсер Wildberries ---
_WB_ID_PATTERNS = (
    r"/catalog/(\d{4,12})",
    r"/product/(\d{4,12})",
    r"[?&](?:card|nm|nmId|sku|id)=(\d{4,12})",
)
_WB_ENDPOINTS = (
    "https://card.wb.ru/cards/v4/detail",
    "https://card.wb.ru/cards/v2/detail",
    "https://card.wb.ru/cards/v1/detail",
    "https://card.wb.ru/cards/detail",
)


def _wb_extract_id(url: str) -> Optional[str]:
    for p in _WB_ID_PATTERNS:
        m = re.search(p, url)
        if m:
            return m.group(1)
    m = re.search(r"(?<!\d)(\d{6,12})(?!\d)", urlparse(url).path)
    return m.group(1) if m else None


def _wb_parse_product(p: dict, url: str) -> ProductInfo:
    price = old = None
    for s in p.get("sizes") or []:
        pr = s.get("price") or {}
        if pr.get("product"):
            price = pr["product"] / 100
            old = (pr.get("basic") or 0) / 100 or None
            break
    if price is None and p.get("salePriceU"):
        price = p["salePriceU"] / 100
        old = (p.get("priceU") or 0) / 100 or None

    brand = p.get("brand") or ""
    name = p.get("name") or ""
    title = f"{brand} / {name}" if brand and name else name or "Товар Wildberries"
    return ProductInfo(
        url=url,
        shop="Wildberries",
        title=title,
        price=price,
        old_price=old,
        source="wb-api",
    )


async def _fetch_wb(client: httpx.AsyncClient, url: str) -> Optional[ProductInfo]:
    nm = _wb_extract_id(url)
    if not nm:
        url = await _resolve_redirects(client, url)
        nm = _wb_extract_id(url)
    if not nm:
        return None

    canonical = f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"
    params = {
        "appType": "1",
        "curr": "rub",
        "dest": "-1257786",
        "spp": "30",
        "lang": "ru",
        "locale": "ru",
        "nm": nm,
    }

    for ep in _WB_ENDPOINTS:
        r = await _request(
            client,
            ep,
            params=params,
            headers=_api_headers("https://www.wildberries.ru"),
            stage="wb-api",
            retries=1,
        )
        if r is None or r.status_code != 200:
            continue
        data = _json_from_text(r.text)
        products = (
            ((data or {}).get("data") or data or {}).get("products")
            if data
            else None
        )
        if products:
            return _wb_parse_product(products[0], canonical)

    # Фолбэк через проксирующий шлюз
    if SCRAPINGANT_KEY or SCRAPERAPI_KEY:
        target = f"{_WB_ENDPOINTS[1]}?{urlencode(params)}"
        text = await _gateway_get(client, target, render=False, stage="wb-gw")
        data = _json_from_text(text) if text else None
        products = (
            ((data or {}).get("data") or {}).get("products") if data else None
        )
        if products:
            info = _wb_parse_product(products[0], canonical)
            info.source = "wb-api+gateway"
            return info
    return None


# --- Парсер Ozon ---
def _ozon_extract_id(url: str) -> Optional[str]:
    for p in (
        r"/product/(?:[^/?#]*-)?(\d{4,})",
        r"/context/detail/id/(\d+)",
        r"/(?:\w+/)*?(\d{6,})/?(?:[?#]|$)",
    ):
        m = re.search(p, urlparse(url).path)
        if m:
            return m.group(1)
    return None


def _ozon_parse_json(data: dict, url: str) -> Optional[ProductInfo]:
    states = data.get("widgetStates") or {}
    title = price = old = None
    for key, raw in states.items():
        try:
            obj = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if key.startswith("webPrice-"):
            price = _to_price(obj.get("price")) or price
            old = _to_price(obj.get("originalPrice")) or old
        elif key.startswith("webProductHeading-"):
            title = obj.get("title") or title

    if price is None and title is None:
        return None
    return ProductInfo(
        url=url,
        shop="Ozon",
        title=title or "Товар Ozon",
        price=price,
        old_price=old,
        source="ozon-json",
    )


_OZON_STATE_RE = re.compile(
    r'<div[^>]+id=["\']state-(web[A-Za-z]+-[^"\']*)["\'][^>]*?data-state=(["\'])(.*?)\2',
    re.S,
)


def _ozon_parse_html(html: str, url: str) -> Optional[ProductInfo]:
    states = {
        m.group(1): _html.unescape(m.group(3))
        for m in _OZON_STATE_RE.finditer(html)
    }
    if states:
        info = _ozon_parse_json({"widgetStates": states}, url)
        if info:
            return info
    return _parse_generic_html(html, url, "Ozon")


async def _fetch_ozon(
    client: httpx.AsyncClient, url: str
) -> Optional[ProductInfo]:
    pid = _ozon_extract_id(url)
    if not pid or "/t/" in urlparse(url).path:
        url = await _resolve_redirects(client, url)
        pid = _ozon_extract_id(url)
    if not pid:
        return None

    canonical = f"https://www.ozon.ru/product/{pid}/"
    path_q = quote(f"/product/{pid}/", safe="")
    entry = f"https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url={path_q}"
    mobile = f"https://api.ozon.ru/composer-api.bx/page/json/v2?url={path_q}"

    # 1. Запрос через мобильные заголовки Ozon
    app_headers = {
        "User-Agent": OZON_APP_UA,
        "Accept": "application/json",
        "Accept-Language": "ru-RU",
        "x-o3-app-name": "ozonapp_android",
        "x-o3-app-version": "17.45.0(2277)",
        "x-o3-device-type": "mobile",
    }
    r = await _request(
        client, mobile, headers=app_headers, stage="ozon-mobile", retries=0
    )
    if r is not None and r.status_code == 200 and not _looks_blocked(r):
        data = _json_from_text(r.text)
        info = _ozon_parse_json(data, canonical) if data else None
        if info and info.price:
            return info

    # 2. Прямой Web Entrypoint
    web_headers = _api_headers("https://www.ozon.ru")
    web_headers["Referer"] = canonical
    r = await _request(
        client, entry, headers=web_headers, stage="ozon-entrypoint", retries=0
    )
    if r is not None and r.status_code == 200 and not _looks_blocked(r):
        data = _json_from_text(r.text)
        info = _ozon_parse_json(data, canonical) if data else None
        if info and info.price:
            return info

    # 3. Шлюзы (ScrapingAnt / ScraperAPI)
    if SCRAPINGANT_KEY or SCRAPERAPI_KEY:
        for target, render, parser in (
            (entry, False, "json"),
            (canonical, True, "html"),
        ):
            text = await _gateway_get(
                client, target, render=render, stage="ozon-gw-" + parser
            )
            if not text:
                continue
            if parser == "json":
                data = _json_from_text(text)
                info = _ozon_parse_json(data, canonical) if data else None
            else:
                info = (
                    None
                    if any(m in text[:4000].lower() for m in _BLOCK_MARKERS)
                    else _ozon_parse_html(text, canonical)
                )
            if info and info.price:
                info.source += "+gateway"
                return info

    return None


async def _fetch_generic(
    client: httpx.AsyncClient, url: str
) -> Optional[ProductInfo]:
    shop = (urlparse(url).hostname or "shop").replace("www.", "")
    r = await _request(
        client, url, headers=_page_headers(), stage="generic", retries=2
    )
    html = None
    if r is not None and r.status_code == 200 and not _looks_blocked(r):
        html = r.text
    elif r is not None:
        html = await _gateway_get(client, url, render=True, stage="generic-gw")
    if not html:
        return None
    return _parse_generic_html(html, url, shop)


async def fetch_product_info(url: str) -> Optional[ProductInfo]:
    m = re.search(
        r"(https?://\S+|(?:www\.)?(?:wildberries|ozon|wb)\.\S+)", url.strip()
    )
    if not m:
        return None
    url = m.group(1).rstrip(").,;")
    if not url.startswith("http"):
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower()

    try:
        async with _make_client() as client:
            if host == "wb.ru" or host.endswith(".wb.ru") or "wildberries." in host:
                return await _fetch_wb(client, url)
            if "ozon." in host:
                return await _fetch_ozon(client, url)
            return await _fetch_generic(client, url)
    except Exception as e:
        logger.exception(f"Ошибка парсинга {url}: {e}")
        return None


# ============================================================================
#  4. ОБРАБОТЧИКИ TELEGRAM
# ============================================================================
@dp.message(CommandStart())
async def cmd_start(msg: types.Message):
    reply = (
        "👋 Бот ЧёПочём на связи!\n\n"
        "Отправь команду:\n"
        "/track <ссылка> <желаемая_цена>\n\n"
        "Команды:\n"
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
        await msg.answer("❌ Укажи корректное число для цены.")
        return

    wait_msg = await msg.answer("🔍 Проверяю ссылку...")
    product = await fetch_product_info(url)

    if not product or product.price is None:
        await wait_msg.edit_text(
            "❌ Не удалось считать цену.\n\n"
            "• Для WB: убедись, что товар есть в наличии.\n"
            "• Для Ozon: из-за блокировки иностранного сервера Render напрямую подключиться не удалось. "
            "Потребуется бесплатный ключ ScrapingAnt в настройках."
        )
        return

    title = (
        (product.title[:45] + "...")
        if product.title and len(product.title) > 45
        else (product.title or "Товар")
    )
    track_id = await add_track(
        msg.from_user.id, url, title, target_price, product.price
    )
    text = (
        f"✅ Товар добавлен! (ID: {track_id})\n\n"
        f"📦 {title}\n"
        f"🏬 Магазин: {product.shop}\n"
        f"💵 Текущая: {product.price:,.2f} {product.currency}\n"
        f"🎯 Ждем: {target_price:,.2f} {product.currency}\n\n"
        f"Как цена упадет — сразу пришлю уведомление!"
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

    text = "📋 Твои товары на контроле:\n\n"
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


# ============================================================================
#  5. ЦИКЛ ПЕРИОДИЧЕСКОЙ ПРОВЕРКИ
# ============================================================================
async def monitor_worker():
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL)
            tracks = await get_all_tracks()
            if not tracks:
                continue

            for t in tracks:
                product = await fetch_product_info(t["url"])
                if not product or product.price is None:
                    continue

                new_price = product.price
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
                        logger.error(f"Ошибка отправки сообщения: {err}")

                await asyncio.sleep(2)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Сбой мониторинга: {e}")


# ============================================================================
#  6. ТОЧКА ВХОДА
# ============================================================================
async def main():
    if not TOKEN or ":" not in TOKEN:
        raise ValueError("Токен BOT_TOKEN не задан!")

    await init_db()
    await start_web_server()

    worker_task = asyncio.create_task(monitor_worker())
    logger.info("Бот ЧёПочём успешно запущен!")

    try:
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
