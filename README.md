# Terminal Page Saver & Viewer

A fully‑featured terminal web browser built in Python, powered by [Textual](https://textual.textualize.io/).  
Scrape, save, and read web pages right inside your terminal, with images, favicons, SVG support, and optional inline video playback.

> **This project was written entirely with the help of AI.**

---

## Features

- 🔍 **Scrape any web page** – enter a URL and the page is downloaded, parsed, and stored locally.
- 📖 **Terminal reading mode** – browse saved pages with proper formatting, images, and clickable links.
- 🖼️ **Full image support** – PNG, JPEG, and even SVG images are downloaded and displayed.
- 🎬 **Video playback** – detect `<video>` tags and play them with `mpv` directly in the terminal (or fall back to your browser).
- 🧭 **Tab‑like navigation** – persistent per‑page scroll positions, just like browser tabs.
- 🏷️ **Favicons & page titles** – the left sidebar shows each site’s icon and name.
- 📂 **Offline storage** – all pages and images are saved under `browser_data/` for later reading.

*Note*: Video files are not saved locally. All page content (text, images, etc.) is stored offline and can be viewed without an internet connection – but videos are only referenced by their original URL and require a network connection to play.

---

## Simple Video

![Video About What Features It Has](https://raw.githubusercontent.com/mhrohani1385/terminal-page-saver/refs/heads/main/Screencast%20from%202026-07-03%2022-27-01.mp4)

---

## Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/mhrohani1385/terminal-page-saver.git
   cd terminal-page-saver
   ```

2. **Install Python dependencies** (Python 3.10+ recommended)
   ```bash
   pip install -r requirements.txt
   ```
   Or manually:
   ```bash
   pip install textual textual-image beautifulsoup4 pillow requests cairosvg
   ```

3. **Run the application**
   ```bash
   python main.py
   ```

---

## Usage

- Type a URL in the input bar and press **Enter** (or click **Save Page**).
- The app fetches the page, downloads all images, and stores it locally.
- Click a saved page on the left to read it.
- **Yellow links** are clickable and open in your system browser.
- **Images** are displayed and scaled to fit your terminal.
- **`Ctrl+V`** to play a video if one was found on the page.

### Quitting
- Press `Ctrl+Q` to exit the app and clear the terminal.

---

## Video Playback

If a page contains a `<video>` tag, a preview poster is shown (if available) and a red **▶ Play Video** link appears.

- **With `mpv` installed**: the video plays directly in the terminal using ASCII‑art output (`--vo=tct`).  
  Any key quits playback and returns you to the app.
- **Without `mpv`**: the video opens in your default web browser.

**Install mpv** (optional but recommended):
```bash
# Linux (Debian/Ubuntu)
sudo apt install mpv

# macOS
brew install mpv

# Windows
choco install mpv
```

---

## How It Works

1. The page HTML is fetched, cleaned, and parsed with **BeautifulSoup**.
2. Images, inline SVGs, and video posters are downloaded in parallel (max 3 concurrent workers).
3. All images are scaled to fit your terminal’s character grid (default 55×22 cells).
4. The page structure is saved as a `structure.json` alongside the raw HTML and image files.
5. When viewing, a separate **vertical scroll view** is created for each page, preserving its scroll position.

---

## Project Structure

```
.
├── main.py               # Application entry point
├── browser_data/          # Local storage (auto‑generated)
│   ├── db.json            # Index of saved pages
│   └── saved_pages/       # One folder per saved page
│       ├── index.html
│       ├── structure.json
│       ├── favicon.png
│       └── images/
└── requirements.txt
```

---

## Built with AI

This project was developed **entirely with AI assistance**.  
Every feature – from the HTML parser and image downloader to the TUI layout and video integration – was designed and implemented through interactive conversations with an AI model.  

No human wrote a single line of code directly; the AI generated, refined, and debugged the entire codebase based on natural language instructions.

---

## Dependencies

- [Textual](https://textual.textualize.io/) – terminal UI framework
- [textual-image](https://github.com/lnqs/textual-image) – image rendering in terminals
- [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/) – HTML parsing
- [Pillow](https://github.com/python-pillow/Pillow) – image processing
- [Requests](https://docs.python-requests.org/) – HTTP requests
- [CairoSVG](https://cairosvg.org/) – SVG to PNG conversion
- [mpv](https://mpv.io/) – (optional) terminal video playback

---

## License

MIT – see [LICENSE](LICENSE) for details.

Enjoy browsing the web, retro‑style! 🚀
