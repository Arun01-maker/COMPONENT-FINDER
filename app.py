import json
import asyncio
from urllib.parse import quote_plus

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
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
        "url": lambda q: f"https://etstore.in/index.php?route=product/search&search={quote_plus(q)}",
    },
    {
        "name": "Robu.in",
        "url": lambda q: f"https://robu.in/?s={quote_plus(q)}&post_type=product",
    },
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
        "unavailable",
    ]):
        return "Out of Stock"

    if any(value in text for value in [
        "in stock",
        "add to cart",
        "buy now",
        "add to basket",
    ]):
        return "In Stock"

    return "Availability Unknown"


async def wait_for_page(page):
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=20000)
    except Exception:
        pass

    try:
        await page.wait_for_load_state("networkidle", timeout=7000)
    except Exception:
        pass

    await page.wait_for_timeout(1000)


async def parse_robu(browser, query):
    page = await browser.new_page()
    try:
        await page.goto(
            DISTRIBUTORS[1]["url"](query),
            wait_until="domcontentloaded",
            timeout=25000,
        )
        await wait_for_page(page)

        products = await page.locator(
            ".product-small, .product-type-simple, li.product, .products .product"
        ).evaluate_all(
            """
            elements => elements.map(item => {
                const titleEl = item.querySelector(
                    ".name a, .name, .product-title, .woocommerce-loop-product__title"
                );
                const linkEl = item.querySelector(
                    "a[href*='/product/'], .name a, a[href]"
                );
                const priceEl = item.querySelector(".price");

                return {
                    title: titleEl ? titleEl.innerText.trim() : "",
                    link: linkEl ? linkEl.href : "",
                    price: priceEl ? priceEl.innerText.trim() : "N/A",
                    text: item.innerText || ""
                };
            }).filter(item => item.title && item.link)
            """
        )

        # Fallback for changes in Robu's search-result HTML.
        if not products:
            products = await page.locator("a[href*='/product/']").evaluate_all(
                """
                links => {
                    const seen = new Set();
                    return links.map(link => {
                        const href = link.href;
                        const title = (link.innerText || "").trim();
                        const card = link.closest(
                            ".product-small, .product-type-simple, li.product, .product, article"
                        );
                        const text = card ? (card.innerText || "") : title;
                        const priceEl = card ? card.querySelector(".price") : null;
                        return {
                            title,
                            link: href,
                            price: priceEl ? priceEl.innerText.trim() : "N/A",
                            text
                        };
                    }).filter(item => {
                        if (!item.title || !item.link || seen.has(item.link)) return false;
                        seen.add(item.link);
                        return true;
                    });
                }
                """
            )

        results = []
        seen_links = set()

        # Open product pages so availability comes from the actual product page,
        # not only from the search-result card.
        for product in products[:12]:
            link = product.get("link")
            if not link or link in seen_links:
                continue
            seen_links.add(link)

            availability = extract_availability(product.get("text", ""))
            detail_page = await browser.new_page()
            try:
                await detail_page.goto(
                    link,
                    wait_until="domcontentloaded",
                    timeout=18000,
                )
                await wait_for_page(detail_page)

                stock_text = await detail_page.locator(
                    ".stock, .availability, .woocommerce-variation-availability, "
                    ".single_add_to_cart_button, .add_to_cart_button"
                ).all_inner_texts()

                detail_text = " ".join(stock_text)
                if not detail_text:
                    detail_text = await detail_page.locator("body").inner_text(timeout=5000)

                availability = extract_availability(detail_text)

                detail_title = await detail_page.locator(
                    "h1.product_title, h1.entry-title, .product_title"
                ).first.inner_text(timeout=3000)
                if detail_title:
                    product["title"] = clean_text(detail_title)

                detail_price = await detail_page.locator(
                    ".summary .price, .product .price, .price"
                ).first.inner_text(timeout=3000)
                if detail_price:
                    product["price"] = clean_text(detail_price)
            except Exception as exc:
                print(f"Robu product detail error: {link} -> {exc}")
            finally:
                await detail_page.close()

            results.append({
                "title": clean_text(product.get("title")),
                "link": link,
                "price": clean_text(product.get("price")) or "N/A",
                "availability": availability,
            })

        return results
    finally:
        await page.close()


async def parse_etstore(browser, query):
    page = await browser.new_page()
    try:
        await page.goto(
            DISTRIBUTORS[0]["url"](query),
            wait_until="domcontentloaded",
            timeout=25000,
        )
        await wait_for_page(page)

        products = await page.locator(".product-thumb").evaluate_all(
            """
            elements => elements.map(item => {
                const titleEl = item.querySelector(".caption h4 a");
                const priceEl = item.querySelector(".price");

                return {
                    title: titleEl ? titleEl.innerText.trim() : "",
                    link: titleEl ? titleEl.href : "",
                    price: priceEl ? priceEl.innerText.trim().split("\\n")[0] : "N/A",
                    text: item.innerText || ""
                };
            }).filter(item => item.title && item.link)
            """
        )

        return [
            {
                "title": clean_text(product.get("title")),
                "link": product.get("link"),
                "price": clean_text(product.get("price")) or "N/A",
                "availability": extract_availability(product.get("text", "")),
            }
            for product in products[:30]
        ]
    finally:
        await page.close()


async def parse_store(browser, store, query):
    if store["name"] == "Robu.in":
        return await parse_robu(browser, query)
    if store["name"] == "ET Store":
        return await parse_etstore(browser, query)
    return []


def sse(data):
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def event_generator(query: str):
    site_names = [d["name"] for d in DISTRIBUTORS]
    yield sse({"type": "init", "sites": site_names})

    # Send the first site status before starting the browser. This prevents the
    # frontend from appearing frozen while Camoufox starts on Render.
    for store in DISTRIBUTORS:
        yield sse({"type": "status", "site": store["name"], "state": "searching"})

    browser_cm = None
    browser = None

    try:
        browser_cm = AsyncCamoufox(
            headless=True,
            humanize=False,
            block_images=True,
        )
        browser = await asyncio.wait_for(browser_cm.__aenter__(), timeout=45)
    except Exception as exc:
        print(f"Camoufox startup error: {exc}")
        yield sse({
            "type": "error",
            "message": "Camoufox could not start on the Render server. Check the Render build logs and make sure 'python -m camoufox fetch' is in the Build Command.",
        })
        for store in DISTRIBUTORS:
            yield sse({
                "type": "status",
                "site": store["name"],
                "state": "done",
                "count": 0,
            })
        yield sse({"type": "done"})
        return

    try:
        for store in DISTRIBUTORS:
            try:
                products = await asyncio.wait_for(
                    parse_store(browser, store, query),
                    timeout=75,
                )

                yield sse({
                    "type": "status",
                    "site": store["name"],
                    "state": "done",
                    "count": len(products),
                })

                if products:
                    yield sse({
                        "type": "result",
                        "site": store["name"],
                        "products": products,
                    })

            except asyncio.TimeoutError:
                print(f"{store['name']} search timed out")
                yield sse({
                    "type": "status",
                    "site": store["name"],
                    "state": "done",
                    "count": 0,
                })
            except Exception as exc:
                print(f"{store['name']} search error: {exc}")
                yield sse({
                    "type": "status",
                    "site": store["name"],
                    "state": "done",
                    "count": 0,
                })

        yield sse({"type": "done"})

    finally:
        if browser_cm is not None:
            try:
                await browser_cm.__aexit__(None, None, None)
            except Exception as exc:
                print(f"Camoufox shutdown error: {exc}")


@app.get("/")
async def root():
    return JSONResponse({"status": "ok", "service": "component-finder"})


@app.get("/api/search")
async def search(q: str):
    if not q.strip():
        return JSONResponse({"error": "Query is required"}, status_code=400)

    return StreamingResponse(
        event_generator(q.strip()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
