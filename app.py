import asyncio
import json
import re
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

ROBU_SEARCH = "https://robu.in/?s={query}&post_type=product"
ET_SEARCH = "https://etstore.in/index.php?route=product/search&search={query}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def clean_text(value):
    return " ".join(str(value or "").split())


def normalize_url(url, base):
    return urljoin(base, clean_text(url))


def availability_from_text(text):
    text = clean_text(text).lower()

    if any(x in text for x in (
        "out of stock",
        "sold out",
        "currently unavailable",
        "unavailable",
        "not available",
    )):
        return "Out of Stock"

    if any(x in text for x in (
        "add to cart",
        "add-to-cart",
        "buy now",
        "in stock",
        "available",
    )):
        return "In Stock"

    return "Availability Unknown"


def extract_price(card):
    price = card.select_one(
        ".price, .woocommerce-Price-amount, .amount, [class*='price']"
    )
    return clean_text(price.get_text(" ", strip=True)) if price else "N/A"


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
            title = clean_text(element.get_text(" ", strip=True))
            if title:
                return title

    title = clean_text(link.get("aria-label", ""))
    if title:
        return title

    return clean_text(link.get_text(" ", strip=True))


def find_product_card(link):
    # Prefer actual WooCommerce product containers.
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

    # Generic fallback for Robu theme changes.
    parent = link.parent
    for _ in range(5):
        if parent is None:
            break
        text = clean_text(parent.get_text(" ", strip=True))
        if len(text) >= 20 and len(text) <= 3000:
            if (
                "₹" in text
                or "add to cart" in text.lower()
                or "read more" in text.lower()
                or "wishlist" in text.lower()
            ):
                return parent
        parent = parent.parent

    return link.parent


def parse_robu_html(html):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()

    # Main product-card path.
    cards = soup.select(
        "li.product, .product-small, .product-type-simple, "
        ".product-type-variable, article.product, .products .product"
    )

    for card in cards:
        link = card.select_one(
            "a[href*='/product/'], a.woocommerce-LoopProduct-link[href]"
        )
        if not link:
            continue

        href = normalize_url(link.get("href", ""), "https://robu.in/")
        if "/product/" not in href or href in seen:
            continue

        title = extract_title(card, link)
        if not title or len(title) < 2:
            continue

        seen.add(href)
        card_text = clean_text(card.get_text(" ", strip=True))

        results.append({
            "title": title,
            "link": href,
            "price": extract_price(card),
            "availability": availability_from_text(card_text),
        })

    # Broad fallback. This is important because Robu has changed
    # product-card markup over time.
    if not results:
        for link in soup.select("a[href*='/product/']"):
            href = normalize_url(link.get("href", ""), "https://robu.in/")
            if not href or href in seen:
                continue

            title = clean_text(link.get_text(" ", strip=True))
            if not title or len(title) < 3:
                title = clean_text(link.get("aria-label", ""))

            # Ignore tiny/navigation links.
            if not title or len(title) < 3 or len(title) > 250:
                continue

            card = find_product_card(link)
            if not card:
                continue

            card_text = clean_text(card.get_text(" ", strip=True))
            if not card_text:
                continue

            seen.add(href)
            results.append({
                "title": title,
                "link": href,
                "price": extract_price(card),
                "availability": availability_from_text(card_text),
            })

    return results[:30]


def parse_et_html(html):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()

    for card in soup.select(".product-thumb"):
        link = card.select_one(".caption h4 a, h4 a, a[href]")
        if not link:
            continue

        href = normalize_url(link.get("href", ""), "https://etstore.in/")
        title = clean_text(link.get_text(" ", strip=True))
        if not title or not href or href in seen:
            continue

        seen.add(href)
        results.append({
            "title": title,
            "link": href,
            "price": extract_price(card),
            "availability": availability_from_text(
                card.get_text(" ", strip=True)
            ),
        })

        if len(results) >= 30:
            break

    return results


async def search_robu(client, query):
    try:
        response = await client.get(
            ROBU_SEARCH.format(query=quote_plus(query)),
            timeout=httpx.Timeout(8.0, connect=4.0),
        )
        response.raise_for_status()

        products = parse_robu_html(response.text)
        print(f"Robu search: {len(products)} products for {query!r}")
        return products, None

    except Exception as exc:
        print(f"Robu HTTP search failed: {exc}")
        return [], str(exc)


async def search_etstore(client, query):
    try:
        response = await client.get(
            ET_SEARCH.format(query=quote_plus(query)),
            timeout=httpx.Timeout(8.0, connect=4.0),
        )
        response.raise_for_status()

        products = parse_et_html(response.text)
        print(f"ET Store search: {len(products)} products for {query!r}")
        return products, None

    except Exception as exc:
        print(f"ET Store search failed: {exc}")
        return [], str(exc)


async def event_generator(query):
    sites = ["ET Store", "Robu.in"]

    yield sse({"type": "init", "sites": sites})

    for site in sites:
        yield sse({
            "type": "status",
            "site": site,
            "state": "searching",
        })

    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
    ) as client:

        async def run_site(site, search_function):
            try:
                products, error = await search_function(client, query)
                return site, products, error
            except Exception as exc:
                return site, [], str(exc)

        tasks = [
            asyncio.create_task(
                run_site("ET Store", search_etstore)
            ),
            asyncio.create_task(
                run_site("Robu.in", search_robu)
            ),
        ]

        for task in asyncio.as_completed(tasks):
            site, products, error = await task

            if error:
                # Do NOT report a network/parser failure as
                # "None found". The old frontend did exactly that.
                yield sse({
                    "type": "status",
                    "site": site,
                    "state": "error",
                    "count": 0,
                    "message": "Search failed",
                })
                continue

            yield sse({
                "type": "status",
                "site": site,
                "state": "done",
                "count": len(products),
            })

            if products:
                yield sse({
                    "type": "result",
                    "site": site,
                    "products": products,
                })

    yield sse({"type": "done"})


def sse(data):
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


@app.get("/")
async def root():
    return JSONResponse({
        "status": "ok",
        "service": "component-finder",
    })


@app.get("/api/search")
async def search(q: str):
    query = clean_text(q)

    if not query:
        return JSONResponse(
            {"error": "Query is required"},
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
