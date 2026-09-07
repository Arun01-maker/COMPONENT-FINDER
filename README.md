# Component Finder — Web App

## Setup
```bash
pip install -r requirements.txt
playwright install chromium
```

## Run
```bash
python app.py
```
Then open http://localhost:5000 in your browser (resize the window or open on
your phone to see the responsive layout — the filter sidebar collapses into a
tap-to-expand drawer under 960px).

## How it works
- `app.py` is a Flask server. It keeps the same Playwright + BeautifulSoup
  scraping and match-scoring logic as the desktop version, but runs it in a
  background thread per search and streams results to the browser over
  Server-Sent Events (`/api/search`) as each distributor finishes, instead of
  building a Tkinter window.
- `templates/index.html` is a single responsive page (no build step) styled
  around a PCB/datasheet look: copper + solder-mask green accents, monospace
  for data (prices/status), and a "current trace" animation along a line
  while a search is running.
