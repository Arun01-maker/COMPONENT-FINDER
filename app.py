import asyncio
import json
import re
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


# ============================================================
# APP
# ============================================================

app = FastAPI(title="Component Finder API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)


# ============================================================
# DISTRIBUTORS
# ============================================================

SITES = [
    {
        "name": "ET Store",
        "base": "https://etstore.in",
        "search": lambda q: (
            "https://etstore.in/index.php?"
            "route=product/search&search=" + quote_plus(q)
        ),
    },

    {
        "name": "Robu.in",
        "base": "https://robu.in",
        "search": lambda q: (
            "https://robu.in/?s=" +
            quote_plus(q) +
            "&post_type=product"
        ),
    },

    {
        "name": "element14",
        "base": "https://in.element14.com",
        "search": lambda q: (
            "https://in.element14.com/search?"
            "st=" + quote_plus(q)
        ),
    },

    {
        "name": "ElectronicsComp",
        "base": "https://www.electronicscomp.com",
        "search": lambda q: (
            "https://www.electronicscomp.com/index.php?"
            "route=product/search&search=" + quote_plus(q)
        ),
    },

    {
        "name": "Evelta",
        "base": "https://evelta.com",
        "search": lambda q: (
            "https://evelta.com/catalogsearch/result/?q=" +
            quote_plus(q)
        ),
    },

    {
        "name": "Tomson Electronics",
        "base": "https://www.tomsonelectronics.com",
        "search": lambda q: (
            "https://www.tomsonelectronics.com/search?"
            "q=" + quote_plus(q)
        ),
    },

    {
        "name": "QuartzComponents",
        "base": "https://quartzcomponents.com",
        "search": lambda q: (
            "https://quartzcomponents.com/search?"
            "q=" + quote_plus(q)
        ),
    },

    {
        "name": "MakerBazar",
        "base": "https://makerbazar.in",
        "search": lambda q: (
            "https://makerbazar.in/search?"
            "q=" + quote_plus(q)
        ),
    },

    {
        "name": "Probots",
        "base": "https://probots.co.in",
        "search": lambda q: (
            "https://probots.co.in/search?"
            "q=" + quote_plus(q)
        ),
    },

    {
        "name": "Sharvi Electronics",
        "base": "https://sharvielectronics.com",
        "search": lambda q: (
            "https://sharvielectronics.com/search?"
            "q=" + quote_plus(q)
        ),
    },

    {
        "name": "Leeds Electronics",
        "base": "https://www.leedsind.com",
        "search": None,
    },

    {
        "name": "Sparefly",
        "base": "https://sparefly.com",
        "search": None,
    },
]


SITE_MAP = {
    site["name"]: site
    for site in SITES
}


# ============================================================
# HTTP SETTINGS
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
    "Accept-Language": "en-IN,en;q=0.9",
    "Cache-Control": "no-cache",
}


HTTP_TIMEOUT = httpx.Timeout(
    connect=8.0,
    read=15.0,
    write=10.0,
    pool=10.0,
)


# ============================================================
# TEXT HELPERS
# ============================================================

def clean_text(value):
    if value is None:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(value)
    ).strip()


def normalize_text(value):
    value = clean_text(value).lower()

    value = value.replace("–", "-")
    value = value.replace("—", "-")
    value = value.replace("_", " ")

    return value


def make_absolute(base, link):
    if not link:
        return ""

    link = clean_text(link)

    if link.startswith("//"):
        return "https:" + link

    return urljoin(base, link)


def same_domain(url, base):
    try:
        a = urlparse(url).netloc.lower()
        b = urlparse(base).netloc.lower()

        return (
            a == b or
            a.endswith("." + b) or
            b.endswith("." + a)
        )

    except Exception:
        return False


# ============================================================
# SEARCH TERM EXTRACTION
# ============================================================

def extract_main_component(query):
    """
    The first query token is treated as the primary part identifier.

    Examples:

        LM2596 3A
            -> LM2596

        Arduino Nano 3.3V
            -> Arduino Nano

        ESP32
            -> ESP32
    """

    q = clean_text(query)

    if not q:
        return ""

    voltage_pattern = re.compile(
        r"^\d+(?:\.\d+)?\s*(?:v|volt|volts)$",
        re.I
    )

    current_pattern = re.compile(
        r"^\d+(?:\.\d+)?\s*(?:a|ma|amp|amps)$",
        re.I
    )

    package_words = {
        "dip",
        "smd",
        "smt",
        "qfn",
        "qfp",
        "sop",
        "soic",
        "to220",
        "to-220",
        "to263",
        "to-263",
        "0805",
        "0603",
        "1206",
    }

    tokens = q.split()

    output = []

    for token in tokens:

        t = token.strip(" ,;")

        if not t:
            continue

        if voltage_pattern.match(t):
            continue

        if current_pattern.match(t):
            continue

        if t.lower() in package_words:
            continue

        output.append(t)

    if not output:
        return tokens[0]

    return " ".join(output)


# ============================================================
# PRODUCT MATCHING
# ============================================================

def normalize_part_token(token):
    return re.sub(
        r"[^a-z0-9]+",
        "",
        normalize_text(token)
    )


def exact_component_match(title, query):
    """
    Strict product matching.

    The product must contain the primary component identifier.

    For multi-word component names, every meaningful token must
    occur in the product title.

    This prevents:

        LM2596 -> LM358
        Arduino Nano -> Arduino Uno
        ESP32 -> ESP8266

    from being returned.
    """

    title_norm = normalize_text(title)

    if not title_norm:
        return False

    main = extract_main_component(query)

    if not main:
        return False

    main_tokens = [
        normalize_part_token(x)
        for x in main.split()
        if len(normalize_part_token(x)) >= 2
    ]

    title_compact = normalize_part_token(title)

    for token in main_tokens:
        if token not in title_compact:
            return False

    return True


def specification_match(title, query):
    """
    Secondary specification filter.

    Specifications are not required when the website does not put
    them into the product title.

    However, if the specification is very distinctive and appears
    directly in the title, it is used for ranking.
    """

    title_norm = normalize_text(title)
    query_norm = normalize_text(query)

    score = 0

    query_tokens = query_norm.split()

    for token in query_tokens:

        token_clean = normalize_part_token(token)

        if not token_clean:
            continue

        if len(token_clean) < 2:
            continue

        if token_clean in normalize_part_token(title_norm):
            score += 1

    return score


# ============================================================
# AVAILABILITY
# ============================================================

IN_STOCK_PATTERNS = [
    r"\bin\s*stock\b",
    r"\binstock\b",
    r"\bavailable\s+in\s+stock\b",
    r"\bstock\s+available\b",
    r"\bavailable\s+now\b",
    r"\bready\s+to\s+ship\b",
    r"\badd\s+to\s+(?:cart|basket)\b",
]


OUT_OF_STOCK_PATTERNS = [
    r"\bout\s+of\s+stock\b",
    r"\bsold\s+out\b",
    r"\bcurrently\s+unavailable\b",
    r"\bunavailable\b",
    r"\bnot\s+available\b",
    r"\bno\s+stock\b",
    r"\bstock\s*:\s*0\b",
]


AVAILABLE_TO_ORDER_PATTERNS = [
    r"\bavailable\s+to\s+order\b",
    r"\bavailable\s+on\s+backorder\b",
    r"\bbackorder\b",
    r"\bback\s+order\b",
    r"\bpre[\s-]?order\b",
]


def parse_number(value):
    if value is None:
        return None

    text = clean_text(value)

    match = re.search(
        r"(?<![\d.])(\d{1,3}(?:,\d{3})*|\d+)(?![\d.])",
        text
    )

    if not match:
        return None

    try:
        return int(match.group(1).replace(",", ""))

    except Exception:
        return None


def availability_from_text(text):
    """
    Conservative availability parser.

    IMPORTANT:
    If there is no reliable availability indication,
    returns UNKNOWN rather than IN_STOCK.
    """

    text = normalize_text(text)

    if not text:
        return {
            "availability": "UNKNOWN",
            "stock_quantity": None,
            "availability_text": "",
        }

    # --------------------------------------------------------
    # OUT OF STOCK HAS PRIORITY
    # --------------------------------------------------------

    for pattern in OUT_OF_STOCK_PATTERNS:
        if re.search(pattern, text, re.I):
            return {
                "availability": "OUT_OF_STOCK",
                "stock_quantity": 0,
                "availability_text": "Out of Stock",
            }

    # --------------------------------------------------------
    # NUMERIC STOCK
    # --------------------------------------------------------

    numeric_patterns = [
        r"availability\s*[:\-]?\s*(\d[\d,]*)",
        r"stock\s*[:\-]?\s*(\d[\d,]*)",
        r"(\d[\d,]*)\s+in\s+stock",
        r"quantity\s*[:\-]?\s*(\d[\d,]*)",
        r"(\d[\d,]*)\s+available",
    ]

    for pattern in numeric_patterns:

        match = re.search(pattern, text, re.I)

        if match:

            number = parse_number(match.group(1))

            if number is not None:

                if number > 0:
                    return {
                        "availability": "IN_STOCK",
                        "stock_quantity": number,
                        "availability_text": (
                            f"{number:,} In Stock"
                        ),
                    }

                return {
                    "availability": "OUT_OF_STOCK",
                    "stock_quantity": 0,
                    "availability_text": "Out of Stock",
                }

    # --------------------------------------------------------
    # AVAILABLE TO ORDER
    # --------------------------------------------------------

    for pattern in AVAILABLE_TO_ORDER_PATTERNS:

        if re.search(pattern, text, re.I):

            return {
                "availability": "AVAILABLE_TO_ORDER",
                "stock_quantity": None,
                "availability_text": "Available to Order",
            }

    # --------------------------------------------------------
    # NORMAL IN-STOCK
    # --------------------------------------------------------

    for pattern in IN_STOCK_PATTERNS:

        if re.search(pattern, text, re.I):

            return {
                "availability": "IN_STOCK",
                "stock_quantity": None,
                "availability_text": "In Stock",
            }

    return {
        "availability": "UNKNOWN",
        "stock_quantity": None,
        "availability_text": "Availability not disclosed",
    }


# ============================================================
# STRUCTURED DATA AVAILABILITY
# ============================================================

def recursive_find_availability(obj):
    """
    Searches JSON-LD for:

        offers.availability
        offers.inventoryLevel
        availability
        inventory
    """

    if isinstance(obj, dict):

        for key, value in obj.items():

            key_lower = str(key).lower()

            if key_lower in {
                "availability",
                "availabilitystatus",
                "stockstatus",
            }:

                if isinstance(value, str):

                    result = availability_from_text(value)

                    if result["availability"] != "UNKNOWN":
                        return result

            if key_lower in {
                "inventorylevel",
                "inventory",
                "stock",
                "quantity",
            }:

                if isinstance(value, (int, float)):

                    number = int(value)

                    if number > 0:
                        return {
                            "availability": "IN_STOCK",
                            "stock_quantity": number,
                            "availability_text": (
                                f"{number:,} In Stock"
                            ),
                        }

            result = recursive_find_availability(value)

            if result:
                return result

    elif isinstance(obj, list):

        for item in obj:

            result = recursive_find_availability(item)

            if result:
                return result

    return None


def parse_json_ld_availability(soup):

    scripts = soup.find_all(
        "script",
        attrs={"type": "application/ld+json"}
    )

    for script in scripts:

        raw = script.string or script.get_text()

        if not raw:
            continue

        try:
            data = json.loads(raw)

        except Exception:
            continue

        result = recursive_find_availability(data)

        if result:
            return result

    return None


# ============================================================
# PRODUCT PAGE AVAILABILITY
# ============================================================

def extract_availability_from_product_page(html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    # --------------------------------------------------------
    # 1. JSON-LD
    # --------------------------------------------------------

    structured = parse_json_ld_availability(soup)

    if structured:
        return structured

    # --------------------------------------------------------
    # 2. META / ITEMPROP
    # --------------------------------------------------------

    meta_values = []

    for element in soup.find_all(
        attrs={
            "itemprop": re.compile(
                r"availability|inventory|stock",
                re.I
            )
        }
    ):

        value = (
            element.get("content")
            or element.get("href")
            or element.get_text(" ", strip=True)
        )

        if value:
            meta_values.append(value)

    if meta_values:

        meta_text = " ".join(meta_values)

        result = availability_from_text(meta_text)

        if result["availability"] != "UNKNOWN":
            return result

    # --------------------------------------------------------
    # 3. WooCommerce / Shopify common selectors
    # --------------------------------------------------------

    selectors = [
        ".stock",
        ".availability",
        ".product-stock",
        ".inventory",
        ".inventory-status",
        ".product-form__inventory",
        ".product__inventory",
        ".product-single__inventory",
        ".quantity",
        "[class*='stock']",
        "[class*='availability']",
        "[id*='stock']",
        "[id*='availability']",
    ]

    snippets = []

    for selector in selectors:

        try:
            elements = soup.select(selector)

        except Exception:
            continue

        for element in elements[:10]:

            text = element.get_text(
                " ",
                strip=True
            )

            if text:
                snippets.append(text)

    if snippets:

        result = availability_from_text(
            " ".join(snippets)
        )

        if result["availability"] != "UNKNOWN":
            return result

    # --------------------------------------------------------
    # 4. Full visible page text
    # --------------------------------------------------------

    for element in soup([
        "script",
        "style",
        "noscript",
        "svg"
    ]):
        element.decompose()

    visible_text = soup.get_text(
        " ",
        strip=True
    )

    return availability_from_text(
        visible_text
    )


# ============================================================
# PRICE
# ============================================================

def extract_price(soup):

    selectors = [
        ".price",
        ".product-price",
        ".price-box",
        ".woocommerce-Price-amount",
        ".money",
        "[class*='price']",
        "[id*='price']",
    ]

    for selector in selectors:

        try:
            element = soup.select_one(selector)

        except Exception:
            continue

        if element:

            text = clean_text(
                element.get_text(
                    " ",
                    strip=True
                )
            )

            if text and len(text) < 150:
                return text

    text = soup.get_text(
        " ",
        strip=True
    )

    match = re.search(
        r"(?:₹|Rs\.?|INR)\s*[\d,]+(?:\.\d+)?",
        text,
        re.I
    )

    if match:
        return match.group(0)

    return "N/A"


# ============================================================
# SEARCH RESULT EXTRACTION
# ============================================================

def looks_like_product_url(url):

    if not url:
        return False

    path = urlparse(url).path.lower()

    excluded = [
        "/category/",
        "/product-category/",
        "/collections/",
        "/search",
        "/cart",
        "/account",
        "/login",
        "/contact",
        "/about",
        "/blog",
        "/tag/",
        "/page/",
    ]

    for item in excluded:

        if item in path:
            return False

    return True


def generic_product_candidates(
    html,
    site,
    query
):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    candidates = []
    seen = set()

    selectors = [
        "article",
        ".product",
        ".product-item",
        ".product-card",
        ".product-small",
        ".product-thumb",
        ".product-grid-item",
        "li.product",
        "[class*='product-item']",
        "[class*='product-card']",
        "[class*='product-thumb']",
    ]

    containers = []

    for selector in selectors:

        try:
            containers.extend(
                soup.select(selector)
            )
        except Exception:
            pass

    # If the site doesn't have obvious product containers,
    # inspect links directly.
    if not containers:
        containers = soup.find_all("a")

    for container in containers:

        if container.name == "a":

            link_element = container

            title = clean_text(
                container.get_text(
                    " ",
                    strip=True
                )
            )

        else:

            link_element = container.select_one(
                "a[href]"
            )

            if not link_element:
                continue

            title_element = (
                container.select_one(
                    "h1,h2,h3,h4,h5,h6,"
                    ".product-title,"
                    ".product-name,"
                    ".name,"
                    "[class*='title'],"
                    "[class*='name']"
                )
            )

            title = clean_text(
                title_element.get_text(
                    " ",
                    strip=True
                )
                if title_element
                else link_element.get_text(
                    " ",
                    strip=True
                )
            )

        if not title:
            continue

        if len(title) < 3 or len(title) > 500:
            continue

        link = make_absolute(
            site["base"],
            link_element.get("href")
        )

        if not link:
            continue

        if not same_domain(
            link,
            site["base"]
        ):
            continue

        if not looks_like_product_url(link):
            continue

        # STRICT component filter
        if not exact_component_match(
            title,
            query
        ):
            continue

        key = link.lower()

        if key in seen:
            continue

        seen.add(key)

        candidates.append({
            "title": title,
            "link": link,
            "price": "N/A",
            "score": specification_match(
                title,
                query
            ),
        })

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    return candidates[:12]


# ============================================================
# SITE-SPECIFIC SEARCH PARSER
# ============================================================

def parse_search_results(
    html,
    site,
    query
):

    name = site["name"]

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    candidates = []

    # --------------------------------------------------------
    # ELEMENT14
    # --------------------------------------------------------

    if name == "element14":

        selectors = [
            "div.search-result",
            "div.product",
            "article",
            "[class*='product']",
        ]

        containers = []

        for selector in selectors:

            containers.extend(
                soup.select(selector)
            )

        for item in containers:

            title_el = item.select_one(
                "h2 a, h3 a, "
                ".product-title a, "
                "[class*='title'] a"
            )

            if not title_el:
                continue

            title = clean_text(
                title_el.get_text(
                    " ",
                    strip=True
                )
            )

            link = make_absolute(
                site["base"],
                title_el.get("href")
            )

            if not exact_component_match(
                title,
                query
            ):
                continue

            candidates.append({
                "title": title,
                "link": link,
                "price": extract_price(item),
                "score": specification_match(
                    title,
                    query
                ),
            })

    # --------------------------------------------------------
    # ELECTRONICSCOMP
    # --------------------------------------------------------

    elif name == "ElectronicsComp":

        containers = soup.select(
            ".product-thumb, "
            ".product-layout, "
            ".product-item, "
            ".product"
        )

        for item in containers:

            title_el = item.select_one(
                ".name a, "
                ".product-name a, "
                "h2 a, "
                "h3 a, "
                "h4 a, "
                "a[href]"
            )

            if not title_el:
                continue

            title = clean_text(
                title_el.get_text(
                    " ",
                    strip=True
                )
            )

            link = make_absolute(
                site["base"],
                title_el.get("href")
            )

            if not exact_component_match(
                title,
                query
            ):
                continue

            candidates.append({
                "title": title,
                "link": link,
                "price": extract_price(item),
                "score": specification_match(
                    title,
                    query
                ),
            })

    # --------------------------------------------------------
    # ET STORE
    # --------------------------------------------------------

    elif name == "ET Store":

        containers = soup.select(
            ".product-thumb"
        )

        for item in containers:

            title_el = item.select_one(
                ".caption h4 a"
            )

            if not title_el:
                continue

            title = clean_text(
                title_el.get_text(
                    " ",
                    strip=True
                )
            )

            link = make_absolute(
                site["base"],
                title_el.get("href")
            )

            if not exact_component_match(
                title,
                query
            ):
                continue

            candidates.append({
                "title": title,
                "link": link,
                "price": extract_price(item),
                "score": specification_match(
                    title,
                    query
                ),
            })

    # --------------------------------------------------------
    # ROBU
    # --------------------------------------------------------

    elif name == "Robu.in":

        containers = soup.select(
            ".product-small, "
            ".product-type-simple, "
            "li.product, "
            ".product"
        )

        for item in containers:

            title_el = item.select_one(
                ".name a, "
                ".product-title a, "
                ".woocommerce-loop-product__title, "
                "h2 a, h3 a, h4 a, "
                "a[href]"
            )

            if not title_el:
                continue

            title = clean_text(
                title_el.get_text(
                    " ",
                    strip=True
                )
            )

            link = make_absolute(
                site["base"],
                title_el.get("href")
            )

            if not exact_component_match(
                title,
                query
            ):
                continue

            candidates.append({
                "title": title,
                "link": link,
                "price": extract_price(item),
                "score": specification_match(
                    title,
                    query
                ),
            })

    # --------------------------------------------------------
    # GENERIC SHOPIFY / OTHER SITES
    # --------------------------------------------------------

    if not candidates:

        candidates = generic_product_candidates(
            html,
            site,
            query
        )

    # --------------------------------------------------------
    # REMOVE DUPLICATES
    # --------------------------------------------------------

    unique = {}
    for product in candidates:

        link = product["link"]

        if link not in unique:
            unique[link] = product

    candidates = list(
        unique.values()
    )

    candidates.sort(
        key=lambda x: x.get(
            "score",
            0
        ),
        reverse=True
    )

    return candidates[:12]


# ============================================================
# HTTP FETCH
# ============================================================

async def fetch_http(
    client,
    url
):

    try:

        response = await client.get(
            url,
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
        )

        if response.status_code >= 400:
            return None

        content_type = (
            response.headers.get(
                "content-type",
                ""
            ).lower()
        )

        if (
            "text/html" not in content_type
            and "application/xhtml" not in content_type
        ):
            return None

        if len(response.text) < 500:
            return None

        return response.text

    except Exception:
        return None


# ============================================================
# CAMOUFOX BROWSER
# ============================================================

class BrowserFetcher:

    def __init__(self):
        self.browser_context = None
        self.browser = None

    async def start(self):

        if not CAMOUFOX_AVAILABLE:
            return False

        try:

            self.browser_context = AsyncCamoufox(
                headless=True
            )

            self.browser = (
                await self.browser_context.__aenter__()
            )

            return True

        except Exception:

            self.browser = None
            self.browser_context = None

            return False

    async def fetch(self, url):

        if not self.browser:
            return None

        page = None

        try:

            page = await self.browser.new_page()

            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=25000,
            )

            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=7000
                )
            except Exception:
                pass

            html = await page.content()

            return html

        except Exception:

            return None

        finally:

            if page:

                try:
                    await page.close()
                except Exception:
                    pass

    async def close(self):

        if self.browser_context:

            try:
                await self.browser_context.__aexit__(
                    None,
                    None,
                    None
                )
            except Exception:
                pass

            self.browser_context = None
            self.browser = None


# ============================================================
# VERIFY ONE PRODUCT
# ============================================================

async def verify_product(
    client,
    browser,
    product
):

    url = product["link"]

    html = await fetch_http(
        client,
        url
    )

    # --------------------------------------------------------
    # CAMOUFOX FALLBACK
    # --------------------------------------------------------

    if not html and browser:

        html = await browser.fetch(
            url
        )

    if not html:

        return {
            **product,
            "availability": "UNKNOWN",
            "stock_quantity": None,
            "availability_text": (
                "Product page could not be verified"
            ),
            "verified": False,
        }

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    # Correct title from product page
    page_title = ""

    for selector in [
        "h1",
        ".product_title",
        ".product-title",
        "meta[property='og:title']",
        "title",
    ]:

        try:

            element = soup.select_one(
                selector
            )

        except Exception:
            element = None

        if element:

            if element.name == "meta":
                value = element.get(
                    "content",
                    ""
                )
            else:
                value = element.get_text(
                    " ",
                    strip=True
                )

            if value:
                page_title = clean_text(
                    value
                )
                break

    if page_title:

        product["title"] = page_title

    # --------------------------------------------------------
    # AVAILABILITY
    # --------------------------------------------------------

    availability = (
        extract_availability_from_product_page(
            html
        )
    )

    # --------------------------------------------------------
    # PRICE
    # --------------------------------------------------------

    price = extract_price(
        soup
    )

    if price != "N/A":
        product["price"] = price

    return {
        **product,
        "availability": availability[
            "availability"
        ],
        "stock_quantity": availability[
            "stock_quantity"
        ],
        "availability_text": availability[
            "availability_text"
        ],
        "verified": True,
    }


# ============================================================
# SEARCH ONE SITE
# ============================================================

async def search_site(
    client,
    browser,
    site,
    query
):

    # Sites without a verified public search endpoint
    # are deliberately marked UNKNOWN instead of inventing
    # search results.

    if not site.get("search"):

        return {
            "products": [],
            "state": "not_supported",
            "message": (
                "No reliable public search endpoint"
            ),
        }

    search_url = site["search"](query)

    html = await fetch_http(
        client,
        search_url
    )

    # Browser fallback
    if not html and browser:

        html = await browser.fetch(
            search_url
        )

    if not html:

        return {
            "products": [],
            "state": "failed",
            "message": "Search page unavailable",
        }

    candidates = parse_search_results(
        html,
        site,
        query
    )

    if not candidates:

        return {
            "products": [],
            "state": "done",
            "message": "No exact matching products",
        }

    # --------------------------------------------------------
    # VERIFY PRODUCT PAGES
    # --------------------------------------------------------

    verified_products = []

    # Verify a limited number of best matches.
    # This prevents a search for a generic component from
    # opening hundreds of pages.
    candidates = candidates[:8]

    semaphore = asyncio.Semaphore(3)

    async def verify_limited(product):

        async with semaphore:

            return await verify_product(
                client,
                browser,
                product
            )

    results = await asyncio.gather(
        *[
            verify_limited(product)
            for product in candidates
        ],
        return_exceptions=True
    )

    for result in results:

        if isinstance(
            result,
            Exception
        ):
            continue

        verified_products.append(
            result
        )

    # --------------------------------------------------------
    # ONLY SHOW PRODUCTS WITH A VERIFIED STATUS
    #
    # UNKNOWN products are still shown, but clearly marked.
    # They are never called "In Stock".
    # --------------------------------------------------------

    return {
        "products": verified_products,
        "state": "done",
        "message": "Product pages verified",
    }


# ============================================================
# SSE
# ============================================================

def sse(data):

    return (
        "data: " +
        json.dumps(
            data,
            ensure_ascii=False
        ) +
        "\n\n"
    )


async def event_generator(
    query,
    requested_sites
):

    query = clean_text(
        query
    )

    # --------------------------------------------------------
    # SELECT SITES
    # --------------------------------------------------------

    if not requested_sites:

        selected = SITES

    else:

        selected_names = {
            clean_text(name)
            for name in requested_sites
        }

        selected = [
            site
            for site in SITES
            if site["name"] in selected_names
        ]

    # --------------------------------------------------------
    # INIT
    # --------------------------------------------------------

    yield sse({
        "type": "init",
        "sites": [
            site["name"]
            for site in selected
        ],
    })

    if not selected:

        yield sse({
            "type": "done"
        })

        return

    browser = BrowserFetcher()

    # Start browser once.
    # It is reused for all failed pages.
    await browser.start()

    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
    ) as client:

        semaphore = asyncio.Semaphore(4)

        async def process_site(site):

            async with semaphore:

                site_name = site["name"]

                yield_data = []

                yield_data.append(
                    sse({
                        "type": "status",
                        "site": site_name,
                        "state": "searching",
                    })
                )

                try:

                    result = await search_site(
                        client,
                        browser,
                        site,
                        query
                    )

                    products = result.get(
                        "products",
                        []
                    )

                    yield_data.append(
                        sse({
                            "type": "status",
                            "site": site_name,
                            "state": "done",
                            "count": len(products),
                            "message": result.get(
                                "message",
                                ""
                            ),
                        })
                    )

                    if products:

                        yield_data.append(
                            sse({
                                "type": "result",
                                "site": site_name,
                                "products": products,
                            })
                        )

                except Exception as exc:

                    yield_data.append(
                        sse({
                            "type": "status",
                            "site": site_name,
                            "state": "done",
                            "count": 0,
                            "message": (
                                "Search error"
                            ),
                        })
                    )

                return yield_data

        tasks = [
            asyncio.create_task(
                process_site(site)
            )
            for site in selected
        ]

        for task in asyncio.as_completed(
            tasks
        ):

            try:

                messages = await task

                for message in messages:
                    yield message

            except Exception:
                pass

    await browser.close()

    yield sse({
        "type": "done"
    })


# ============================================================
# API
# ============================================================

@app.get("/api/sites")
async def get_sites():

    return {
        "sites": [
            {
                "name": site["name"],
                "search_supported": bool(
                    site.get("search")
                ),
            }
            for site in SITES
        ]
    }


@app.get("/api/search")
async def search(
    q: str,
    sites: list[str] | None = Query(
        default=None
    )
):

    return StreamingResponse(
        event_generator(
            q,
            sites
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
        "name": "Component Finder API",
        "status": "online",
        "sites": [
            site["name"]
            for site in SITES
        ],
    }
