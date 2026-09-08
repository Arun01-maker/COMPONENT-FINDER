import asyncio
import json
import re
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse


# ============================================================
#                    FASTAPI APP
# ============================================================

app = FastAPI()


# ============================================================
#                    CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)


# ============================================================
#                    SEARCH URLS
# ============================================================

ROBU_SEARCH = (
    "https://robu.in/?s={query}&post_type=product"
)

ET_SEARCH = (
    "https://www.etstore.in/search?q={query}"
)


# ============================================================
#                    HTTP HEADERS
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
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
#                    TEXT HELPERS
# ============================================================

def clean_text(value):
    return " ".join(
        str(value or "").split()
    )


def normalize_url(url, base):
    return urljoin(
        base,
        clean_text(url)
    )


# ============================================================
#              EXTRACT ACTUAL COMPONENT NAME
# ============================================================

def extract_component_name(query):
    """
    The frontend sends a combined query such as:

        LM2596 SMD 3A
        Arduino Nano SMD 5V
        ESP32 3.3V
        Resistor SMD 0805

    We remove known parameter values and keep the actual
    component name.

    The resulting component name is then used for strict
    product-title matching.
    """

    query = clean_text(query)

    if not query:
        return ""

    words = query.split()

    filtered = []

    skip_next = False

    for i, word in enumerate(words):

        if skip_next:
            skip_next = False
            continue

        lower = word.lower()

        # ----------------------------------------------------
        # MOUNT TYPE
        # ----------------------------------------------------

        if lower in (
            "smd",
            "smt",
            "dip",
            "through",
            "hole",
            "through-hole",
        ):
            continue

        # ----------------------------------------------------
        # COMMON VOLTAGE VALUES
        # ----------------------------------------------------

        if re.fullmatch(
            r"\d+(?:\.\d+)?\s*v",
            lower,
        ):
            continue

        # ----------------------------------------------------
        # COMMON CURRENT VALUES
        # ----------------------------------------------------

        if re.fullmatch(
            r"\d+(?:\.\d+)?\s*(?:a|ma|ua)",
            lower,
        ):
            continue

        # ----------------------------------------------------
        # PACKAGE / FOOTPRINT
        # ----------------------------------------------------

        if re.fullmatch(
            r"(?:sop|soic|qfn|qfp|dip|to|dfn|"
            r"bga|lga|sot|msop|tssop|ssop)"
            r"[-_]?\d+[a-z0-9-]*",
            lower,
        ):
            continue

        if re.fullmatch(
            r"\d{3,4}",
            lower,
        ):
            # Usually footprints such as 0805,
            # 0603, 1206, etc.
            if len(lower) in (3, 4):
                continue

        # ----------------------------------------------------
        # OTHERWISE KEEP THE WORD
        # ----------------------------------------------------

        filtered.append(word)

    component_name = clean_text(
        " ".join(filtered)
    )

    # --------------------------------------------------------
    # FALLBACK
    # --------------------------------------------------------

    if not component_name:
        component_name = words[0]

    return component_name


# ============================================================
#             STRICT PRODUCT NAME MATCHING
# ============================================================

def product_matches_component(
    product_title,
    component_name
):
    """
    Product is accepted only when the searched component
    name is present in the product title.

    Examples:

        LM2596
        -> LM2596S DC-DC Buck Converter       YES

        LM2596
        -> D882 Transistor                    NO

        Arduino Nano
        -> Arduino Nano V3                    YES

        Arduino Nano
        -> Arduino Uno                        NO
    """

    title = clean_text(
        product_title
    ).lower()

    component = clean_text(
        component_name
    ).lower()

    if not title or not component:
        return False

    # --------------------------------------------------------
    # NORMALIZE
    # --------------------------------------------------------

    title_normalized = re.sub(
        r"[^a-z0-9]+",
        " ",
        title
    ).strip()

    component_normalized = re.sub(
        r"[^a-z0-9]+",
        " ",
        component
    ).strip()

    if not component_normalized:
        return False

    # --------------------------------------------------------
    # DIRECT PHRASE MATCH
    # --------------------------------------------------------

    if component_normalized in title_normalized:
        return True

    # --------------------------------------------------------
    # HANDLE PART-NUMBER STYLE NAMES
    #
    # Example:
    # Search: LM2596
    # Title: LM2596S DC-DC Buck Converter
    #
    # LM2596 should match LM2596S.
    # --------------------------------------------------------

    compact_component = re.sub(
        r"[^a-z0-9]",
        "",
        component_normalized
    )

    compact_title = re.sub(
        r"[^a-z0-9]",
        "",
        title_normalized
    )

    if (
        compact_component
        and compact_component in compact_title
    ):
        return True

    return False


# ============================================================
#                    FILTER RESULTS
# ============================================================

def filter_matching_products(
    products,
    query
):

    component_name = extract_component_name(
        query
    )

    filtered = []

    seen = set()

    for product in products:

        title = clean_text(
            product.get(
                "title",
                ""
            )
        )

        link = clean_text(
            product.get(
                "link",
                ""
            )
        )

        if not title:
            continue

        if not product_matches_component(
            title,
            component_name
        ):
            continue

        if link in seen:
            continue

        seen.add(link)

        filtered.append(product)

    print(
        f"Component filter: "
        f"{query!r} -> "
        f"{component_name!r} -> "
        f"{len(filtered)} matches"
    )

    return filtered[:30]


# ============================================================
#                    AVAILABILITY
# ============================================================

def availability_from_text(text):

    text = clean_text(
        text
    ).lower()

    # --------------------------------------------------------
    # OUT OF STOCK
    # --------------------------------------------------------

    if any(
        value in text
        for value in (
            "out of stock",
            "sold out",
            "currently unavailable",
            "unavailable",
            "not available",
        )
    ):
        return "Out of Stock"

    # --------------------------------------------------------
    # IN STOCK
    # --------------------------------------------------------

    if any(
        value in text
        for value in (
            "add to cart",
            "add-to-cart",
            "buy now",
            "in stock",
            "available",
        )
    ):
        return "In Stock"

    return "Availability Unknown"


# ============================================================
#                    PRICE
# ============================================================

def extract_price(card):

    price = card.select_one(
        ".price, "
        ".woocommerce-Price-amount, "
        ".amount, "
        "[class*='price'], "
        ".price-item--regular, "
        ".price-item"
    )

    if price:

        return clean_text(
            price.get_text(
                " ",
                strip=True
            )
        )

    return "N/A"


# ============================================================
#                    ROBU TITLE
# ============================================================

def extract_robu_title(
    card,
    link
):

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

        element = card.select_one(
            selector
        )

        if element:

            title = clean_text(
                element.get_text(
                    " ",
                    strip=True
                )
            )

            if title:
                return title

    title = clean_text(
        link.get(
            "aria-label",
            ""
        )
    )

    if title:
        return title

    return clean_text(
        link.get_text(
            " ",
            strip=True
        )
    )


# ============================================================
#                    ROBU CARD
# ============================================================

def find_robu_product_card(
    link
):

    selectors = (
        "li.product",
        ".product-small",
        ".product-type-simple",
        ".product-type-variable",
        "article.product",
        ".product",
    )

    for selector in selectors:

        card = link.find_parent(
            selector
        )

        if card:
            return card

    parent = link.parent

    for _ in range(6):

        if parent is None:
            break

        text = clean_text(
            parent.get_text(
                " ",
                strip=True
            )
        )

        lower_text = text.lower()

        if (
            len(text) >= 20
            and len(text) <= 3000
            and (
                "₹" in text
                or "add to cart" in lower_text
                or "read more" in lower_text
                or "wishlist" in lower_text
                or "in stock" in lower_text
                or "out of stock" in lower_text
            )
        ):
            return parent

        parent = parent.parent

    return link.parent


# ============================================================
#                    ROBU PARSER
# ============================================================

def parse_robu_html(
    html
):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    results = []

    seen = set()

    # --------------------------------------------------------
    # NORMAL PRODUCT CARDS
    # --------------------------------------------------------

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
            link.get(
                "href",
                ""
            ),
            "https://robu.in/"
        )

        if (
            "/product/" not in href
            or href in seen
        ):
            continue

        title = extract_robu_title(
            card,
            link
        )

        if (
            not title
            or len(title) < 2
        ):
            continue

        card_text = clean_text(
            card.get_text(
                " ",
                strip=True
            )
        )

        results.append(
            {
                "title": title,
                "link": href,
                "price":
                    extract_price(card),
                "availability":
                    availability_from_text(
                        card_text
                    ),
            }
        )

        seen.add(href)

        if len(results) >= 50:
            break

    # --------------------------------------------------------
    # BROAD FALLBACK
    # --------------------------------------------------------

    if not results:

        for link in soup.select(
            "a[href*='/product/']"
        ):

            href = normalize_url(
                link.get(
                    "href",
                    ""
                ),
                "https://robu.in/"
            )

            if (
                not href
                or "/product/" not in href
                or href in seen
            ):
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

            if (
                not title
                or len(title) < 3
                or len(title) > 250
            ):
                continue

            card = find_robu_product_card(
                link
            )

            if not card:
                continue

            card_text = clean_text(
                card.get_text(
                    " ",
                    strip=True
                )
            )

            if not card_text:
                continue

            results.append(
                {
                    "title": title,
                    "link": href,
                    "price":
                        extract_price(card),
                    "availability":
                        availability_from_text(
                            card_text
                        ),
                }
            )

            seen.add(href)

            if len(results) >= 50:
                break

    return results


# ============================================================
#                    ET STORE TITLE
# ============================================================

def extract_et_title(
    card,
    link
):

    selectors = (
        ".card__heading a",
        ".card__heading",
        ".full-unstyled-link",
        ".product-title",
        ".product-card__title",
        "h3 a",
        "h3",
        "h2 a",
        "h2",
        "h4 a",
        "h4",
    )

    for selector in selectors:

        element = card.select_one(
            selector
        )

        if element:

            title = clean_text(
                element.get_text(
                    " ",
                    strip=True
                )
            )

            if title:
                return title

    title = clean_text(
        link.get(
            "aria-label",
            ""
        )
    )

    if title:
        return title

    return clean_text(
        link.get_text(
            " ",
            strip=True
        )
    )


# ============================================================
#                    ET STORE CARD
# ============================================================

def find_et_product_card(
    link
):

    selectors = (
        ".card-wrapper",
        ".product-card-wrapper",
        ".card",
        "li.grid__item",
        ".product-card",
    )

    for selector in selectors:

        card = link.find_parent(
            selector
        )

        if card:
            return card

    parent = link.parent

    for _ in range(7):

        if parent is None:
            break

        text = clean_text(
            parent.get_text(
                " ",
                strip=True
            )
        )

        lower_text = text.lower()

        if (
            len(text) >= 20
            and (
                "₹" in text
                or "add to cart" in lower_text
                or "sold out" in lower_text
                or "buy now" in lower_text
            )
        ):
            return parent

        parent = parent.parent

    return link.parent


# ============================================================
#                    ET STORE AVAILABILITY
# ============================================================

def get_et_availability(
    card
):

    buttons = card.select(
        "button, "
        "a, "
        ".quick-add__submit, "
        ".button"
    )

    button_text = " ".join(
        clean_text(
            button.get_text(
                " ",
                strip=True
            )
        ).lower()
        for button in buttons
    )

    if (
        "sold out" in button_text
        or "out of stock" in button_text
    ):
        return "Out of Stock"

    if (
        "add to cart" in button_text
        or "add to bag" in button_text
        or "buy now" in button_text
    ):
        return "In Stock"

    return availability_from_text(
        card.get_text(
            " ",
            strip=True
        )
    )


# ============================================================
#                    ET STORE PARSER
# ============================================================

def parse_et_html(
    html
):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    results = []

    seen = set()

    # --------------------------------------------------------
    # PRODUCT CARDS
    # --------------------------------------------------------

    cards = soup.select(
        ".card-wrapper, "
        ".product-card-wrapper, "
        ".card, "
        "li.grid__item, "
        ".product-card"
    )

    for card in cards:

        link = card.select_one(
            'a[href*="/products/"]'
        )

        if not link:
            continue

        href = normalize_url(
            link.get(
                "href",
                ""
            ),
            "https://www.etstore.in/"
        )

        if (
            "/products/" not in href
            or href in seen
        ):
            continue

        title = extract_et_title(
            card,
            link
        )

        if not title:

            image = card.select_one(
                "img"
            )

            if image:

                title = clean_text(
                    image.get(
                        "alt",
                        ""
                    )
                )

        if (
            not title
            or len(title) < 3
        ):
            continue

        results.append(
            {
                "title": title,
                "link": href,
                "price":
                    extract_price(card),
                "availability":
                    get_et_availability(
                        card
                    ),
            }
        )

        seen.add(href)

        if len(results) >= 50:
            break

    # --------------------------------------------------------
    # FALLBACK PRODUCT LINKS
    # --------------------------------------------------------

    if not results:

        for link in soup.select(
            'a[href*="/products/"]'
        ):

            href = normalize_url(
                link.get(
                    "href",
                    ""
                ),
                "https://www.etstore.in/"
            )

            if (
                not href
                or "/products/" not in href
                or href in seen
            ):
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

                image = link.select_one(
                    "img"
                )

                if image:

                    title = clean_text(
                        image.get(
                            "alt",
                            ""
                        )
                    )

            if (
                not title
                or len(title) < 3
            ):
                continue

            card = find_et_product_card(
                link
            )

            if not card:
                continue

            results.append(
                {
                    "title": title,
                    "link": href,
                    "price":
                        extract_price(card),
                    "availability":
                        get_et_availability(
                            card
                        ),
                }
            )

            seen.add(href)

            if len(results) >= 50:
                break

    return results


# ============================================================
#                    ET STORE SEARCH
# ============================================================

async def search_etstore(
    client,
    query
):

    try:

        url = ET_SEARCH.format(
            query=quote_plus(query)
        )

        print(
            f"Searching ET Store: {url}"
        )

        response = await client.get(
            url,
            timeout=httpx.Timeout(
                8.0,
                connect=4.0
            )
        )

        response.raise_for_status()

        products = parse_et_html(
            response.text
        )

        # ----------------------------------------------------
        # STRICT COMPONENT FILTER
        # ----------------------------------------------------

        products = filter_matching_products(
            products,
            query
        )

        print(
            f"ET Store final results: "
            f"{len(products)}"
        )

        return products, None

    except Exception as exc:

        print(
            f"ET Store search failed: "
            f"{exc}"
        )

        return [], str(exc)


# ============================================================
#                    ROBU HTTP SEARCH
# ============================================================

async def search_robu_http(
    client,
    query
):

    url = ROBU_SEARCH.format(
        query=quote_plus(query)
    )

    print(
        f"Searching Robu HTTP: {url}"
    )

    response = await client.get(
        url,
        timeout=httpx.Timeout(
            7.0,
            connect=3.0
        )
    )

    response.raise_for_status()

    products = parse_robu_html(
        response.text
    )

    products = filter_matching_products(
        products,
        query
    )

    print(
        f"Robu HTTP final results: "
        f"{len(products)}"
    )

    return products


# ============================================================
#                    ROBU CAMOUFOX SEARCH
# ============================================================

async def search_robu_camoufox(
    query
):

    print(
        f"Starting Camoufox for Robu: "
        f"{query!r}"
    )

    from camoufox.async_api import (
        AsyncCamoufox
    )

    url = ROBU_SEARCH.format(
        query=quote_plus(query)
    )

    products = []

    async with AsyncCamoufox(
        headless=True
    ) as browser:

        page = await browser.new_page()

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=20000
        )

        # Give the Robu page a short amount of time
        # to finish rendering.
        await page.wait_for_timeout(
            1200
        )

        html = await page.content()

        products = parse_robu_html(
            html
        )

        products = filter_matching_products(
            products,
            query
        )

        await page.close()

    print(
        f"Robu Camoufox final results: "
        f"{len(products)}"
    )

    return products


# ============================================================
#                    ROBU SEARCH
# ============================================================

async def search_robu(
    client,
    query
):

    try:

        # ----------------------------------------------------
        # FIRST: FAST HTTP REQUEST
        # ----------------------------------------------------

        products = await search_robu_http(
            client,
            query
        )

        return products, None

    except Exception as http_error:

        print(
            f"Robu HTTP failed: "
            f"{http_error}"
        )

        # ----------------------------------------------------
        # SECOND: CAMOUFOX FALLBACK
        # ----------------------------------------------------

        try:

            products = await asyncio.wait_for(
                search_robu_camoufox(
                    query
                ),
                timeout=25
            )

            return products, None

        except Exception as camoufox_error:

            print(
                f"Robu Camoufox failed: "
                f"{camoufox_error}"
            )

            return [], (
                f"HTTP search failed; "
                f"browser search failed"
            )


# ============================================================
#                    SSE
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
#                    EVENT GENERATOR
# ============================================================

async def event_generator(
    query
):

    sites = [
        "ET Store",
        "Robu.in"
    ]

    # --------------------------------------------------------
    # INITIALIZE
    # --------------------------------------------------------

    yield sse(
        {
            "type": "init",
            "sites": sites
        }
    )

    # --------------------------------------------------------
    # SEARCHING STATUS
    # --------------------------------------------------------

    for site in sites:

        yield sse(
            {
                "type": "status",
                "site": site,
                "state": "searching"
            }
        )

    # --------------------------------------------------------
    # HTTP CLIENT
    # --------------------------------------------------------

    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True
    ) as client:

        async def run_site(
            site,
            search_function
        ):

            try:

                products, error = (
                    await search_function(
                        client,
                        query
                    )
                )

                return (
                    site,
                    products,
                    error
                )

            except Exception as exc:

                return (
                    site,
                    [],
                    str(exc)
                )

        # ----------------------------------------------------
        # SEARCH BOTH AT SAME TIME
        # ----------------------------------------------------

        tasks = [
            asyncio.create_task(
                run_site(
                    "ET Store",
                    search_etstore
                )
            ),
            asyncio.create_task(
                run_site(
                    "Robu.in",
                    search_robu
                )
            )
        ]

        # ----------------------------------------------------
        # PROCESS EACH SITE AS IT FINISHES
        # ----------------------------------------------------

        for task in asyncio.as_completed(
            tasks
        ):

            site, products, error = (
                await task
            )

            # ------------------------------------------------
            # ERROR
            # ------------------------------------------------

            if error:

                yield sse(
                    {
                        "type": "status",
                        "site": site,
                        "state": "error",
                        "count": 0,
                        "message":
                            "Search failed"
                    }
                )

                continue

            # ------------------------------------------------
            # DONE
            # ------------------------------------------------

            yield sse(
                {
                    "type": "status",
                    "site": site,
                    "state": "done",
                    "count":
                        len(products)
                }
            )

            # ------------------------------------------------
            # RESULTS
            # ------------------------------------------------

            if products:

                yield sse(
                    {
                        "type": "result",
                        "site": site,
                        "products": products
                    }
                )

    # --------------------------------------------------------
    # COMPLETE
    # --------------------------------------------------------

    yield sse(
        {
            "type": "done"
        }
    )


# ============================================================
#                    ROOT
# ============================================================

@app.get("/")
async def root():

    return JSONResponse(
        {
            "status": "ok",
            "service": "component-finder"
        }
    )


# ============================================================
#                    SEARCH API
# ============================================================

@app.get("/api/search")
async def search(
    q: str
):

    query = clean_text(
        q
    )

    if not query:

        return JSONResponse(
            {
                "error":
                    "Query is required"
            },
            status_code=400
        )

    return StreamingResponse(
        event_generator(
            query
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control":
                "no-cache, no-transform",
            "Connection":
                "keep-alive",
            "X-Accel-Buffering":
                "no"
        }
    )
