import asyncio
import json
import re
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from camoufox.async_api import AsyncCamoufox


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)


# ============================================================
# URLS
# ============================================================

ROBU_SEARCH = "https://robu.in/?s={query}&post_type=product"
ET_SEARCH = "https://etstore.in/index.php?route=product/search&search={query}"

ROBU_BASE = "https://robu.in/"


# ============================================================
# HEADERS
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


# ============================================================
# BASIC HELPERS
# ============================================================

def clean_text(value):
    return " ".join(str(value or "").split())


def normalize_url(url, base):
    return urljoin(base, clean_text(url))


def normalize_component(value):
    value = clean_text(value).lower()

    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def compact_component(value):
    return re.sub(r"[^a-z0-9]", "", normalize_component(value))


# ============================================================
# COMPONENT NAME EXTRACTION
# ============================================================

def extract_component_name(query):
    """
    Extract the actual component/part name from the complete
    frontend query.

    Examples:

        LM2596 SMD 3A
        -> LM2596

        Arduino Nano 5V
        -> Arduino Nano

        NE555 DIP-8
        -> NE555

        10K Resistor 0805
        -> 10K Resistor
    """

    original = clean_text(query)

    if not original:
        return ""

    value = original

    # Remove common mount/package/specification words.
    remove_words = [
        "smd",
        "tht",
        "through hole",
        "through-hole",
        "dip",
        "soic",
        "sop",
        "qfn",
        "qfp",
        "to220",
        "to-220",
        "to263",
        "to-263",
        "to92",
        "to-92",
        "dfn",
        "bga",
        "lga",
        "msop",
        "tssop",
        "ssop",
        "d2pak",
        "dpaK",
        "module",
        "ic",
    ]

    for word in remove_words:
        value = re.sub(
            r"\b" + re.escape(word) + r"\b",
            " ",
            value,
            flags=re.IGNORECASE,
        )

    # Remove common electrical specification values.
    value = re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:v|volt|volts)\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:a|amp|amps)\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:ma|milliamp|milliamps)\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:w|watt|watts)\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )

    # Remove package dimensions such as 0805.
    value = re.sub(
        r"\b\d{4}\b",
        " ",
        value,
    )

    value = re.sub(r"\s+", " ", value).strip()

    if not value:
        return original

    return value


# ============================================================
# STRICT PRODUCT MATCHING
# ============================================================

def product_matches_component(title, component_name):
    title_norm = normalize_component(title)
    component_norm = normalize_component(component_name)

    if not title_norm or not component_norm:
        return False

    # Exact phrase match.
    if component_norm in title_norm:
        return True

    # Compact part-number match.
    title_compact = compact_component(title)
    component_compact = compact_component(component_name)

    if component_compact and component_compact in title_compact:
        return True

    return False


def filter_matching_products(products, query):
    component_name = extract_component_name(query)

    filtered = []
    seen = set()

    for product in products:
        title = clean_text(product.get("title", ""))
        link = clean_text(product.get("link", ""))

        if not title or not link:
            continue

        if link in seen:
            continue

        if product_matches_component(title, component_name):
            seen.add(link)
            filtered.append(product)

    return filtered[:30]


# ============================================================
# AVAILABILITY
# ============================================================

def availability_from_text(text):
    text = clean_text(text).lower()

    # Check unavailable states FIRST.
    if any(
        x in text
        for x in (
            "out of stock",
            "sold out",
            "currently unavailable",
            "not available",
            "unavailable",
            "out-of-stock",
        )
    ):
        return "Out of Stock"

    if any(
        x in text
        for x in (
            "add to cart",
            "add-to-cart",
            "buy now",
            "in stock",
            "available",
        )
    ):
        return "In Stock"

    return "Availability Unknown"


def extract_price(card):
    price = card.select_one(
        ".price, "
        ".woocommerce-Price-amount, "
        ".amount, "
        "[class*='price']"
    )

    if price:
        return clean_text(price.get_text(" ", strip=True))

    return "N/A"


# ============================================================
# PRODUCT TITLE EXTRACTION
# ============================================================

def extract_title(card, link):
    selectors = (
        ".name a",
        ".name",
        ".product-title",
        ".woocommerce-loop-product__title",
        "h2 a",
        "h2",
        "h3 a",
        "h3",
        "h4 a",
        "h4",
    )

    for selector in selectors:
        element = card.select_one(selector)

        if element:
            title = clean_text(
                element.get_text(" ", strip=True)
            )

            if title:
                return title

    title = clean_text(
        link.get("aria-label", "")
    )

    if title:
        return title

    return clean_text(
        link.get_text(" ", strip=True)
    )


# ============================================================
# FIND PRODUCT CARD
# ============================================================

def find_product_card(link):
    for selector in (
        "li.product",
        ".product-small",
        ".product-type-simple",
        ".product-type-variable",
        "article.product",
        ".product",
    ):
        card = link.find_parent(selector)

        if card:
            return card

    parent = link.parent

    for _ in range(6):
        if parent is None:
            break

        text = clean_text(
            parent.get_text(" ", strip=True)
        )

        if 20 <= len(text) <= 4000:
            lower = text.lower()

            if (
                "₹" in text
                or "add to cart" in lower
                or "read more" in lower
                or "wishlist" in lower
                or "in stock" in lower
                or "out of stock" in lower
            ):
                return parent

        parent = parent.parent

    return link.parent


# ============================================================
# ROBU HTML PARSER
# ============================================================

def parse_robu_html(html):
    soup = BeautifulSoup(html, "html.parser")

    results = []
    seen = set()

    cards = soup.select(
        "li.product, "
        ".product-small, "
        ".product-type-simple, "
        ".product-type-variable, "
        "article.product, "
        ".products .product"
    )

    for card in cards:
        link = card.select_one(
            "a[href*='/product/'], "
            "a.woocommerce-LoopProduct-link[href]"
        )

        if not link:
            continue

        href = normalize_url(
            link.get("href", ""),
            ROBU_BASE,
        )

        if "/product/" not in href:
            continue

        if href in seen:
            continue

        title = extract_title(card, link)

        if not title or len(title) < 2:
            continue

        seen.add(href)

        card_text = clean_text(
            card.get_text(" ", strip=True)
        )

        results.append(
            {
                "title": title,
                "link": href,
                "price": extract_price(card),
                "availability": availability_from_text(
                    card_text
                ),
            }
        )

    # Broad fallback for theme changes.
    if not results:
        for link in soup.select(
            "a[href*='/product/']"
        ):
            href = normalize_url(
                link.get("href", ""),
                ROBU_BASE,
            )

            if not href or href in seen:
                continue

            title = clean_text(
                link.get_text(" ", strip=True)
            )

            if not title or len(title) < 3:
                title = clean_text(
                    link.get("aria-label", "")
                )

            if (
                not title
                or len(title) < 3
                or len(title) > 300
            ):
                continue

            card = find_product_card(link)

            if not card:
                continue

            card_text = clean_text(
                card.get_text(" ", strip=True)
            )

            seen.add(href)

            results.append(
                {
                    "title": title,
                    "link": href,
                    "price": extract_price(card),
                    "availability": availability_from_text(
                        card_text
                    ),
                }
            )

    return results[:50]


# ============================================================
# ET STORE PARSER
# ============================================================

def parse_et_html(html):
    soup = BeautifulSoup(html, "html.parser")

    results = []
    seen = set()

    for card in soup.select(".product-thumb"):
        link = card.select_one(
            ".caption h4 a, h4 a, a[href]"
        )

        if not link:
            continue

        href = normalize_url(
            link.get("href", ""),
            "https://etstore.in/",
        )

        title = clean_text(
            link.get_text(" ", strip=True)
        )

        if not title or not href:
            continue

        if href in seen:
            continue

        seen.add(href)

        results.append(
            {
                "title": title,
                "link": href,
                "price": extract_price(card),
                "availability": availability_from_text(
                    card.get_text(
                        " ",
                        strip=True
                    )
                ),
            }
        )

        if len(results) >= 30:
            break

    return results


# ============================================================
# ROBU CATEGORY DETECTION
# ============================================================

def robu_category_urls(query):
    q = normalize_component(query)

    urls = []

    # 555 / timer IC
    if any(
        x in q
        for x in (
            "555",
            "timer",
            "timer ic",
        )
    ):
        urls.append(
            "https://robu.in/product-category/"
            "clock-and-timer-ic/"
        )

    # LM2596 / buck converters
    if any(
        x in q
        for x in (
            "lm2596",
            "lm2576",
            "lm2595",
            "buck converter",
            "step down",
        )
    ):
        urls.append(
            "https://robu.in/product-category/"
            "buck-converter/"
        )

        urls.append(
            "https://robu.in/product-category/"
            "switching-ic/"
        )

    # Arduino / development boards
    if any(
        x in q
        for x in (
            "arduino",
            "esp32",
            "esp8266",
            "development board",
            "dev board",
        )
    ):
        urls.append(
            "https://robu.in/product-category/"
            "development-boards/"
        )

    # Resistors
    if "resistor" in q or re.search(
        r"\b\d+(?:\.\d+)?\s*[kKmM]?\s*(?:ohm|r)\b",
        q,
    ):
        urls.append(
            "https://robu.in/product-category/"
            "resistors/"
        )

    # Capacitors
    if "capacitor" in q:
        urls.append(
            "https://robu.in/product-category/"
            "capacitors/"
        )

    # Diodes
    if "diode" in q:
        urls.append(
            "https://robu.in/product-category/"
            "diodes/"
        )

    # Transistors
    if "transistor" in q:
        urls.append(
            "https://robu.in/product-category/"
            "transistors/"
        )

    # IC / integrated circuits
    if (
        "ic" in q
        or "integrated circuit" in q
        or re.match(
            r"^(lm|ne|tlc|cd|74|at|pic|stm|ir|mc)\w+",
            q,
        )
    ):
        urls.append(
            "https://robu.in/product-category/"
            "integrated-circuits/"
        )

    # Always include normal Robu search last.
    urls.append(
        ROBU_SEARCH.format(
            query=quote_plus(
                clean_text(query)
            )
        )
    )

    # Remove duplicates while keeping order.
    output = []

    for url in urls:
        if url not in output:
            output.append(url)

    return output


# ============================================================
# ROBU HTTP SEARCH
# ============================================================

async def search_robu_http(client, query):
    urls = robu_category_urls(query)

    all_products = []

    for url in urls:
        try:
            response = await client.get(
                url,
                timeout=httpx.Timeout(
                    12.0,
                    connect=5.0,
                ),
            )

            response.raise_for_status()

            products = parse_robu_html(
                response.text
            )

            all_products.extend(products)

            # Once we have actual matching products,
            # don't unnecessarily request more pages.
            matching = filter_matching_products(
                all_products,
                query,
            )

            if matching:
                return matching

        except Exception as exc:
            print(
                f"Robu HTTP page failed: "
                f"{url} -> {exc}"
            )

    matching = filter_matching_products(
        all_products,
        query,
    )

    if matching:
        return matching

    raise RuntimeError(
        "Robu HTTP search returned no matching products"
    )


# ============================================================
# ROBU CAMOUFOX SEARCH
# ============================================================

async def search_robu_camoufox(query):
    urls = robu_category_urls(query)

    all_products = []

    async with AsyncCamoufox(
        headless="virtual"
    ) as browser:

        page = await browser.new_page()

        for url in urls:
            try:
                print(
                    f"Robu Camoufox opening: {url}"
                )

                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=25000,
                )

                # Give Robu's JavaScript/theme time
                # to render product cards.
                await page.wait_for_timeout(
                    1800
                )

                html = await page.content()

                products = parse_robu_html(
                    html
                )

                print(
                    f"Robu Camoufox parsed "
                    f"{len(products)} products"
                )

                all_products.extend(products)

                matching = filter_matching_products(
                    all_products,
                    query,
                )

                if matching:
                    return matching

            except Exception as exc:
                print(
                    f"Robu Camoufox page failed: "
                    f"{url} -> {exc}"
                )

        matching = filter_matching_products(
            all_products,
            query,
        )

        if matching:
            return matching

    raise RuntimeError(
        "Robu Camoufox search returned no matching products"
    )


# ============================================================
# ROBU SEARCH
# ============================================================

async def search_robu(client, query):
    # --------------------------------------------------------
    # FIRST: normal HTTP
    # --------------------------------------------------------
    try:
        products = await search_robu_http(
            client,
            query,
        )

        print(
            f"Robu HTTP success: "
            f"{len(products)} matching products "
            f"for {query!r}"
        )

        return products, None

    except Exception as exc:
        print(
            f"Robu HTTP failed: {exc}"
        )

    # --------------------------------------------------------
    # SECOND: Camoufox
    # --------------------------------------------------------
    try:
        products = await asyncio.wait_for(
            search_robu_camoufox(query),
            timeout=55,
        )

        print(
            f"Robu Camoufox success: "
            f"{len(products)} matching products "
            f"for {query!r}"
        )

        return products, None

    except Exception as exc:
        print(
            f"Robu Camoufox failed: {exc}"
        )

    return [], "Robu search failed"


# ============================================================
# ET STORE SEARCH
# ============================================================

async def search_etstore(client, query):
    try:
        response = await client.get(
            ET_SEARCH.format(
                query=quote_plus(query)
            ),
            timeout=httpx.Timeout(
                10.0,
                connect=5.0,
            ),
        )

        response.raise_for_status()

        products = parse_et_html(
            response.text
        )

        # Keep only products matching the
        # actual component name.
        products = filter_matching_products(
            products,
            query,
        )

        print(
            f"ET Store: "
            f"{len(products)} matching products "
            f"for {query!r}"
        )

        return products, None

    except Exception as exc:
        print(
            f"ET Store failed: {exc}"
        )

        return [], str(exc)


# ============================================================
# SSE
# ============================================================

def sse(data):
    return (
        "data: "
        + json.dumps(
            data,
            ensure_ascii=False
        )
        + "\n\n"
    )


# ============================================================
# SEARCH EVENT GENERATOR
# ============================================================

async def event_generator(query):
    sites = [
        "ET Store",
        "Robu.in",
    ]

    # Initial site list.
    yield sse(
        {
            "type": "init",
            "sites": sites,
        }
    )

    # Searching state.
    for site in sites:
        yield sse(
            {
                "type": "status",
                "site": site,
                "state": "searching",
            }
        )

    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
    ) as client:

        async def run_site(
            site,
            search_function,
        ):
            try:
                products, error = (
                    await search_function(
                        client,
                        query,
                    )
                )

                return (
                    site,
                    products,
                    error,
                )

            except Exception as exc:
                return (
                    site,
                    [],
                    str(exc),
                )

        tasks = [
            asyncio.create_task(
                run_site(
                    "ET Store",
                    search_etstore,
                )
            ),
            asyncio.create_task(
                run_site(
                    "Robu.in",
                    search_robu,
                )
            ),
        ]

        for task in asyncio.as_completed(
            tasks
        ):
            (
                site,
                products,
                error,
            ) = await task

            # ------------------------------------------------
            # ERROR
            # ------------------------------------------------
            if error:
                print(
                    f"{site} ERROR: {error}"
                )

                yield sse(
                    {
                        "type": "status",
                        "site": site,
                        "state": "error",
                        "count": 0,
                        "message": "Search failed",
                    }
                )

                continue

            # ------------------------------------------------
            # SUCCESS
            # ------------------------------------------------
            yield sse(
                {
                    "type": "status",
                    "site": site,
                    "state": "done",
                    "count": len(products),
                }
            )

            if products:
                yield sse(
                    {
                        "type": "result",
                        "site": site,
                        "products": products,
                    }
                )

    # Search completed.
    yield sse(
        {
            "type": "done"
        }
    )


# ============================================================
# ROOT
# ============================================================

@app.get("/")
async def root():
    return JSONResponse(
        {
            "status": "ok",
            "service": "component-finder",
        }
    )


# ============================================================
# SEARCH API
# ============================================================

@app.get("/api/search")
async def search(q: str):
    query = clean_text(q)

    if not query:
        return JSONResponse(
            {
                "error": "Query is required"
            },
            status_code=400,
        )

    return StreamingResponse(
        event_generator(query),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
