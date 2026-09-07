import asyncio
import json
import queue
import threading
import urllib.parse

from flask import Flask, Response, render_template, request, stream_with_context
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

app = Flask(__name__)

TARGET_SITES = [
    {
        "name": "Robu.in",
        "search_url_template": "https://robu.in/?s={query}&post_type=product",
        "card_selector": "div.product-grid-item, li.product, div.wd-product, div.product-wrapper, div.product",
        "title_selector": ".wd-entities-title, .woocommerce-loop-product__title, .product-title, h3",
        "price_selector": "span.price, .amount, .price",
        "link_selector": "a.wd-entities-title, a.woocommerce-LoopProduct-link, a.product-image-link, a"
    },
    {
        "name": "ETStore",
        "search_url_template": "https://www.etstore.in/search?q={query}",
        "card_selector": ".product-item, .grid__item, div.card-wrapper, .card",
        "title_selector": ".product-item__title, .card__heading, .full-unstyled-link, h3",
        "price_selector": ".price-item--sale, .price-item--regular, .price, .price-item",
        "link_selector": "a.full-unstyled-link, a.card-wrapper, a"
    },
    {
        "name": "Sharvi Electronics",
        "search_url_template": "https://sharvielectronics.com/?s={query}&post_type=product",
        "card_selector": "li.product, div.product-small, div.col-inner",
        "title_selector": ".woocommerce-loop-product__title, .product-title, p.name",
        "price_selector": "span.price, .amount",
        "link_selector": "a.woocommerce-LoopProduct-link, a"
    },
    {
        "name": "Leeds Electronics",
        "search_url_template": "https://www.leedscart.com/index.php?route=product/search&search={query}",
        "card_selector": ".product-layout, .product-thumb",
        "title_selector": "h4 a, .caption a, .name a",
        "price_selector": ".price, .price-new",
        "link_selector": "h4 a, .caption a, .name a"
    },
    {
        "name": "Element14 India",
        "search_url_template": "https://in.element14.com/w/c/?st={query}",
        "card_selector": "tr.productRow, div.productDisplay, tr.tblRow",
        "title_selector": "a.description, .productDescription, td.description a",
        "price_selector": ".price, .discountPrice, td.price",
        "link_selector": "a.description, .productDescription, td.description a"
    },
    {
        "name": "Rajiv Electronics",
        "search_url_template": "https://rajivelectronics.com/search?q={query}",
        "card_selector": ".product-item, .grid__item, div.card-wrapper",
        "title_selector": ".product-item__title, .card__heading, a",
        "price_selector": ".price, .price-item",
        "link_selector": "a"
    },
    {
        "name": "Sparefly",
        "search_url_template": "https://sparefly.com/search?q={query}",
        "card_selector": ".product-item, .grid__item, div.card-wrapper",
        "title_selector": ".product-item__title, .card__heading, a",
        "price_selector": ".price, .price-item",
        "link_selector": "a"
    }
]


def get_active_filters(args):
    filters = []
    mount_choice = args.get("mount", "any")
    if mount_choice == "smd":
        filters.append("smd")
    elif mount_choice == "through_hole":
        filters.append("through hole")

    for key in ("voltage", "current", "package", "extra"):
        val = (args.get(key) or "").strip().lower()
        if val:
            filters.append(val)

    return filters


def calculate_match_score(title, base_query, filters):
    title_lower = title.lower()
    score = 0

    if base_query.lower() in title_lower:
        score += 10

    for f in filters:
        if f == "through hole":
            if any(th in title_lower for th in ["through hole", "through-hole", "thd", "dip"]):
                score += 2
        elif f == "smd":
            if any(smd in title_lower for smd in ["smd", "smt", "surface mount", "sop", "soic", "qfn", "sot"]):
                score += 2
        else:
            if f in title_lower:
                score += 2

    return score


async def scrape_page(page, url, site_info, base_query, filters):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)

        try:
            await page.wait_for_selector(site_info["card_selector"], timeout=4000)
        except Exception:
            await page.wait_for_timeout(1500)

        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text().lower()

        no_match_keywords = ["no results found", "0 results", "no items found", "no products found", "nothing found"]
        if any(k in page_text for k in no_match_keywords):
            return []

        cards = soup.select(site_info["card_selector"])
        extracted_products = []

        for card in cards[:8]:
            title_el = card.select_one(site_info["title_selector"])
            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            if not title or len(title) < 3:
                continue

            price_el = card.select_one(site_info["price_selector"])
            if price_el:
                raw_price = price_el.get_text(strip=True)
                clean_price = raw_price.replace("Regular price", "").replace("Sale price", "").strip()
                if not clean_price.startswith("₹") and any(char.isdigit() for char in clean_price):
                    clean_price = "₹" + clean_price.replace("Rs.", "").strip()
                price = clean_price if clean_price else "Check Site"
            else:
                price = "Check Site"

            link_el = card.select_one(site_info["link_selector"])
            link = url
            if link_el and link_el.has_attr("href"):
                href = link_el["href"]
                if href.startswith("http"):
                    link = href
                elif href.startswith("/"):
                    domain = "/".join(url.split("/")[:3])
                    link = f"{domain}{href}"

            score = calculate_match_score(title, base_query, filters)
            extracted_products.append({
                "title": title,
                "price": price,
                "link": link,
                "score": score
            })

        extracted_products.sort(key=lambda x: x["score"], reverse=True)
        return extracted_products

    except Exception:
        return []


async def fetch_site_data(page, site_info, base_query, filters):
    site_name = site_info["name"]
    clean_query = urllib.parse.quote(base_query.strip())
    target_url = site_info["search_url_template"].format(query=clean_query)

    products = await scrape_page(page, target_url, site_info, base_query, filters)

    status = "Available" if products else "Not Available"
    return {"site": site_name, "status": status, "url": target_url, "products": products[:3]}


async def run_search(query, filters, out_queue):
    total_sites = len(TARGET_SITES)
    out_queue.put({"type": "meta", "total": total_sites})

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage"
            ]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
            }
        )
        page = await context.new_page()

        for idx, site in enumerate(TARGET_SITES, 1):
            out_queue.put({"type": "status", "index": idx, "total": total_sites, "site": site["name"]})
            result = await fetch_site_data(page, site, query, filters)
            out_queue.put({"type": "result", **result})

        await browser.close()

    out_queue.put({"type": "done"})


def sse_event_stream(query, filters):
    out_queue: "queue.Queue" = queue.Queue()

    def worker():
        try:
            asyncio.run(run_search(query, filters, out_queue))
        except Exception as exc:
            out_queue.put({"type": "error", "message": str(exc)})
            out_queue.put({"type": "done"})

    threading.Thread(target=worker, daemon=True).start()

    while True:
        item = out_queue.get()
        yield f"data: {json.dumps(item)}\n\n"
        if item.get("type") == "done":
            break


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def api_search():
    query = (request.args.get("q") or "").strip()
    if not query:
        def error_stream():
            yield f"data: {json.dumps({'type': 'error', 'message': 'No query provided'})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return Response(stream_with_context(error_stream()), mimetype="text/event-stream")

    filters = get_active_filters(request.args)
    return Response(
        stream_with_context(sse_event_stream(query, filters)),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
