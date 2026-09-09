import asyncio
import json
import re
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

# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------
# Search is HTTP-first. Camoufox is started ONLY if a normal request fails.
HTTP_TIMEOUT = 5.0
SITE_TIMEOUT = 7.0
PRODUCT_TIMEOUT = 4.5
CAMOUFOX_START_TIMEOUT = 12.0
CAMOUFOX_PAGE_TIMEOUT = 8000
MAX_LISTING_RESULTS = 5
MAX_VERIFY_RESULTS = 5
MAX_CONCURRENT_SITES = 10

SITES = [
    {"id": "01", "name": "ET Store", "kind": "generic", "search": "https://etstore.in/index.php?route=product/search&search={q}", "origin": "https://etstore.in"},
    {"id": "02", "name": "Robu.in", "kind": "robu", "search": "https://robu.in/?s={q}&post_type=product", "origin": "https://robu.in"},
    {"id": "03", "name": "element14", "kind": "element14", "search": "https://in.element14.com/search?st={q}", "origin": "https://in.element14.com"},
    {"id": "04", "name": "ElectronicsComp", "kind": "generic", "search": "https://www.electronicscomp.com/index.php?route=product/search&search={q}", "origin": "https://www.electronicscomp.com"},
    {"id": "05", "name": "Evelta", "kind": "generic", "search": "https://evelta.com/catalogsearch/result/?q={q}", "origin": "https://evelta.com"},
    {"id": "06", "name": "Tomson Electronics", "kind": "shopify", "search": "https://www.tomsonelectronics.com/search?q={q}", "origin": "https://www.tomsonelectronics.com"},
    {"id": "07", "name": "QuartzComponents", "kind": "shopify", "search": "https://quartzcomponents.com/search?q={q}", "origin": "https://quartzcomponents.com"},
    {"id": "08", "name": "MakerBazar", "kind": "shopify", "search": "https://makerbazar.in/search?q={q}", "origin": "https://makerbazar.in"},
    {"id": "09", "name": "Probots", "kind": "shopify", "search": "https://probots.co.in/search?q={q}", "origin": "https://probots.co.in"},
    {"id": "10", "name": "Sharvi Electronics", "kind": "shopify", "search": "https://sharvielectronics.com/search?q={q}", "origin": "https://sharvielectronics.com"},
    {"id": "11", "name": "Leeds Electronics", "kind": "generic", "search": "https://www.leedsind.com/?s={q}", "origin": "https://www.leedsind.com"},
    {"id": "12", "name": "Sparefly", "kind": "shopify", "search": "https://sparefly.com/search?q={q}", "origin": "https://sparefly.com"},
]

SITE_BY_ID = {s["id"]: s for s in SITES}
SITE_BY_NAME = {s["name"].lower(): s for s in SITES}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}

ROBU_API = "https://robu.in/wp-json/wc/store/v1/products"


# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------
def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


def normalize(value):
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def extract_part(query):
    """LM2596 3A -> LM2596, ESP32 3.3V -> ESP32, Arduino Nano -> Arduino Nano."""
    q = clean_text(query)
    tokens = q.split()
    if not tokens:
        return ""

    # Prefer a mixed letter+number identifier such as LM2596 or ESP32.
    # Do not accidentally treat a package/size token such as 0805 as the
    # component identifier for queries like "Resistor SMD 0805".
    for token in tokens:
        token = token.strip(" ,;:/()[]{}")
        if re.search(r"[A-Za-z]", token) and re.search(r"\d", token):
            return token

    return " ".join(tokens[:2])


def query_tokens(query):
    return [x for x in normalize(query).split() if len(x) > 1]


def component_match(title, query):
    nt = normalize(title)
    part = normalize(extract_part(query))
    if not part or part not in nt:
        return False

    # Text names need their first two words; numeric part numbers use the part ID.
    if not re.search(r"\d", part):
        return all(token in nt for token in query_tokens(query)[:2])
    return True


def title_score(title, query):
    nt = normalize(title)
    nq = normalize(query)
    part = normalize(extract_part(query))
    score = 0
    if part and part in nt:
        score += 100
    if nq and nq in nt:
        score += 60
    for token in query_tokens(query):
        if token in nt:
            score += 10
    return score


def absolute_url(site, href):
    return urljoin(site["origin"], href or "")


def valid_product_url(site, link):
    try:
        a = urlparse(link)
        b = urlparse(site["origin"])
        return a.scheme in ("http", "https") and a.netloc == b.netloc
    except Exception:
        return False


def extract_price(block):
    if not block or not hasattr(block, "select_one"):
        return "N/A"
    node = block.select_one(
        ".price, .product-price, .price-box, [class*=price], [data-price]"
    )
    return clean_text(node.get_text(" ", strip=True)) if node else "N/A"


# -----------------------------------------------------------------------------
# AVAILABILITY PARSING
# -----------------------------------------------------------------------------
def parse_availability(text):
    low = clean_text(text).lower()
    if not low:
        return "UNKNOWN", None

    # Numeric quantity first.
    numeric_patterns = [
        r"(?:availability|available|stock|quantity)\s*[:\-]?\s*([0-9][0-9,]*)",
        r"([0-9][0-9,]*)\s+(?:in\s+stock|units?\s+in\s+stock|pcs?\s+in\s+stock)",
        r"only\s+([0-9][0-9,]*)\s+in\s+stock",
        r"([0-9][0-9,]*)\s+available",
    ]
    for pattern in numeric_patterns:
        match = re.search(pattern, low, re.I)
        if match:
            qty = int(match.group(1).replace(",", ""))
            return ("IN_STOCK" if qty > 0 else "OUT_OF_STOCK"), qty

    if re.search(r"\bout\s*of\s*stock\b|\bsold\s*out\b|\bcurrently unavailable\b|\bunavailable\b", low):
        return "OUT_OF_STOCK", 0

    if re.search(r"\bavailable\s+to\s+order\b|\bavailable\s+on\s+order\b|\bback\s*order\b|\bpre[- ]?order\b", low):
        return "AVAILABLE_TO_ORDER", None

    if re.search(r"\bin\s*stock\b|\bin-stock\b", low):
        return "IN_STOCK", None

    return "UNKNOWN", None


def structured_availability(soup):
    """Read schema.org Product/Offer availability without scanning the whole page."""
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
            if not isinstance(obj, dict):
                continue

            graph = obj.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)

            offers = obj.get("offers")
            if isinstance(offers, list):
                stack.extend(offers)
            elif isinstance(offers, dict):
                stack.append(offers)

            availability = str(obj.get("availability", "")).lower()
            if "instock" in availability:
                qty = obj.get("inventoryLevel")
                if isinstance(qty, dict):
                    qty = qty.get("value")
                try:
                    qty = int(qty) if qty is not None else None
                except Exception:
                    qty = None
                return "IN_STOCK", qty
            if "outofstock" in availability:
                return "OUT_OF_STOCK", 0
            if "backorder" in availability or "preorder" in availability:
                return "AVAILABLE_TO_ORDER", None

    return "UNKNOWN", None


def availability_from_html(html_text):
    soup = BeautifulSoup(html_text, "html.parser")

    state, qty = structured_availability(soup)
    if state != "UNKNOWN":
        return state, qty

    # Important: do NOT scan the whole page for "add to cart". That often causes
    # false positives from recommendations/header widgets. Use product regions only.
    selectors = [
        ".availability", ".stock", ".stock-status", ".product-stock",
        ".inventory", ".product-info", ".product-information", ".product-form",
        ".summary", "form.cart", "main",
    ]

    best = ("UNKNOWN", None)
    for selector in selectors:
        for node in soup.select(selector)[:3]:
            state, qty = parse_availability(node.get_text(" ", strip=True))
            if state == "IN_STOCK" and qty is not None:
                return state, qty
            if state == "OUT_OF_STOCK":
                best = (state, qty)
            elif state == "AVAILABLE_TO_ORDER" and best[0] == "UNKNOWN":
                best = (state, qty)
            elif state == "IN_STOCK" and best[0] == "UNKNOWN":
                best = (state, qty)

    return best


def availability_label(state, qty):
    if state == "IN_STOCK":
        return f"In Stock ({qty:,})" if qty is not None else "In Stock"
    if state == "OUT_OF_STOCK":
        return "Out of Stock"
    if state == "AVAILABLE_TO_ORDER":
        return "Available to Order"
    return "Unavailable"


# -----------------------------------------------------------------------------
# PRODUCT EXTRACTION
# -----------------------------------------------------------------------------
def product_blocks(soup, kind):
    if kind == "robu":
        selectors = [
            "li.product", ".product-small", ".product-grid-item",
            ".product-type-simple", ".products .product",
        ]
    elif kind == "element14":
        selectors = [".product-listing", ".product-item", ".search-result", "article"]
    elif kind == "shopify":
        selectors = [
            ".product-card", ".product-grid-item", ".grid-product",
            ".product-item", ".card-wrapper", "li.grid__item", "article",
        ]
    else:
        selectors = [
            ".product-thumb", ".product-item", ".product-card",
            ".product-grid-item", ".product", "article", "li",
        ]

    blocks = []
    seen = set()
    for selector in selectors:
        for block in soup.select(selector):
            marker = id(block)
            if marker not in seen:
                seen.add(marker)
                blocks.append(block)
    return blocks


def extract_products(html_text, site, query):
    soup = BeautifulSoup(html_text, "html.parser")
    candidates = []
    part = normalize(extract_part(query))

    for block in product_blocks(soup, site["kind"]):
        links = block.select("a[href]")
        if not links:
            continue

        # Product link whose anchor text best matches the part number/name.
        links.sort(key=lambda a: (
            0 if part and part in normalize(a.get_text(" ", strip=True)) else 1,
            len(a.get_text(" ", strip=True)),
        ))
        link_node = links[0]
        link = absolute_url(site, link_node.get("href", ""))
        if not valid_product_url(site, link):
            continue

        title_node = block.select_one(
            "h1,h2,h3,h4,h5,.product-title,.product-name,.name,"
            ".woocommerce-loop-product__title,.caption h4 a"
        )
        title = clean_text(
            title_node.get_text(" ", strip=True)
            if title_node else link_node.get_text(" ", strip=True)
        )

        if len(title) < 3 or len(title) > 350:
            continue
        if not component_match(title, query):
            continue

        local_text = clean_text(block.get_text(" ", strip=True))
        state, qty = parse_availability(local_text)

        candidates.append({
            "title": title,
            "link": link,
            "price": extract_price(block),
            "availability_state": state,
            "stock_quantity": qty,
            "availability": availability_label(state, qty),
            "_score": title_score(title, query),
        })

    # Fallback only when normal product cards were not detected.
    if not candidates:
        for anchor in soup.select("a[href]"):
            title = clean_text(anchor.get_text(" ", strip=True))
            if not title or len(title) < 3 or len(title) > 220:
                continue
            if not component_match(title, query):
                continue
            link = absolute_url(site, anchor.get("href", ""))
            if not valid_product_url(site, link):
                continue
            candidates.append({
                "title": title,
                "link": link,
                "price": "N/A",
                "availability_state": "UNKNOWN",
                "stock_quantity": None,
                "availability": "Unavailable",
                "_score": title_score(title, query),
            })

    unique = {}
    for item in candidates:
        unique[item["link"]] = item

    return sorted(unique.values(), key=lambda x: x["_score"], reverse=True)[:MAX_LISTING_RESULTS]


# -----------------------------------------------------------------------------
# HTTP + LAZY CAMOUFOX
# -----------------------------------------------------------------------------
async def fetch_http(client, url, timeout=HTTP_TIMEOUT):
    try:
        response = await client.get(url, timeout=timeout, follow_redirects=True)
        if response.status_code >= 400 or not response.text:
            return None
        return response.text
    except Exception as exc:
        print(f"HTTP failed: {url} -> {exc}")
        return None


class BrowserFallback:
    def __init__(self):
        self.cm = None
        self.browser = None
        self.lock = asyncio.Lock()

    async def get(self):
        # Camoufox is NEVER started during normal startup.
        if self.browser is not None:
            return self.browser
        if AsyncCamoufox is None:
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
                    timeout=CAMOUFOX_START_TIMEOUT,
                )
            except Exception as exc:
                print(f"Camoufox startup failed: {exc}")
                self.cm = None
                self.browser = None

        return self.browser

    async def close(self):
        if self.cm is not None:
            try:
                await self.cm.__aexit__(None, None, None)
            except Exception:
                pass
        self.cm = None
        self.browser = None


async def camoufox_fetch(browser, url):
    if browser is None:
        return None
    page = None
    try:
        page = await browser.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=CAMOUFOX_PAGE_TIMEOUT)
        return await page.content()
    except Exception as exc:
        print(f"Camoufox page failed: {url} -> {exc}")
        return None
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


# -----------------------------------------------------------------------------
# SHOPIFY FAST PATH
# -----------------------------------------------------------------------------
async def search_shopify(client, site, query):
    """Use Shopify's search-suggest JSON when available. This is more reliable
    than scraping a JS-rendered search page and still verifies the product page
    before the final result is returned.
    """
    url = site["origin"].rstrip("/") + "/search/suggest.json"
    params = {
        "q": query,
        "resources[type]": "product",
        "resources[limit]": str(MAX_LISTING_RESULTS),
        "resources[options][unavailable_products]": "show",
    }
    try:
        response = await client.get(url, params=params, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        products = ((data.get("resources") or {}).get("results") or {}).get("products") or []
        results = []
        for item in products:
            title = clean_text(item.get("title", ""))
            if not title or not component_match(title, query):
                continue
            link = absolute_url(site, item.get("url", ""))
            if not valid_product_url(site, link):
                continue
            state = "IN_STOCK" if item.get("available") is True else (
                "OUT_OF_STOCK" if item.get("available") is False else "UNKNOWN"
            )
            results.append({
                "title": title,
                "link": link,
                "price": clean_text(str(item.get("price", "") or "N/A")),
                "availability_state": state,
                "stock_quantity": None,
                "availability": availability_label(state, None),
                "_score": title_score(title, query),
            })
        return sorted(results, key=lambda x: x["_score"], reverse=True)[:MAX_LISTING_RESULTS]
    except Exception as exc:
        print(f"Shopify JSON failed for {site['name']}: {exc}")
        return None


# -----------------------------------------------------------------------------
# ROBU FAST PATH
# -----------------------------------------------------------------------------
async def search_robu_api(client, query):
    try:
        response = await client.get(
            ROBU_API,
            params={
                "search": query,
                "per_page": MAX_LISTING_RESULTS,
                "catalog_visibility": "visible",
            },
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            return None

        results = []
        for item in data:
            title = clean_text(item.get("name", ""))
            link = item.get("permalink", "")
            if not title or not link or not component_match(title, query):
                continue

            prices = item.get("prices") or {}
            raw_price = prices.get("price_html") or prices.get("price") or ""
            price = clean_text(
                BeautifulSoup(str(raw_price), "html.parser").get_text(" ", strip=True)
            )

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
                state, qty = parse_availability(clean_text(stock.get("text", "")))

            results.append({
                "title": title,
                "link": link,
                "price": price or "N/A",
                "availability": availability_label(state, qty),
                "availability_state": state,
                "stock_quantity": qty,
                "_score": title_score(title, query),
            })

        return sorted(results, key=lambda x: x["_score"], reverse=True)[:MAX_LISTING_RESULTS]
    except Exception as exc:
        print(f"Robu API failed: {exc}")
        return None


# -----------------------------------------------------------------------------
# SEARCH ONE SITE
# -----------------------------------------------------------------------------
async def verify_unknown_products(client, browser_fallback, products):
    # Verify every returned product page (up to MAX_VERIFY_RESULTS). This makes
    # the product page, rather than the search result card, the final source of
    # availability whenever the page exposes an explicit state.
    to_verify = products[:MAX_VERIFY_RESULTS]
    if not to_verify:
        return

    async def verify(product):
        page_html = await fetch_http(client, product["link"], timeout=PRODUCT_TIMEOUT)
        if page_html is None:
            try:
                browser = await browser_fallback.get()
                page_html = await camoufox_fetch(browser, product["link"])
            except Exception:
                page_html = None

        if page_html:
            state, qty = availability_from_html(page_html)
            if state != "UNKNOWN":
                product["availability_state"] = state
                product["stock_quantity"] = qty
                product["availability"] = availability_label(state, qty)

    await asyncio.gather(*(verify(p) for p in to_verify))


async def search_site(client, browser_fallback, site, query):
    # Shopify stores expose a structured product search endpoint. Use it first.
    if site["kind"] == "shopify":
        shopify_results = await search_shopify(client, site, query)
        if shopify_results:
            products = shopify_results
        else:
            products = []
    # Robu: structured API is the fastest availability source.
    elif site["kind"] == "robu":
        api_results = await search_robu_api(client, query)
        products = api_results or []
    else:
        products = []

    # Normal HTML search is the next path for non-Shopify sites, and a fallback
    # for Shopify stores whose JSON search endpoint did not return products.
    if not products:
        search_url = site["search"].format(q=quote_plus(query))
        html_text = await fetch_http(client, search_url)

        # If HTTP returned a JS shell or an incomplete search page, retry the
        # search itself in Camoufox. Previously this fallback happened only when
        # HTTP failed, which caused many valid products to appear as "not found".
        if html_text is not None:
            products = extract_products(html_text, site, query)
        if not products:
            browser = await browser_fallback.get()
            html_text = await camoufox_fetch(browser, search_url)
            if html_text:
                products = extract_products(html_text, site, query)

    if not products:
        return []

    # Verify the actual product pages. A search-card badge is not treated as
    # authoritative when the product page gives a different explicit state.
    await verify_unknown_products(client, browser_fallback, products)

    verified = [p for p in products if p["availability_state"] != "UNKNOWN"]
    for p in verified:
        p.pop("_score", None)
    return verified


# -----------------------------------------------------------------------------
# SSE STREAM
# -----------------------------------------------------------------------------
def sse(data):
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def event_generator(query, selected_sites=None):
    if selected_sites:
        wanted = set()
        for value in selected_sites:
            value = value.strip()
            if value in SITE_BY_ID:
                wanted.add(value)
            elif value.lower() in SITE_BY_NAME:
                wanted.add(SITE_BY_NAME[value.lower()]["id"])
        sites = [s for s in SITES if s["id"] in wanted]
    else:
        sites = SITES

    yield sse({
        "type": "init",
        "sites": [{"id": s["id"], "name": s["name"]} for s in sites],
    })

    browser_fallback = BrowserFallback()

    try:
        limits = httpx.Limits(max_connections=24, max_keepalive_connections=12)
        async with httpx.AsyncClient(
            headers=HEADERS,
            follow_redirects=True,
            limits=limits,
        ) as client:
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_SITES)

            async def run_one(site):
                async with semaphore:
                    try:
                        products = await asyncio.wait_for(
                            search_site(client, browser_fallback, site, query),
                            timeout=SITE_TIMEOUT,
                        )
                        return site, products, None
                    except asyncio.TimeoutError:
                        print(f"{site['name']} timed out")
                        return site, [], "timeout"
                    except Exception as exc:
                        print(f"{site['name']} error: {exc}")
                        return site, [], "error"

            tasks = [asyncio.create_task(run_one(site)) for site in sites]
            checked = 0
            total = len(sites)

            for task in asyncio.as_completed(tasks):
                site, products, error = await task
                checked += 1

                yield sse({
                    "type": "site_done",
                    "site_id": site["id"],
                    "checked": checked,
                    "total": total,
                    "count": len(products),
                    "state": "done" if error is None else "failed",
                })

                if products:
                    for product in products:
                        product["site"] = site["name"]
                    yield sse({
                        "type": "result",
                        "site_id": site["id"],
                        "site": site["name"],
                        "products": products,
                    })

            yield sse({"type": "done", "checked": checked, "total": total})

    finally:
        await browser_fallback.close()


# -----------------------------------------------------------------------------
# API ENDPOINTS
# -----------------------------------------------------------------------------
@app.get("/")
async def root():
    return JSONResponse({"status": "ok", "service": "component-finder"})


@app.get("/api/sites")
async def get_sites():
    return {
        "sites": [{"id": s["id"], "name": s["name"]} for s in SITES]
    }


@app.get("/api/search")
async def search(
    q: str,
    sites: list[str] | None = Query(default=None),
):
    query = clean_text(q)
    if not query:
        return JSONResponse({"error": "Query is required"}, status_code=400)

    return StreamingResponse(
        event_generator(query, sites),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
