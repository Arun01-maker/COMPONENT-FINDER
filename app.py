import asyncio
import json
import re
import time
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

try:
    from camoufox.async_api import AsyncCamoufox
except Exception:
    AsyncCamoufox = None

app = FastAPI(title="Component Finder API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

# Timeouts are per operation. A slow site cannot block the complete search.
HTTP_TIMEOUT = 6.0
SITE_TIMEOUT = 28.0
PRODUCT_TIMEOUT = 7.0
BROWSER_START_TIMEOUT = 18.0
BROWSER_PAGE_TIMEOUT = 12000
MAX_LISTING_RESULTS = 8
MAX_VERIFY_RESULTS = 4
MAX_CONCURRENT_SITES = 12
MAX_BROWSER_PAGES = 4

SITES = [
    {"id": "01", "name": "ET Store", "kind": "shopify", "search": "https://www.etstore.in/search?q={q}", "origin": "https://www.etstore.in"},
    {"id": "02", "name": "Robu.in", "kind": "robu", "search": "https://robu.in/?s={q}&post_type=product", "origin": "https://robu.in"},
    {"id": "03", "name": "element14 India", "kind": "element14", "search": "https://in.element14.com/search?st={q}", "origin": "https://in.element14.com"},
    {"id": "04", "name": "ElectronicsComp", "kind": "opencart", "search": "https://www.electronicscomp.com/index.php?route=product/search&search={q}", "origin": "https://www.electronicscomp.com"},
    {"id": "05", "name": "Evelta", "kind": "magento", "search": "https://evelta.com/catalogsearch/result/?q={q}", "origin": "https://evelta.com"},
    {"id": "06", "name": "Tomson Electronics", "kind": "shopify", "search": "https://www.tomsonelectronics.com/search?q={q}", "origin": "https://www.tomsonelectronics.com"},
    {"id": "07", "name": "QuartzComponents", "kind": "shopify", "search": "https://quartzcomponents.com/search?q={q}", "origin": "https://quartzcomponents.com"},
    {"id": "08", "name": "MakerBazar", "kind": "shopify", "search": "https://makerbazar.in/search?q={q}", "origin": "https://makerbazar.in"},
    {"id": "09", "name": "Probots", "kind": "woocommerce", "search": "https://probots.co.in/?s={q}&post_type=product", "origin": "https://probots.co.in"},
    {"id": "10", "name": "Sharvi Electronics", "kind": "woocommerce", "search": "https://sharvielectronics.com/?s={q}&post_type=product", "origin": "https://sharvielectronics.com"},
    {"id": "11", "name": "Leeds Electronic Industry Inc", "kind": "generic", "search": "https://www.leedsind.net/?s={q}", "origin": "https://www.leedsind.net"},
    {"id": "12", "name": "Sparefly", "kind": "shopify", "search": "https://sparefly.com/search?q={q}", "origin": "https://sparefly.com"},
]
SITE_BY_ID = {s["id"]: s for s in SITES}
SITE_BY_NAME = {s["name"].lower(): s for s in SITES}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-IN,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}
ROBU_API = "https://robu.in/wp-json/wc/store/v1/products"


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def extract_part(query):
    # Prefer a distinctive alphanumeric part number. Do not use a specification
    # such as 3A/5V/0805 as the primary identifier when a real part number exists.
    tokens = clean_text(query).split()
    for token in tokens:
        token = token.strip(" ,;:/()[]{}")
        if len(token) >= 3 and re.search(r"[A-Za-z]", token) and re.search(r"\d", token):
            return token
    for token in tokens:
        token = token.strip(" ,;:/()[]{}")
        if len(token) >= 3 and re.search(r"[A-Za-z]", token):
            return token
    return " ".join(tokens[:2])


def query_variants(query):
    original = clean_text(query)
    part = extract_part(original)
    values = [original]
    if part and normalize(part) != normalize(original):
        values.append(part)
    # A second useful form removes common electrical parameter words which often
    # prevent distributor search engines from returning the base component.
    tokens = original.split()
    if len(tokens) > 1 and part:
        reduced = " ".join(t for t in tokens if normalize(t) != normalize(part))
        if reduced and len(reduced) >= 3:
            # Keep the full query first; reduced is only a fallback.
            values.append(part + " " + " ".join(t for t in tokens if normalize(t) not in {normalize(part), "smd", "smt", "dip"}))
    out = []
    seen = set()
    for v in values:
        v = clean_text(v)
        if v and normalize(v) not in seen:
            seen.add(normalize(v)); out.append(v)
    return out[:3]


def query_tokens(query):
    return [x for x in normalize(query).split() if len(x) > 1]


def component_match(title, query):
    nt = normalize(title)
    part = normalize(extract_part(query))
    if not part:
        return False
    # A real part number must be present. This avoids unrelated products that
    # merely contain words such as "module" or "3A".
    if part not in nt:
        # Allow compact forms such as LM2596S-ADJ vs LM2596S ADJ.
        compact_title = re.sub(r"[^a-z0-9]", "", nt)
        compact_part = re.sub(r"[^a-z0-9]", "", part)
        if not compact_part or compact_part not in compact_title:
            return False
    return True


def title_score(title, query):
    nt = normalize(title); nq = normalize(query); part = normalize(extract_part(query))
    score = 0
    if part and part in nt: score += 120
    if nq and nq in nt: score += 70
    for token in query_tokens(query):
        if token in nt: score += 10
    return score


def absolute_url(site, href):
    return urljoin(site["origin"], href or "")


def valid_product_url(site, link):
    try:
        a = urlparse(link); b = urlparse(site["origin"])
        return a.scheme in ("http", "https") and a.netloc.lower() == b.netloc.lower()
    except Exception:
        return False


def parse_availability(text):
    low = clean_text(text).lower()
    if not low:
        return "UNKNOWN", None
    # Specific quantity forms first.
    patterns = [
        r"(?:availability|available|stock|quantity)\s*[:\-]?\s*([0-9][0-9,]*)",
        r"only\s+([0-9][0-9,]*)\s+(?:items?\s+)?in\s+stock",
        r"([0-9][0-9,]*)\s+(?:items?\s+)?in\s+stock",
        r"([0-9][0-9,]*)\s+available",
    ]
    for pattern in patterns:
        m = re.search(pattern, low, re.I)
        if m:
            try:
                qty = int(m.group(1).replace(",", ""))
                return ("IN_STOCK" if qty > 0 else "OUT_OF_STOCK"), qty
            except ValueError:
                pass
    if re.search(r"\bout\s*of\s*stock\b|\bsold\s*out\b|\bcurrently unavailable\b", low):
        return "OUT_OF_STOCK", 0
    if re.search(r"\bavailable\s+to\s+order\b|\bavailable\s+on\s+order\b|\bback\s*order\b|\bpre[- ]?order\b", low):
        return "AVAILABLE_TO_ORDER", None
    if re.search(r"\bin\s*stock\b|\bin-stock\b|\bavailable\b", low):
        return "IN_STOCK", None
    return "UNKNOWN", None


def availability_label(state, qty):
    if state == "IN_STOCK": return f"In Stock ({qty:,})" if qty is not None else "In Stock"
    if state == "OUT_OF_STOCK": return "Out of Stock"
    if state == "AVAILABLE_TO_ORDER": return "Available to Order"
    return "Availability Not Confirmed"


def structured_availability(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw: continue
        try: data = json.loads(raw)
        except Exception: continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, list): stack.extend(obj); continue
            if not isinstance(obj, dict): continue
            if isinstance(obj.get("@graph"), list): stack.extend(obj["@graph"])
            offers = obj.get("offers")
            if isinstance(offers, list): stack.extend(offers)
            elif isinstance(offers, dict): stack.append(offers)
            av = str(obj.get("availability", "")).lower()
            qty = obj.get("inventoryLevel")
            if isinstance(qty, dict): qty = qty.get("value")
            try: qty = int(qty) if qty is not None else None
            except Exception: qty = None
            if "instock" in av: return "IN_STOCK", qty
            if "outofstock" in av: return "OUT_OF_STOCK", 0
            if "backorder" in av or "preorder" in av: return "AVAILABLE_TO_ORDER", None
    return "UNKNOWN", None


def availability_from_html(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    state, qty = structured_availability(soup)
    if state != "UNKNOWN": return state, qty

    # Common stock metadata/data attributes.
    for node in soup.select("[data-stock], [data-stock-quantity], [data-available], meta[itemprop=availability], meta[property*=availability]")[:20]:
        raw = clean_text(node.get("content") or node.get("data-stock") or node.get("data-stock-quantity") or node.get("data-available"))
        if raw:
            st, q = parse_availability(raw)
            if st != "UNKNOWN": return st, q
            low = raw.lower()
            if "instock" in low or low in {"true", "1"}: return "IN_STOCK", None
            if "outofstock" in low or low in {"false", "0"}: return "OUT_OF_STOCK", 0

    selectors = [
        ".availability", ".stock", ".stock-status", ".product-stock", ".inventory", ".availability-status",
        ".product-info", ".product-information", ".product-form", ".product__info-container", ".product-form__buttons",
        ".product-single__meta", ".summary", "form.cart", "main", "#content",
    ]
    best = ("UNKNOWN", None)
    for selector in selectors:
        for node in soup.select(selector)[:6]:
            txt = clean_text(node.get_text(" ", strip=True))
            st, q = parse_availability(txt)
            if st == "IN_STOCK" and q is not None: return st, q
            if st == "OUT_OF_STOCK": best = (st, q)
            elif st == "AVAILABLE_TO_ORDER" and best[0] == "UNKNOWN": best = (st, q)
            elif st == "IN_STOCK" and best[0] == "UNKNOWN": best = (st, q)

    # Only inspect controls that belong to a product form. This avoids header/cart
    # buttons producing false "in stock" results.
    for form in soup.select("form.cart, form[action*=cart], .product-form, .product-form__buttons")[:4]:
        txt = clean_text(form.get_text(" ", strip=True)).lower()
        st, q = parse_availability(txt)
        if st != "UNKNOWN": return st, q
        for node in form.select("button, input[type=submit], a"):
            label = clean_text(node.get("value") or node.get_text(" ", strip=True)).lower()
            disabled = node.has_attr("disabled") or str(node.get("aria-disabled", "")).lower() == "true"
            cls = " ".join(node.get("class", [])).lower()
            if any(x in label for x in ("add to cart", "buy now", "add to basket")) and not disabled and "disabled" not in cls:
                return "IN_STOCK", None

    body = clean_text(soup.get_text(" ", strip=True))
    if re.search(r"\bout\s*of\s*stock\b|\bsold\s*out\b", body, re.I): return "OUT_OF_STOCK", 0
    return best


def extract_price(block):
    node = block.select_one(".price, .product-price, .price-box, [class*=price], [data-price]") if block else None
    return clean_text(node.get_text(" ", strip=True)) if node else "N/A"


def candidate_from_anchor(anchor, site, query, block=None):
    href = anchor.get("href", "")
    link = absolute_url(site, href)
    if not valid_product_url(site, link): return None
    direct = clean_text(anchor.get_text(" ", strip=True))
    context = direct
    node = block or anchor
    # Prefer an actual product heading anywhere in the product card.
    title_node = node.select_one("h1,h2,h3,h4,h5,.product-title,.product-name,.name,.woocommerce-loop-product__title") if hasattr(node, "select_one") else None
    if title_node:
        t = clean_text(title_node.get_text(" ", strip=True))
        if len(t) >= 3: direct = t
    parent = anchor
    for _ in range(3):
        parent = parent.parent
        if not parent: break
        text = clean_text(parent.get_text(" ", strip=True))
        if 3 <= len(text) <= 1200: context = text
    title = direct or context[:350]
    if not component_match(title + " " + context[:500] + " " + href, query): return None
    score = title_score(title + " " + context[:250], query)
    if normalize(extract_part(query)) in normalize(href): score += 30
    return {"title": title[:350], "link": link, "price": extract_price(block or parent), "_score": score}


def extract_candidates(html_text, site, query):
    soup = BeautifulSoup(html_text, "html.parser")
    candidates = {}
    part = normalize(extract_part(query))
    selectors = [
        "article", "li.product", ".product", ".product-item", ".product-card", ".product-thumb",
        ".grid-product", ".grid__item", ".card-wrapper", ".product-grid-item", ".search-result",
        ".product-listing", ".product-list-item", ".item-product", ".catalog-product",
    ]
    for selector in selectors:
        for block in soup.select(selector):
            links = block.select("a[href]")
            if not links: continue
            links.sort(key=lambda a: (0 if part and part in normalize(a.get_text(" ", strip=True) + " " + a.get("href", "")) else 1, -len(a.get_text(" ", strip=True))))
            for anchor in links[:3]:
                c = candidate_from_anchor(anchor, site, query, block)
                if c:
                    c["price"] = extract_price(block) or c["price"]
                    candidates[c["link"]] = c
                    break

    # Custom themes often have no recognizable card class. Look for anchors whose
    # visible text or href contains the actual part number.
    if len(candidates) < MAX_LISTING_RESULTS:
        for anchor in soup.select("a[href]"):
            txt = clean_text(anchor.get_text(" ", strip=True)); href = anchor.get("href", "")
            hay = normalize(txt + " " + href)
            if part and part not in hay and re.sub(r"[^a-z0-9]", "", part) not in re.sub(r"[^a-z0-9]", "", hay): continue
            if len(txt) < 3 and part not in normalize(href): continue
            c = candidate_from_anchor(anchor, site, query)
            if c: candidates.setdefault(c["link"], c)
            if len(candidates) >= MAX_LISTING_RESULTS: break

    out = sorted(candidates.values(), key=lambda x: x["_score"], reverse=True)[:MAX_LISTING_RESULTS]
    for item in out:
        item.update({"availability_state": "UNKNOWN", "stock_quantity": None, "availability": "Availability Not Confirmed"})
    return out


async def fetch_http(client, url, timeout=HTTP_TIMEOUT):
    try:
        r = await client.get(url, timeout=timeout, follow_redirects=True)
        text = r.text or ""
        if r.status_code >= 400 or not text: return None, r.status_code, r.url
        return text, r.status_code, r.url
    except Exception as exc:
        print(f"HTTP failed {url}: {type(exc).__name__}: {exc}")
        return None, None, None


class Browser:
    def __init__(self):
        self.cm = None; self.browser = None; self.lock = asyncio.Lock(); self.page_sem = asyncio.Semaphore(MAX_BROWSER_PAGES)

    async def start(self):
        if self.browser is not None: return self.browser
        if AsyncCamoufox is None: return None
        async with self.lock:
            if self.browser is not None: return self.browser
            try:
                self.cm = AsyncCamoufox(headless=True, humanize=False, block_images=True)
                self.browser = await asyncio.wait_for(self.cm.__aenter__(), timeout=BROWSER_START_TIMEOUT)
            except Exception as exc:
                print(f"Camoufox startup failed: {type(exc).__name__}: {exc}")
                self.cm = None; self.browser = None
        return self.browser

    async def fetch(self, url):
        async with self.page_sem:
            browser = await self.start()
            if browser is None: return None
            page = None
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_PAGE_TIMEOUT)
                await page.wait_for_timeout(700)
                return await page.content()
            except Exception as exc:
                print(f"Browser failed {url}: {type(exc).__name__}: {exc}")
                return None
            finally:
                if page:
                    try: await page.close()
                    except Exception: pass

    async def close(self):
        if self.cm:
            try: await self.cm.__aexit__(None, None, None)
            except Exception: pass
        self.cm = None; self.browser = None


async def shopify_search(client, site, query):
    origin = site["origin"].rstrip("/")
    for variant in query_variants(query):
        # First use Shopify's JSON endpoint.
        url = origin + "/search/suggest.json"
        try:
            r = await client.get(url, params={
                "q": variant, "resources[type]": "product", "resources[limit]": str(MAX_LISTING_RESULTS),
                "resources[options][unavailable_products]": "show",
            }, timeout=HTTP_TIMEOUT)
            if r.is_success:
                data = r.json()
                products = (((data.get("resources") or {}).get("results") or {}).get("products") or [])
                out = []
                for p in products:
                    title = clean_text(p.get("title")); link = absolute_url(site, p.get("url", ""))
                    if title and valid_product_url(site, link) and component_match(title, query):
                        out.append({"title": title, "link": link, "price": clean_text(str(p.get("price") or "N/A")), "availability_state": "UNKNOWN", "stock_quantity": None, "availability": "Availability Not Confirmed", "_score": title_score(title, query)})
                if out: return sorted(out, key=lambda x: x["_score"], reverse=True)[:MAX_LISTING_RESULTS], True
        except Exception as exc:
            print(f"Shopify JSON failed {site['name']}: {type(exc).__name__}: {exc}")

        # Some Shopify themes expose /search.json even when suggest.json does not.
        for url in (origin + "/search.json", origin + "/search"):
            try:
                r = await client.get(url, params={"q": variant, "type": "product", "options[unavailable_products]": "show"}, timeout=HTTP_TIMEOUT)
                if r.is_success:
                    if "json" in r.headers.get("content-type", "") or url.endswith("search.json"):
                        try:
                            data = r.json(); products = data.get("products") if isinstance(data, dict) else None
                        except Exception: products = None
                        if products:
                            out=[]
                            for p in products:
                                title=clean_text(p.get("title")); link=absolute_url(site, p.get("url") or ("/products/"+str(p.get("handle"))) if p.get("handle") else "")
                                if title and valid_product_url(site, link) and component_match(title, query): out.append({"title":title,"link":link,"price":clean_text(str(p.get("price") or "N/A")),"availability_state":"UNKNOWN","stock_quantity":None,"availability":"Availability Not Confirmed","_score":title_score(title,query)})
                            if out: return sorted(out,key=lambda x:x["_score"],reverse=True)[:MAX_LISTING_RESULTS], True
                    else:
                        found = extract_candidates(r.text, site, query)
                        if found: return found, True
            except Exception as exc:
                print(f"Shopify fallback failed {site['name']}: {type(exc).__name__}: {exc}")
    return [], False


async def woocommerce_search(client, site, query):
    url = site["origin"].rstrip("/") + "/wp-json/wc/store/v1/products"
    reached = False
    for variant in query_variants(query):
        try:
            r = await client.get(url, params={"search": variant, "per_page": MAX_LISTING_RESULTS, "catalog_visibility": "visible"}, timeout=HTTP_TIMEOUT)
            if not r.is_success: continue
            reached = True
            data = r.json()
            if not isinstance(data, list): continue
            out=[]
            for item in data:
                title=clean_text(item.get("name")); link=item.get("permalink") or ""
                if not title or not link or not component_match(title, query): continue
                prices=item.get("prices") or {}; raw=prices.get("price_html") or prices.get("price") or ""
                price=clean_text(BeautifulSoup(str(raw),"html.parser").get_text(" ",strip=True)) or "N/A"
                if item.get("is_in_stock") is True: state="IN_STOCK"
                elif item.get("is_in_stock") is False: state="OUT_OF_STOCK"
                elif item.get("is_on_backorder"): state="AVAILABLE_TO_ORDER"
                else: state="UNKNOWN"
                qty=None
                stock=item.get("stock_availability") or {}
                if state=="UNKNOWN": state,qty=parse_availability(clean_text(stock.get("text")))
                out.append({"title":title,"link":link,"price":price,"availability_state":state,"stock_quantity":qty,"availability":availability_label(state,qty),"_score":title_score(title,query)})
            if out: return sorted(out,key=lambda x:x["_score"],reverse=True)[:MAX_LISTING_RESULTS], True
        except Exception as exc:
            print(f"Woo search failed {site['name']}: {type(exc).__name__}: {exc}")
    return [], reached


async def robu_search(client, query):
    reached = False
    for variant in query_variants(query):
        try:
            r=await client.get(ROBU_API,params={"search":variant,"per_page":MAX_LISTING_RESULTS},timeout=HTTP_TIMEOUT)
            if not r.is_success: continue
            reached = True
            data=r.json(); out=[]
            for p in data if isinstance(data,list) else []:
                title=clean_text(p.get("name")); link=p.get("permalink")
                if not title or not link or not component_match(title,query): continue
                prices=p.get("prices") or {}; price=clean_text(BeautifulSoup(str(prices.get("price_html") or prices.get("price") or ""),"html.parser").get_text(" ",strip=True)) or "N/A"
                if p.get("is_in_stock") is True: state="IN_STOCK"
                elif p.get("is_in_stock") is False: state="OUT_OF_STOCK"
                elif p.get("is_on_backorder"): state="AVAILABLE_TO_ORDER"
                else: state="UNKNOWN"
                qty=None; stock=p.get("stock_availability") or {}
                if state=="UNKNOWN": state,qty=parse_availability(clean_text(stock.get("text")))
                out.append({"title":title,"link":link,"price":price,"availability_state":state,"stock_quantity":qty,"availability":availability_label(state,qty),"_score":title_score(title,query)})
            if out: return sorted(out,key=lambda x:x["_score"],reverse=True)[:MAX_LISTING_RESULTS], True
        except Exception as exc: print(f"Robu API failed: {type(exc).__name__}: {exc}")
    return [], reached


def site_search_urls(site, query):
    q=quote_plus(query); origin=site["origin"].rstrip("/"); urls=[]
    urls.append(site["search"].format(q=q))
    kind=site["kind"]
    if kind in {"woocommerce","generic"}:
        urls += [f"{origin}/?s={q}&post_type=product", f"{origin}/?post_type=product&s={q}", f"{origin}/search/?q={q}", f"{origin}/search?q={q}", f"{origin}/index.php?route=product/search&search={q}"]
    elif kind == "shopify":
        urls += [f"{origin}/search?q={q}&type=product", f"{origin}/search?type=product&q={q}"]
    elif kind == "magento":
        urls += [f"{origin}/catalogsearch/result/?q={q}", f"{origin}/search?q={q}"]
    elif kind == "opencart":
        urls += [f"{origin}/index.php?route=product/search&search={q}", f"{origin}/search?search={q}"]
    out=[]; seen=set()
    for u in urls:
        if u not in seen: seen.add(u); out.append(u)
    return out


async def native_html_search(client, site, query):
    reached = False
    for variant in query_variants(query):
        for url in site_search_urls(site, variant):
            html,status,_ = await fetch_http(client,url)
            if html is None: continue
            reached = True
            found=extract_candidates(html,site,query)
            if found: return found, True
            # Do not stop on the first empty page: themes and search endpoints can
            # differ. Continue through the remaining native search forms.
    return [], reached


async def browser_search(browser, site, query):
    if await browser.start() is None: return [], False
    for variant in query_variants(query):
        for url in site_search_urls(site,variant)[:3]:
            html=await browser.fetch(url)
            if not html: continue
            found=extract_candidates(html,site,query)
            if found: return found, True
            # Continue to the next search form if this page rendered but did not
            # expose the expected product cards.
    return [], False


async def shopify_product_json(client, site, link):
    try:
        path=urlparse(link).path.rstrip("/")
        if not path.startswith("/products/"): return "UNKNOWN",None
        r=await client.get(site["origin"].rstrip("/")+path+".js",timeout=PRODUCT_TIMEOUT)
        if not r.is_success: return "UNKNOWN",None
        data=r.json()
        variants=data.get("variants") or []
        if not variants: return "UNKNOWN",None
        available=[v for v in variants if v.get("available") is True]
        if available: return "IN_STOCK",None
        if all(v.get("available") is False for v in variants): return "OUT_OF_STOCK",0
    except Exception as exc:
        print(f"Shopify product JSON failed {site['name']}: {type(exc).__name__}: {exc}")
    return "UNKNOWN",None


async def verify_product(client,browser,product,site):
    # Shopify has a public product JSON representation that is often more reliable
    # than parsing theme text.
    if site["kind"]=="shopify":
        st,q=await shopify_product_json(client,site,product["link"])
        if st!="UNKNOWN":
            product.update({"availability_state":st,"stock_quantity":q,"availability":availability_label(st,q),"verified":True})
            return True

    html,_,_=await fetch_http(client,product["link"],PRODUCT_TIMEOUT)
    state,qty=("UNKNOWN",None)
    if html: state,qty=availability_from_html(html)
    if state=="UNKNOWN" or html is None:
        browser_html=await browser.fetch(product["link"])
        if browser_html:
            bstate,bqty=availability_from_html(browser_html)
            if bstate!="UNKNOWN" or state=="UNKNOWN": state,qty=bstate,bqty
    if state=="UNKNOWN": return False
    product.update({"availability_state":state,"stock_quantity":qty,"availability":availability_label(state,qty),"verified":True})
    return True


async def verify_products(client,browser,products,site):
    candidates=products[:MAX_VERIFY_RESULTS]
    results=await asyncio.gather(*(verify_product(client,browser,p,site) for p in candidates),return_exceptions=True)
    return [p for p,ok in zip(candidates,results) if ok is True and p.get("verified")]


async def search_site(client,browser,site,query):
    started=time.perf_counter(); products=[]; reachable=False; methods=[]; errors=[]
    try:
        if site["kind"]=="robu":
            products,api_ok=await robu_search(client,query); methods.append("WooCommerce API")
            reachable = reachable or api_ok
        elif site["kind"]=="shopify":
            products,api_ok=await shopify_search(client,site,query); methods.append("Shopify API")
            reachable = reachable or api_ok
        elif site["kind"]=="woocommerce":
            products,api_ok=await woocommerce_search(client,site,query); methods.append("WooCommerce API")
            reachable = reachable or api_ok

        if not products:
            found,ok=await native_html_search(client,site,query)
            methods.append("website search")
            reachable = reachable or ok
            if found: products=found

        if not products:
            found,ok=await browser_search(browser,site,query)
            methods.append("browser search")
            reachable = reachable or ok
            if found: products=found

        if not products:
            if reachable:
                return [], "not_found", {"method": "+".join(methods), "elapsed_ms": int((time.perf_counter()-started)*1000), "error": None}
            return [], "unavailable", {"method": "+".join(methods), "elapsed_ms": int((time.perf_counter()-started)*1000), "error": "Website could not be reached by HTTP or browser"}

        verified=await verify_products(client,browser,products,site)
        if verified:
            return verified,"found",{"method":" + ".join(methods),"elapsed_ms":int((time.perf_counter()-started)*1000),"error":None}
        return [],"unverified",{"method":" + ".join(methods),"elapsed_ms":int((time.perf_counter()-started)*1000),"error":"Product page found but explicit availability was not confirmed"}
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        return [],"error",{"method":" + ".join(methods),"elapsed_ms":int((time.perf_counter()-started)*1000),"error":errors[-1]}


def sse(data): return f"data: {json.dumps(data,ensure_ascii=False)}\n\n"


async def event_generator(query,selected_sites=None):
    if selected_sites:
        wanted=set()
        for value in selected_sites:
            value=value.strip()
            if value in SITE_BY_ID: wanted.add(value)
            elif value.lower() in SITE_BY_NAME: wanted.add(SITE_BY_NAME[value.lower()]["id"])
        sites=[s for s in SITES if s["id"] in wanted]
    else: sites=list(SITES)

    yield sse({"type":"init","sites":[{"id":s["id"],"name":s["name"],"kind":s["kind"]} for s in sites]})
    browser=Browser()
    # Start browser in the background, but browser startup is no longer part of
    # every site's HTTP timeout. HTTP search can finish independently.
    browser_task=asyncio.create_task(browser.start())
    try:
        limits=httpx.Limits(max_connections=50,max_keepalive_connections=25)
        async with httpx.AsyncClient(headers=HEADERS,follow_redirects=True,limits=limits) as client:
            semaphore=asyncio.Semaphore(MAX_CONCURRENT_SITES)
            async def run_one(site):
                async with semaphore:
                    return await asyncio.wait_for(search_site(client,browser,site,query),timeout=SITE_TIMEOUT)

            tasks=[]
            for site in sites:
                yield sse({"type":"site_status","site_id":site["id"],"site":site["name"],"state":"searching"})
                async def one(site=site):
                    try:
                        result=await asyncio.wait_for(search_site(client,browser,site,query),timeout=SITE_TIMEOUT)
                        return site,result,None
                    except asyncio.TimeoutError:
                        return site,([],"timeout",{"method":"","elapsed_ms":int(SITE_TIMEOUT*1000),"error":"Site search timeout"}),None
                    except Exception as exc:
                        return site,([],"error",{"method":"","elapsed_ms":0,"error":f"{type(exc).__name__}: {exc}"}),None
                tasks.append(asyncio.create_task(one()))

            checked=0; total=len(sites)
            for completed_task in asyncio.as_completed(tasks):
                site,result,_=await completed_task
                products,state,info=result
                if site is None:
                    continue
                checked+=1
                for p in products:
                    p["site_id"]=site["id"]; p["site"]=site["name"]; p.pop("_score",None)
                if products:
                    yield sse({"type":"result","site_id":site["id"],"site":site["name"],"products":products})
                ui_state="done" if state in {"found","not_found","unverified"} else "failed"
                yield sse({"type":"site_done","site_id":site["id"],"site":site["name"],"checked":checked,"total":total,"count":len(products),"state":ui_state,"result":state,"method":info.get("method"),"elapsed_ms":info.get("elapsed_ms"),"error":info.get("error")})

            yield sse({"type":"done","checked":checked,"total":total})
            try: await browser_task
            except Exception: pass
    finally:
        await browser.close()


@app.get("/")
async def root(): return {"status":"ok","service":"component-finder","sites":len(SITES)}

@app.get("/api/health")
async def health():
    return {"status":"ok","camoufox_installed":AsyncCamoufox is not None,"sites":len(SITES)}

@app.get("/api/sites")
async def get_sites(): return {"sites":[{"id":s["id"],"name":s["name"],"kind":s["kind"]} for s in SITES]}

@app.get("/api/search")
async def search(q:str,sites:list[str]|None=Query(default=None)):
    query=clean_text(q)
    if not query: return JSONResponse({"error":"Query is required"},status_code=400)
    return StreamingResponse(event_generator(query,sites),media_type="text/event-stream",headers={"Cache-Control":"no-cache, no-transform","Connection":"keep-alive","X-Accel-Buffering":"no"})
