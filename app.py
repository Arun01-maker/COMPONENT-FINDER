import json
import re
import asyncio
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

try:
    from camoufox.async_api import AsyncCamoufox
    CAMOUFOX_AVAILABLE = True
except Exception:
    CAMOUFOX_AVAILABLE = False


app = FastAPI(title="Component Finder API")


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)


# ============================================================
# DISTRIBUTOR CONFIGURATION
# ============================================================

DISTRIBUTORS = [
    {
        "name": "ET Store",
        "search_url": lambda q:
            f"https://etstore.in/index.php?route=product/search&search={quote_plus(q)}",
    },

    {
        "name": "Robu.in",
        "search_url": lambda q:
            f"https://robu.in/?s={quote_plus(q)}&post_type=product",
    },

    {
        "name": "element14",
        "search_url": lambda q:
            f"https://in.element14.com/search?st={quote_plus(q)}",
    },

    {
        "name": "Leeds Electronics",
        "search_url": lambda q:
            f"https://www.leedsind.com/search?q={quote_plus(q)}",
    },

    {
        "name": "Tomson Electronics",
        "search_url": lambda q:
            f"https://www.tomsonelectronics.com/search?q={quote_plus(q)}",
    },

    {
        "name": "Sparefly",
        "search_url": lambda q:
            f"https://sparefly.com/search?q={quote_plus(q)}",
    },

    {
        "name": "Sharvi Electronics",
        "search_url": lambda q:
            f"https://sharvielectronics.com/?s={quote_plus(q)}",
    },

    {
        "name": "ElectronicsComp",
        "search_url": lambda q:
            f"https://www.electronicscomp.com/catalogsearch/result/?q={quote_plus(q)}",
    },

    {
        "name": "QuartzComponents",
        "search_url": lambda q:
            f"https://quartzcomponents.com/search?q={quote_plus(q)}",
    },

    {
        "name": "Evelta",
        "search_url": lambda q:
            f"https://evelta.com/catalogsearch/result/?q={quote_plus(q)}",
    },

    {
        "name": "MakerBazar",
        "search_url": lambda q:
            f"https://makerbazar.in/search?q={quote_plus(q)}",
    },

    {
        "name": "Probots",
        "search_url": lambda q:
            f"https://probots.co.in/search?q={quote_plus(q)}",
    },
]


# ============================================================
# HELPERS
# ============================================================

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


def normalize(text):
    if not text:
        return ""

    text = text.lower()
    text = text.replace("–", "-")
    text = text.replace("—", "-")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_price(text):
    if not text:
        return "N/A"

    text = re.sub(r"\s+", " ", text)
    text = text.strip()

    if len(text) > 80:
        return "N/A"

    return text


def clean_title(text):
    if not text:
        return ""

    text = re.sub(r"\s+", " ", text)
    return text.strip()


def absolute_url(base_url, link):
    if not link:
        return ""

    return urljoin(base_url, link)


def extract_main_component(query):
    """
    Extract the actual component name from the complete query.

    Example:
        LM2596 DIP 3A
        -> LM2596

        Arduino Nano 5V
        -> Arduino Nano

    The first part is intentionally kept strict so unrelated
    components are not returned.
    """

    q = normalize(query)

    # Known multi-word components
    known_multi = [
        "arduino nano",
        "arduino uno",
        "arduino mega",
        "esp32 devkit",
        "esp32 wroom",
        "raspberry pi",
        "logic level converter",
        "buck converter",
        "boost converter",
        "dc dc converter",
    ]

    for item in known_multi:
        if item in q:
            return item

    # IC / part-number style
    m = re.match(
        r"^([a-z0-9]+(?:[-_][a-z0-9]+)*(?:\.[a-z0-9]+)?)",
        q,
        re.I,
    )

    if m:
        return m.group(1)

    # Otherwise first 1-3 meaningful words
    words = q.split()

    if not words:
        return ""

    return " ".join(words[:2])


def product_matches(product, query):
    """
    Strict filtering.

    A product must contain the actual component name.
    This prevents LM2596 searches from returning random
    resistors, transistors, diodes, etc.
    """

    title = normalize(product.get("title", ""))
    query_normalized = normalize(query)

    component = extract_main_component(query_normalized)

    if not component:
        return False

    if component not in title:
        return False

    # Additional important specifications are checked when
    # they are explicit in the search query.
    words = query_normalized.split()

    # Voltage
    voltage_patterns = [
        r"\b\d+(?:\.\d+)?v\b",
        r"\b\d+(?:\.\d+)?\s*volt\b",
    ]

    for pattern in voltage_patterns:
        matches = re.findall(pattern, query_normalized)

        for value in matches:
            if normalize(value) not in title:
                # Voltage is a secondary condition, so don't reject
                # immediately when distributor titles omit it.
                pass

    return True


def parse_generic(html, base_url, query):
    """
    Generic parser used by most distributor sites.
    """

    soup = BeautifulSoup(html, "html.parser")
    products = []

    selectors = [
        ".product-thumb",
        ".product-item",
        ".product-card",
        ".product-small",
        ".product",
        ".grid-product",
        ".product-grid-item",
        "li.product",
        ".item.product",
        ".product-tile",
        ".product-item-info",
    ]

    items = []

    for selector in selectors:
        found = soup.select(selector)

        if found:
            items.extend(found)

    # Remove duplicates
    seen_nodes = set()
    unique_items = []

    for item in items:
        marker = str(item)[:500]

        if marker not in seen_nodes:
            seen_nodes.add(marker)
            unique_items.append(item)

    for item in unique_items:

        title_node = (
            item.select_one(
                ".product-title, "
                ".product-name, "
                ".name, "
                ".caption h4 a, "
                ".product-item-link, "
                "h2 a, "
                "h3 a, "
                "h4 a, "
                "h2, "
                "h3, "
                "h4"
            )
        )

        if not title_node:
            continue

        title = clean_title(title_node.get_text(" ", strip=True))

        if not title:
            continue

        link_node = (
            item.select_one(
                "a.product-title, "
                "a.product-name, "
                ".product-title a, "
                ".product-name a, "
                ".caption h4 a, "
                ".product-item-link, "
                "h2 a, "
                "h3 a, "
                "h4 a, "
                "a"
            )
        )

        link = ""

        if link_node:
            link = absolute_url(
                base_url,
                link_node.get("href", "")
            )

        price_node = item.select_one(
            ".price, "
            ".product-price, "
            ".price-box, "
            ".woocommerce-Price-amount, "
            ".money, "
            "[class*='price']"
        )

        price = (
            clean_price(price_node.get_text(" ", strip=True))
            if price_node
            else "N/A"
        )

        product = {
            "title": title,
            "link": link,
            "price": price,
            "availability": "Availability Unknown",
        }

        if product_matches(product, query):
            products.append(product)

    return products


def parse_etstore(html, query):
    soup = BeautifulSoup(html, "html.parser")
    products = []

    for item in soup.select(".product-thumb"):

        title_node = item.select_one(".caption h4 a")

        if not title_node:
            continue

        title = clean_title(
            title_node.get_text(" ", strip=True)
        )

        if not product_matches({"title": title}, query):
            continue

        link = absolute_url(
            "https://etstore.in/",
            title_node.get("href", "")
        )

        price_node = item.select_one(".price")

        price = (
            clean_price(price_node.get_text(" ", strip=True))
            if price_node
            else "N/A"
        )

        products.append(
            {
                "title": title,
                "link": link,
                "price": price,
                "availability": "Availability Unknown",
            }
        )

    return products


def parse_robu(html, query):
    soup = BeautifulSoup(html, "html.parser")
    products = []

    selectors = [
        ".product-small",
        "li.product",
        ".product-type-simple",
        ".product",
    ]

    items = []

    for selector in selectors:
        items.extend(soup.select(selector))

    seen = set()

    for item in items:

        title_node = (
            item.select_one(".name")
            or item.select_one(".product-title")
            or item.select_one(".woocommerce-loop-product__title")
            or item.select_one("h2")
            or item.select_one("h3")
        )

        if not title_node:
            continue

        title = clean_title(
            title_node.get_text(" ", strip=True)
        )

        if not title:
            continue

        if normalize(title) in seen:
            continue

        seen.add(normalize(title))

        if not product_matches({"title": title}, query):
            continue

        link_node = item.select_one("a[href]")

        link = ""

        if link_node:
            link = absolute_url(
                "https://robu.in/",
                link_node.get("href", "")
            )

        price_node = (
            item.select_one(".price")
            or item.select_one(".woocommerce-Price-amount")
        )

        price = (
            clean_price(price_node.get_text(" ", strip=True))
            if price_node
            else "N/A"
        )

        # Try to detect availability from card text
        card_text = normalize(
            item.get_text(" ", strip=True)
        )

        if "out of stock" in card_text:
            availability = "Out of Stock"
        elif "in stock" in card_text:
            availability = "In Stock"
        else:
            availability = "Availability Unknown"

        products.append(
            {
                "title": title,
                "link": link,
                "price": price,
                "availability": availability,
            }
        )

    return products


def parse_site(store_name, html, query):

    if store_name == "ET Store":
        return parse_etstore(html, query)

    if store_name == "Robu.in":
        return parse_robu(html, query)

    return parse_generic(
        html,
        next(
            (
                s["search_url"](query)
                for s in DISTRIBUTORS
                if s["name"] == store_name
            ),
            "",
        ),
        query,
    )


# ============================================================
# HTTP SEARCH
# ============================================================

async def http_search(store, query):

    url = store["search_url"](query)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,image/avif,"
            "image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-IN,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    timeout = httpx.Timeout(
        connect=8.0,
        read=15.0,
        write=8.0,
        pool=8.0,
    )

    async with httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
    ) as client:

        response = await client.get(url)

        response.raise_for_status()

        products = parse_site(
            store["name"],
            response.text,
            query,
        )

        return products


# ============================================================
# CAMOUFOX FALLBACK
# ============================================================

async def camoufox_search(store, query):

    if not CAMOUFOX_AVAILABLE:
        return []

    url = store["search_url"](query)

    try:
        async with AsyncCamoufox(
            headless=True,
            humanize=True,
        ) as browser:

            page = await browser.new_page()

            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=25000,
            )

            await page.wait_for_timeout(1500)

            html = await page.content()

            return parse_site(
                store["name"],
                html,
                query,
            )

    except Exception:
        return []


# ============================================================
# SEARCH ONE DISTRIBUTOR
# ============================================================

async def search_store(store, query):

    try:
        products = await http_search(
            store,
            query,
        )

        if products:
            return products

    except Exception:
        pass

    # Browser fallback for sites that block normal HTTP
    try:
        products = await camoufox_search(
            store,
            query,
        )

        return products

    except Exception:
        return []


# ============================================================
# SSE EVENT GENERATOR
# ============================================================

async def event_generator(query, selected_sites):

    # --------------------------------------------------------
    # Determine which sites will actually be searched
    # --------------------------------------------------------

    valid_names = {
        store["name"]
        for store in DISTRIBUTORS
    }

    if selected_sites:
        selected_set = {
            name.strip()
            for name in selected_sites
            if name.strip() in valid_names
        }

        stores_to_search = [
            store
            for store in DISTRIBUTORS
            if store["name"] in selected_set
        ]

    else:
        stores_to_search = DISTRIBUTORS

    site_names = [
        store["name"]
        for store in stores_to_search
    ]

    yield (
        "data: "
        + json.dumps(
            {
                "type": "init",
                "sites": site_names,
            }
        )
        + "\n\n"
    )

    # --------------------------------------------------------
    # Search distributors concurrently
    # --------------------------------------------------------

    async def run_store(store):

        site = store["name"]

        return site, await search_store(
            store,
            query,
        )

    tasks = []

    for store in stores_to_search:

        yield (
            "data: "
            + json.dumps(
                {
                    "type": "status",
                    "site": store["name"],
                    "state": "searching",
                }
            )
            + "\n\n"
        )

        tasks.append(
            asyncio.create_task(
                run_store(store)
            )
        )

    # --------------------------------------------------------
    # Return results as soon as each site finishes
    # --------------------------------------------------------

    pending = set(tasks)

    while pending:

        done_tasks, pending = await asyncio.wait(
            pending,
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in done_tasks:

            try:
                site, products = await task

            except Exception:
                continue

            yield (
                "data: "
                + json.dumps(
                    {
                        "type": "status",
                        "site": site,
                        "state": "done",
                        "count": len(products),
                    }
                )
                + "\n\n"
            )

            if products:

                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "result",
                            "site": site,
                            "products": products,
                        }
                    )
                    + "\n\n"
                )

    yield (
        "data: "
        + json.dumps(
            {
                "type": "done",
            }
        )
        + "\n\n"
    )


# ============================================================
# API
# ============================================================

@app.get("/api/sites")
async def get_sites():

    return {
        "sites": [
            store["name"]
            for store in DISTRIBUTORS
        ]
    }


@app.get("/api/search")
async def search(
    q: str,
    sites: list[str] | None = Query(default=None),
):

    q = q.strip()

    if not q:

        async def empty_stream():

            yield (
                "data: "
                + json.dumps(
                    {
                        "type": "error",
                        "message": "Search query is empty",
                    }
                )
                + "\n\n"
            )

        return StreamingResponse(
            empty_stream(),
            media_type="text/event-stream",
        )

    return StreamingResponse(
        event_generator(
            q,
            sites,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/")
async def root():

    return {
        "status": "online",
        "service": "Component Finder API",
        "sites": len(DISTRIBUTORS),
    }
