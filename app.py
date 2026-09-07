import os
import json
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

@app.route("/")
def index():
    return {"status": "Component Finder API is live and running!"}

def scrape_robu(query):
    products = []
    try:
        url = f"https://robu.in/?s={query}&post_type=product"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select("li.product, div.product, .product-grid-item")
            
            for item in items[:6]:
                title_el = item.select_one(".product-title, .woocommerce-loop-product__title, h2, h3")
                price_el = item.select_one(".price, .amount")
                link_el = item.select_one("a[href]")
                
                if title_el and link_el:
                    title = title_el.get_text(strip=True)
                    price = price_el.get_text(strip=True) if price_el else "Check Site"
                    link = link_el["href"]
                    
                    products.append({
                        "title": title,
                        "price": price,
                        "link": link
                    })
    except Exception as e:
        print(f"Robu scraper error: {e}")
        
    return {"type": "result", "site": "Robu.in", "products": products}

def scrape_electronicscomp(query):
    products = []
    try:
        url = f"https://www.electronicscomp.com/index.php?route=product/search&search={query}"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select(".product-layout, .product-thumb")
            
            for item in items[:6]:
                title_el = item.select_one(".name a, .caption h4 a")
                price_el = item.select_one(".price, .price-new")
                
                if title_el:
                    title = title_el.get_text(strip=True)
                    price = price_el.get_text(strip=True) if price_el else "Check Site"
                    link = title_el.get("href", url)
                    
                    products.append({
                        "title": title,
                        "price": price,
                        "link": link
                    })
    except Exception as e:
        print(f"ElectronicsComp scraper error: {e}")
        
    return {"type": "result", "site": "ElectronicsComp", "products": products}

@app.route("/api/search")
def search():
    query = request.args.get("q", "").strip()
    
    def generate():
        if not query:
            yield f"data: {json.dumps({'type': 'error', 'message': 'No query provided'})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'meta', 'total': 2})}\n\n"
        
        # Scrape target distributors
        robu_res = scrape_robu(query)
        yield f"data: {json.dumps(robu_res)}\n\n"
        
        ecomp_res = scrape_electronicscomp(query)
        yield f"data: {json.dumps(ecomp_res)}\n\n"
        
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(generate(), mimetype="text/event-stream")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
