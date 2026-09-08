import asyncio
import json
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
#                    HELPER FUNCTIONS
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


def availability_from_text(text):
    text = clean_text(text).lower()

    # --------------------------------------------------------
    # OUT OF STOCK
    # --------------------------------------------------------

    if any(
        word in text
        for word in (
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
        word in text
        for word in (
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

def extract_robu_title(card, link):

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
#                    ROBU PRODUCT CARD
# ============================================================

def find_robu_product_card(link):

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

def parse_robu_html(html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    results = []
    seen = set()

    # --------------------------------------------------------
    # STANDARD WOOCOMMERCE PRODUCT CARDS
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
            link.get("href", ""),
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

        if not title or len(title) < 2:
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
                "price": extract_price(card),
                "availability":
                    availability_from_text(
                        card_text
                    ),
            }
        )

        seen.add(href)

        if len(results) >= 30:
            break

    # --------------------------------------------------------
    # ROBU FALLBACK
    # --------------------------------------------------------

    if not results:

        for link in soup.select(
            "a[href*='/product/']"
        ):

            href = normalize_url(
                link.get("href", ""),
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

            if len(results) >= 30:
                break

    return results[:30]


# ============================================================
#                    ET STORE TITLE
# ============================================================

def extract_et_title(card, link):

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
#                    ET STORE PRODUCT CARD
# ============================================================

def find_et_product_card(link):

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

def get_et_availability(card):

    # --------------------------------------------------------
    # CHECK BUTTONS
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # CHECK CARD TEXT
    # --------------------------------------------------------

    return availability_from_text(
        card.get_text(
            " ",
            strip=True
        )
    )


# ============================================================
#                    ET STORE PARSER
# ============================================================

def parse_et_html(html):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    results = []
    seen = set()

    # --------------------------------------------------------
    # SHOPIFY PRODUCT CARDS
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
            link.get("href", ""),
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

        # ----------------------------------------------------
        # IMAGE ALT FALLBACK
        # ----------------------------------------------------

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

        price = extract_price(
            card
        )

        availability = get_et_availability(
            card
        )

        results.append(
            {
                "title": title,
                "link": href,
                "price": price,
                "availability":
                    availability,
            }
        )

        seen.add(href)

        if len(results) >= 30:
            break

    # --------------------------------------------------------
    # DIRECT SHOPIFY PRODUCT LINK FALLBACK
    # --------------------------------------------------------

    if not results:

        for link in soup.select(
            'a[href*="/products/"]'
        ):

            href = normalize_url(
                link.get("href", ""),
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

            card_text = clean_text(
                card.get_text(
                    " ",
                    strip=True
                )
            )

            price = extract_price(
                card
            )

            availability = get_et_availability(
                card
            )

            results.append(
                {
                    "title": title,
                    "link": href,
                    "price": price,
                    "availability":
                        availability,
                }
            )

            seen.add(href)

            if len(results) >= 30:
                break

    return results[:30]


# ============================================================
#                    SEARCH ET STORE
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

        print(
            f"ET Store search: "
            f"{len(products)} products "
            f"for {query!r}"
        )

        return products, None

    except Exception as exc:

        print(
            f"ET Store search failed: "
            f"{exc}"
        )

        return [], str(exc)


# ============================================================
#                    SEARCH ROBU
# ============================================================

async def search_robu(
    client,
    query
):

    try:

        url = ROBU_SEARCH.format(
            query=quote_plus(query)
        )

        print(
            f"Searching Robu: {url}"
        )

        response = await client.get(
            url,
            timeout=httpx.Timeout(
                8.0,
                connect=4.0
            )
        )

        response.raise_for_status()

        products = parse_robu_html(
            response.text
        )

        print(
            f"Robu search: "
            f"{len(products)} products "
            f"for {query!r}"
        )

        return products, None

    except Exception as exc:

        print(
            f"Robu search failed: "
            f"{exc}"
        )

        return [], str(exc)


# ============================================================
#                    SSE FUNCTION
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
#                    SEARCH EVENT GENERATOR
# ============================================================

async def event_generator(query):

    sites = [
        "ET Store",
        "Robu.in"
    ]

    # --------------------------------------------------------
    # INITIALIZE FRONTEND
    # --------------------------------------------------------

    yield sse(
        {
            "type": "init",
            "sites": sites
        }
    )

    # --------------------------------------------------------
    # START STATUS
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

        # ----------------------------------------------------
        # RUN ONE DISTRIBUTOR
        # ----------------------------------------------------

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
        # RUN BOTH DISTRIBUTORS AT SAME TIME
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
        # PROCESS RESULTS AS THEY FINISH
        # ----------------------------------------------------

        for task in asyncio.as_completed(
            tasks
        ):

            site, products, error = (
                await task
            )

            # ------------------------------------------------
            # SEARCH ERROR
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
            # SEARCH COMPLETED
            # ------------------------------------------------

            yield sse(
                {
                    "type": "status",
                    "site": site,
                    "state": "done",
                    "count": len(products)
                }
            )

            # ------------------------------------------------
            # SEND PRODUCTS
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
    # SEARCH FINISHED
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
async def search(q: str):

    query = clean_text(q)

    if not query:

        return JSONResponse(
            {
                "error":
                    "Query is required"
            },
            status_code=400
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
                "no"
        }
    )
