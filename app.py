import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from camoufox.async_api import AsyncCamoufox

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

if __name__ == "__main__":
    import uvicorn
    # Bind dynamically to PORT assigned by Render
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
