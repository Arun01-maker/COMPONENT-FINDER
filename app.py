import os
import urllib.parse
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from bs4 import BeautifulSoup
import httpx
from camoufox.async_api import AsyncCamoufox

# Persistent browser instance
camoufox_browser = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global camoufox_browser
    try:
        # Launch Camoufox headless browser on server boot
        camoufox_browser = AsyncCamoufox(
            headless=True,
            args=["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox"]
        )
        await camoufox_browser.__aenter__()
        print("Camoufox browser successfully started.")
    except Exception as e:
        print(f"Camoufox startup warning: {e}")

    yield

    # Clean up browser on shutdown
    if camoufox_browser:
        try:
            await camoufox_browser.__aexit__(None, None, None)
            print("Camoufox browser closed.")
        except Exception as e:
            print(f"Error shutting down browser: {e}")

app = FastAPI(title="Component Finder API", lifespan=lifespan)

# Enable CORS for web frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# --- Scraper 1: ET Store (HTTPX) ---
async def scrape_etstore(client: httpx.AsyncClient, query: str):
    results = []
    encoded_query = urllib.parse.quote(query)
    url = f"https://etstore.in/?s={encoded_query}&post_type=product"
    
    try:
        response = await client.get(url, headers=HEADERS, timeout=8.0)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            products = soup.select(".product") or soup.select("li.product")
            
            for prod in products[:10]:
                title_elem = prod.select_one(".woocommerce-loop-product__title") or prod.select_one("h2")
                price_elem = prod.select_one(".price")
                link_elem = prod.select_one("a")
                
                if title_elem and link_elem:
                    results.append({
                        "distributor": "ET Store",
                        "title": title_elem.text.strip(),
                        "price": price_elem.text.strip() if price_elem else "N/A",
                        "link": link_elem.get("href", "")
                    })
    except Exception as e:
        print(f"ET Store scraping error: {e}")
    
    return results

# --- Scraper 2: Robu.in (Camoufox) ---
async def scrape_robu(query: str):
    results = []
    if not camoufox_browser:
        print("Camoufox browser is not available.")
        return results

    encoded_query = urllib.parse.quote(query)
    url = f"https://robu.in/?s={encoded_query}&post_type=product"
    
    try:
        page = await camoufox_browser.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        
        content = await page.content()
        await page.close()
        
        soup = BeautifulSoup(content, "html.parser")
        products = soup.select(".product") or soup.select(".product-inner")
        
        for prod in products[:10]:
            title_elem = (
                prod.select_one(".product-title") 
                or prod.select_one("h2") 
                or prod.select_one(".woocommerce-loop-product__title")
            )
            price_elem = prod.select_one(".price")
            link_elem = prod.select_one("a")
            
            if title_elem and link_elem:
                results.append({
                    "distributor": "Robu.in",
                    "title": title_elem.text.strip(),
                    "price": price_elem.text.strip() if price_elem else "N/A",
                    "link": link_elem.get("href", "")
                })
    except Exception as e:
        print(f"Robu.in Camoufox scraping error: {e}")
        
    return results

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>Component Finder API Running</h1>")

@app.get("/api/search")
async def search_parts(q: str = Query("", description="Search component")):
    if not q.strip():
        return JSONResponse({"success": True, "results": []})
        
    async with httpx.AsyncClient(follow_redirects=True) as client:
        et_task = asyncio.create_task(scrape_etstore(client, q))
        robu_task = asyncio.create_task(scrape_robu(q))
        
        et_results, robu_results = await asyncio.gather(et_task, robu_task)
    
    combined = et_results + robu_results
    return JSONResponse({
        "success": True,
        "count": len(combined),
        "results": combined
    })

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
