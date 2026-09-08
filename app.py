import asyncio
from flask import Flask, render_template, request, jsonify
from camoufox.async_api import AsyncCamoufox

app = Flask(__name__, template_folder=".")

async def fetch_robo_in_details(query):
    """
    Fetches component details from robo.in using Camoufox to bypass bot detection.
    """
    url = f"https://robo.in/search?q={query}"
    
    # Launch Camoufox headless browser
    async with AsyncCamoufox(headless=True) as browser:
        page = await browser.new_page()
        
        # Navigate to target page
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        
        # Example extraction logic for Robo.in (Update selectors based on target DOM structure)
        results = []
        products = await page.query_selector_all(".product-card, .grid__item")
        
        for product in products[:5]:  # Get top 5 results
            title_el = await product.query_selector(".product-card__title, .card__heading")
            price_el = await product.query_selector(".price-item--regular, .price")
            link_el = await product.query_selector("a")
            
            title = await title_el.inner_text() if title_el else "N/A"
            price = await price_el.inner_text() if price_el else "N/A"
            link = await link_el.get_attribute("href") if link_el else "#"
            
            if link and not link.startswith("http"):
                link = f"https://robo.in{link}"
                
            results.append({
                "title": title.strip(),
                "price": price.strip(),
                "link": link
            })
            
        return results

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/search", methods=["POST"])
def search():
    data = request.get_json() or {}
    query = data.get("query", "")
    
    if not query:
        return jsonify({"error": "Query parameter is required"}), 400
    
    try:
        # Run async Camoufox scraper in synchronous Flask route
        results = asyncio.run(fetch_robo_in_details(query))
        return jsonify({"success": True, "data": results})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
