import os
import json
import asyncio
import subprocess
from flask import Flask, request, Response, render_template
from flask_cors import CORS
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

# Auto-install Playwright Chromium binary on app boot if missing
try:
    print("Checking and installing Playwright Chromium browser...")
    subprocess.run(["python", "-m", "playwright", "install", "chromium"], check=True)
except Exception as e:
    print(f"Playwright installation warning: {e}")

app = Flask(__name__)
CORS(app)

# Root route returning API status (prevents TemplateNotFound errors on Render)
@app.route("/")
def index():
    return {"status": "Component Finder API is live and running!"}

async def scrape_site(context, site_info, query):
    name = site_info["name"]
    url = site_info["search_url"].format(query=query)
    products = []
    
    try:
        page = await context.new_page()
        await page.goto(url, timeout=25000, wait_until="domcontentloaded")
        
        # Simple extraction strategy per page
        content = await page.content()
        soup = BeautifulSoup(content, "html.parser")
        
        # Example selector parsing (Adjust selectors based on target sites)
        items = soup.select(site_info.get("container_selector", ".product-grid .product-item"))
        
        for item in items[:5]:
            title_el = item.select_one(site_info.get("title_selector", ".product-title, .name, h2, h3"))
            price_el = item.select_one(site_info.get("price_selector", ".price, .amount"))
            link_el = item.select_one("a[href]")
            
            if title_el:
                title = title_el.get_text(strip=True)
                price = price_el.get_text(strip=True) if price_el else "N/A"
                link = link_el["href"] if link_el else url
                if not link.startswith("http"):
                    link = site_info["base_url"] + link
                
                products.append({
                    "title": title,
                    "price": price,
                    "link": link
                })
        
        await page.close()
        return {"type": "result", "site": name, "status": "Available", "products": products}
    except Exception as e:
        return {"type": "result", "site": name, "status": "Error", "products": [], "error": str(e)}

async def run_scrapers(query):
    sites = [
        {
            "name": "Robu.in",
            "base_url": "https://robu.in",
            "search_url": "https://robu.in/?s={query}&post_type=product",
            "container_selector": ".product-grid-item",
            "title_selector": ".product-title",
            "price_selector": ".price"
        }
        # Add additional site targets here as needed
    ]
    
    yield f"data: {json.dumps({'type': 'meta', 'total': len(sites)})}\n\n"
    
    async with async_playwright() as p:
        # Launch Chromium with headless configuration
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        
        tasks = [scrape_site(context, site, query) for site in sites]
        for task in asyncio.as_completed(tasks):
            result = await task
            yield f"data: {json.dumps(result)}\n\n"
            
        await browser.close()
        
    yield f"data: {json.dumps({'type': 'done'})}\n\n"

@app.route("/api/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return Response("data: " + json.dumps({"type": "error", "message": "Query parameter missing"}) + "\n\n", mimetype="text/event-stream")

    def generate():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            async_gen = run_scrapers(query)
            while True:
                try:
                    item = loop.run_until_complete(async_gen.__anext__())
                    yield item
                except StopAsyncIteration:
                    break
        finally:
            loop.close()

    return Response(generate(), mimetype="text/event-stream")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
