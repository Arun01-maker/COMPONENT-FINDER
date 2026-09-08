import json
import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx
from bs4 import BeautifulSoup
from camoufox.async_api import AsyncCamoufox

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DISTRIBUTORS = [
    {
        "name": "ET Store",
        "url": lambda q: f"https://etstore.in/index.php?route=product/search&search={q}",
        "parse": lambda html: [
            {
                "title": item.select_one(".caption h4 a").get_text(strip=True),
                "link": item.select_one(".caption h4 a")["href"],
                "price": item.select_one(".price").get_text(strip=True).split('\n')[0]
            }
            for item in BeautifulSoup(html, "html.parser").select(".product-thumb")
            if item.select_one(".caption h4 a")
        ]
    },
    {
        "name": "Robu.in",
        "url": lambda q: f"https://robu.in/?s={q}&post_type=product",
        "parse": lambda html: [
            {
                "title": item.select_one(".name, .product-title").get_text(strip=True),
                "link": item.select_one("a")["href"],
                "price": item.select_one(".price").get_text(strip=True)
                if item.select_one(".price")
                else "N/A"
            }
            for item in BeautifulSoup(html, "html.parser").select(
                ".product-small, .product-type-simple"
            )
            if item.select_one(".name, .product-title")
        ]
    }
]


def clean_text(value):
    return " ".join((value or "").split())


def extract_availability(text):
    text = clean_text(text).lower()

    if any(value in text for value in [
        "out of stock",
        "sold out",
        "currently unavailable",
        "not available",
        "unavailable"
    ]):
        return "Out of Stock"

    if any(value in text for value in [
        "in stock",
        "available",
        "add to cart",
        "buy now",
        "add to basket"
    ]):
        return "In Stock"

    return "Availability Unknown"


async def parse_robu_with_camoufox(browser, query):
    page = await browser.new_page()

    try:
        url = f"https://robu.in/?s={query}&post_type=product"

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=20000
        )

        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=8000
            )
        except Exception:
            pass

        await page.wait_for_timeout(1500)

        products = await page.locator(
            ".product-small, .product-type-simple"
        ).evaluate_all(
            """
            elements => elements.map(item => {
                const titleEl = item.querySelector(
                    ".name, .product-title"
                );

                const linkEl = item.querySelector(
                    "a[href]"
                );

                const priceEl = item.querySelector(
                    ".price"
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

                    text: item.innerText || ""
                };
            }).filter(item => item.title && item.link)
            """
        )

        results = []

        for product in products[:30]:
            results.append({
                "title": clean_text(
                    product.get("title")
                ),

                "link": product.get("link"),

                "price": clean_text(
                    product.get("price")
                ) or "N/A",

                "availability": extract_availability(
                    product.get("text", "")
                )
            })

        return results

    finally:
        await page.close()


async def parse_etstore_with_camoufox(browser, query):
    page = await browser.new_page()

    try:
        url = (
            "https://etstore.in/index.php"
            "?route=product/search"
            f"&search={query}"
        )

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=20000
        )

        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=8000
            )
        except Exception:
            pass

        await page.wait_for_timeout(1000)

        products = await page.locator(
            ".product-thumb"
        ).evaluate_all(
            """
            elements => elements.map(item => {
                const titleEl = item.querySelector(
                    ".caption h4 a"
                );

                const priceEl = item.querySelector(
                    ".price"
                );

                return {
                    title: titleEl
                        ? titleEl.innerText.trim()
                        : "",

                    link: titleEl
                        ? titleEl.href
                        : "",

                    price: priceEl
                        ? priceEl.innerText.trim().split("\\n")[0]
                        : "N/A",

                    text: item.innerText || ""
                };
            }).filter(item => item.title && item.link)
            """
        )

        results = []

        for product in products[:30]:
            results.append({
                "title": clean_text(
                    product.get("title")
                ),

                "link": product.get("link"),

                "price": clean_text(
                    product.get("price")
                ) or "N/A",

                "availability": extract_availability(
                    product.get("text", "")
                )
            })

        return results

    finally:
        await page.close()


async def parse_store_with_camoufox(
    browser,
    store,
    query
):
    if store["name"] == "Robu.in":
        return await parse_robu_with_camoufox(
            browser,
            query
        )

    if store["name"] == "ET Store":
        return await parse_etstore_with_camoufox(
            browser,
            query
        )

    return []


async def event_generator(query: str):
    site_names = [
        d["name"]
        for d in DISTRIBUTORS
    ]

    yield (
        "data: "
        + json.dumps({
            "type": "init",
            "sites": site_names
        })
        + "\n\n"
    )

    async with AsyncCamoufox(
        headless=True,
        humanize=True
    ) as browser:

        for store in DISTRIBUTORS:

            yield (
                "data: "
                + json.dumps({
                    "type": "status",
                    "site": store["name"],
                    "state": "searching"
                })
                + "\n\n"
            )

            try:

                products = await parse_store_with_camoufox(
                    browser,
                    store,
                    query
                )

                yield (
                    "data: "
                    + json.dumps({
                        "type": "status",
                        "site": store["name"],
                        "state": "done",
                        "count": len(products)
                    })
                    + "\n\n"
                )

                if products:

                    yield (
                        "data: "
                        + json.dumps({
                            "type": "result",
                            "site": store["name"],
                            "products": products
                        })
                        + "\n\n"
                    )

            except Exception as exc:

                print(
                    f"{store['name']} search error: {exc}"
                )

                yield (
                    "data: "
                    + json.dumps({
                        "type": "status",
                        "site": store["name"],
                        "state": "done",
                        "count": 0
                    })
                    + "\n\n"
                )

    yield (
        "data: "
        + json.dumps({
            "type": "done"
        })
        + "\n\n"
    )


@app.get("/api/search")
async def search(q: str):
    return StreamingResponse(
        event_generator(q),
        media_type="text/event-stream"
    )
