import os
import json
import asyncio
import urllib.parse
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from bs4 import BeautifulSoup
import httpx
from camoufox.async_api import AsyncCamoufox

# Global browser instance manager
camoufox_browser = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global camoufox_browser
    # Initialize Camoufox headless browser with anti-detection flags
    camoufox_browser = AsyncCamoufox(
        headless=True,
        args=["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox"]
    )
    await camoufox_browser.__aenter__()
    print("Camoufox browser initialized and ready.")
    
    yield
    
    # Clean up browser on app shutdown
    if camoufox_browser:
        await camoufox_browser.__aexit__(None, None, None)
        print("Camoufox browser closed.")

app = FastAPI(title="Component Finder API", lifespan=lifespan)

# Enable CORS for frontend integration (e.g., GitHub Pages)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Scraper 1: ET Store (using httpx) ---
async def scrape_etstore(query: str):
    results = []
    encoded_query = urllib.parse.quote(query)
    url = f"https://etstore.in/?s={encoded_query}&post_type=product"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, "html.parser")
                products = soup.select(".product") or soup.select("li.product")
                
                for prod in products[:10]:
                    title_elem = prod.select_one(".woocommerce-loop-product__title") or prod.select_one("h2")
                    price_elem = prod.select_one(".price")
                    link_elem = prod.select_one("a")
                    img_elem = prod.select_one("img")
                    
                    if title_elem and link_elem:
                        results.append({
                            "distributor": "ET Store",
                            "title": title_elem.text.strip(),
                            "price": price_elem.text.strip() if price_elem else "N/A",
                            "link": link_elem.get("href", ""),
                            "image": img_elem.get("src", "") if img_elem else ""
                        })
    except Exception as e:
        print(f"Error scraping ET Store: {e}")
    
    return results

# --- Scraper 2: Robu.in (using Camoufox) ---
async def scrape_robu(query: str):
    results = []
    if not camoufox_browser:
        return results

    encoded_query = urllib.parse.quote(query)
    url = f"https://robu.in/?s={encoded_query}&post_type=product"
    
    try:
        page = await camoufox_browser.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        
        # Extract HTML content once page renders
        content = await page.content()
        await page.close()
        
        soup = BeautifulSoup(content, "html.parser")
        products = soup.select(".product") or soup.select(".product-inner")
        
        for prod in products[:10]:
            title_elem = prod.select_one(".product-title") or prod.select_one("h2") or prod.select_one(".woocommerce-loop-product__title")
            price_elem = prod.select_one(".price")
            link_elem = prod.select_one("a")
            img_elem = prod.select_one("img")
            
            if title_elem and link_elem:
                results.append({
                    "distributor": "Robu.in",
                    "title": title_elem.text.strip(),
                    "price": price_elem.text.strip() if price_elem else "N/A",
                    "link": link_elem.get("href", ""),
                    "image": img_elem.get("src", "") if img_elem else ""
                })
    except Exception as e:
        print(f"Error scraping Robu.in via Camoufox: {e}")
        
    return results

# --- Frontend & Search Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>Component Finder API Running</h1>")

@app.get("/api/search")
async def search_parts(q: str = Query(..., description="Search term for components")):
    async def event_generator():
        yield f"data: {json.dumps({'type': 'status', 'message': f'Searching ET Store and Robu.in for \"{q}\"...'})}\n\n"
        
        # Run both scrapers concurrently
        et_task = asyncio.create_task(scrape_etstore(q))
        robu_task = asyncio.create_task(scrape_robu(q))
        
        et_results, robu_results = await asyncio.gather(et_task, robu_task)
        
        combined_results = et_results + robu_results
        
        yield f"data: {json.dumps({'type': 'results', 'data': combined_results})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'message': 'Search complete'})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
