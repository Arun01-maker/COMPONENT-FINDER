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

# Fast HTTP is tried first. Camoufox is started in parallel so it is ready when
# a distributor needs JavaScript/browser rendering.
HTTP_TIMEOUT = 4.5
SITE_TIMEOUT = 18.0
PRODUCT_TIMEOUT = 5.5
CAMOUFOX_START_TIMEOUT = 14.0
CAMOUFOX_PAGE_TIMEOUT = 9000
MAX_LISTING_RESULTS = 6
MAX_VERIFY_RESULTS = 6
MAX_CONCURRENT_SITES = 12
# Lower numbers are launched first. All sites still run concurrently once their
# priority batch starts, so fast structured endpoints can return immediately.
SITE_PRIORITY = {
    "robu": 0,
    "shopify": 1,
    "element14": 2,
    "opencart": 3,
    "woocommerce": 4,
    "magento": 5,
    "generic": 6,
}

SITES = [
    {"id": "01", "name": "ET Store", "kind": "shopify", "search": "https://www.etstore.in/search?q={q}", "origin": "https://www.etstore.in"},
    {"id": "02", "name": "Robu.in", "kind": "robu", "search": "https://robu.in/?s={q}&post_type=product", "origin": "https://robu.in"},
    {"id": "03", "name": "element14 India", "kind": "element14", "search": "https://in.element14.com/search?st={q}", "origin": "https://in.element14.com"},
    {"id": "04", "name": "ElectronicsComp", "kind": "opencart", "search": "https://www.electronicscomp.com/index.php?route=product/search&search={q}", "origin": "https://www.electronicscomp.com"},
    {"id": "05", "name": "Evelta", "kind": "magento", "search": "https://evelta.com/catalogsearch/result/?q={q}", "origin": "https://evelta.com"},
    {"id": "06", "name": "Tomson Electronics", "kind": "shopify", "search": "https://www.tomsonelectronics.com/search?q={q}", "origin": "https://www.tomsonelectronics.com"},
    {"id": "07", "name": "QuartzComponents", "kind": "shopify", "search": "https://quartzcomponents.com/search?q={q}", "origin": "https://quartzcomponents.com"},
    {"id": "08", "name": "MakerBazar", "kind": "shopify", "search": "https://makerbazar.in/search?q={q}", "origin": "https://makerbazar.in"},
    {"id": "09", "name": "Probots", "kind": "woocommerce", "search": "https://probots.co.in/?s={q}&post_type=product", "origin": "https://probots.co.in"},
    {"id": "10", "name": "Sharvi Electronics", "kind": "woocommerce", "search": "https://sharvielectronics.com/?s={q}&post_type=product", "origin": "https://sharvielectronics.com"},
    {"id": "11", "name": "Leeds Electronics Industry", "kind": "generic", "search": "https://www.leedsind.net/?s={q}", "origin": "https://www.leedsind.net"},
    {"id": "12", "name": "Sparefly", "kind": "shopify", "search": "https://sparefly.com/search?q={q}", "origin": "https://sparefly.com"},
]
SITE_BY_ID = {s["id"]: s for s in SITES}
SITE_BY_NAME = {s["name"].lower(): s for s in SITES}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Cache-Control": "no-cache",
}
ROBU_API = "https://robu.in/wp-json/wc/store/v1/products"


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def extract_part(query):
    tokens = clean_text(query).split()
    if not tokens:
        return ""
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
    if not re.search(r"\d", part):
        return all(t in nt for t in query_tokens(query)[:2])
    return True


def title_score(title, query):
    nt = normalize(title)
    nq = normalize(query)
    part = normalize(extract_part(query))
    score = 0
    if part and part in nt:
        score += 100
    if nq and nq in nt:
        score += 70
    for token in query_tokens(query):
        if token in nt:
            score += 12
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


def parse_availability(text):
    low = clean_text(text).lower()
    if not low:
        return "UNKNOWN", None

    patterns = [
        r"(?:availability|available|stock|quantity)\s*[:\-]?\s*([0-9][0-9,]*)",
        r"only\s+([0-9][0-9,]*)\s+(?:items?\s+)?in\s+stock",
        r"([0-9][0-9,]*)\s+(?:items?\s+)?in\s+stock",
        r"([0-9][0-9,]*)\s+available",
    ]
    for pattern in patterns:
        m = re.search(pattern, low, re.I)
        if m:
            qty = int(m.group(1).replace(",", ""))
            return ("IN_STOCK" if qty > 0 else "OUT_OF_STOCK"), qty

    if re.search(r"\bout\s*of\s*stock\b|\bsold\s*out\b|\bcurrently unavailable\b|\bunavailable\b", low):
        return "OUT_OF_STOCK", 0
    if re.search(r"\bavailable\s+to\s+order\b|\bavailable\s+on\s+order\b|\bback\s*order\b|\bpre[- ]?order\b", low):
        return "AVAILABLE_TO_ORDER", None
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
            if "instock" in av:
                qty = obj.get("inventoryLevel")
                if isinstance(qty, dict):
                    qty = qty.get("value")
                try:
                    qty = int(qty) if qty is not None else None
                except Exception:
                    qty = None
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

    # Look only at likely product/commerce regions first.
    selectors = [
        ".availability", ".stock", ".stock-status", ".product-stock", ".inventory",
        ".product-info", ".product-information", ".product-form", ".product__info-container",
        ".product-form__buttons", ".product-single__meta", ".summary", "form.cart", "main",
    ]
    best = ("UNKNOWN", None)
    for selector in selectors:
        for node in soup.select(selector)[:4]:
            txt = clean_text(node.get_text(" ", strip=True))
            state, qty = parse_availability(txt)
            if state == "IN_STOCK" and qty is not None:
                return state, qty
            if state == "OUT_OF_STOCK":
                best = (state, qty)
            elif state == "AVAILABLE_TO_ORDER" and best[0] == "UNKNOWN":
                best = (state, qty)
            elif state == "IN_STOCK" and best[0] == "UNKNOWN":
                best = (state, qty)

    # Active Add-to-cart is useful when a store does not print the word stock.
    buttons = []
    for node in soup.select("button, input[type=submit], a"):
        txt = clean_text(node.get("value") or node.get_text(" ", strip=True)).lower()
        if any(x in txt for x in ("add to cart", "buy now", "add to basket")):
            buttons.append(node)
    for node in buttons[:6]:
        disabled = node.has_attr("disabled") or str(node.get("aria-disabled", "")).lower() == "true"
        if disabled:
            continue
        cls = " ".join(node.get("class", [])).lower()
        if "disabled" not in cls:
            return "IN_STOCK", None

    # Explicit disabled/out-of-stock controls.
    body = clean_text(soup.get_text(" ", strip=True))
    if re.search(r"\bout\s*of\s*stock\b|\bsold\s*out\b", body, re.I):
        return "OUT_OF_STOCK", 0
    return best


def extract_price(block):
    node = block.select_one(".price, .product-price, .price-box, [class*=price], [data-price]") if block else None
    return clean_text(node.get_text(" ", strip=True)) if node else "N/A"


def candidate_from_anchor(anchor, site, query):
    href = anchor.get("href", "")
    link = absolute_url(site, href)
    if not valid_product_url(site, link):
        return None

    direct = clean_text(anchor.get_text(" ", strip=True))
    parent = anchor
    context = direct
    for _ in range(4):
        parent = parent.parent
        if not parent:
            break
        text = clean_text(parent.get_text(" ", strip=True))
        if len(text) >= len(direct) and len(text) <= 900:
            context = text
        title_node = parent.select_one("h1,h2,h3,h4,h5,.product-title,.product-name,.name,.woocommerce-loop-product__title")
        if title_node:
            t = clean_text(title_node.get_text(" ", strip=True))
            if len(t) >= 3:
                direct = t
                break

    title = direct or context[:250]
    if not component_match(title + " " + context[:400], query):
        return None
    score = title_score(title, query)
    if normalize(extract_part(query)) in normalize(href):
        score += 25
    return {"title": title[:350], "link": link, "price": extract_price(parent), "_score": score}


def extract_candidates(html_text, site, query):
    soup = BeautifulSoup(html_text, "html.parser")
    candidates = {}
    part = normalize(extract_part(query))

    # First pass: likely product cards.
    selectors = [
        "article", "li.product", ".product", ".product-item", ".product-card", ".product-thumb",
        ".grid-product", ".grid__item", ".card-wrapper", ".product-grid-item", ".search-result",
        ".product-listing", ".product-list-item",
    ]
    for selector in selectors:
        for block in soup.select(selector):
            links = block.select("a[href]")
            if not links:
                continue
            links.sort(key=lambda a: (0 if part and part in normalize(a.get_text(" ", strip=True)) else 1, len(a.get_text(" ", strip=True))))
            c = candidate_from_anchor(links[0], site, query)
            if c:
                c["price"] = extract_price(block)
                candidates[c["link"]] = c

    # Second pass: all matching product-looking anchors. This catches custom
    # distributor themes that do not use standard product-card classes.
    if len(candidates) < MAX_LISTING_RESULTS:
        for anchor in soup.select("a[href]"):
            txt = clean_text(anchor.get_text(" ", strip=True))
            href = anchor.get("href", "")
            if len(txt) < 3 and part not in normalize(href):
                continue
            if part and part not in normalize(txt + " " + href):
                continue
            c = candidate_from_anchor(anchor, site, query)
            if c:
                candidates.setdefault(c["link"], c)

    out = sorted(candidates.values(), key=lambda x: x["_score"], reverse=True)[:MAX_LISTING_RESULTS]
    for item in out:
        item.update({"availability_state": "UNKNOWN", "stock_quantity": None, "availability": "Availability Not Confirmed"})
    return out


async def fetch_http(client, url, timeout=HTTP_TIMEOUT):
    try:
        r = await client.get(url, timeout=timeout, follow_redirects=True)
        if r.status_code >= 400 or not r.text:
            return None
        return r.text
    except Exception as exc:
        print(f"HTTP failed {url}: {exc}")
        return None


class Browser:
    def __init__(self):
        self.cm = None
        self.browser = None
        self.lock = asyncio.Lock()

    async def start(self):
        if self.browser is not None or AsyncCamoufox is None:
            return self.browser
        async with self.lock:
            if self.browser is not None:
                return self.browser
            try:
                self.cm = AsyncCamoufox(headless=True, humanize=False, block_images=True)
                self.browser = await asyncio.wait_for(self.cm.__aenter__(), timeout=CAMOUFOX_START_TIMEOUT)
            except Exception as exc:
                print(f"Camoufox startup failed: {exc}")
                self.cm = None
                self.browser = None
        return self.browser

    async def fetch(self, url):
        browser = await self.start()
        if browser is None:
            return None
        page = None
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=CAMOUFOX_PAGE_TIMEOUT)
            await page.wait_for_timeout(250)
            return await page.content()
        except Exception as exc:
            print(f"Browser failed {url}: {exc}")
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


async def shopify_search(client, site, query):
    # Shopify's JSON suggestion endpoint is fast and does not require rendering.
    url = site["origin"].rstrip("/") + "/search/suggest.json"
    try:
        r = await client.get(url, params={
            "q": query,
            "resources[type]": "product",
            "resources[limit]": str(MAX_LISTING_RESULTS),
            "resources[options][unavailable_products]": "show",
        }, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        products = (((data.get("resources") or {}).get("results") or {}).get("products") or [])
        out = []
        for p in products:
            title = clean_text(p.get("title"))
            if not title or not component_match(title, query):
                continue
            link = absolute_url(site, p.get("url", ""))
            if not valid_product_url(site, link):
                continue
            out.append({
                "title": title,
                "link": link,
                "price": clean_text(str(p.get("price") or "N/A")),
                "availability_state": "UNKNOWN",
                "stock_quantity": None,
                "availability": "Availability Not Confirmed",
                "_score": title_score(title, query),
            })
        return sorted(out, key=lambda x: x["_score"], reverse=True)[:MAX_LISTING_RESULTS]
    except Exception as exc:
        print(f"Shopify search failed {site['name']}: {exc}")
        return []


async def robu_search(client, query):
    try:
        r = await client.get(ROBU_API, params={"search": query, "per_page": MAX_LISTING_RESULTS}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        out = []
        for p in data if isinstance(data, list) else []:
            title = clean_text(p.get("name"))
            link = p.get("permalink")
            if not title or not link or not component_match(title, query):
                continue
            prices = p.get("prices") or {}
            price = clean_text(BeautifulSoup(str(prices.get("price_html") or prices.get("price") or ""), "html.parser").get_text(" ", strip=True)) or "N/A"
            if p.get("is_in_stock") is True:
                state = "IN_STOCK"
            elif p.get("is_in_stock") is False:
                state = "OUT_OF_STOCK"
            elif p.get("is_on_backorder"):
                state = "AVAILABLE_TO_ORDER"
            else:
                state = "UNKNOWN"
            qty = None
            stock = p.get("stock_availability") or {}
            if state == "UNKNOWN":
                state, qty = parse_availability(clean_text(stock.get("text")))
            out.append({"title": title, "link": link, "price": price, "availability_state": state, "stock_quantity": qty, "availability": availability_label(state, qty), "_score": title_score(title, query)})
        return sorted(out, key=lambda x: x["_score"], reverse=True)[:MAX_LISTING_RESULTS]
    except Exception as exc:
        print(f"Robu API failed: {exc}")
        return []


def site_search_urls(site, query):
    q = quote_plus(query)
    origin = site["origin"].rstrip("/")
    urls = [site["search"].format(q=q)]
    if site["kind"] in {"woocommerce", "generic"}:
        for u in [
            f"{origin}/?s={q}&post_type=product",
            f"{origin}/?post_type=product&s={q}",
            f"{origin}/search/?q={q}",
            f"{origin}/search?q={q}",
            f"{origin}/index.php?route=product/search&search={q}",
        ]:
            if u not in urls:
                urls.append(u)
    if site["kind"] == "shopify":
        u = f"{origin}/search?q={q}&type=product"
        if u not in urls:
            urls.append(u)
    return urls


async def verify_product(client, browser, product, site):
    html = await fetch_http(client, product["link"], PRODUCT_TIMEOUT)
    state = qty = None
    if html:
        state, qty = availability_from_html(html)

    # If HTTP cannot prove availability, render the actual product page. This is
    # deliberately done for UNKNOWN as well as failed HTTP pages.
    if state == "UNKNOWN" or html is None:
        browser_html = await browser.fetch(product["link"])
        if browser_html:
            bstate, bqty = availability_from_html(browser_html)
            if bstate != "UNKNOWN" or state == "UNKNOWN":
                state, qty = bstate, bqty

    if state == "UNKNOWN":
        return False
    product["availability_state"] = state
    product["stock_quantity"] = qty
    product["availability"] = availability_label(state, qty)
    product["verified"] = True
    return True


async def verify_products(client, browser, products, site):
    # Verify all candidates concurrently. Product-page verification is the source
    # of truth; a search-result badge is never enough.
    await asyncio.gather(*(verify_product(client, browser, p, site) for p in products[:MAX_VERIFY_RESULTS]))
    return [p for p in products if p.get("verified")]


async def search_site(client, browser, site, query):
    # 1. Use structured distributor endpoints where available.
    products = []
    if site["kind"] == "robu":
        products = await robu_search(client, query)
    elif site["kind"] == "shopify":
        products = await shopify_search(client, site, query)

    # 2. Direct website search. If the fast HTML result has no match, browser
    # rendering performs the same search URL inside the actual website.
    if not products:
        for url in site_search_urls(site, query):
            html = await fetch_http(client, url)
            if html:
                products = extract_candidates(html, site, query)
            if products:
                break

    if not products:
        for url in site_search_urls(site, query)[:3]:
            html = await browser.fetch(url)
            if html:
                products = extract_candidates(html, site, query)
            if products:
                break

    if not products:
        return [], "not_found"

    verified = await verify_products(client, browser, products, site)
    return verified, "found" if verified else "found_unverified"


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
        sites = list(SITES)

    # Fastest-first ordering. Structured/API and Shopify searches are launched
    # before browser-heavy generic sites, while the whole batch remains concurrent.
    sites.sort(key=lambda s: (SITE_PRIORITY.get(s["kind"], 99), int(s["id"])))

    yield sse({"type": "init", "sites": [{"id": s["id"], "name": s["name"], "kind": s["kind"]} for s in sites]})
    browser = Browser()
    # Start browser while the fast HTTP searches are running.
    browser_task = asyncio.create_task(browser.start())

    try:
        limits = httpx.Limits(max_connections=40, max_keepalive_connections=20)
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, limits=limits) as client:
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_SITES)

            async def run_one(site):
                async with semaphore:
                    yield_data = {"type": "site_status", "site_id": site["id"], "state": "searching"}
                    # Status is emitted by the outer loop before result completion.
                    try:
                        products, state = await asyncio.wait_for(search_site(client, browser, site, query), timeout=SITE_TIMEOUT)
                        return site, products, state, None
                    except asyncio.TimeoutError:
                        return site, [], "timeout", "timeout"
                    except Exception as exc:
                        print(f"{site['name']} error: {exc}")
                        return site, [], "error", str(exc)

            tasks = [asyncio.create_task(run_one(site)) for site in sites]
            for site in sites:
                yield sse({"type": "site_status", "site_id": site["id"], "site": site["name"], "state": "searching"})

            checked = 0
            total = len(sites)
            for task in asyncio.as_completed(tasks):
                site, products, state, error = await task
                checked += 1
                if products:
                    for p in products:
                        p["site_id"] = site["id"]
                        p["site"] = site["name"]
                        p.pop("_score", None)
                    yield sse({"type": "result", "site_id": site["id"], "site": site["name"], "products": products})
                yield sse({
                    "type": "site_done",
                    "site_id": site["id"],
                    "site": site["name"],
                    "checked": checked,
                    "total": total,
                    "count": len(products),
                    "state": "done" if state in {"found", "not_found", "found_unverified"} else "failed",
                    "result": state,
                })

            yield sse({"type": "done", "checked": checked, "total": total})
            try:
                await browser_task
            except Exception:
                pass
    finally:
        await browser.close()


@app.get("/")
async def root():
    return {"status": "ok", "service": "component-finder", "sites": len(SITES)}


@app.get("/api/sites")
async def get_sites():
    return {"sites": [{"id": s["id"], "name": s["name"]} for s in SITES]}


@app.get("/api/search")
async def search(q: str, sites: list[str] | None = Query(default=None)):
    query = clean_text(q)
    if not query:
        return JSONResponse({"error": "Query is required"}, status_code=400)
    return StreamingResponse(
        event_generator(query, sites),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
