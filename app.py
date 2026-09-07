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

def scrape_site(site_config, query):
    products = []
    try:
        url = site_config["url_template"].format(query=query)
        resp = requests.get(url, headers=HEADERS, timeout=8)
        
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select(site_config["card_selector"])
            
            for item in items[:4]:
                title_el = item.select_one(site_config["title_selector"])
                price_el = item.select_one(site_config["price_selector"])
                link_el = item.select_one(site_config["link_selector"])
                
                if title_el:
                    title = title_el.get_text(strip=True)
                    price = price_el.get_text(strip=True) if price_el else "Check Site"
                    
                    # Ensure price format is clean
                    price = price.replace("Regular price", "").replace("Sale price", "").strip()
                    if not price.startswith("₹") and "Rs." not in price and any(c.isdigit() for c in price):
                        price = "₹" + price
                    
                    # Handle full/relative URLs
                    link = url
                    if link_el:
                        href = link_el.get("href", "")
                        if href.startswith("http"):
                            link = href
                        elif href.startswith("/"):
                            link = site_config["base_url"] + href
                            
                    products.append({
                        "title": title,
                        "price": price,
                        "link": link
                    })
    except Exception as e:
        print(f"Error scraping {site_config['name']}: {e}")
        
    return {"type": "result", "site": site_config["name"], "products": products}

@app.route("/api/search")
def search():
    query = request.args.get("q", "").strip()
    
    def generate():
        if not query:
            yield f"data: {json.dumps({'type': 'error', 'message': 'No query provided'})}\n\n"
            return

        # Target 7 Distributors Configuration
        sites = [
            {
                "name": "Robu.in",
                "base_url": "https://robu.in",
                "url_template": "https://robu.in/?s={query}&post_type=product",
                "card_selector": "li.product, .product-grid-item, div.product",
                "title_selector": ".product-title, .woocommerce-loop-product__title, h2, h3",
                "price_selector": ".price, .amount",
                "link_selector": "a.woocommerce-LoopProduct-link, a"
            },
            {
                "name": "ETStore",
                "base_url": "https://www.etstore.in",
                "url_template": "https://www.etstore.in/search?q={query}",
                "card_selector": ".product-item, .grid__item, .card-wrapper",
                "title_selector": ".product-item__title, .card__heading, a",
                "price_selector": ".price-item--sale, .price-item--regular, .price",
                "link_selector": "a.full-unstyled-link, a"
            },
            {
                "name": "Sharvi Electronics",
                "base_url": "https://sharvielectronics.com",
                "url_template": "https://sharvielectronics.com/?s={query}&post_type=product",
                "card_selector": "li.product, div.product-small",
                "title_selector": ".woocommerce-loop-product__title, .product-title",
                "price_selector": ".price, .amount",
                "link_selector": "a"
            },
            {
                "name": "Leeds Electronics",
                "base_url": "https://www.leedscart.com",
                "url_template": "https://www.leedscart.com/index.php?route=product/search&search={query}",
                "card_selector": ".product-layout, .product-thumb",
                "title_selector": ".name a, .caption h4 a",
                "price_selector": ".price, .price-new",
                "link_selector": ".name a, .caption h4 a"
            },
            {
                "name": "Element14 India",
                "base_url": "https://in.element14.com",
                "url_template": "https://in.element14.com/w/c/?st={query}",
                "card_selector": "tr.productRow, tr.tblRow, div.productDisplay",
                "title_selector": "a.description, .productDescription",
                "price_selector": ".price, .discountPrice",
                "link_selector": "a.description, .productDescription"
            },
            {
                "name": "Rajiv Electronics",
                "base_url": "https://rajivelectronics.com",
                "url_template": "https://rajivelectronics.com/search?q={query}",
                "card_selector": ".product-item, .grid__item, .card-wrapper",
                "title_selector": ".product-item__title, .card__heading, a",
                "price_selector": ".price, .price-item",
                "link_selector": "a"
            },
            {
                "name": "Sparefly",
                "base_url": "https://sparefly.com",
                "url_template": "https://sparefly.com/search?q={query}",
                "card_selector": ".product-item, .grid__item, .card-wrapper",
                "title_selector": ".product-item__title, .card__heading, a",
                "price_selector": ".price, .price-item",
                "link_selector": "a"
            }
        ]

        yield f"data: {json.dumps({'type': 'meta', 'total': len(sites)})}\n\n"
        
        for site in sites:
            result = scrape_site(site, query)
            yield f"data: {json.dumps(result)}\n\n"
            
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(generate(), mimetype="text/event-stream")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
