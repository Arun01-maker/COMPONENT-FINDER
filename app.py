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
# CONSTANTS
# ============================================================

ROBU_BASE = "https://robu.in"

ET_SEARCH = (
    "https://etstore.in/index.php"
    "?route=product/search&search={query}"
)

ROBU_SEARCH = (
    "https://robu.in/?s={query}&post_type=product"
)

JINA_PREFIX = "https://r.jina.ai/https://robu.in"


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


# ============================================================
# TEXT HELPERS
# ============================================================

def clean_text(value):
    return " ".join(str(value or "").split())


def normalize_text(value):
    value = clean_text(value).lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def compact_text(value):
    return re.sub(
        r"[^a-z0-9]",
        "",
        normalize_text(value)
    )


def normalize_url(url, base):
    return urljoin(base, clean_text(url))


# ============================================================
# COMPONENT EXTRACTION
# ============================================================

def extract_component_name(query):
    value = clean_text(query)

    if not value:
        return ""

    # Remove mounting/package information.
    remove_words = [
        "smd",
        "tht",
        "dip",
        "soic",
        "sop",
        "qfn",
        "qfp",
        "bga",
        "lga",
        "msop",
        "tssop",
        "ssop",
        "dfn",
        "d2pak",
        "to220",
        "to-220",
        "to263",
        "to-263",
        "to92",
        "to-92",
        "module",
        "ic",
        "through hole",
        "through-hole",
    ]

    for word in remove_words:
        value = re.sub(
            r"\b" + re.escape(word) + r"\b",
            " ",
            value,
            flags=re.IGNORECASE,
        )

    # Remove voltage.
    value = re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:v|volt|volts)\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )

    # Remove current.
    value = re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:a|amp|amps|ma)\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )

    # Remove power.
    value = re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:w|watt|watts)\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )

    # Remove common footprint numbers.
    value = re.sub(
        r"\b\d{4}\b",
        " ",
        value,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    ).strip()

    return value or clean_text(query)


# ============================================================
# STRICT MATCH
# ============================================================

def product_matches_component(title, component):
    title_normal = normalize_text(title)
    component_normal = normalize_text(component)

    if not title_normal or not component_normal:
        return False

    # Normal phrase match.
    if component_normal in title_normal:
        return True

    # Part-number match.
    title_compact = compact_text(title)
    component_compact = compact_text(component)

    if (
        component_compact
        and component_compact in title_compact
    ):
        return True

    return False


def filter_products(products, query):
    component = extract_component_name(query)

    output = []
    seen = set()

    for product in products:
        title = clean_text(
            product.get("title", "")
        )

        link = clean_text(
            product.get("link", "")
        )

        if not title or not link:
            continue

        if link in seen:
            continue

        if product_matches_component(
            title,
            component,
        ):
            seen.add(link)
            output.append(product)

    return output[:30]


# ============================================================
# AVAILABILITY
# ============================================================

def get_availability(text):
    text = clean_text(text).lower()

    if any(
        word in text
        for word in [
            "out of stock",
            "sold out",
            "currently unavailable",
            "unavailable",
            "not available",
        ]
    ):
        return "Out of Stock"

    if any(
        word in text
        for word in [
            "add to cart",
            "buy now",
            "in stock",
            "available",
        ]
    ):
        return "In Stock"

    return "Availability Unknown"


# ============================================================
# PRICE
# ============================================================

def get_price(card):
    element = card.select_one(
        ".price, "
        ".amount, "
        ".woocommerce-Price-amount, "
        "[class*='price']"
    )

    if element:
        return clean_text(
            element.get_text(
                " ",
                strip=True
            )
        )

    return "N/A"


# ============================================================
# ROBU PRODUCT PARSER
# ============================================================

def parse_robu_html(html):
    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    products = []
    seen = set()

    selectors = [
        "li.product",
        ".product-small",
        ".product-type-simple",
        ".product-type-variable",
        "article.product",
        ".products .product",
    ]

    cards = []

    for selector in selectors:
        cards.extend(
            soup.select(selector)
        )

    for card in cards:

        link = card.select_one(
            "a[href*='/product/']"
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

        title_element = card.select_one(
            ".name a, "
            ".name, "
            ".product-title, "
            ".woocommerce-loop-product__title, "
            "h2 a, "
            "h2, "
            "h3 a, "
            "h3, "
            "h4 a, "
            "h4"
        )

        if title_element:
            title = clean_text(
                title_element.get_text(
                    " ",
                    strip=True
                )
            )
        else:
            title = clean_text(
                link.get_text(
                    " ",
                    strip=True
                )
            )

        if not title:
            title = clean_text(
                link.get(
                    "aria-label",
                    ""
                )
            )

        if not title:
            continue

        card_text = clean_text(
            card.get_text(
                " ",
                strip=True
            )
        )

        seen.add(href)

        products.append(
            {
                "title": title,
                "link": href,
                "price": get_price(card),
                "availability": get_availability(
                    card_text
                ),
            }
        )

    # Broad fallback.
    if not products:

        for link in soup.select(
            "a[href*='/product/']"
        ):

            href = normalize_url(
                link.get("href", ""),
                ROBU_BASE,
            )

            if href in seen:
                continue

            title = clean_text(
                link.get_text(
                    " ",
                    strip=True
                )
            )

            if not title:
                title = clean_text(
                    link.get(
                        "aria-label",
                        ""
                    )
                )

            if not title:
                continue

            if len(title) > 300:
                continue

            parent = link.parent

            for _ in range(5):
                if parent is None:
                    break

                text = clean_text(
                    parent.get_text(
                        " ",
                        strip=True
                    )
                )

                if len(text) > len(title):
                    break

                parent = parent.parent

            if parent is None:
                continue

            card_text = clean_text(
                parent.get_text(
                    " ",
                    strip=True
                )
            )

            seen.add(href)

            products.append(
                {
                    "title": title,
                    "link": href,
                    "price": "N/A",
                    "availability":
                        get_availability(
                            card_text
                        ),
                }
            )

    return products


# ============================================================
# ROBU JINA MARKDOWN PARSER
# ============================================================

def parse_jina_robu(text):
    products = []
    seen = set()

    # Jina normally returns Markdown links:
    #
    # [PRODUCT NAME](https://robu.in/product/...)
    #
    pattern = re.compile(
        r"\[([^\]]+)\]"
        r"\((https://robu\.in/product/[^)\s]+)\)",
        re.IGNORECASE,
    )

    matches = pattern.findall(text)

    for title, link in matches:

        title = clean_text(title)
        link = clean_text(link)

        if not title or not link:
            continue

        if link in seen:
            continue

        if "/product/" not in link:
            continue

        seen.add(link)

        # Try to find nearby price text.
        price = "N/A"

        position = text.find(link)

        if position >= 0:
            nearby = text[
                max(0, position - 250):
                position + 500
            ]

            price_match = re.search(
                r"₹\s*[\d,]+(?:\.\d+)?",
                nearby
            )

            if price_match:
                price = price_match.group(0)

        products.append(
            {
                "title": title,
                "link": link,
                "price": price,
                "availability":
                    get_availability(
                        text[
                            max(0, position - 500):
                            position + 800
                        ]
                    ),
            }
        )

    return products


# ============================================================
# ROBU CATEGORY URLS
# ============================================================

def get_robu_urls(query):
    q = normalize_text(query)

    urls = []

    # 555 / Timer.
    if (
        "555" in q
        or "timer" in q
    ):
        urls.append(
            "https://robu.in/product-category/"
            "clock-and-timer-ic/"
        )

    # LM2596 / Buck.
    if any(
        x in q
        for x in [
            "lm2596",
            "lm2576",
            "lm2595",
            "buck converter",
            "step down",
        ]
    ):
        urls.append(
            "https://robu.in/product-category/"
            "buck-converter/"
        )

        urls.append(
            "https://robu.in/product-category/"
            "switching-ic/"
        )

    # Arduino / ESP.
    if any(
        x in q
        for x in [
            "arduino",
            "esp32",
            "esp8266",
        ]
    ):
        urls.append(
            "https://robu.in/product-category/"
            "development-boards/"
        )

    # Resistors.
    if "resistor" in q:
        urls.append(
            "https://robu.in/product-category/"
            "resistors/"
        )

    # Capacitors.
    if "capacitor" in q:
        urls.append(
            "https://robu.in/product-category/"
            "capacitors/"
        )

    # Diodes.
    if "diode" in q:
        urls.append(
            "https://robu.in/product-category/"
            "diodes/"
        )

    # Transistors.
    if "transistor" in q:
        urls.append(
            "https://robu.in/product-category/"
            "transistors/"
        )

    # Always include normal search.
    urls.append(
        ROBU_SEARCH.format(
            query=quote_plus(
                clean_text(query)
            )
        )
    )

    result = []

    for url in urls:
        if url not in result:
            result.append(url)

    return result


# ============================================================
# ROBU DIRECT HTTP
# ============================================================

async def robu_direct(client, query):

    for url in get_robu_urls(query):

        try:

            response = await client.get(
                url,
                timeout=httpx.Timeout(
                    15.0,
                    connect=6.0,
                ),
                follow_redirects=True,
            )

            if response.status_code != 200:
                continue

            products = parse_robu_html(
                response.text
            )

            products = filter_products(
                products,
                query,
            )

            if products:
                print(
                    "ROBU DIRECT SUCCESS:",
                    len(products),
                )

                return products

        except Exception as exc:

            print(
                "ROBU DIRECT ERROR:",
                repr(exc),
            )

    return []


# ============================================================
# ROBU JINA FALLBACK
# ============================================================

async def robu_jina(client, query):

    for url in get_robu_urls(query):

        try:

            jina_url = (
                JINA_PREFIX
                + url.replace(
                    "https://robu.in",
                    ""
                )
            )

            print(
                "ROBU JINA:",
                jina_url,
            )

            response = await client.get(
                jina_url,
                timeout=httpx.Timeout(
                    25.0,
                    connect=10.0,
                ),
                follow_redirects=True,
                headers={
                    "User-Agent":
                        "Mozilla/5.0",
                    "Accept":
                        "text/markdown,text/plain,*/*",
                },
            )

            if response.status_code != 200:
                print(
                    "JINA STATUS:",
                    response.status_code,
                )
                continue

            products = parse_jina_robu(
                response.text
            )

            products = filter_products(
                products,
                query,
            )

            if products:

                print(
                    "ROBU JINA SUCCESS:",
                    len(products),
                )

                return products

        except Exception as exc:

            print(
                "ROBU JINA ERROR:",
                repr(exc),
            )

    return []


# ============================================================
# ROBU CAMOUFOX
# ============================================================

async def robu_camoufox(query):

    try:

        async with AsyncCamoufox(
            headless=True
        ) as browser:

            page = await browser.new_page()

            for url in get_robu_urls(query):

                try:

                    print(
                        "ROBU CAMOUFOX:",
                        url,
                    )

                    await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )

                    await page.wait_for_timeout(
                        2500
                    )

                    html = await page.content()

                    products = parse_robu_html(
                        html
                    )

                    products = filter_products(
                        products,
                        query,
                    )

                    if products:

                        print(
                            "ROBU CAMOUFOX SUCCESS:",
                            len(products),
                        )

                        return products

                except Exception as exc:

                    print(
                        "CAMOUFOX PAGE ERROR:",
                        repr(exc),
                    )

    except Exception as exc:

        print(
            "CAMOUFOX START ERROR:",
            repr(exc),
        )

    return []


# ============================================================
# ROBU MAIN SEARCH
# ============================================================

async def search_robu(client, query):

    # --------------------------------------------------------
    # 1. DIRECT ROBU
    # --------------------------------------------------------

    products = await robu_direct(
        client,
        query,
    )

    if products:
        return products, None


    # --------------------------------------------------------
    # 2. JINA READER
    # --------------------------------------------------------

    products = await robu_jina(
        client,
        query,
    )

    if products:
        return products, None


    # --------------------------------------------------------
    # 3. CAMOUFOX
    # --------------------------------------------------------

    try:

        products = await asyncio.wait_for(
            robu_camoufox(query),
            timeout=60,
        )

    except Exception as exc:

        print(
            "CAMOUFOX TIMEOUT:",
            repr(exc),
        )

        products = []

    if products:
        return products, None


    # --------------------------------------------------------
    # IMPORTANT:
    # Don't report a technical failure to the UI.
    # Return a normal completed search with zero results.
    # --------------------------------------------------------

    return [], None


# ============================================================
# ET STORE
# ============================================================

async def search_etstore(client, query):

    try:

        response = await client.get(
            ET_SEARCH.format(
                query=quote_plus(query)
            ),
            timeout=httpx.Timeout(
                12.0,
                connect=6.0,
            ),
            follow_redirects=True,
        )

        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        products = []

        for item in soup.select(
            ".product-thumb"
        ):

            link = item.select_one(
                ".caption h4 a"
            )

            if not link:
                continue

            title = clean_text(
                link.get_text(
                    " ",
                    strip=True
                )
            )

            href = normalize_url(
                link.get("href", ""),
                "https://etstore.in/",
            )

            price_element = item.select_one(
                ".price"
            )

            price = (
                clean_text(
                    price_element.get_text(
                        " ",
                        strip=True
                    )
                )
                if price_element
                else "N/A"
            )

            products.append(
                {
                    "title": title,
                    "link": href,
                    "price": price,
                    "availability":
                        get_availability(
                            item.get_text(
                                " ",
                                strip=True
                            )
                        ),
                }
            )

        products = filter_products(
            products,
            query,
        )

        return products, None

    except Exception as exc:

        print(
            "ET STORE ERROR:",
            repr(exc),
        )

        return [], str(exc)


# ============================================================
# SSE HELPER
# ============================================================

def sse(data):

    return (
        "data: "
        + json.dumps(
            data,
            ensure_ascii=False,
        )
        + "\n\n"
    )


# ============================================================
# EVENT GENERATOR
# ============================================================

async def event_generator(query):

    sites = [
        "ET Store",
        "Robu.in",
    ]

    yield sse(
        {
            "type": "init",
            "sites": sites,
        }
    )

    # Searching status.
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

        async def run_et():

            products, error = (
                await search_etstore(
                    client,
                    query,
                )
            )

            return (
                "ET Store",
                products,
                error,
            )

        async def run_robu():

            products, error = (
                await search_robu(
                    client,
                    query,
                )
            )

            return (
                "Robu.in",
                products,
                error,
            )

        tasks = [
            asyncio.create_task(
                run_et()
            ),
            asyncio.create_task(
                run_robu()
            ),
        ]

        for task in asyncio.as_completed(
            tasks
        ):

            site, products, error = (
                await task
            )

            # --------------------------------------------
            # Technical error.
            # --------------------------------------------

            if error:

                yield sse(
                    {
                        "type": "status",
                        "site": site,
                        "state": "done",
                        "count": 0,
                    }
                )

                continue

            # --------------------------------------------
            # Normal completion.
            # --------------------------------------------

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
            "Cache-Control":
                "no-cache, no-transform",
            "Connection":
                "keep-alive",
            "X-Accel-Buffering":
                "no",
        },
    )
