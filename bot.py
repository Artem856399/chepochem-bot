# ============================================================================
#  PRICE PARSER  (вставить целиком в bot.py или импортировать)
#  Зависимости: только httpx.   Python 3.9+
#
#  Переменные окружения (все необязательные):
#    SCRAPINGANT_KEY  - ключ ScrapingAnt (бесплатно ~10k кредитов/мес)
#    SCRAPERAPI_KEY   - ключ ScraperAPI (бесплатно ~1000 запросов/мес)
#    PARSER_PROXY     - свой прокси, напр. http://user:pass@host:port (лучше RU)
#
#  Использование:
#      info = await fetch_product_info(url)
#      if info is None: ... (все стратегии провалились, причины в логах)
#      info.price может быть None, если товар нет в наличии.
# ============================================================================
import asyncio
import html as _html
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import quote, urljoin, urlparse

import httpx

logger = logging.getLogger("price_parser")

SCRAPINGANT_KEY = os.getenv("SCRAPINGANT_KEY", "").strip()
SCRAPERAPI_KEY = os.getenv("SCRAPERAPI_KEY", "").strip()
PARSER_PROXY = os.getenv("PARSER_PROXY", "").strip()

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
OZON_APP_UA = "ozonapp_android/17.45.0+2277"


# --------------------------------------------------------------------------
#  Модель результата
# --------------------------------------------------------------------------
@dataclass
class ProductInfo:
    url: str
    shop: str
    title: Optional[str] = None
    price: Optional[float] = None       # текущая цена (со скидкой)
    old_price: Optional[float] = None   # цена до скидки
    currency: str = "RUB"
    in_stock: Optional[bool] = None
    image: Optional[str] = None
    source: str = ""                    # какая стратегия сработала

    def __str__(self) -> str:
        p = f"{self.price:,.0f}".replace(",", " ") if self.price is not None else "нет в наличии"
        return f"[{self.shop}] {self.title or '?'} — {p} {self.currency if self.price else ''}".strip()


# --------------------------------------------------------------------------
#  Заголовки и клиент
# --------------------------------------------------------------------------
def _page_headers(referer: Optional[str] = None) -> Dict[str, str]:
    h = {
        "User-Agent": UA_DESKTOP,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate",  # без br: нужен brotli
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
    h.update({
        "Accept": "application/json, text/plain, */*",
        "Origin": origin,
        "Referer": origin + "/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    })
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
            return httpx.AsyncClient(proxy=PARSER_PROXY, **kwargs)      # httpx >= 0.26
        except TypeError:
            return httpx.AsyncClient(proxies=PARSER_PROXY, **kwargs)    # старые версии
    return httpx.AsyncClient(**kwargs)


# --------------------------------------------------------------------------
#  Низкоуровневые помощники
# --------------------------------------------------------------------------
_BLOCK_MARKERS = (
    "antibot", "captcha", "доступ ограничен", "access denied", "are you a robot",
    "just a moment", "challenge-platform", "/abt/", "подтвердите, что вы не робот",
)


def _looks_blocked(resp: httpx.Response) -> bool:
    if resp.status_code in (403, 429, 451, 498):
        return True
    if len(resp.text) < 30000:
        low = resp.text[:4000].lower()
        return any(m in low for m in _BLOCK_MARKERS)
    return False


async def _request(
    client: httpx.AsyncClient, url: str, *, headers: Dict[str, str], stage: str,
    params: Optional[dict] = None, retries: int = 2, follow: bool = True,
    timeout: Optional[float] = None,
) -> Optional[httpx.Response]:
    short = url if len(url) < 140 else url[:140] + "…"
    for attempt in range(retries + 1):
        try:
            r = await client.get(
                url, headers=headers, params=params, follow_redirects=follow,
                timeout=timeout if timeout else httpx.USE_CLIENT_DEFAULT,
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            logger.warning("[%s] сетевая ошибка (%d/%d) %s: %r", stage, attempt + 1, retries + 1, short, e)
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            return None
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            logger.warning("[%s] HTTP %s, повтор %d/%d: %s", stage, r.status_code, attempt + 1, retries, short)
            await asyncio.sleep(2 * (attempt + 1))
            continue
        level = logging.INFO if r.status_code == 200 else logging.WARNING
        logger.log(level, "[%s] HTTP %s (%d байт) %s", stage, r.status_code, len(r.content), short)
        return r
    return None


async def _resolve_redirects(client: httpx.AsyncClient, url: str, max_hops: int = 6) -> str:
    """Ручное раскрытие коротких ссылок: не грузим конечную (защищённую) страницу."""
    for hop in range(max_hops):
        try:
            r = await client.get(url, headers=_page_headers(), follow_redirects=False)
        except Exception as e:
            logger.warning("[resolve] hop %d ошибка %r для %s", hop, e, url)
            break
        loc = r.headers.get("location")
        if r.status_code in (301, 302, 303, 307, 308) and loc:
            url = urljoin(url, loc)
            logger.info("[resolve] hop %d -> %s", hop + 1, url)
            continue
        logger.info("[resolve] финал: HTTP %s %s", r.status_code, url)
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
    """JSON напрямую, либо из обёртки (<pre>…</pre> от браузерных шлюзов)."""
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
                d = json.loads(candidate[i:j + 1])
                return d if isinstance(d, dict) else None
            except ValueError:
                pass
    return None


async def _gateway_get(client: httpx.AsyncClient, target: str, render: bool, stage: str) -> Optional[str]:
    """Запрос через бесплатный проксирующий API (если задан ключ)."""
    if SCRAPINGANT_KEY:
        r = await _request(
            client, "https://api.scrapingant.com/v2/general",
            params={"url": target, "x-api-key": SCRAPINGANT_KEY,
                    "browser": "true" if render else "false", "proxy_country": "RU"},
            headers={"Accept": "*/*"}, stage=stage + "/scrapingant", retries=1, timeout=75.0,
        )
    elif SCRAPERAPI_KEY:
        r = await _request(
            client, "https://api.scraperapi.com/",
            params={"api_key": SCRAPERAPI_KEY, "url": target, "country_code": "ru",
                    "render": "true" if render else "false"},
            headers={"Accept": "*/*"}, stage=stage + "/scraperapi", retries=1, timeout=75.0,
        )
    else:
        return None
    if r is None or r.status_code != 200:
        return None
    return r.text


# --------------------------------------------------------------------------
#  Schema.org / JSON-LD / OpenGraph (универсальный разбор HTML)
# --------------------------------------------------------------------------
_LD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I)


def _iter_nodes(node: Any) -> Iterator[dict]:
    if isinstance(node, list):
        for x in node:
            yield from _iter_nodes(x)
    elif isinstance(node, dict):
        yield node
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from _iter_nodes(v)


def _is_type(node: dict, name: str) -> bool:
    t = node.get("@type")
    return (t == name) or (isinstance(t, list) and name in t)


def _product_from_ld(ld_objects: List[Any]) -> Optional[dict]:
    for obj in ld_objects:
        for n in _iter_nodes(obj):
            if not _is_type(n, "Product"):
                continue
            offers = n.get("offers")
            offer_list = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []
            price = None
            in_stock = None
            currency = None
            for o in offer_list:
                if not isinstance(o, dict):
                    continue
                spec = o.get("priceSpecification")
                if isinstance(spec, list) and spec:
                    spec = spec[0]
                p = (_to_price(o.get("price")) or _to_price(o.get("lowPrice"))
                     or (_to_price(spec.get("price")) if isinstance(spec, dict) else None))
                if p and (price is None or p < price):
                    price = p
                    currency = o.get("priceCurrency") or currency
                av = str(o.get("availability", ""))
                if av:
                    in_stock = (in_stock or False) or ("InStock" in av or "LimitedAvailability" in av)
            img = n.get("image")
            if isinstance(img, list):
                img = img[0] if img else None
            if isinstance(img, dict):
                img = img.get("url")
            return {"title": n.get("name"), "price": price, "currency": currency or "RUB",
                    "in_stock": in_stock, "image": img if isinstance(img, str) else None}
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
    ld_objects: List[Any] = []
    for m in _LD_RE.finditer(html):
        try:
            ld_objects.append(json.loads(_html.unescape(m.group(1).strip())))
        except ValueError:
            continue
    data = _product_from_ld(ld_objects) or {}
    title = data.get("title") or _meta(html, "og:title")
    price = data.get("price")
    currency = data.get("currency") or "RUB"
    src = "json-ld" if data.get("price") else ""
    if price is None:  # OpenGraph / microdata
        for key in ("product:price:amount", "og:price:amount", "product:sale_price:amount", "price"):
            price = _to_price(_meta(html, key))
            if price:
                currency = _meta(html, "product:price:currency") or _meta(html, "og:price:currency") \
                    or _meta(html, "priceCurrency") or currency
                src = "opengraph"
                break
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        title = _html.unescape(m.group(1)).strip() if m else None
    if price is None and data.get("in_stock") is None:
        return None
    return ProductInfo(
        url=url, shop=shop, title=title, price=price, currency=currency,
        in_stock=data.get("in_stock"), image=data.get("image") or _meta(html, "og:image"),
        source=src or "json-ld",
    )


# --------------------------------------------------------------------------
#  WILDBERRIES
# --------------------------------------------------------------------------
_WB_ID_PATTERNS = (
    r"/catalog/(\d{4,12})",
    r"/product/(\d{4,12})",
    r"[?&](?:card|nm|nmId|sku|id)=(\d{4,12})",
)
_WB_ORIGIN = "https://www.wildberries.ru"
# dest=-1257786 — Москва; spp — соц. скидка; порядок = от новых версий к старым
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
    m = re.search(r"(?<!\d)(\d{6,12})(?!\d)", urlparse(url).path)  # крайний случай
    return m.group(1) if m else None


def _wb_parse_product(p: dict, url: str, nm: str) -> ProductInfo:
    price = old = None
    # v2/v4: цены в sizes[].price (копейки): basic — до скидки, product — со скидкой
    for s in p.get("sizes") or []:
        pr = s.get("price") or {}
        if pr.get("product"):
            price = pr["product"] / 100
            old = (pr.get("basic") or 0) / 100 or None
            break
    # v1 / старый формат
    if price is None and p.get("salePriceU"):
        price = p["salePriceU"] / 100
        old = (p.get("priceU") or 0) / 100 or None
    if price is None and isinstance(p.get("extended"), dict):
        ext = p["extended"]
        if ext.get("clientPriceU"):
            price, old = ext["clientPriceU"] / 100, (ext.get("basicPriceU") or 0) / 100 or None
    if old is not None and price is not None and old <= price:
        old = None
    qty = p.get("totalQuantity")
    in_stock = (qty > 0) if isinstance(qty, int) else (price is not None)
    brand = p.get("brand") or ""
    name = p.get("name") or ""
    return ProductInfo(
        url=url, shop="Wildberries", title=(f"{brand} / {name}" if brand and name else name or None),
        price=price, old_price=old, in_stock=in_stock, source="wb-api",
        image=None,
    )


async def _fetch_wb(client: httpx.AsyncClient, url: str) -> Optional[ProductInfo]:
    nm = _wb_extract_id(url)
    if not nm:  # короткие/мобильные ссылки: раскрываем
        url = await _resolve_redirects(client, url)
        nm = _wb_extract_id(url)
    if not nm:
        logger.error("[wb] не удалось извлечь артикул из %s", url)
        return None
    logger.info("[wb] артикул %s", nm)
    canonical = f"{_WB_ORIGIN}/catalog/{nm}/detail.aspx"
    params = {"appType": "1", "curr": "rub", "dest": "-1257786", "spp": "30",
              "lang": "ru", "locale": "ru", "nm": nm}

    blocked = 0
    for ep in _WB_ENDPOINTS:
        r = await _request(client, ep, params=params, headers=_api_headers(_WB_ORIGIN),
                           stage="wb-api", retries=1)
        if r is None:
            continue
        if r.status_code != 200:
            blocked += r.status_code in (403, 429, 498)
            continue
        data = _json_from_text(r.text)
        products = ((data or {}).get("data") or data or {}).get("products") if data else None
        if products:
            info = _wb_parse_product(products[0], canonical, nm)
            logger.info("[wb] успех через %s: %s", ep, info)
            return info
        logger.warning("[wb] %s: 200, но products пуст (товар снят/другой регион?)", ep)

    # Фолбэк: тот же API через шлюз (RU-IP)
    if blocked or True:
        from urllib.parse import urlencode
        target = f"{_WB_ENDPOINTS[1]}?{urlencode(params)}"
        text = await _gateway_get(client, target, render=False, stage="wb-gw")
        data = _json_from_text(text) if text else None
        products = ((data or {}).get("data") or {}).get("products") if data else None
        if products:
            info = _wb_parse_product(products[0], canonical, nm)
            info.source = "wb-api+gateway"
            return info
    logger.error("[wb] все стратегии провалились для %s (блокировок: %d). "
                 "Нужен RU-прокси: PARSER_PROXY или SCRAPINGANT_KEY.", nm, blocked)
    return None


# --------------------------------------------------------------------------
#  OZON
# --------------------------------------------------------------------------
def _ozon_extract_id(url: str) -> Optional[str]:
    for p in (r"/product/(?:[^/?#]*-)?(\d{4,})", r"/context/detail/id/(\d+)", r"/(?:\w+/)*?(\d{6,})/?(?:[?#]|$)"):
        m = re.search(p, urlparse(url).path)
        if m:
            return m.group(1)
    return None


def _ozon_parse_json(data: dict, url: str) -> Optional[ProductInfo]:
    states = data.get("widgetStates") or {}
    title = price = old = image = None
    in_stock: Optional[bool] = None
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
            if "isAvailable" in obj:
                in_stock = bool(obj["isAvailable"])
        elif key.startswith("webProductHeading-"):
            title = obj.get("title") or title
        elif key.startswith("webOutOfStock-"):
            in_stock = False
        elif key.startswith("webGallery-"):
            image = obj.get("coverImage") or image
    if price is None:  # JSON-LD внутри seo.script
        lds = []
        for s in (data.get("seo") or {}).get("script") or []:
            try:
                lds.append(json.loads(s.get("innerHTML", "")))
            except (ValueError, AttributeError):
                continue
        ld = _product_from_ld(lds)
        if ld:
            price, title = ld["price"], title or ld["title"]
            in_stock = ld["in_stock"] if in_stock is None else in_stock
            image = image or ld["image"]
    if old is not None and price is not None and old <= price:
        old = None
    if price is None and title is None:
        return None
    if price is None and in_stock is None:
        in_stock = False
    return ProductInfo(url=url, shop="Ozon", title=title, price=price, old_price=old,
                       in_stock=in_stock, image=image, source="ozon-json")


_OZON_STATE_RE = re.compile(
    r'<div[^>]+id=["\']state-(web[A-Za-z]+-[^"\']*)["\'][^>]*?data-state=(["\'])(.*?)\2', re.S)


def _ozon_parse_html(html: str, url: str) -> Optional[ProductInfo]:
    # У Ozon НЕТ __NEXT_DATA__: состояние лежит в <div id="state-webPrice-..." data-state='{...}'>
    states = {m.group(1): _html.unescape(m.group(3)) for m in _OZON_STATE_RE.finditer(html)}
    if states:
        info = _ozon_parse_json({"widgetStates": states}, url)
        if info:
            info.source = "ozon-html-state"
            return info
    info = _parse_generic_html(html, url, "Ozon")
    if info:
        info.source = "ozon-" + info.source
    return info


async def _fetch_ozon(client: httpx.AsyncClient, url: str) -> Optional[ProductInfo]:
    host = (urlparse(url).hostname or "").lower()
    pid = _ozon_extract_id(url)
    if not pid or "/t/" in urlparse(url).path or host.startswith("ozon.onelink"):
        url = await _resolve_redirects(client, url)
        pid = _ozon_extract_id(url)
    if not pid:
        logger.error("[ozon] не удалось определить ID товара из %s", url)
        return None
    logger.info("[ozon] ID товара %s", pid)
    canonical = f"https://www.ozon.ru/product/{pid}/"
    path_q = quote(f"/product/{pid}/", safe="")
    entry = f"https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url={path_q}"
    mobile = f"https://api.ozon.ru/composer-api.bx/page/json/v2?url={path_q}"

    # 1) мобильный шлюз (антибот мягче, но не гарантировано)
    app_headers = {
        "User-Agent": OZON_APP_UA, "Accept": "application/json", "Accept-Language": "ru-RU",
        "x-o3-app-name": "ozonapp_android", "x-o3-app-version": "17.45.0(2277)",
        "x-o3-device-type": "mobile",
    }
    r = await _request(client, mobile, headers=app_headers, stage="ozon-mobile", retries=0)
    if r is not None and r.status_code == 200 and not _looks_blocked(r):
        data = _json_from_text(r.text)
        info = _ozon_parse_json(data, canonical) if data else None
        if info:
            info.source = "ozon-mobile-api"
            return info
        logger.warning("[ozon-mobile] 200, но JSON без цены/названия")
    elif r is not None:
        logger.warning("[ozon-mobile] заблокировано: HTTP %s", r.status_code)

    # 2) веб-entrypoint API
    web_headers = _api_headers("https://www.ozon.ru")
    web_headers["Referer"] = canonical
    r = await _request(client, entry, headers=web_headers, stage="ozon-entrypoint", retries=0)
    if r is not None and r.status_code == 200 and not _looks_blocked(r):
        data = _json_from_text(r.text)
        info = _ozon_parse_json(data, canonical) if data else None
        if info:
            return info
        logger.warning("[ozon-entrypoint] 200, но JSON без цены/названия")
    elif r is not None:
        logger.warning("[ozon-entrypoint] заблокировано: HTTP %s (антифрод по IP дата-центра)", r.status_code)

    # 3) HTML страницы напрямую
    r = await _request(client, canonical, headers=_page_headers("https://www.ozon.ru/"),
                       stage="ozon-html", retries=0)
    if r is not None and r.status_code == 200 and not _looks_blocked(r):
        info = _ozon_parse_html(r.text, canonical)
        if info:
            return info
        logger.warning("[ozon-html] страница получена, но данные о цене не найдены")
    elif r is not None:
        logger.warning("[ozon-html] заблокировано/капча: HTTP %s", r.status_code)

    # 4) Проксирующий API (единственный реально надёжный бесплатный путь)
    if SCRAPINGANT_KEY or SCRAPERAPI_KEY:
        for target, render, parser in ((entry, False, "json"), (canonical, True, "html")):
            text = await _gateway_get(client, target, render=render, stage="ozon-gw-" + parser)
            if not text:
                continue
            if parser == "json":
                data = _json_from_text(text)
                info = _ozon_parse_json(data, canonical) if data else None
            else:
                info = None if any(m in text[:4000].lower() for m in _BLOCK_MARKERS) \
                    else _ozon_parse_html(text, canonical)
            if info:
                info.source += "+gateway"
                return info
            logger.warning("[ozon-gw-%s] ответ получен, но разобрать не удалось", parser)
    else:
        logger.error("[ozon] прямые запросы заблокированы, а ключи SCRAPINGANT_KEY/SCRAPERAPI_KEY "
                     "или PARSER_PROXY не заданы — дальше идти нечем.")
    logger.error("[ozon] все стратегии провалились для ID %s", pid)
    return None


# --------------------------------------------------------------------------
#  Универсальный фолбэк
# --------------------------------------------------------------------------
async def _fetch_generic(client: httpx.AsyncClient, url: str) -> Optional[ProductInfo]:
    shop = (urlparse(url).hostname or "shop").replace("www.", "")
    r = await _request(client, url, headers=_page_headers(), stage="generic", retries=2)
    html = None
    if r is not None and r.status_code == 200 and not _looks_blocked(r):
        html = r.text
    elif r is not None:
        logger.warning("[generic] %s ответил HTTP %s / антибот", shop, r.status_code)
        html = await _gateway_get(client, url, render=True, stage="generic-gw")
    if not html:
        logger.error("[generic] не удалось получить HTML %s", url)
        return None
    info = _parse_generic_html(html, url, shop)
    if info is None:
        logger.error("[generic] на странице %s нет JSON-LD/OpenGraph с ценой (цена рисуется JS?)", shop)
    return info


# --------------------------------------------------------------------------
#  ТОЧКА ВХОДА
# --------------------------------------------------------------------------
def _is_wb(host: str) -> bool:
    return host == "wb.ru" or host.endswith(".wb.ru") or "wildberries." in host


def _is_ozon(host: str) -> bool:
    return "ozon." in host


async def fetch_product_info(url: str) -> Optional[ProductInfo]:
    """Возвращает ProductInfo или None (причины — в логах 'price_parser')."""
    m = re.search(r"(https?://\S+|(?:www\.)?(?:wildberries|ozon|wb)\.\S+)", url.strip())
    if not m:
        logger.error("[main] в сообщении нет ссылки: %r", url[:100])
        return None
    url = m.group(1).rstrip(").,;")
    if not url.startswith("http"):
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower()
    try:
        async with _make_client() as client:
            if _is_wb(host):
                return await _fetch_wb(client, url)
            if _is_ozon(host):
                return await _fetch_ozon(client, url)
            return await _fetch_generic(client, url)
    except Exception:
        logger.exception("[main] необработанная ошибка при разборе %s", url)
        return None
