import asyncio
import json
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from camoufox.async_api import AsyncCamoufox


# ============================================================
#                    FASTAPI APPLICATION
# ============================================================

app = FastAPI()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
#                    ROBU API
# ============================================================

ROBU_API = "https://robu.in/wp-json/wc/store/v1/products"


# ============================================================
#                    DISTRIBUTORS
# ============================================================

DISTRIBUTORS = [
    {
        "name": "ET Store",
        "url": lambda q: (
            "https://etstore.in/index.php"
            f"?route=product/search&search={quote_plus(q)}"
        ),
    },
    {
        "name": "Robu.in",
    },
]


# ============================================================
#                    TEXT CLEANING
# ============================================================

def clean_text(value):
    return " ".join((value or "").split())


# ============================================================
#              ROBU AVAILABILITY DETECTION
# ============================================================

def robu_availability(product):
    """
    Determine availability from Robu's WooCommerce
    Store API product data.
    """

    # Backorder takes priority
    if product.get("is_on_backorder") is True:
        return "Backorder"

    # Direct inventory status
    if product.get("is_in_stock") is True:
        return "In Stock"

    if product.get("is_in_stock") is False:
        return "Out of Stock"

    # Fallback to stock availability text
    stock = product.get("stock_availability") or {}

    if isinstance(stock, dict):
        stock_text = clean_text(
            stock.get("text", "")
        ).lower()
    else:
        stock_text = clean_text(
            str(stock)
        ).lower()

    if any(
        value in stock_text
        for value in [
            "out of stock",
            "sold out",
            "unavailable",
        ]
    ):
        return "Out of Stock"

    if any(
        value in stock_text
        for value in [
            "in stock",
            "available",
        ]
    ):
        return "In Stock"

    return "Availability Unknown"


# ============================================================
#              GENERAL AVAILABILITY DETECTION
# ============================================================

def text_availability(text):
    text = clean_text(text).lower()

    if any(
        value in text
        for value in [
            "out of stock",
            "sold out",
            "currently unavailable",
            "not available",
            "unavailable",
        ]
    ):
        return "Out of Stock"

    if any(
        value in text
        for value in [
            "in stock",
            "add to cart",
            "buy now",
            "add to basket",
        ]
    ):
        return "In Stock"

    return "Availability Unknown"


# ============================================================
#                  ROBU DIRECT API SEARCH
# ============================================================

async def search_robu_api(client, query):
    """
    Search Robu using its WooCommerce Store API.

    Returns:
        list   -> successful API request
        None   -> API failed and Camoufox fallback is needed
    """

    try:
        response = await client.get(
            ROBU_API,
            params={
                "search": query,
                "per_page": 30,
                "catalog_visibility": "visible",
            },
            timeout=10,
        )

        response.raise_for_status()

        data = response.json()

        if not isinstance(data, list):
            print("Robu API returned unexpected data")
            return None

        results = []
        seen_links = set()

        for product in data:

            title = clean_text(
                product.get("name", "")
            )

            link = clean_text(
                product.get("permalink", "")
            )

            if not title or not link:
                continue

            if link in seen_links:
                continue

            seen_links.add(link)

            prices = product.get("prices") or {}

            price = (
                prices.get("price_html")
                or prices.get("price")
                or ""
            )

            price = clean_text(price)

            results.append(
                {
                    "title": title,
                    "link": link,
                    "price": price or "N/A",
                    "availability": robu_availability(product),
                    "sku": clean_text(
                        product.get("sku", "")
                    ),
                }
            )

        print(
            f"Robu API: {len(results)} products found for '{query}'"
        )

        return results

    except Exception as exc:

        print(
            f"Robu Store API failed: {exc}"
        )

        return None


# ============================================================
#                  ROBU CAMOUFOX FALLBACK
# ============================================================

async def search_robu_camoufox(query):
    """
    Camoufox is only started if Robu's direct API fails.
    This prevents normal searches from being slowed down
    by browser startup.
    """

    browser_cm = None
    browser = None
    page = None

    try:

        print(
            "Starting Camoufox fallback for Robu..."
        )

        browser_cm = AsyncCamoufox(
            headless=True,
            humanize=False,
            block_images=True,
        )

        browser = await asyncio.wait_for(
            browser_cm.__aenter__(),
            timeout=30,
        )

        page = await browser.new_page()

        url = (
            "https://robu.in/"
            f"?s={quote_plus(query)}"
            "&post_type=product"
        )

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=18000,
        )

        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=5000,
            )
        except Exception:
            pass

        await page.wait_for_timeout(500)

        products = await page.locator(
            "li.product, "
            ".product-small, "
            ".product-type-simple, "
            ".products .product"
        ).evaluate_all(
            """
            elements => elements.map(item => {

                const titleEl =
                    item.querySelector(
                        ".name a, "
                        ".name, "
                        ".product-title, "
                        ".woocommerce-loop-product__title"
                    );

                const linkEl =
                    item.querySelector(
                        "a[href*='/product/'], "
                        ".name a, "
                        "a[href]"
                    );

                const priceEl =
                    item.querySelector(".price");

                const stockEl =
                    item.querySelector(
                        ".stock, "
                        ".availability, "
                        ".single_add_to_cart_button, "
                        ".add_to_cart_button"
                    );

                return {
                    title: titleEl
                        ? titleEl.innerText.trim()
                        : "",

                    link: linkEl
                        ? linkEl.href
                        : "",

                    price: priceEl
                        ? priceEl.innerText.trim()
                        : "N/A",

                    text: stockEl
                        ? stockEl.innerText.trim()
                        : (item.innerText || "")
                };

            }).filter(
                item => item.title && item.link
            )
            """
        )

        results = []
        seen_links = set()

        for product in products[:30]:

            link = clean_text(
                product.get("link", "")
            )

            if not link:
                continue

            if link in seen_links:
                continue

            seen_links.add(link)

            results.append(
                {
                    "title": clean_text(
                        product.get("title", "")
                    ),
                    "link": link,
                    "price": clean_text(
                        product.get("price", "")
                    ) or "N/A",
                    "availability": text_availability(
                        product.get("text", "")
                    ),
                }
            )

        print(
            f"Robu Camoufox: {len(results)} products found"
        )

        return results

    except Exception as exc:

        print(
            f"Robu Camoufox fallback failed: {exc}"
        )

        return []

    finally:

        if page is not None:
            try:
                await page.close()
            except Exception:
                pass

        if browser_cm is not None:
            try:
                await browser_cm.__aexit__(
                    None,
                    None,
                    None,
                )
            except Exception as exc:
                print(
                    f"Camoufox shutdown error: {exc}"
                )


# ============================================================
#                    ET STORE SEARCH
# ============================================================

async def search_etstore(client, query):

    try:

        response = await client.get(
            DISTRIBUTORS[0]["url"](query),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/140.0 Safari/537.36"
                )
            },
            timeout=10,
        )

        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        results = []
        seen_links = set()

        for item in soup.select(
            ".product-thumb"
        ):

            title_el = item.select_one(
                ".caption h4 a"
            )

            price_el = item.select_one(
                ".price"
            )

            if not title_el:
                continue

            title = clean_text(
                title_el.get_text(
                    " ",
                    strip=True,
                )
            )

            link = clean_text(
                title_el.get(
                    "href",
                    "",
                )
            )

            if not title or not link:
                continue

            if link in seen_links:
                continue

            seen_links.add(link)

            price = "N/A"

            if price_el:
                price = clean_text(
                    price_el.get_text(
                        " ",
                        strip=True,
                    )
                )

            results.append(
                {
                    "title": title,
                    "link": link,
                    "price": price,
                    "availability": text_availability(
                        item.get_text(
                            " ",
                            strip=True,
                        )
                    ),
                }
            )

            if len(results) >= 30:
                break

        print(
            f"ET Store: {len(results)} products found for '{query}'"
        )

        return results

    except Exception as exc:

        print(
            f"ET Store search failed: {exc}"
        )

        return []


# ============================================================
#                 ROBU SEARCH HANDLER
# ============================================================

async def search_robu(client, query):

    # --------------------------------------------------------
    # First try direct Robu inventory API.
    # This is the normal fast path.
    # --------------------------------------------------------

    api_results = await search_robu_api(
        client,
        query,
    )

    if api_results is not None:

        return api_results

    # --------------------------------------------------------
    # Only if API failed, use Camoufox.
    # --------------------------------------------------------

    print(
        "Robu API unavailable."
        " Switching to Camoufox fallback."
    )

    return await search_robu_camoufox(
        query
    )


# ============================================================
#                    SSE HELPER
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
#                    SEARCH EVENT GENERATOR
# ============================================================

async def event_generator(query):

    site_names = [
        store["name"]
        for store in DISTRIBUTORS
    ]

    # --------------------------------------------------------
    # Tell frontend which distributors are being searched.
    # --------------------------------------------------------

    yield sse(
        {
            "type": "init",
            "sites": site_names,
        }
    )

    # --------------------------------------------------------
    # Immediately show searching state.
    # --------------------------------------------------------

    for store in DISTRIBUTORS:

        yield sse(
            {
                "type": "status",
                "site": store["name"],
                "state": "searching",
            }
        )

    # --------------------------------------------------------
    # HTTP client
    # --------------------------------------------------------

    async with httpx.AsyncClient(
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            )
        },
        follow_redirects=True,
    ) as client:

        # ----------------------------------------------------
        # Search functions
        # ----------------------------------------------------

        async def run_robu():

            try:

                products = await asyncio.wait_for(
                    search_robu(
                        client,
                        query,
                    ),
                    timeout=30,
                )

                return (
                    "Robu.in",
                    products,
                )

            except asyncio.TimeoutError:

                print(
                    "Robu search timed out"
                )

                return (
                    "Robu.in",
                    [],
                )

            except Exception as exc:

                print(
                    f"Robu search error: {exc}"
                )

                return (
                    "Robu.in",
                    [],
                )

        async def run_etstore():

            try:

                products = await asyncio.wait_for(
                    search_etstore(
                        client,
                        query,
                    ),
                    timeout=15,
                )

                return (
                    "ET Store",
                    products,
                )

            except asyncio.TimeoutError:

                print(
                    "ET Store search timed out"
                )

                return (
                    "ET Store",
                    [],
                )

            except Exception as exc:

                print(
                    f"ET Store search error: {exc}"
                )

                return (
                    "ET Store",
                    [],
                )

        # ----------------------------------------------------
        # Run both distributor searches simultaneously.
        # ----------------------------------------------------

        tasks = [
            asyncio.create_task(
                run_robu()
            ),
            asyncio.create_task(
                run_etstore()
            ),
        ]

        # ----------------------------------------------------
        # Send results as each distributor finishes.
        # ----------------------------------------------------

        for task in asyncio.as_completed(
            tasks
        ):

            site_name, products = await task

            # ----------------------------------------------
            # Distributor status
            # ----------------------------------------------

            yield sse(
                {
                    "type": "status",
                    "site": site_name,
                    "state": "done",
                    "count": len(products),
                }
            )

            # ----------------------------------------------
            # Products
            # ----------------------------------------------

            if products:

                yield sse(
                    {
                        "type": "result",
                        "site": site_name,
                        "products": products,
                    }
                )

        # ----------------------------------------------------
        # Search completed
        # ----------------------------------------------------

        yield sse(
            {
                "type": "done",
            }
        )


# ============================================================
#                       ROOT ENDPOINT
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
#                     SEARCH ENDPOINT
# ============================================================

@app.get("/api/search")
async def search(q: str):

    query = q.strip()

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
