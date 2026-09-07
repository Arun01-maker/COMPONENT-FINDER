import os
import json
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5"
}

@app.route("/")
def index():
    return {"status": "Component Finder API is live and running!"}

def scrape_robu(query):
    products = []
    try:
        url = f"https://robu.in/?s={query}&post_type=product"
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select(".product, .product-grid-item, li.type-product")
            for item in items[:4]:
                title_el = item.select_one(".product-title, .woocommerce-loop-product__title, h2, h3, .entry-title")
                price_el = item.select_one(".price, .amount")
                link_el = item.select_one("a[href]")
                if title_el:
                    title = title_el.get_text(strip=True)
                    price = price_el.get_text(strip=True) if price_el else "Check Site"
                    link = link_el["href"] if link_el else url
                    products.append({"title": title, "price": price, "link": link})
    except Exception as e:
        print(f"Robu error: {e}")
    return products

def scrape_shopify_site(site_name, base_url, query):
    products = []
    try:
        # Use Shopify's native JSON predictive search API
        url = f"{base_url}/search/suggest.json?q={query}&resources[type]=product&resources[limit]=4"
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("resources", {}).get("results", {}).get("products", [])
            for item in items:
                title = item.get("title")
                price = f"₹{item.get('price')}" if item.get('price') else "Check Site"
                link = base_url + item.get("url", "")
                products.append({"title": title, "price": price, "link": link})
    except Exception as e:
        print(f"Shopify search error for {site_name}: {e}")
    return products

def scrape_sharvi(query):
    products = []
    try:
        url = f"https://sharvielectronics.com/?s={query}&post_type=product"
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select("li.product, div.product-small, .product-item")
            for item in items[:4]:
                title_el = item.select_one(".woocommerce-loop-product__title, .product-title, h3, a")
                price_el = item.select_one(".price, .amount")
                link_el = item.select_one("a[href]")
                if title_el:
                    title = title_el.get_text(strip=True)
                    price = price_el.get_text(strip=True) if price_el else "Check Site"
                    link = link_el["href"] if link_el else url
                    products.append({"title": title, "price": price, "link": link})
    except Exception as e:
        print(f"Sharvi error: {e}")
    return products

def scrape_leeds(query):
    products = []
    try:
        url = f"https://www.leedscart.com/index.php?route=product/search&search={query}"
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select(".product-layout, .product-thumb")
            for item in items[:4]:
                title_el = item.select_one(".name a, .caption h4 a")
                price_el = item.select_one(".price, .price-new")
                if title_el:
                    title = title_el.get_text(strip=True)
                    price = price_el.get_text(strip=True) if price_el else "Check Site"
                    link = title_el.get("href", url)
                    products.append({"title": title, "price": price, "link": link})
    except Exception as e:
        print(f"Leeds error: {e}")
    return products

def scrape_element14(query):
    products = []
    try:
        url = f"https://in.element14.com/w/c/?st={query}"
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select("tr.productRow, tr.tblRow, .productDisplay")
            for item in items[:4]:
                title_el = item.select_one("a.description, .productDescription, .title")
                price_el = item.select_one(".price, .discountPrice")
                link_el = item.select_one("a[href]")
                if title_el:
                    title = title_el.get_text(strip=True)
                    price = price_el.get_text(strip=True) if price_el else "Check Site"
                    link = link_el["href"] if link_el else url
                    if not link.startswith("http"):
                        link = "https://in.element14.com" + link
                    products.append({"title": title, "price": price, "link": link})
    except Exception as e:
        print(f"Element14 error: {e}")
    return products

@app.route("/api/search")
def search():
    query = request.args.get("q", "").strip()
    
    def generate():
        if not query:
            yield f"data: {json.dumps({'type': 'error', 'message': 'No query provided'})}\n\n"
            return

        targets = [
            {"name": "Robu.in", "func": lambda q: scrape_robu(q)},
            {"name": "ETStore", "func": lambda q: scrape_shopify_site("ETStore", "https://www.etstore.in", q)},
            {"name": "Sharvi Electronics", "func": lambda q: scrape_sharvi(q)},
            {"name": "Leeds Electronics", "func": lambda q: scrape_leeds(q)},
            {"name": "Element14 India", "func": lambda q: scrape_element14(q)},
            {"name": "Rajiv Electronics", "func": lambda q: scrape_shopify_site("Rajiv Electronics", "https://rajivelectronics.com", q)},
            {"name": "Sparefly", "func": lambda q: scrape_shopify_site("Sparefly", "https://sparefly.com", q)}
        ]

        # 1. Send store list to frontend
        site_names = [t["name"] for t in targets]
        yield f"data: {json.dumps({'type': 'init', 'sites': site_names})}\n\n"
        
        # 2. Iterate through each site individually
        for target in targets:
            site_name = target["name"]
            
            # Emit searching badge state
            yield f"data: {json.dumps({'type': 'status', 'site': site_name, 'state': 'searching'})}\n\n"
            
            # Execute scraper
            products = target["func"](query)
            count = len(products)
            
            # Emit status state (Available vs Not Available)
            yield f"data: {json.dumps({'type': 'status', 'site': site_name, 'state': 'done', 'count': count})}\n\n"
            
            # Emit product payloads
            yield f"data: {json.dumps({'type': 'result', 'site': site_name, 'products': products})}\n\n"
            
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(generate(), mimetype="text/event-stream")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
