import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from camoufox.async_api import AsyncCamoufox
import asyncio
import json

# Global reference for browser instance
camoufox_browser = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global camoufox_browser
    # Initialize Camoufox headless browser
    camoufox_browser = AsyncCamoufox(
        headless=True,
        args=["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox"]
    )
    await camoufox_browser.__aenter__()
    print("Camoufox browser successfully started.")
    
    yield
    
    # Clean up browser on shutdown
    if camoufox_browser:
        await camoufox_browser.__aexit__(None, None, None)
        print("Camoufox browser closed.")

app = FastAPI(title="Component Finder API", lifespan=lifespan)

# Mount index.html / static files if present
if os.path.exists("index.html"):
    @app.get("/", response_class=HTMLResponse)
    async def serve_index():
        with open("index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())

@app.get("/health")
async def health_check():
    return {"status": "ok", "browser_connected": camoufox_browser is not None}

@app.get("/scrape")
async def scrape_url(url: str):
    if not camoufox_browser:
        raise HTTPException(status_code=500, detail="Browser is not initialized.")
    
    try:
        page = await camoufox_browser.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        content = await page.content()
        title = await page.title()
        await page.close()
        
        return JSONResponse({
            "success": True,
            "title": title,
            "html_length": len(content)
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/search")
async def api_search(q: str):
    """Server-Sent Events (SSE) endpoint expected by the frontend's EventSource.

    This returns a small simulated stream of events with the same message
    shapes the frontend expects: init, status, result, done.

    If camoufox_browser is available you can extend this to perform
    real scrapes and emit real product results.
    """
    async def event_generator():
        sites = ["Mouser", "Digi-Key", "Element14"]
        # init event with list of sites
        yield f"data: {json.dumps({'type':'init','sites':sites})}\n\n"
        await asyncio.sleep(0.2)

        for site in sites:
            # searching status
            yield f"data: {json.dumps({'type':'status','site':site,'state':'searching'})}\n\n"
            await asyncio.sleep(0.4)

            # simulated result count and product(s)
            count = 1
            products = [{
                'title': f"{q} - Generic Listing",
                'price': '₹99',
                'link': f"https://example.com/{q.replace(' ', '%20')}"
            }]

            # done status for this site
            yield f"data: {json.dumps({'type':'status','site':site,'state':'done','count':count})}\n\n"
            await asyncio.sleep(0.15)

            # send result(s)
            yield f"data: {json.dumps({'type':'result','site':site,'products':products})}\n\n"
            await asyncio.sleep(0.2)

        # final done event
        yield f"data: {json.dumps({'type':'done'})}\n\n"

    return StreamingResponse(event_generator(), media_type='text/event-stream')


if __name__ == "__main__":
    import uvicorn
    # Bind dynamically to PORT assigned by Render
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
