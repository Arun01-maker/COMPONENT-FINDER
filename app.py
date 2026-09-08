import json
import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx
from bs4 import BeautifulSoup
from camufox import AsyncCamufox

camufox_browser = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global camufox_browser
    # Initialize Camufox browser with memory flags suitable for Render
    camufox_browser = AsyncCamufox(
        headless=True,
        args=["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox"]
    )
    await camufox_browser.__aenter__()
    yield
    if camufox_browser:
        await camufox_browser.__aexit__(None, None, None)

app = FastAPI(lifespan=lifespan)

# Allow requests from GitHub Pages or frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def fetch_et_store(query: str):
    url = f"https://etstore.in/index.php?route=product/search&search={query}"
    headers = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(headers=headers, timeout=10.0, follow_redirects=True) as client:
        resp = await client.get(url)
        soup = BeautifulSoup(resp.text, "html.parser")
        products = []
        for item in soup.select(".product-thumb"):
            link_el = item.select_one(".caption h4 a")
            price_el = item.select_one(".price")
            if link_el:
                products.append({
                    "title": link_el.get_text(strip=True),
                    "link": link_el.get("href", ""),
                    "price": price_el.get_text(strip=True).split("\n")[0] if price_el else "N/A"
                })
        return products

async def fetch_robu_camufox(query: str):
    url = f"https://robu.in/?s={query}&post_type=product"
    page = await camufox_browser.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        html = await page.content()
    finally:
        await page.close()
        
    soup = BeautifulSoup(html, "html.parser")
    products = []
    for item in soup.select(".product-small, .product-type-simple"):
        title_el = item.select_one(".name, .product-title")
        link_el = item.select_one("a")
        price_el = item.select_one(".price")
        if title_el and link_el:
            products.append({
                "title": title_el.get_text(strip=True),
                "link": link_el.get("href", ""),
                "price": price_el.get_text(strip=True) if price_el else "N/A"
            })
    return products

async def scrape_site(site_name: str, fetch_func, query: str, queue: asyncio.Queue):
    await queue.put(f"data: {json.dumps({'type': 'status', 'site': site_name, 'state': 'searching'})}\n\n")
    try:
        products = await fetch_func(query)
        await queue.put(f"data: {json.dumps({'type': 'status', 'site': site_name, 'state': 'done', 'count': len(products)})}\n\n")
        if products:
            await queue.put(f"data: {json.dumps({'type': 'result', 'site': site_name, 'products': products})}\n\n")
    except Exception:
        await queue.put(f"data: {json.dumps({'type': 'status', 'site': site_name, 'state': 'done', 'count': 0})}\n\n")

async def event_generator(query: str):
    site_names = ["ET Store", "Robu.in"]
    yield f"data: {json.dumps({'type': 'init', 'sites': site_names})}\n\n"

    queue = asyncio.Queue()

    scrapers = [
        asyncio.create_task(scrape_site("ET Store", fetch_et_store, query, queue)),
        asyncio.create_task(scrape_site("Robu.in", fetch_robu_camufox, query, queue)),
    ]

    completed = 0
    while completed < len(scrapers):
        message = await queue.get()
        yield message
        if '"state": "done"' in message:
            completed += 1

    await asyncio.gather(*scrapers, return_exceptions=True)
    yield f"data: {json.dumps({'type': 'done'})}\n\n"

@app.get("/api/search")
async def search(q: str):
    return StreamingResponse(
        event_generator(q),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )
