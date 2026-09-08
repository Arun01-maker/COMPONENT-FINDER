# ============================================================
# COMPONENT FINDER - MULTI WEBSITE BACKEND
# ============================================================
#
# FastAPI + SSE
# HTTP scraping first
# Camoufox browser fallback
#
# Supported websites:
#   1. ET Store
#   2. Robu.in
#   3. element14
#   4. Leeds Electronics
#   5. Tomson Electronics
#   6. Sparefly
#   7. Sharvi Electronics
#   8. ElectronicsComp
#   9. QuartzComponents
#  10. Evelta
#  11. MakerBazar
#  12. Probots
#
# Existing frontend can continue using:
#   /api/search?q=LM2596
#
# ============================================================

import asyncio
import json
import re
from urllib.parse import quote_plus, urljoin, quote

import httpx
from bs4 import BeautifulSoup

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

# ============================================================
# OPTIONAL CAMOUFOX
# ============================================================

try:
    from camoufox.async_api import AsyncCamoufox
    CAMOUFOX_AVAILABLE = True
except Exception:
    AsyncCamoufox = None
    CAMOUFOX_AVAILABLE = False


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Component Finder",
    version="3.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)


# ============================================================
# SETTINGS
# ============================================================

HTTP_TIMEOUT = 12.0
BROWSER_TIMEOUT = 18_000

MAX_PRODUCTS_PER_SITE = 25
MAX_BROWSER_PRODUCTS = 12

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}


# ============================================================
# STORE DEFINITIONS
# ============================================================

STORES = [
    {
        "name": "ET Store",
        "type": "generic",
        "search": lambda q: (
            f"https://etstore.in/index.php?"
            f"route=product/search&search={quote_plus(q)}"
        ),
    },

    {
        "name": "Robu.in",
        "type": "robu",
        "search": lambda q: (
            f"https://robu.in/?s={quote_plus(q)}&post_type=product"
        ),
    },

    {
        "name": "element14",
        "type": "element14",
        "search": lambda q: (
            f"https://in.element14.com/search?st={quote_plus(q)}"
        ),
    },

    {
        "name": "Leeds Electronics",
        "type": "generic",
        "search": lambda q: (
            f"https://www.leedsind.com/?s={quote_plus(q)}"
        ),
    },

    {
        "name": "Tomson Electronics",
        "type": "tomson",
        "search": lambda q: (
            f"https://www.tomsonelectronics.com/search?q={quote_plus(q)}"
        ),
    },

    {
        "name": "Sparefly",
        "type": "generic",
        "search": lambda q: (
            f"https://sparefly.com/?s={quote_plus(q)}"
        ),
    },

    {
        "name": "Sharvi Electronics",
        "type": "generic",
        "search": lambda q: (
            f"https://www.google.com/search?q="
            f"site%3Aamazon.in+%22Sharvi+Electronics%22+"
            f"{quote_plus(q)}"
        ),
    },

    {
        "name": "ElectronicsComp.com",
        "type": "generic",
        "search": lambda q: (
            f"https://www.electronicscomp.com/"
            f"index.php?route=product/search&search={quote_plus(q)}"
        ),
    },

    {
        "name": "QuartzComponents",
        "type": "shopify",
        "search": lambda q: (
            f"https://quartzcomponents.com/search?q={quote_plus(q)}"
        ),
    },

    {
        "name": "Evelta",
        "type": "evelta",
        "search": lambda q: (
            f"https://evelta.com/search?q={quote_plus(q)}"
        ),
    },

    {
        "name": "MakerBazar",
        "type": "shopify",
        "search": lambda q: (
            f"https://makerbazar.in/search?q={quote_plus(q)}"
        ),
    },

    {
        "name": "Probots",
        "type": "probots",
        "search": lambda q: (
            f"https://probots.co.in/catalogsearch/result/"
            f"?q={quote_plus(q)}"
        ),
    },
]


# ============================================================
# TEXT HELPERS
# ============================================================

def clean_text(value):
    if not value:
        return ""

    value = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize(value):
    value = clean_text(value).lower()

    # Replace common separators with spaces.
    value = value.replace("-", " ")
    value = value.replace("_", " ")
    value = value.replace("/", " ")
    value = value.replace("\\", " ")

    # Remove special characters.
    value = re.sub(r"[^a-z0-9.+ ]+", " ", value)

    value = re.sub(r"\s+", " ", value)

    return value.strip()


def normalize_compact(value):
    return re.sub(r"[^a-z0-9]", "", normalize(value))


def absolute_url(base, href):
    if not href:
        return ""

    href = href.strip()

    if href.startswith("#"):
        return ""

    return urljoin(base, href)


# ============================================================
# QUERY / MATCHING
# ============================================================

GENERIC_WORDS = {
    "buy",
    "online",
    "india",
    "electronic",
    "electronics",
    "component",
    "components",
    "module",
    "modules",
    "board",
    "boards",
    "ic",
    "chip",
    "part",
    "parts",
}


def extract_search_terms(query):
    """
    Creates strict matching terms from the user's query.

    Example:

        LM2596 3A

    becomes:

        ["lm2596"]

    Example:

        Arduino Nano 5V

    becomes:

        ["arduino", "nano"]

    Technical specification values are deliberately not always
    treated as product-name requirements.
    """

    q = normalize(query)

    words = q.split()

    terms = []

    for word in words:

        compact = re.sub(r"[^a-z0-9]", "", word)

        if not compact:
            continue

        # Ignore generic words.
        if compact in GENERIC_WORDS:
            continue

        # Ignore common electrical specification values.
        if re.fullmatch(
            r"\d+(\.\d+)?(v|mv|a|ma|w|mw|ohm|kohm|mohm|uf|nf|pf|hz|khz|mhz|ghz)",
            compact
        ):
            continue

        # Package dimensions.
        if re.fullmatch(r"\d{3,4}", compact):
            continue

        # Common package names.
        if compact in {
            "dip",
            "smd",
            "smt",
            "sop",
            "soic",
            "qfn",
            "qfp",
            "tssop",
            "to220",
            "to263",
            "to92",
        }:
            continue

        if len(compact) >= 2:
            terms.append(compact)

    return terms


def strict_product_match(title, query):
    """
    Strict matching.

    All important component-name terms must occur in the
    product title.

    This prevents:

        LM2596

    from returning:

        LM317
        7805
        diode
        resistor
        capacitor

    """

    title_norm = normalize(title)
    title_compact = normalize_compact(title)

    terms = extract_search_terms(query)

    if not terms:
        return True

    for term in terms:

        term_norm = normalize(term)
        term_compact = normalize_compact(term)

        if not term_norm:
            continue

        # Normal match.
        if term_norm in title_norm:
            continue

        # Compact match.
        if term_compact in title_compact:
            continue

        return False

    return True


# ============================================================
# AVAILABILITY
# ============================================================

OUT_OF_STOCK_PATTERNS = [
    "out of stock",
    "sold out",
    "currently unavailable",
    "not available",
    "unavailable",
    "temporarily unavailable",
    "no stock",
    "stock unavailable",
]


IN_STOCK_PATTERNS = [
    "in stock",
    "available",
    "add to cart",
    "buy now",
    "order now",
    "ships in",
    "ready to ship",
]


def detect_availability(text):
    text = normalize(text)

    for pattern in OUT_OF_STOCK_PATTERNS:
        if pattern in text:
            return "Out of Stock"

    for pattern in IN_STOCK_PATTERNS:
        if pattern in text:
            return "In Stock"

    return "Availability Unknown"


def extract_price(text):
    if not text:
        return "N/A"

    text = clean_text(text)

    patterns = [
        r"(₹\s?[\d,]+(?:\.\d+)?)",
        r"(Rs\.?\s?[\d,]+(?:\.\d+)?)",
        r"(INR\s?[\d,]+(?:\.\d+)?)",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        if match:
            return match.group(1)

    return "N/A"


# ============================================================
# GENERIC PRODUCT PARSER
# ============================================================

def parse_generic_products(html, base_url, query):
    soup = BeautifulSoup(html, "html.parser")

    products = []
    seen = set()

    selectors = [
        "li.product",
        ".product",
        ".product-item",
        ".product-card",
        ".product-thumb",
        ".product-small",
        ".product-type-simple",
        ".grid-product",
        ".product-grid-item",
        ".card-product",
        ".product-inner",
        "article",
    ]

    nodes = []

    for selector in selectors:
        found = soup.select(selector)

        if found:
            nodes.extend(found)

    # Remove duplicate DOM nodes.
    unique_nodes = []

    seen_nodes = set()

    for node in nodes:
        marker = id(node)

        if marker not in seen_nodes:
            seen_nodes.add(marker)
            unique_nodes.append(node)

    for item in unique_nodes:

        title_element = (
            item.select_one(
                "h1, h2, h3, h4, h5, "
                ".product-title, "
                ".product-name, "
                ".name, "
                ".woocommerce-loop-product__title, "
                ".card-title, "
                ".product-item-link, "
                "a"
            )
        )

        if not title_element:
            continue

        title = clean_text(title_element.get_text(" ", strip=True))

        if not title:
            continue

        if len(title) < 2:
            continue

        if not strict_product_match(title, query):
            continue

        link_element = item.select_one(
            "a[href]"
        )

        if not link_element:
            continue

        link = absolute_url(
            base_url,
            link_element.get("href")
        )

        if not link:
            continue

        if link in seen:
            continue

        seen.add(link)

        item_text = clean_text(
            item.get_text(" ", strip=True)
        )

        price_element = item.select_one(
            ".price, "
            ".product-price, "
            ".price-box, "
            ".woocommerce-Price-amount, "
            ".amount"
        )

        if price_element:
            price_text = clean_text(
                price_element.get_text(" ", strip=True)
            )
        else:
            price_text = item_text

        price = extract_price(price_text)

        availability = detect_availability(item_text)

        products.append({
            "title": title,
            "link": link,
            "price": price,
            "availability": availability,
        })

        if len(products) >= MAX_PRODUCTS_PER_SITE:
            break

    return products


# ============================================================
# SHOPIFY PARSER
# ============================================================

def parse_shopify_products(html, base_url, query):
    soup = BeautifulSoup(html, "html.parser")

    products = []
    seen = set()

    selectors = [
        ".product-card",
        ".card",
        ".grid__item",
        ".product-item",
        ".product-grid-item",
        "li.grid__item",
    ]

    nodes = []

    for selector in selectors:
        nodes.extend(soup.select(selector))

    for item in nodes:

        title_element = item.select_one(
            ".card__heading a, "
            ".card__heading, "
            ".product-title a, "
            ".product-title, "
            ".product-card__title a, "
            ".product-card__title, "
            "h2 a, "
            "h3 a, "
            "h2, "
            "h3"
        )

        if not title_element:
            continue

        title = clean_text(
            title_element.get_text(" ", strip=True)
        )

        if not title:
            continue

        if not strict_product_match(title, query):
            continue

        link_element = item.select_one("a[href]")

        if not link_element:
            continue

        link = absolute_url(
            base_url,
            link_element.get("href")
        )

        if not link or link in seen:
            continue

        seen.add(link)

        text = clean_text(
            item.get_text(" ", strip=True)
        )

        price = extract_price(text)

        availability = detect_availability(text)

        products.append({
            "title": title,
            "link": link,
            "price": price,
            "availability": availability,
        })

        if len(products) >= MAX_PRODUCTS_PER_SITE:
            break

    return products


# ============================================================
# ELEMENT14 PARSER
# ============================================================

def parse_element14(html, base_url, query):
    soup = BeautifulSoup(html, "html.parser")

    products = []
    seen = set()

    selectors = [
        ".product-listing",
        ".product-item",
        ".listing-item",
        ".product-list",
        "article",
        "[data-product-id]",
    ]

    nodes = []

    for selector in selectors:
        nodes.extend(soup.select(selector))

    for item in nodes:

        title_element = item.select_one(
            "h2 a, "
            "h3 a, "
            "h4 a, "
            ".product-name a, "
            ".product-name, "
            ".description a, "
            "a[href]"
        )

        if not title_element:
            continue

        title = clean_text(
            title_element.get_text(" ", strip=True)
        )

        if not title:
            continue

        if not strict_product_match(title, query):
            continue

        link_element = (
            title_element
            if title_element.name == "a"
            else item.select_one("a[href]")
        )

        if not link_element:
            continue

        link = absolute_url(
            base_url,
            link_element.get("href")
        )

        if not link or link in seen:
            continue

        seen.add(link)

        text = clean_text(
            item.get_text(" ", strip=True)
        )

        price = extract_price(text)

        availability = detect_availability(text)

        # element14 often uses explicit stock wording.
        stock_match = re.search(
            r"([\d,]+)\s+in\s+stock",
            text,
            flags=re.IGNORECASE
        )

        if stock_match:
            availability = (
                f"In Stock ({stock_match.group(1)})"
            )

        products.append({
            "title": title,
            "link": link,
            "price": price,
            "availability": availability,
        })

        if len(products) >= MAX_PRODUCTS_PER_SITE:
            break

    return products


# ============================================================
# ROBU CATEGORY ROUTING
# ============================================================

def get_robu_urls(query):
    q = normalize(query)

    urls = []

    # Direct search.
    urls.append(
        f"https://robu.in/?s={quote_plus(query)}"
        f"&post_type=product"
    )

    # Category pages are used as additional fallbacks.
    if "lm2596" in q or "buck converter" in q:
        urls.extend([
            "https://robu.in/product-category/buck-converter/",
            "https://robu.in/product-category/switching-ic/",
        ])

    elif "555" in q or "timer" in q:
        urls.append(
            "https://robu.in/product-category/clock-and-timer-ic/"
        )

    elif "arduino" in q or "esp32" in q or "esp8266" in q:
        urls.append(
            "https://robu.in/product-category/development-boards/"
        )

    elif "resistor" in q:
        urls.append(
            "https://robu.in/product-category/resistors/"
        )

    elif "capacitor" in q:
        urls.append(
            "https://robu.in/product-category/capacitors/"
        )

    elif "diode" in q:
        urls.append(
            "https://robu.in/product-category/diodes/"
        )

    elif "transistor" in q:
        urls.append(
            "https://robu.in/product-category/transistors/"
        )

    # Remove duplicates.
    result = []

    for url in urls:
        if url not in result:
            result.append(url)

    return result


# ============================================================
# ROBU PARSER
# ============================================================

def parse_robu_products(html, base_url, query):
    soup = BeautifulSoup(html, "html.parser")

    products = []
    seen = set()

    selectors = [
        "li.product",
        ".product-small",
        ".product-type-simple",
        ".product",
        ".product-item",
        ".product-card",
        "article",
    ]

    nodes = []

    for selector in selectors:
        nodes.extend(soup.select(selector))

    for item in nodes:

        title_element = item.select_one(
            ".name a, "
            ".name, "
            ".product-title a, "
            ".product-title, "
            ".woocommerce-loop-product__title, "
            "h2 a, "
            "h3 a, "
            "h4 a"
        )

        if not title_element:
            continue

        title = clean_text(
            title_element.get_text(" ", strip=True)
        )

        if not title:
            continue

        if not strict_product_match(title, query):
            continue

        link_element = item.select_one(
            "a[href]"
        )

        if not link_element:
            continue

        link = absolute_url(
            base_url,
            link_element.get("href")
        )

        if not link or link in seen:
            continue

        seen.add(link)

        text = clean_text(
            item.get_text(" ", strip=True)
        )

        price_element = item.select_one(
            ".price, "
            ".woocommerce-Price-amount, "
            ".amount"
        )

        if price_element:
            price_text = clean_text(
                price_element.get_text(" ", strip=True)
            )
        else:
            price_text = text

        price = extract_price(price_text)

        availability = detect_availability(text)

        products.append({
            "title": title,
            "link": link,
            "price": price,
            "availability": availability,
        })

        if len(products) >= MAX_PRODUCTS_PER_SITE:
            break

    return products


# ============================================================
# PROBOTS PARSER
# ============================================================

def parse_probots_products(html, base_url, query):
    soup = BeautifulSoup(html, "html.parser")

    products = []
    seen = set()

    selectors = [
        ".product-item",
        "li.product-item",
        ".product",
        ".item.product",
        "article",
    ]

    nodes = []

    for selector in selectors:
        nodes.extend(soup.select(selector))

    for item in nodes:

        title_element = item.select_one(
            ".product-item-link, "
            ".product-name a, "
            ".product-name, "
            "h2 a, "
            "h3 a, "
            "h4 a"
        )

        if not title_element:
            continue

        title = clean_text(
            title_element.get_text(" ", strip=True)
        )

        if not title:
            continue

        if not strict_product_match(title, query):
            continue

        link_element = (
            title_element
            if title_element.name == "a"
            else item.select_one("a[href]")
        )

        if not link_element:
            continue

        link = absolute_url(
            base_url,
            link_element.get("href")
        )

        if not link or link in seen:
            continue

        seen.add(link)

        text = clean_text(
            item.get_text(" ", strip=True)
        )

        price = extract_price(text)
        availability = detect_availability(text)

        products.append({
            "title": title,
            "link": link,
            "price": price,
            "availability": availability,
        })

        if len(products) >= MAX_PRODUCTS_PER_SITE:
            break

    return products


# ============================================================
# TOMSON PARSER
# ============================================================

def parse_tomson_products(html, base_url, query):
    soup = BeautifulSoup(html, "html.parser")

    products = []
    seen = set()

    selectors = [
        ".product-card",
        ".productgrid--item",
        ".product-item",
        ".product",
        ".card",
        "article",
    ]

    nodes = []

    for selector in selectors:
        nodes.extend(soup.select(selector))

    for item in nodes:

        title_element = item.select_one(
            ".product-card__title a, "
            ".product-card__title, "
            ".product-title a, "
            ".product-title, "
            "h3 a, "
            "h3, "
            "h2 a, "
            "h2"
        )

        if not title_element:
            continue

        title = clean_text(
            title_element.get_text(" ", strip=True)
        )

        if not title:
            continue

        if not strict_product_match(title, query):
            continue

        link_element = item.select_one(
            "a[href]"
        )

        if not link_element:
            continue

        link = absolute_url(
            base_url,
            link_element.get("href")
        )

        if not link or link in seen:
            continue

        seen.add(link)

        text = clean_text(
            item.get_text(" ", strip=True)
        )

        price = extract_price(text)
        availability = detect_availability(text)

        products.append({
            "title": title,
            "link": link,
            "price": price,
            "availability": availability,
        })

        if len(products) >= MAX_PRODUCTS_PER_SITE:
            break

    return products


# ============================================================
# EVELTA PARSER
# ============================================================

def parse_evelta_products(html, base_url, query):
    soup = BeautifulSoup(html, "html.parser")

    products = []
    seen = set()

    selectors = [
        ".product-item",
        ".product",
        ".product-card",
        ".item.product",
        "article",
    ]

    nodes = []

    for selector in selectors:
        nodes.extend(soup.select(selector))

    for item in nodes:

        title_element = item.select_one(
            ".product-item-link",
            ".product-name",
            ".product-title",
            "h2 a",
            "h3 a",
            "h4 a",
        )

        if not title_element:
            # BeautifulSoup select_one doesn't accept a list,
            # so fallback below.
            title_element = item.select_one(
                ".product-item-link, "
                ".product-name, "
                ".product-title, "
                "h2 a, "
                "h3 a, "
                "h4 a"
            )

        if not title_element:
            continue

        title = clean_text(
            title_element.get_text(" ", strip=True)
        )

        if not title:
            continue

        if not strict_product_match(title, query):
            continue

        link_element = (
            title_element
            if title_element.name == "a"
            else item.select_one("a[href]")
        )

        if not link_element:
            continue

        link = absolute_url(
            base_url,
            link_element.get("href")
        )

        if not link or link in seen:
            continue

        seen.add(link)

        text = clean_text(
            item.get_text(" ", strip=True)
        )

        price = extract_price(text)
        availability = detect_availability(text)

        stock_match = re.search(
            r"([\d,]+)\s+in\s+stock",
            text,
            flags=re.IGNORECASE
        )

        if stock_match:
            availability = (
                f"In Stock ({stock_match.group(1)})"
            )

        products.append({
            "title": title,
            "link": link,
            "price": price,
            "availability": availability,
        })

        if len(products) >= MAX_PRODUCTS_PER_SITE:
            break

    return products


# ============================================================
# PARSER SELECTOR
# ============================================================

def parse_products(store_type, html, base_url, query):

    if store_type == "robu":
        return parse_robu_products(
            html,
            base_url,
            query
        )

    if store_type == "shopify":
        return parse_shopify_products(
            html,
            base_url,
            query
        )

    if store_type == "element14":
        return parse_element14(
            html,
            base_url,
            query
        )

    if store_type == "tomson":
        return parse_tomson_products(
            html,
            base_url,
            query
        )

    if store_type == "evelta":
        return parse_evelta_products(
            html,
            base_url,
            query
        )

    if store_type == "probots":
        return parse_probots_products(
            html,
            base_url,
            query
        )

    return parse_generic_products(
        html,
        base_url,
        query
    )


# ============================================================
# HTTP FETCH
# ============================================================

async def fetch_http(client, url):
    try:

        response = await client.get(
            url,
            headers=HEADERS,
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
        )

        if response.status_code >= 400:
            return None

        if not response.text:
            return None

        return response.text

    except Exception:
        return None


# ============================================================
# CAMOUFOX FETCH
# ============================================================

async def fetch_camoufox(url):

    if not CAMOUFOX_AVAILABLE:
        return None

    browser = None

    try:

        async with AsyncCamoufox(
            headless=True,
            humanize=True,
        ) as browser:

            page = await browser.new_page()

            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=BROWSER_TIMEOUT,
            )

            # Give JavaScript-rendered products time to appear.
            await page.wait_for_timeout(1800)

            html = await page.content()

            return html

    except Exception:
        return None


# ============================================================
# INDIVIDUAL PRODUCT AVAILABILITY
# ============================================================

async def check_product_availability(
    client,
    product
):

    try:

        response = await client.get(
            product["link"],
            headers=HEADERS,
            timeout=8.0,
            follow_redirects=True,
        )

        if response.status_code >= 400:
            return product

        text = clean_text(response.text)

        availability = detect_availability(text)

        if availability != "Availability Unknown":
            product["availability"] = availability

        # Re-check price if search result didn't have one.
        if product.get("price") == "N/A":

            price = extract_price(text)

            if price != "N/A":
                product["price"] = price

        return product

    except Exception:
        return product


# ============================================================
# SEARCH ONE STORE
# ============================================================

async def search_store(
    store,
    query,
    client
):

    name = store["name"]

    # --------------------------------------------------------
    # ROBU
    # --------------------------------------------------------

    if store["type"] == "robu":

        urls = get_robu_urls(query)

        all_products = []

        for url in urls:

            html = await fetch_http(
                client,
                url
            )

            if html:

                products = parse_robu_products(
                    html,
                    url,
                    query
                )

                all_products.extend(products)

                if len(all_products) >= MAX_PRODUCTS_PER_SITE:
                    break

        # Deduplicate.
        unique = []

        seen = set()

        for product in all_products:

            key = (
                product["link"]
                or product["title"]
            )

            if key in seen:
                continue

            seen.add(key)
            unique.append(product)

        if unique:

            unique = unique[:MAX_PRODUCTS_PER_SITE]

            # Check actual product pages for availability.
            checked = await asyncio.gather(
                *[
                    check_product_availability(
                        client,
                        product
                    )
                    for product in unique[:10]
                ],
                return_exceptions=True
            )

            final_products = []

            for result in checked:

                if isinstance(result, dict):
                    final_products.append(result)

            if final_products:
                return final_products

            return unique

        # Camoufox fallback for Robu.
        for url in urls[:2]:

            html = await fetch_camoufox(url)

            if not html:
                continue

            products = parse_robu_products(
                html,
                url,
                query
            )

            if products:
                return products[:MAX_PRODUCTS_PER_SITE]

        return []


    # --------------------------------------------------------
    # NORMAL STORE
    # --------------------------------------------------------

    url = store["search"](query)

    html = await fetch_http(
        client,
        url
    )

    if html:

        products = parse_products(
            store["type"],
            html,
            url,
            query
        )

        if products:
            return products[:MAX_PRODUCTS_PER_SITE]

    # --------------------------------------------------------
    # CAMOUFOX FALLBACK
    # --------------------------------------------------------

    html = await fetch_camoufox(url)

    if html:

        products = parse_products(
            store["type"],
            html,
            url,
            query
        )

        if products:
            return products[:MAX_PRODUCTS_PER_SITE]

    return []


# ============================================================
# SEARCH ALL STORES
# ============================================================

async def search_all_stores(query):

    timeout = httpx.Timeout(
        connect=8.0,
        read=HTTP_TIMEOUT,
        write=8.0,
        pool=8.0,
    )

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=HEADERS,
    ) as client:

        tasks = []

        for store in STORES:

            tasks.append(
                search_store(
                    store,
                    query,
                    client
                )
            )

        results = await asyncio.gather(
            *tasks,
            return_exceptions=True
        )

        output = []

        for index, result in enumerate(results):

            store = STORES[index]

            if isinstance(result, Exception):
                output.append({
                    "site": store["name"],
                    "products": [],
                    "error": True,
                })

            else:
                output.append({
                    "site": store["name"],
                    "products": result or [],
                    "error": False,
                })

        return output


# ============================================================
# SSE HELPERS
# ============================================================

def sse(data):
    return (
        f"data: {json.dumps(data, ensure_ascii=False)}"
        f"\n\n"
    )


# ============================================================
# SSE SEARCH GENERATOR
# ============================================================

async def event_generator(query):

    query = clean_text(query)

    if not query:

        yield sse({
            "type": "error",
            "message": "Please enter a component name."
        })

        yield sse({
            "type": "done"
        })

        return

    site_names = [
        store["name"]
        for store in STORES
    ]

    # Initial list.
    yield sse({
        "type": "init",
        "sites": site_names
    })

    # --------------------------------------------------------
    # We send searching status immediately.
    # --------------------------------------------------------

    for site in site_names:

        yield sse({
            "type": "status",
            "site": site,
            "state": "searching"
        })

    # --------------------------------------------------------
    # Run searches in parallel.
    # --------------------------------------------------------

    timeout = httpx.Timeout(
        connect=8.0,
        read=HTTP_TIMEOUT,
        write=8.0,
        pool=8.0,
    )

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers=HEADERS,
    ) as client:

        async def run_one(store):

            try:

                products = await search_store(
                    store,
                    query,
                    client
                )

                return {
                    "site": store["name"],
                    "products": products or [],
                    "error": False,
                }

            except Exception as exc:

                print(
                    f"[{store['name']}] ERROR: {exc}"
                )

                return {
                    "site": store["name"],
                    "products": [],
                    "error": True,
                }

        tasks = [
            asyncio.create_task(
                run_one(store)
            )
            for store in STORES
        ]

        # ----------------------------------------------------
        # Send each site's result as soon as it finishes.
        # ----------------------------------------------------

        remaining = set(tasks)

        while remaining:

            done, remaining = await asyncio.wait(
                remaining,
                return_when=asyncio.FIRST_COMPLETED
            )

            for task in done:

                try:
                    result = task.result()

                except Exception as exc:

                    print(
                        f"[SEARCH ERROR] {exc}"
                    )

                    continue

                site = result["site"]
                products = result["products"]

                # --------------------------------------------
                # Success
                # --------------------------------------------

                if products:

                    yield sse({
                        "type": "status",
                        "site": site,
                        "state": "done",
                        "count": len(products),
                    })

                    # Add site to each product.
                    for product in products:
                        product["site"] = site

                    yield sse({
                        "type": "result",
                        "site": site,
                        "products": products,
                    })

                # --------------------------------------------
                # No result
                # --------------------------------------------

                else:

                    yield sse({
                        "type": "status",
                        "site": site,
                        "state": "done",
                        "count": 0,
                    })

    # --------------------------------------------------------
    # COMPLETE
    # --------------------------------------------------------

    yield sse({
        "type": "done"
    })


# ============================================================
# API
# ============================================================

@app.get("/")
async def root():

    return {
        "status": "ok",
        "service": "component-finder",
        "version": "3.0",
        "stores": [
            store["name"]
            for store in STORES
        ],
        "camoufox": CAMOUFOX_AVAILABLE,
    }


@app.get("/api/search")
async def search(q: str):

    return StreamingResponse(
        event_generator(q),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ============================================================
# LOCAL RUN
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
