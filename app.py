import os
import json
import re
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, Response
from flask_cors import CORS
import concurrent.futures

app = Flask(__name__)
CORS(app)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5"
}

# --- HELPER TO EXTRACT PARAMETRIC SPECS FROM TITLE ---

def extract_specs(title):
    t_upper = title.upper()
    
    # 1. Mount Type
    mount = "Other"
    if any(k in t_upper for k in ["SMD", "SMT", "SURFACE MOUNT"]):
        mount = "SMD/SMT"
    elif any(k in t_upper for k in ["DIP", "THROUGH HOLE", "THT", "PLUG-IN"]):
        mount = "Through Hole"

    # 2. Package / Footprint
    package = "Generic"
    pkg_matches = re.findall(r'\b(SOP-?\d+|SOIC-?\d+|TSSOP-?\d+|TO-?220|TO-?92|QFN-?\d+|TQFP-?\d+|0603|0805|1206|DIP-?\d+)\b', t_upper)
    if pkg_matches:
        package = pkg_matches[0]

    # 3. Voltage
    voltage = "N/A"
    volt_matches = re.findall(r'\b(\d+(?:\.\d+)?\s*V(?:DC|AC)?)\b', t_upper)
    if volt_matches:
        voltage = volt_matches[0]

    # 4. Current
    current = "N/A"
    curr_matches = re.findall(r'\b(\d+(?:\.\d+)?\s*(?:A|MA))\b', t_upper)
    if curr_matches:
        current = curr_matches[0]

    return {
        "mount": mount,
        "package": package,
        "voltage": voltage,
        "current": current
    }

# --- SCRAPER FUNCTIONS ---

def scrape_woocommerce(site_name, base_url, query):
    products = []
    try:
        url = f"{base_url}/?s={query}&post_type=product"
        resp = requests.get(url, headers=HEADERS, timeout=6)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select(".product, .product-grid-item, li.type-product, .product-small")
            for item in items[:4]:
                title_el = item.select_one(".product-title, .woocommerce-loop-product__title, h2, h3, .name")
                price_el = item.select_one(".price, .amount")
                link_el = item.select_one("a[href]")
                if title_el:
                    title = title_el.get_text(strip=True)
                    price = price_el.get_text(strip=True) if price_el else "Check Site"
                    link = link_el["href"] if link_el else url
                    specs = extract_specs(title)
                    products.append({"title": title, "price": price, "link": link, "specs": specs})
    except Exception as e:
        print(f"Error scraping {site_name}: {e}")
    return products

def scrape_shopify(site_name, base_url, query):
    products = []
    try:
        url = f"{base_url}/search/suggest.json?q={query}&resources[type]=product&resources[limit]=4"
        resp = requests.get(url, headers=HEADERS, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("resources", {}).get("results", {}).get("products", [])
            for item in items:
                title = item.get("title")
                price_val = item.get("price")
                price = f"₹{price_val}" if price_val else "Check Site"
                link = base_url + item.get("url", "")
                specs = extract_specs(title)
                products.append({"title": title, "price": price, "link": link, "specs": specs})
    except Exception as e:
        print(f"Error scraping {site_name}: {e}")
    return products

def scrape_opencart(site_name, base_url, query):
    products = []
    try:
        url = f"{base_url}/index.php?route=product/search&search={query}"
        resp = requests.get(url, headers=HEADERS, timeout=6)
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
                    specs = extract_specs(title)
                    products.append({"title": title, "price": price, "link": link, "specs": specs})
    except Exception as e:
        print(f"Error scraping {site_name}: {e}")
    return products

def scrape_evelta(query):
    products = []
    try:
        url = f"https://www.evelta.com/index.php?subcats=Y&pcode_from_q=Y&pshort=Y&pfull=Y&pname=Y&pkeywords=Y&search_performed=Y&q={query}&dispatch=products.search"
        resp = requests.get(url, headers=HEADERS, timeout=6)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.select(".ty-column4, .ty-grid-list__item")
            for item in items[:4]:
                title_el = item.select_one(".product-title, .ty-grid-list__item-name a")
                price_el = item.select_one(".ty-price, .price")
                if title_el:
                    title = title_el.get_text(strip=True)
                    price = price_el.get_text(strip=True) if price_el else "Check Site"
                    link = title_el.get("href", url)
                    specs = extract_specs(title)
                    products.append({"title": title, "price": price, "link": link, "specs": specs})
    except Exception as e:
        print(f"Error scraping Evelta: {e}")
    return products

def fetch_site_data(target, query):
    site_name = target["name"]
    products = target["func"](query)
    return {
        "site": site_name,
        "count": len(products),
        "products": products
    }

# --- MAIN SSE ROUTE ---

@app.route("/api/search")
def search():
    query = request.args.get("q", "").strip()

    def generate():
        if not query:
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        targets = [
            {"name": "Leeds Electronics", "func": lambda q: scrape_opencart("Leeds Electronics", "https://www.leedscart.com", q)},
            {"name": "ElectronicsComp", "func": lambda q: scrape_opencart("ElectronicsComp", "https://www.electronicscomp.com", q)},
            {"name": "Robu.in", "func": lambda q: scrape_woocommerce("Robu.in", "https://robu.in", q)},
            {"name": "ETStore", "func": lambda q: scrape_shopify("ETStore", "https://www.etstore.in", q)},
            {"name": "Sharvi Electronics", "func": lambda q: scrape_woocommerce("Sharvi Electronics", "https://sharvielectronics.com", q)},
            {"name": "Rajiv Electronics", "func": lambda q: scrape_shopify("Rajiv Electronics", "https://rajivelectronics.com", q)},
            {"name": "Sparefly", "func": lambda q: scrape_shopify("Sparefly", "https://sparefly.com", q)},
            {"name": "Evelta Electronics", "func": lambda q: scrape_evelta(q)},
            {"name": "DNK Technologies", "func": lambda q: scrape_shopify("DNK Technologies", "https://dnktech.in", q)}
        ]

        site_names = [t["name"] for t in targets]

        yield f"data: {json.dumps({'type': 'init', 'sites': site_names})}\n\n"

        for name in site_names:
            yield f"data: {json.dumps({'type': 'status', 'site': name, 'state': 'searching'})}\n\n"

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as executor:
            future_to_target = {executor.submit(fetch_site_data, target, query): target for target in targets}

            for future in concurrent.futures.as_completed(future_to_target):
                result = future.result()

                if result["products"]:
                    yield f"data: {json.dumps({'type': 'result', 'site': result['site'], 'products': result['products']})}\n\n"

                yield f"data: {json.dumps({'type': 'status', 'site': result['site'], 'state': 'done', 'count': result['count']})}\n\n"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(generate(), mimetype="text/event-stream")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
