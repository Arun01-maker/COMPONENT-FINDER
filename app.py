
import asyncio
import json
import re
import time
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

try:
    from camoufox.async_api import AsyncCamoufox
except Exception:
    AsyncCamoufox = None

app = FastAPI(title="Component Finder API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

# ---------------------------------------------------------------------------
# Search settings
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = 8.0
SITE_TIMEOUT = 45.0
PRODUCT_TIMEOUT = 12.0
BROWSER_START_TIMEOUT = 25.0
BROWSER_PAGE_TIMEOUT = 18000
BROWSER_WAIT_MS = 1200

MAX_LISTING_RESULTS = 10
MAX_VERIFY_RESULTS = 6
MAX_CONCURRENT_SITES = 12
MAX_BROWSER_PAGES = 4

# Leeds' public company site is leedsind.net, but its actual searchable
# e-commerce catalog is leedscart.com. The UI still calls it Leeds Electronic
# Industry Inc.
SITES = [
    {
        "id": "01", "name": "ET Store", "kind": "shopify",
        "search": "https://www.etstore.in/search?q={q}",
        "origin": "https://www.etstore.in",
        "cards": [".product-item", ".grid__item", ".card-wrapper", "li.product"],
    },
    {
        "id": "02", "name": "Robu.in", "kind": "robu",
        "search": "https://robu.in/?s={q}&post_type=product",
        "origin": "https://robu.in",
        "cards": ["li.product", ".product-small", ".product-type-simple"],
    },
    {
        "id": "03", "name": "element14 India", "kind": "element14",
        "search": "https://in.element14.com/w/c/?st={q}",
        "origin": "https://in.element14.com",
        "cards": ["tr.productRow", "div.productDisplay", ".product-listing", ".product-item"],
    },
    {
        "id": "04", "name": "ElectronicsComp", "kind": "opencart",
        "search": "https://www.electronicscomp.com/index.php?route=product/search&search={q}",
        "origin": "https://www.electronicscomp.com",
        "cards": [".product-layout", ".product-thumb", ".product-grid"],
    },
    {
        "id": "05", "name": "Evelta", "kind": "magento",
        "search": "https://evelta.com/catalogsearch/result/?q={q}",
        "origin": "https://evelta.com",
        "cards": [".product-item", ".product-item-info", ".item.product"],
    },
    {
        "id": "06", "name": "Tomson Electronics", "kind": "shopify",
        "search": "https://www.tomsonelectronics.com/search?q={q}",
        "origin": "https://www.tomsonelectronics.com",
        "cards": [".grid__item", ".product-card", ".card-wrapper"],
    },
    {
        "id": "07", "name": "QuartzComponents", "kind": "shopify",
        "search": "https://quartzcomponents.com/search?q={q}",
        "origin": "https://quartzcomponents.com",
        "cards": [".grid__item", ".product-card", ".card-wrapper"],
    },
    {
        "id": "08", "name": "MakerBazar", "kind": "shopify",
        "search": "https://makerbazar.in/search?q={q}",
        "origin": "https://makerbazar.in",
        "cards": [".grid__item", ".product-card", ".card-wrapper"],
    },
    {
        "id": "09", "name": "Probots", "kind": "woocommerce",
        "search": "https://probots.co.in/?s={q}&post_type=product",
        "origin": "https://probots.co.in",
        "cards": ["li.product", ".product-small", ".product"],
    },
    {
        "id": "10", "name": "Sharvi Electronics", "kind": "woocommerce",
        "search": "https://sharvielectronics.com/?s={q}&post_type=product",
        "origin": "https://sharvielectronics.com",
        "cards": ["li.product", ".product-small", ".product"],
    },
    {
        "id": "11", "name": "Leeds Electronic Industry Inc", "kind": "leedscart",
        "search": "https://www.leedscart.com/index.php?route=product/search&search={q}",
        "origin": "https://www.leedscart.com",
        "cards": [".product-layout", ".product-thumb", ".product-grid"],
    },
    {
        "id": "12", "name": "Sparefly", "kind": "shopify",
        "search": "https://sparefly.com/search?q={q}",
        "origin": "https://sparefly.com",
        "cards": [".grid__item", ".product-card", ".card-wrapper"],
    },
]

SITE_BY_ID = {s["id"]: s for s in SITES}
SITE_BY_NAME = {s["name"].lower(): s for s in SITES}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

ROBU_API = "https://robu.in/wp-json/wc/store/v1/products"


# ---------------------------------------------------------------------------
# Text / matching
# ---------------------------------------------------------------------------
def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def compact(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def extract_part(query):
    tokens = clean_text(query).split()
    # Prefer tokens that look like real part numbers:
    # LM2596, SN74LVC541A, ESP32, STM32F103C8T6, etc.
    for token in tokens:
        t = token.strip(" ,;:/()[]{}")
        if len(t) >= 3 and re.search(r"[A-Za-z]", t) and re.search(r"\d", t):
            # Ignore pure electrical specifications.
            if re.fullmatch(r"\d+(?:\.\d+)?(?:v|a|ma|w|khz|mhz|mm|cm|ohm|k|r)?", t, re.I):
                continue
            return t
    for token in tokens:
        t = token.strip(" ,;:/()[]{}")
        if len(t) >= 3 and re.search(r"[A-Za-z]", t):
            return t
    return " ".join(tokens[:2])


def query_variants(query):
    original = clean_text(query)
    part = extract_part(original)
    variants = [original]

    if part and normalize(part) != normalize(original):
        variants.append(part)

    # Keep package/specifiers as a fallback, but never before the exact query.
    tokens = original.split()
    if part and len(tokens) > 1:
        extra = [
            t for t in tokens
            if normalize(t) not in {normalize(part), "smd", "smt", "dip", "sop", "soic"}
        ]
        if extra:
            variants.append(clean_text(part + " " + " ".join(extra)))

    out, seen = [], set()
    for v in variants:
        key = normalize(v)
        if v and key not in seen:
            seen.add(key)
            out.append(v)
    return out[:3]


def component_match(title, query):
    part = extract_part(query)
    if not part:
        return False

    hay = normalize(title)
    p = normalize(part)

    if p in hay:
        return True

    return compact(part) in compact(title)


def title_score(title, query):
    nt = normalize(title)
    nq = normalize(query)
    part = normalize(extract_part(query))
    score = 0

    if part and part in nt:
        score += 150
    if nq and nq in nt:
        score += 80

    for token in normalize(query).split():
        if len(token) > 1 and token in nt:
            score += 8

    return score


def absolute_url(site, href):
    return urljoin(site["origin"] + "/", href or "")


def valid_product_url(site, link):
    try:
        a = urlparse(link)
        b = urlparse(site["origin"])
        return (
            a.scheme in ("http", "https")
            and a.netloc.lower() == b.netloc.lower()
            and a.path not in ("", "/")
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Availability detection
# ---------------------------------------------------------------------------
def parse_availability(text):
    low = clean_text(text).lower()
    if not low:
        return "UNKNOWN", None

    # Explicit negative states first.
    if re.search(
        r"\bout\s*of\s*stock\b|\bsold\s*out\b|\bcurrently unavailable\b|"
        r"\bunavailable\b|\bnot available\b",
        low,
    ):
        return "OUT_OF_STOCK", 0

    if re.search(
        r"\bavailable\s+to\s+order\b|\bavailable\s+on\s+order\b|"
        r"\bback\s*order\b|\bbackorder\b|\bpre[- ]?order\b|"
        r"\b5-7\s+days?\s+for\s+delivery\b|\b\d+\s*-\s*\d+\s+days?\s+for\s+delivery\b",
        low,
    ):
        return "AVAILABLE_TO_ORDER", None

    patterns = [
        r"(?:only\s+)?([0-9][0-9,]*)\s+(?:items?\s+)?in\s+stock",
        r"(?:availability|available|stock|quantity)\s*[:\-]?\s*([0-9][0-9,]*)",
        r"([0-9][0-9,]*)\s+available",
    ]

    for pattern in patterns:
        m = re.search(pattern, low, re.I)
        if m:
            try:
                qty = int(m.group(1).replace(",", ""))
                return ("IN_STOCK" if qty > 0 else "OUT_OF_STOCK"), qty
            except ValueError:
                pass

    if re.search(r"\bin\s*stock\b|\bin-stock\b", low):
        return "IN_STOCK", None

    return "UNKNOWN", None


def availability_label(state, qty):
    if state == "IN_STOCK":
        return f"In Stock ({qty:,})" if qty is not None else "In Stock"
    if state == "OUT_OF_STOCK":
        return "Out of Stock"
    if state == "AVAILABLE_TO_ORDER":
        return "Available to Order"
    return "Availability Not Confirmed"


def structured_availability(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        stack = data if isinstance(data, list) else [data]

        while stack:
            obj = stack.pop()

            if isinstance(obj, list):
                stack.extend(obj)
                continue

            if not isinstance(obj, dict):
                continue

            if isinstance(obj.get("@graph"), list):
                stack.extend(obj["@graph"])

            offers = obj.get("offers")
            if isinstance(offers, list):
                stack.extend(offers)
            elif isinstance(offers, dict):
                stack.append(offers)

            av = str(obj.get("availability", "")).lower()
            qty = obj.get("inventoryLevel")

            if isinstance(qty, dict):
                qty = qty.get("value")

            try:
                qty = int(qty) if qty is not None else None
            except Exception:
                qty = None

            if "instock" in av:
                return "IN_STOCK", qty
            if "outofstock" in av:
                return "OUT_OF_STOCK", 0
            if "backorder" in av or "preorder" in av:
                return "AVAILABLE_TO_ORDER", None

    return "UNKNOWN", None


def availability_from_html(html_text):
    soup = BeautifulSoup(html_text, "html.parser")

    state, qty = structured_availability(soup)
    if state != "UNKNOWN":
        return state, qty

    # Product-specific metadata.
    for node in soup.select(
        "[data-stock], [data-stock-quantity], [data-available], "
        "[data-inventory], meta[itemprop=availability], meta[property*=availability]"
    )[:30]:
        raw = clean_text(
            node.get("content")
            or node.get("data-stock")
            or node.get("data-stock-quantity")
            or node.get("data-available")
            or node.get("data-inventory")
        )

        if not raw:
            continue

        st, q = parse_availability(raw)

        if st != "UNKNOWN":
            return st, q

        low = raw.lower()

        if "instock" in low or low in {"true", "1"}:
            return "IN_STOCK", None

        if "outofstock" in low or low in {"false", "0"}:
            return "OUT_OF_STOCK", 0

    # Common product stock areas.
    best = ("UNKNOWN", None)

    selectors = [
        ".availability",
        ".stock",
        ".stock-status",
        ".product-stock",
        ".inventory",
        ".availability-status",
        ".product-info",
        ".product-information",
        ".product-form",
        ".product-form__buttons",
        ".product-single__meta",
        ".summary",
        "form.cart",
        "main",
        "#content",
    ]

    for selector in selectors:
        for node in soup.select(selector)[:8]:
            text = clean_text(node.get_text(" ", strip=True))
            st, q = parse_availability(text)

            if st == "IN_STOCK" and q is not None:
                return st, q

            if st == "OUT_OF_STOCK":
                best = (st, q)

            elif st == "AVAILABLE_TO_ORDER" and best[0] == "UNKNOWN":
                best = (st, q)

            elif st == "IN_STOCK" and best[0] == "UNKNOWN":
                best = (st, q)

    # Product form buttons are a useful fallback.
    forms = soup.select(
        "form.cart, form[action*=cart], .product-form, "
        ".product-form__buttons, form[action*=checkout]"
    )[:5]

    for form in forms:
        text = clean_text(form.get_text(" ", strip=True))
        st, q = parse_availability(text)

        if st != "UNKNOWN":
            return st, q

        for node in form.select("button, input[type=submit], a"):
            label = clean_text(
                node.get("value") or node.get_text(" ", strip=True)
            ).lower()

            disabled = (
                node.has_attr("disabled")
                or str(node.get("aria-disabled", "")).lower() == "true"
            )

            classes = " ".join(node.get("class", [])).lower()

            if (
                any(
                    x in label
                    for x in (
                        "add to cart",
                        "add to basket",
                        "buy now",
                        "purchase",
                    )
                )
                and not disabled
                and "disabled" not in classes
            ):
                return "IN_STOCK", None

    # Do not infer stock from a generic "available" word on the page.
    body = clean_text(soup.get_text(" ", strip=True))

    if re.search(r"\bout\s*of\s*stock\b|\bsold\s*out\b", body, re.I):
        return "OUT_OF_STOCK", 0

    return best


# ---------------------------------------------------------------------------
# Candidate extraction
# ---------------------------------------------------------------------------
def extract_price(block):
    if not block:
        return "N/A"

    selectors = [
        ".price",
        ".price-item",
        ".price-box",
        ".product-price",
        ".product__price",
        ".amount",
        "[data-price]",
        "[class*=price]",
    ]

    for selector in selectors:
        node = block.select_one(selector)

        if node:
            text = clean_text(
                node.get("content")
                or node.get("data-price")
                or node.get_text(" ", strip=True)
            )

            if text:
                return text

    return "N/A"


def candidate_from_anchor(anchor, site, query, block=None):
    href = anchor.get("href", "")
    link = absolute_url(site, href)

    if not valid_product_url(site, link):
        return None

    node = block or anchor

    title_node = None

    if hasattr(node, "select_one"):
        title_node = node.select_one(
            "h1,h2,h3,h4,h5,"
            ".product-title,.product-name,.name,"
            ".woocommerce-loop-product__title,"
            ".product-item__title,.card__heading,"
            ".description,.productDescription"
        )

    title = clean_text(
        title_node.get_text(" ", strip=True)
        if title_node
        else anchor.get_text(" ", strip=True)
    )

    # For links with very little visible text, inspect parent context.
    context = title

    parent = anchor
    for _ in range(4):
        parent = parent.parent

        if not parent:
            break

        text = clean_text(parent.get_text(" ", strip=True))

        if 3 <= len(text) <= 1600:
            context = text

    haystack = f"{title} {context[:900]} {href}"

    if not component_match(haystack, query):
        return None

    score = title_score(title + " " + context[:300], query)

    if compact(extract_part(query)) in compact(href):
        score += 40

    return {
        "title": title[:350] or context[:350],
        "link": link,
        "price": extract_price(block or parent),
        "availability_state": "UNKNOWN",
        "stock_quantity": None,
        "availability": "Availability Not Confirmed",
        "_score": score,
    }


def extract_candidates(html_text, site, query):
    soup = BeautifulSoup(html_text, "html.parser")
    candidates = {}

    part = normalize(extract_part(query))
    custom_cards = site.get("cards", [])

    selectors = custom_cards + [
        "li.product",
        "article",
        ".product-item",
        ".product-card",
        ".product-thumb",
        ".grid__item",
        ".grid-product",
        ".card-wrapper",
        ".product-grid-item",
        ".search-result",
        ".product-listing",
        ".product-list-item",
        ".item-product",
        ".catalog-product",
        "tr.productRow",
        "div.productDisplay",
    ]

    seen_selectors = set()

    for selector in selectors:
        if selector in seen_selectors:
            continue

        seen_selectors.add(selector)

        for block in soup.select(selector)[:80]:
            links = block.select("a[href]")

            if not links:
                continue

            links.sort(
                key=lambda a: (
                    0 if part and part in normalize(
                        a.get_text(" ", strip=True) + " " + a.get("href", "")
                    ) else 1,
                    -len(a.get_text(" ", strip=True)),
                )
            )

            for anchor in links[:5]:
                c = candidate_from_anchor(
                    anchor,
                    site,
                    query,
                    block,
                )

                if c:
                    c["price"] = extract_price(block)
                    candidates[c["link"]] = c
                    break

    # Generic anchor fallback.
    if len(candidates) < MAX_LISTING_RESULTS:
        for anchor in soup.select("a[href]"):
            txt = clean_text(anchor.get_text(" ", strip=True))
            href = anchor.get("href", "")

            hay = normalize(txt + " " + href)

            if part:
                if part not in hay and compact(part) not in compact(hay):
                    continue

            if len(txt) < 3 and part not in normalize(href):
                continue

            c = candidate_from_anchor(anchor, site, query)

            if c:
                candidates.setdefault(c["link"], c)

            if len(candidates) >= MAX_LISTING_RESULTS:
                break

    result = sorted(
        candidates.values(),
        key=lambda x: x["_score"],
        reverse=True,
    )[:MAX_LISTING_RESULTS]

    return result


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
async def fetch_http(client, url, timeout=HTTP_TIMEOUT):
    try:
        response = await client.get(
            url,
            timeout=timeout,
            follow_redirects=True,
        )

        text = response.text or ""

        if response.status_code >= 400 or not text:
            return None, response.status_code, str(response.url)

        return text, response.status_code, str(response.url)

    except Exception as exc:
        print(
            f"[HTTP] {url} -> "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return None, None, None


# ---------------------------------------------------------------------------
# Camoufox browser
# ---------------------------------------------------------------------------
class Browser:
    def __init__(self):
        self.cm = None
        self.browser = None
        self.lock = asyncio.Lock()
        self.page_sem = asyncio.Semaphore(MAX_BROWSER_PAGES)

    async def start(self):
        if self.browser is not None:
            return self.browser

        if AsyncCamoufox is None:
            print("[BROWSER] Camoufox module unavailable", flush=True)
            return None

        async with self.lock:
            if self.browser is not None:
                return self.browser

            try:
                self.cm = AsyncCamoufox(
                    headless=True,
                    humanize=False,
                    block_images=True,
                )

                self.browser = await asyncio.wait_for(
                    self.cm.__aenter__(),
                    timeout=BROWSER_START_TIMEOUT,
                )

                print("[BROWSER] Camoufox started", flush=True)

            except Exception as exc:
                print(
                    f"[BROWSER] startup failed: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )

                self.cm = None
                self.browser = None

        return self.browser

    async def fetch(self, url):
        async with self.page_sem:
            browser = await self.start()

            if browser is None:
                return None

            page = None

            try:
                page = await browser.new_page()

                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=BROWSER_PAGE_TIMEOUT,
                )

                await page.wait_for_timeout(BROWSER_WAIT_MS)

                return await page.content()

            except Exception as exc:
                print(
                    f"[BROWSER] {url} -> "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )
                return None

            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass

    async def close(self):
        if self.cm:
            try:
                await self.cm.__aexit__(None, None, None)
            except Exception:
                pass

        self.cm = None
        self.browser = None


# ---------------------------------------------------------------------------
# Native distributor APIs
# ---------------------------------------------------------------------------
async def shopify_search(client, site, query):
    origin = site["origin"].rstrip("/")

    for variant in query_variants(query):
        # 1. Shopify suggest API.
        try:
            response = await client.get(
                origin + "/search/suggest.json",
                params={
                    "q": variant,
                    "resources[type]": "product",
                    "resources[limit]": str(MAX_LISTING_RESULTS),
                    "resources[options][unavailable_products]": "show",
                },
                timeout=HTTP_TIMEOUT,
            )

            if response.is_success:
                data = response.json()

                products = (
                    (data.get("resources") or {})
                    .get("results", {})
                    .get("products", [])
                )

                found = []

                for item in products:
                    title = clean_text(item.get("title"))
                    link = absolute_url(
                        site,
                        item.get("url", ""),
                    )

                    if (
                        title
                        and valid_product_url(site, link)
                        and component_match(title, query)
                    ):
                        found.append(
                            {
                                "title": title,
                                "link": link,
                                "price": clean_text(
                                    str(item.get("price") or "N/A")
                                ),
                                "availability_state": "UNKNOWN",
                                "stock_quantity": None,
                                "availability": "Availability Not Confirmed",
                                "_score": title_score(title, query),
                            }
                        )

                if found:
                    return (
                        sorted(
                            found,
                            key=lambda x: x["_score"],
                            reverse=True,
                        )[:MAX_LISTING_RESULTS],
                        True,
                    )

        except Exception as exc:
            print(
                f"[SHOPIFY API] {site['name']}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

        # 2. Shopify search.json.
        try:
            response = await client.get(
                origin + "/search.json",
                params={
                    "q": variant,
                    "type": "product",
                },
                timeout=HTTP_TIMEOUT,
            )

            if response.is_success:
                data = response.json()

                products = (
                    data.get("products", [])
                    if isinstance(data, dict)
                    else []
                )

                found = []

                for item in products:
                    title = clean_text(item.get("title"))
                    handle = item.get("handle")
                    link = absolute_url(
                        site,
                        item.get("url")
                        or (f"/products/{handle}" if handle else ""),
                    )

                    if (
                        title
                        and valid_product_url(site, link)
                        and component_match(title, query)
                    ):
                        found.append(
                            {
                                "title": title,
                                "link": link,
                                "price": clean_text(
                                    str(
                                        item.get("price")
                                        or "N/A"
                                    )
                                ),
                                "availability_state": "UNKNOWN",
                                "stock_quantity": None,
                                "availability": "Availability Not Confirmed",
                                "_score": title_score(title, query),
                            }
                        )

                if found:
                    return (
                        sorted(
                            found,
                            key=lambda x: x["_score"],
                            reverse=True,
                        )[:MAX_LISTING_RESULTS],
                        True,
                    )

        except Exception as exc:
            print(
                f"[SHOPIFY SEARCH.JSON] {site['name']}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    return [], False


async def woocommerce_search(client, site, query):
    url = site["origin"].rstrip("/") + "/wp-json/wc/store/v1/products"
    reached = False

    for variant in query_variants(query):
        try:
            response = await client.get(
                url,
                params={
                    "search": variant,
                    "per_page": MAX_LISTING_RESULTS,
                    "catalog_visibility": "visible",
                },
                timeout=HTTP_TIMEOUT,
            )

            if not response.is_success:
                continue

            reached = True

            data = response.json()

            if not isinstance(data, list):
                continue

            found = []

            for item in data:
                title = clean_text(item.get("name"))
                link = item.get("permalink") or ""

                if (
                    not title
                    or not link
                    or not component_match(title, query)
                ):
                    continue

                prices = item.get("prices") or {}

                raw_price = (
                    prices.get("price_html")
                    or prices.get("price")
                    or ""
                )

                price = clean_text(
                    BeautifulSoup(
                        str(raw_price),
                        "html.parser",
                    ).get_text(" ", strip=True)
                ) or "N/A"

                if item.get("is_in_stock") is True:
                    state = "IN_STOCK"
                elif item.get("is_in_stock") is False:
                    state = "OUT_OF_STOCK"
                elif item.get("is_on_backorder"):
                    state = "AVAILABLE_TO_ORDER"
                else:
                    state = "UNKNOWN"

                qty = None

                stock = item.get("stock_availability") or {}

                if state == "UNKNOWN":
                    state, qty = parse_availability(
                        clean_text(stock.get("text"))
                    )

                found.append(
                    {
                        "title": title,
                        "link": link,
                        "price": price,
                        "availability_state": state,
                        "stock_quantity": qty,
                        "availability": availability_label(
                            state,
                            qty,
                        ),
                        "_score": title_score(title, query),
                    }
                )

            if found:
                return (
                    sorted(
                        found,
                        key=lambda x: x["_score"],
                        reverse=True,
                    )[:MAX_LISTING_RESULTS],
                    True,
                )

        except Exception as exc:
            print(
                f"[WOOCOMMERCE API] {site['name']}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    return [], reached


async def robu_search(client, query):
    reached = False

    for variant in query_variants(query):
        try:
            response = await client.get(
                ROBU_API,
                params={
                    "search": variant,
                    "per_page": MAX_LISTING_RESULTS,
                },
                timeout=HTTP_TIMEOUT,
            )

            if not response.is_success:
                continue

            reached = True

            data = response.json()

            if not isinstance(data, list):
                continue

            found = []

            for item in data:
                title = clean_text(item.get("name"))
                link = item.get("permalink") or ""

                if (
                    not title
                    or not link
                    or not component_match(title, query)
                ):
                    continue

                prices = item.get("prices") or {}

                raw_price = (
                    prices.get("price_html")
                    or prices.get("price")
                    or ""
                )

                price = clean_text(
                    BeautifulSoup(
                        str(raw_price),
                        "html.parser",
                    ).get_text(" ", strip=True)
                ) or "N/A"

                if item.get("is_in_stock") is True:
                    state = "IN_STOCK"
                elif item.get("is_in_stock") is False:
                    state = "OUT_OF_STOCK"
                elif item.get("is_on_backorder"):
                    state = "AVAILABLE_TO_ORDER"
                else:
                    state = "UNKNOWN"

                qty = None

                stock = item.get("stock_availability") or {}

                if state == "UNKNOWN":
                    state, qty = parse_availability(
                        clean_text(stock.get("text"))
                    )

                found.append(
                    {
                        "title": title,
                        "link": link,
                        "price": price,
                        "availability_state": state,
                        "stock_quantity": qty,
                        "availability": availability_label(
                            state,
                            qty,
                        ),
                        "_score": title_score(title, query),
                    }
                )

            if found:
                return (
                    sorted(
                        found,
                        key=lambda x: x["_score"],
                        reverse=True,
                    )[:MAX_LISTING_RESULTS],
                    True,
                )

        except Exception as exc:
            print(
                f"[ROBU API] {type(exc).__name__}: {exc}",
                flush=True,
            )

    return [], reached


# ---------------------------------------------------------------------------
# Website search URLs
# ---------------------------------------------------------------------------
def search_urls(site, query):
    q = quote_plus(query)
    origin = site["origin"].rstrip("/")

    urls = [site["search"].format(q=q)]

    kind = site["kind"]

    if kind == "leedscart":
        urls += [
            f"{origin}/index.php?route=product/search&search={q}",
            f"{origin}/index.php?route=product/search&search={q}&description=true",
        ]

    elif kind == "element14":
        urls += [
            f"{origin}/w/c/?st={q}",
            f"{origin}/search?st={q}",
        ]

    elif kind in {"woocommerce", "generic"}:
        urls += [
            f"{origin}/?s={q}&post_type=product",
            f"{origin}/?post_type=product&s={q}",
            f"{origin}/?s={q}",
        ]

    elif kind == "opencart":
        urls += [
            f"{origin}/index.php?route=product/search&search={q}",
        ]

    elif kind == "magento":
        urls += [
            f"{origin}/catalogsearch/result/?q={q}",
        ]

    elif kind == "shopify":
        urls += [
            f"{origin}/search?q={q}&type=product",
        ]

    result = []
    seen = set()

    for url in urls:
        if url not in seen:
            seen.add(url)
            result.append(url)

    return result


# ---------------------------------------------------------------------------
# Website search: HTTP + browser
# ---------------------------------------------------------------------------
async def native_search(client, site, query):
    reached = False

    # Exact query first.
    for url in search_urls(site, query)[:2]:
        html, status, _ = await fetch_http(
            client,
            url,
            HTTP_TIMEOUT,
        )

        if html is None:
            continue

        reached = True

        candidates = extract_candidates(
            html,
            site,
            query,
        )

        if candidates:
            return candidates, reached, "website HTTP search"

    # Only the strongest part number as fallback.
    part = extract_part(query)

    if part and normalize(part) != normalize(query):
        for url in search_urls(site, part)[:1]:
            html, status, _ = await fetch_http(
                client,
                url,
                HTTP_TIMEOUT,
            )

            if html is None:
                continue

            reached = True

            candidates = extract_candidates(
                html,
                site,
                query,
            )

            if candidates:
                return candidates, reached, "website HTTP part search"

    return [], reached, "website HTTP search"


async def browser_search(browser, site, query):
    if await browser.start() is None:
        return [], False, "browser unavailable"

    reached = False

    # Browser search is deliberately done inside the distributor website.
    # No search engine is used.
    for url in search_urls(site, query)[:2]:
        html = await browser.fetch(url)

        if not html:
            continue

        reached = True

        candidates = extract_candidates(
            html,
            site,
            query,
        )

        if candidates:
            return candidates, reached, "website browser search"

    part = extract_part(query)

    if part and normalize(part) != normalize(query):
        url = search_urls(site, part)[0]

        html = await browser.fetch(url)

        if html:
            reached = True

            candidates = extract_candidates(
                html,
                site,
                query,
            )

            if candidates:
                return candidates, reached, "website browser part search"

    return [], reached, "website browser search"


# ---------------------------------------------------------------------------
# Product verification
# ---------------------------------------------------------------------------
async def shopify_product_json(client, site, link):
    try:
        path = urlparse(link).path.rstrip("/")

        if not path.startswith("/products/"):
            return "UNKNOWN", None

        response = await client.get(
            site["origin"].rstrip("/") + path + ".js",
            timeout=PRODUCT_TIMEOUT,
        )

        if not response.is_success:
            return "UNKNOWN", None

        data = response.json()

        variants = data.get("variants") or []

        if not variants:
            return "UNKNOWN", None

        available = [
            v for v in variants
            if v.get("available") is True
        ]

        if available:
            return "IN_STOCK", None

        if all(v.get("available") is False for v in variants):
            return "OUT_OF_STOCK", 0

    except Exception as exc:
        print(
            f"[SHOPIFY PRODUCT] {site['name']}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

    return "UNKNOWN", None


async def verify_product(client, browser, product, site):
    # Shopify structured product state.
    if site["kind"] == "shopify":
        state, qty = await shopify_product_json(
            client,
            site,
            product["link"],
        )

        if state != "UNKNOWN":
            product.update(
                {
                    "availability_state": state,
                    "stock_quantity": qty,
                    "availability": availability_label(
                        state,
                        qty,
                    ),
                    "verified": True,
                }
            )
            return True

    # Product page via normal HTTP.
    html, _, _ = await fetch_http(
        client,
        product["link"],
        PRODUCT_TIMEOUT,
    )

    state = "UNKNOWN"
    qty = None

    if html:
        state, qty = availability_from_html(html)

    # Product page via real browser if HTTP was blocked or ambiguous.
    if state == "UNKNOWN" or html is None:
        browser_html = await browser.fetch(
            product["link"]
        )

        if browser_html:
            browser_state, browser_qty = availability_from_html(
                browser_html
            )

            if browser_state != "UNKNOWN":
                state, qty = browser_state, browser_qty

    if state == "UNKNOWN":
        return False

    product.update(
        {
            "availability_state": state,
            "stock_quantity": qty,
            "availability": availability_label(
                state,
                qty,
            ),
            "verified": True,
        }
    )

    return True


async def verify_products(client, browser, products, site):
    candidates = products[:MAX_VERIFY_RESULTS]

    results = await asyncio.gather(
        *(
            verify_product(
                client,
                browser,
                product,
                site,
            )
            for product in candidates
        ),
        return_exceptions=True,
    )

    verified = []

    for product, ok in zip(candidates, results):
        if ok is True and product.get("verified"):
            verified.append(product)

    return verified


# ---------------------------------------------------------------------------
# One distributor
# ---------------------------------------------------------------------------
async def search_site(client, browser, site, query):
    started = time.perf_counter()
    methods = []
    reachable = False

    try:
        products = []

        # Fast structured API.
        if site["kind"] == "robu":
            products, ok = await robu_search(
                client,
                query,
            )
            methods.append("Robu API")
            reachable |= ok

        elif site["kind"] == "shopify":
            products, ok = await shopify_search(
                client,
                site,
                query,
            )
            methods.append("Shopify API")
            reachable |= ok

        elif site["kind"] == "woocommerce":
            products, ok = await woocommerce_search(
                client,
                site,
                query,
            )
            methods.append("WooCommerce API")
            reachable |= ok

        # Actual website search.
        if not products:
            products, ok, method = await native_search(
                client,
                site,
                query,
            )

            methods.append(method)
            reachable |= ok

        # Actual browser search inside the website.
        if not products:
            products, ok, method = await browser_search(
                browser,
                site,
                query,
            )

            methods.append(method)
            reachable |= ok

        if not products:
            if reachable:
                return (
                    [],
                    "not_found",
                    {
                        "method": " + ".join(methods),
                        "elapsed_ms": int(
                            (time.perf_counter() - started) * 1000
                        ),
                        "error": None,
                    },
                )

            return (
                [],
                "unavailable",
                {
                    "method": " + ".join(methods),
                    "elapsed_ms": int(
                        (time.perf_counter() - started) * 1000
                    ),
                    "error": "Website could not be reached",
                },
            )

        # Open/verify the actual product page.
        verified = await verify_products(
            client,
            browser,
            products,
            site,
        )

        if verified:
            return (
                verified,
                "found",
                {
                    "method": " + ".join(methods),
                    "elapsed_ms": int(
                        (time.perf_counter() - started) * 1000
                    ),
                    "error": None,
                },
            )

        return (
            [],
            "unverified",
            {
                "method": " + ".join(methods),
                "elapsed_ms": int(
                    (time.perf_counter() - started) * 1000
                ),
                "error": (
                    "Matching product found, but its product page "
                    "did not expose explicit availability"
                ),
            },
        )

    except asyncio.TimeoutError:
        raise

    except Exception as exc:
        return (
            [],
            "error",
            {
                "method": " + ".join(methods),
                "elapsed_ms": int(
                    (time.perf_counter() - started) * 1000
                ),
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------
def sse(data):
    return (
        "data: "
        + json.dumps(
            data,
            ensure_ascii=False,
        )
        + "\n\n"
    )


async def event_generator(query, selected_sites=None):
    if selected_sites:
        wanted = set()

        for value in selected_sites:
            value = value.strip()

            if value in SITE_BY_ID:
                wanted.add(value)

            elif value.lower() in SITE_BY_NAME:
                wanted.add(
                    SITE_BY_NAME[value.lower()]["id"]
                )

        sites = [
            site
            for site in SITES
            if site["id"] in wanted
        ]

    else:
        sites = list(SITES)

    yield sse(
        {
            "type": "init",
            "sites": [
                {
                    "id": site["id"],
                    "name": site["name"],
                    "kind": site["kind"],
                }
                for site in sites
            ],
        }
    )

    browser = Browser()

    # Start Camoufox independently so it does not block the HTTP path.
    browser_start_task = asyncio.create_task(
        browser.start()
    )

    try:
        limits = httpx.Limits(
            max_connections=60,
            max_keepalive_connections=30,
        )

        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            limits=limits,
        ) as client:

            tasks = []

            for site in sites:
                yield sse(
                    {
                        "type": "site_status",
                        "site_id": site["id"],
                        "site": site["name"],
                        "state": "searching",
                    }
                )

                async def one(site=site):
                    try:
                        result = await asyncio.wait_for(
                            search_site(
                                client,
                                browser,
                                site,
                                query,
                            ),
                            timeout=SITE_TIMEOUT,
                        )

                        return site, result

                    except asyncio.TimeoutError:
                        return (
                            site,
                            (
                                [],
                                "timeout",
                                {
                                    "method": "",
                                    "elapsed_ms": int(
                                        SITE_TIMEOUT * 1000
                                    ),
                                    "error": "Site search timeout",
                                },
                            ),
                        )

                    except Exception as exc:
                        return (
                            site,
                            (
                                [],
                                "error",
                                {
                                    "method": "",
                                    "elapsed_ms": 0,
                                    "error": (
                                        f"{type(exc).__name__}: {exc}"
                                    ),
                                },
                            ),
                        )

                tasks.append(
                    asyncio.create_task(one())
                )

            checked = 0
            total = len(tasks)

            for completed_task in asyncio.as_completed(tasks):
                site, result = await completed_task

                products, state, info = result

                checked += 1

                for product in products:
                    product["site_id"] = site["id"]
                    product["site"] = site["name"]
                    product.pop("_score", None)

                if products:
                    yield sse(
                        {
                            "type": "result",
                            "site_id": site["id"],
                            "site": site["name"],
                            "products": products,
                        }
                    )

                yield sse(
                    {
                        "type": "site_done",
                        "site_id": site["id"],
                        "site": site["name"],
                        "checked": checked,
                        "total": total,
                        "count": len(products),
                        "state": (
                            "done"
                            if state
                            in {
                                "found",
                                "not_found",
                                "unverified",
                            }
                            else "failed"
                        ),
                        "result": state,
                        "method": info.get("method"),
                        "elapsed_ms": info.get("elapsed_ms"),
                        "error": info.get("error"),
                    }
                )

            yield sse(
                {
                    "type": "done",
                    "checked": checked,
                    "total": total,
                }
            )

            try:
                await browser_start_task
            except Exception:
                pass

    finally:
        await browser.close()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "component-finder",
        "sites": len(SITES),
    }


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "camoufox_installed": AsyncCamoufox is not None,
        "sites": len(SITES),
    }


@app.get("/api/sites")
async def get_sites():
    return {
        "sites": [
            {
                "id": site["id"],
                "name": site["name"],
                "kind": site["kind"],
            }
            for site in SITES
        ]
    }


@app.get("/api/search")
async def search(
    q: str,
    sites: list[str] | None = Query(default=None),
):
    query = clean_text(q)

    if not query:
        return JSONResponse(
            {"error": "Query is required"},
            status_code=400,
        )

    return StreamingResponse(
        event_generator(
            query,
            sites,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
