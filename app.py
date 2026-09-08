import json
import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx
from bs4 import BeautifulSoup

app = FastAPI()

# Enable CORS so GitHub Pages can call your Render API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DISTRIBUTORS = [
    {
        "name": "ET Store",
        "url": lambda q: f"https://etstore.in/index.php?route=product/search&search={q}",
        "parse": lambda html: [
            {
                "title": item.select_one(".caption h4 a").get_text(strip=True),
                "link": item.select_one(".caption h4 a")["href"],
                "price": item.select_one(".price").get_text(strip=True).split('\n')[0]
            }
            for item in BeautifulSoup(html, "html.parser").select(".product-thumb")
            if item.select_one(".caption h4 a")
        ]
    },
    {
        "name": "Robu.in",
        "url": lambda q: f"https://robu.in/?s={q}&post_type=product",
        "parse": lambda html: [
            {
                "title": item.select_one(".name, .product-title").get_text(strip=True),
                "link": item.select_one("a")["href"],
                "price": item.select_one(".price").get_text(strip=True) if item.select_one(".price") else "N/A"
            }
            for item in BeautifulSoup(html, "html.parser").select(".product-small, .product-type-simple")
            if item.select_one(".name, .product-title")
        ]
    }
]

async def event_generator(query: str):
    site_names = [d["name"] for d in DISTRIBUTORS]
    yield f"data: {json.dumps({'type': 'init', 'sites': site_names})}\n\n"

    async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}) as client:
        for store in DISTRIBUTORS:
            yield f"data: {json.dumps({'type': 'status', 'site': store['name'], 'state': 'searching'})}\n\n"
            try:
                resp = await client.get(store["url"](query), timeout=7.0)
                products = store["parse"](resp.text)
                
                yield f"data: {json.dumps({'type': 'status', 'site': store['name'], 'state': 'done', 'count': len(products)})}\n\n"
                if products:
                    yield f"data: {json.dumps({'type': 'result', 'site': store['name'], 'products': products})}\n\n"
            except Exception:
                yield f"data: {json.dumps({'type': 'status', 'site': store['name'], 'state': 'done', 'count': 0})}\n\n"

    yield f"data: {json.dumps({'type': 'done'})}\n\n"

@app.get("/api/search")
async def search(q: str):
    return StreamingResponse(event_generator(q), media_type="text/event-stream")
