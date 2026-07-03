"""
Terminal Page Saver and Viewer
A TUI application for saving, managing, and reading web pages in the terminal.
Now with video playback support (mpv or browser fallback) and poster preview.
"""

import os
import json
import re
import shutil
import string
import tempfile
import threading
import requests
import webbrowser
from datetime import datetime
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple
from enum import Enum
import subprocess
from bs4 import BeautifulSoup
from PIL import Image as PILImage
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, ListView, ListItem, Label, Input, Button
from textual.containers import Horizontal, Vertical, VerticalScroll, Container
from textual_image.widget import Image
from textual import work
import cairosvg


# ============================================================================
# CONSTANTS AND CONFIGURATION
# ============================================================================

class Config:
    """Application configuration constants."""
    DATA_DIR = "browser_data"
    BASE_DIR = os.path.join(DATA_DIR, "saved_pages")
    DB_FILE = os.path.join(DATA_DIR, "db.json")
    
    HTTP_HEADERS = {
        "User-Agent": "TerminalPageSaver/1.0 (https://github.com/mhrohani1385/terminal-page-saver; m.h.rohani1385@gmail.com)"
    }
    
    # Terminal display limits (characters)
    MAX_CELL_WIDTH = 55
    MAX_CELL_HEIGHT = 22
    
    # Threading
    MAX_DOWNLOAD_WORKERS = 3
    REQUEST_TIMEOUT = 10
    FAVICON_TIMEOUT = 5


# ============================================================================
# DATA STRUCTURES
# ============================================================================

class PageElementType(Enum):
    """Types of elements that can appear in a saved page."""
    TEXT = "text"
    IMAGE = "image"
    IMAGE_FAILED = "image_failed"
    CAPTION = "caption"
    FAVICON = "favicon"
    IMAGE_PENDING = "image_pending"
    VIDEO = "video"               # new video element type


@dataclass
class SavedPageEntry:
    """Represents a saved page entry in the database."""
    path: str
    title: str
    favicon: Optional[str] = None
    url: Optional[str] = None
    
    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "title": self.title,
            "favicon": self.favicon,
            "url": self.url
        }
    
    @staticmethod
    def from_dict(data: dict) -> "SavedPageEntry":
        if isinstance(data, str):
            folder = os.path.basename(os.path.dirname(data))
            title = folder.split("_", 1)[-1].replace("_", " ") if "_" in folder else folder
            return SavedPageEntry(path=data, title=title, favicon=None)
        return SavedPageEntry(
            path=data["path"],
            title=data.get("title", "Untitled"),
            favicon=data.get("favicon"),
            url=data.get("url")
        )


# ============================================================================
# DATABASE MANAGEMENT
# ============================================================================

class PageDatabase:
    """Manages persistence of saved pages to JSON database."""
    
    def __init__(self, db_file: str):
        self.db_file = db_file
        self._ensure_db_exists()
    
    def _ensure_db_exists(self) -> None:
        os.makedirs(os.path.dirname(self.db_file) or ".", exist_ok=True)
        if not os.path.exists(self.db_file):
            with open(self.db_file, "w") as f:
                json.dump([], f)
    
    def load_all(self) -> List[SavedPageEntry]:
        with open(self.db_file, "r") as f:
            data = json.load(f)
        return [SavedPageEntry.from_dict(item) for item in data]
    
    def add(self, entry: SavedPageEntry) -> bool:
        all_entries = self.load_all()
        if any(item.path == entry.path for item in all_entries):
            return False
        all_entries.append(entry)
        self._save_all(all_entries)
        return True
    
    def _save_all(self, entries: List[SavedPageEntry]) -> None:
        with open(self.db_file, "w") as f:
            json.dump([e.to_dict() for e in entries], f, indent=2)


# ============================================================================
# IMAGE AND CONTENT PROCESSING
# ============================================================================

class ImageProcessor:
    @staticmethod
    def scale_for_terminal(pil_img: PILImage.Image) -> PILImage.Image:
        orig_w, orig_h = pil_img.size
        term_w = orig_w
        term_h = orig_h / 2.0
        scale = min(
            Config.MAX_CELL_WIDTH / term_w,
            Config.MAX_CELL_HEIGHT / term_h,
            1.0
        )
        final_cell_w = max(1, int(term_w * scale))
        final_cell_h = max(1, int(term_h * scale))
        return pil_img.resize((final_cell_w, final_cell_h * 2), PILImage.LANCZOS)
    
    @staticmethod
    def parse_svg_dimension(value: Optional[str]) -> Optional[float]:
        if value is None:
            return None
        value = value.lower().strip()
        if '%' in value or value == '':
            return None
        for suffix in ['px', 'em', 'ex', 'pt', 'pc', 'in', 'cm', 'mm']:
            if value.endswith(suffix):
                value = value[:-len(suffix)]
                break
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    
    @staticmethod
    def convert_svg_to_png(svg_content: bytes, width: int = 100, height: int = 100):
        return cairosvg.svg2png(
            bytestring=svg_content,
            output_width=width,
            output_height=height
        )


class FaviconDownloader:
    @staticmethod
    def get_favicon_url(soup: BeautifulSoup, base_url: str) -> str:
        icon_link = soup.find('link', rel=lambda r: r and 'icon' in r.lower())
        if icon_link and icon_link.get('href'):
            return urljoin(base_url, icon_link['href'])
        return urljoin(base_url, '/favicon.ico')
    
    @staticmethod
    def download_and_save(favicon_url: str, save_dir: str) -> Optional[str]:
        try:
            resp = requests.get(
                favicon_url,
                headers=Config.HTTP_HEADERS,
                timeout=Config.FAVICON_TIMEOUT
            )
            if resp.status_code != 200:
                return None
            content = resp.content
            content_type = resp.headers.get('Content-Type', '')
            is_svg = 'svg' in content_type or favicon_url.lower().endswith('.svg')
            if is_svg:
                png_data = ImageProcessor.convert_svg_to_png(content, width=64, height=64)
                img = PILImage.open(BytesIO(png_data))
            else:
                img = PILImage.open(BytesIO(content))
            img = img.convert('RGBA')
            img.thumbnail((64, 64), PILImage.LANCZOS)
            favicon_path = os.path.join(save_dir, 'favicon.png')
            img.save(favicon_path, 'PNG')
            return 'favicon.png'
        except Exception:
            return None


# ============================================================================
# HTML PARSING AND CONTENT EXTRACTION
# ============================================================================

class HTMLContentExtractor:
    REMOVE_TAGS = ["script", "style", "iframe"]
    TEXT_TAGS = ["p", "h1", "h2", "h3", "h4"]
    CAPTION_TAGS = ["caption", "figcaption"]
    # Added "video" to search tags
    SEARCH_TAGS = TEXT_TAGS + ["img"] + CAPTION_TAGS + ["svg", "video"]
    
    def __init__(self, base_url: str, save_dir: str):
        self.base_url = base_url
        self.save_dir = save_dir
        self.img_dir = os.path.join(save_dir, "images")
        os.makedirs(self.img_dir, exist_ok=True)
    
    def clean_html(self, soup: BeautifulSoup) -> None:
        for tag in soup(self.REMOVE_TAGS):
            tag.decompose()
        for noscript_tag in soup("noscript"):
            noscript_tag.unwrap()
    
    def _extract_image_source(self, img_tag) -> Optional[str]:
        src = (
            img_tag.get("src") or
            img_tag.get("data-src") or
            img_tag.get("data-original") or
            img_tag.get("data-lazy-src")
        )
        if not src and img_tag.get("srcset"):
            src = img_tag.get("srcset").split(",")[0].strip().split(" ")[0]
        return src

    def _extract_video_source(self, video_tag) -> Optional[str]:
        """Get the best video source from a <video> element."""
        src = video_tag.get("src") or video_tag.get("data-src")
        if src:
            return src
        source = video_tag.find("source")
        if source and source.get("src"):
            return source["src"]
        return None

    def _extract_video_poster(self, video_tag) -> Optional[str]:
        """Return the poster URL for a video, if available."""
        poster = video_tag.get("poster") or video_tag.get("data-poster")
        if poster:
            return urljoin(self.base_url, poster)
        return None
    
    def _format_text_element(self, tag) -> str:
        parts = []
        for child in tag.children:
            if child.name is None:
                if child.string:
                    parts.append(str(child.string))
            elif child.name == 'a':
                link_text = child.get_text()
                href = child.get("href")
                if href:
                    full_url = urljoin(self.base_url, href)
                    safe_url = full_url.replace('"', '%22').replace("'", '%27')
                    parts.append(f"[@click=\"app.open_link('{safe_url}')\"]{link_text}[/]")
                else:
                    parts.append(link_text)
            elif child.name in ['strong', 'b']:
                parts.append(f"[b]{child.get_text()}[/b]")
            elif child.name in ['em', 'i']:
                parts.append(f"[i]{child.get_text()}[/i]")
            else:
                parts.append(child.get_text())
        full_text = "".join(parts)
        return re.sub(r'\s+', ' ', full_text).strip()


# ============================================================================
# IMAGE DOWNLOAD MANAGER
# ============================================================================

class ImageDownloadManager:
    def __init__(self, base_url: str, img_dir: str, status_callback):
        self.base_url = base_url
        self.img_dir = img_dir
        self.status_callback = status_callback
        self.thread_local = threading.local()
    
    def _get_session(self) -> requests.Session:
        if not hasattr(self.thread_local, "session"):
            session = requests.Session()
            session.headers.update(Config.HTTP_HEADERS)
            session.headers["Referer"] = self.base_url
            self.thread_local.session = session
        return self.thread_local.session
    
    def _process_remote_image(self, img_url: str, alt_text: str, caption: str) -> dict:
        session = self._get_session()
        try:
            resp = session.get(img_url, timeout=Config.REQUEST_TIMEOUT)
            if resp.status_code == 429:
                return {
                    "type": PageElementType.IMAGE_FAILED.value,
                    "alt": alt_text, "caption": caption,
                    "error": "HTTP 429 (Rate Limited)", "error_type": "RateLimitError",
                    "url": img_url, "status_code": 429
                }
            if resp.status_code != 200:
                return {
                    "type": PageElementType.IMAGE_FAILED.value,
                    "alt": alt_text, "caption": caption,
                    "error": f"HTTP {resp.status_code}", "error_type": "HTTPError",
                    "url": img_url, "status_code": resp.status_code
                }
            content = resp.content
            content_type = resp.headers.get('Content-Type', '')
            is_svg = 'svg' in content_type or img_url.lower().endswith('.svg')
            return self._save_image_content(content, img_url, alt_text, caption, is_svg)
        except Exception as e:
            return {
                "type": PageElementType.IMAGE_FAILED.value,
                "alt": alt_text, "caption": caption,
                "error": str(e), "error_type": type(e).__name__,
                "url": img_url
            }
    
    def _process_inline_svg(self, svg_markup: str, alt_text: str, caption: str, orig_idx: int) -> dict:
        try:
            svg_soup = BeautifulSoup(svg_markup, 'html.parser')
            svg_elem = svg_soup.find('svg')
            if not svg_elem:
                raise ValueError("No <svg> element found")
            
            existing_style = str(svg_elem.get('style', ''))
            if 'color:' not in existing_style.lower():
                new_style = (existing_style + '; color: white;').strip('; ')
                svg_elem['style'] = new_style
            
            output_w, output_h = self._compute_svg_dimensions(svg_elem)
            for attr in ['width', 'height']:
                if attr in svg_elem.attrs:
                    del svg_elem[attr]
            svg_elem['width'] = str(output_w)
            svg_elem['height'] = str(output_h)
            if not svg_elem.get('viewBox') and not svg_elem.get('viewbox'):
                svg_elem['viewBox'] = f"0 0 {output_w} {output_h}"
            
            png_data = ImageProcessor.convert_svg_to_png(
                str(svg_elem).encode('utf-8'), width=output_w, height=output_h
            )
            pil_img = PILImage.open(BytesIO(png_data))
            pil_img = ImageProcessor.scale_for_terminal(pil_img)

            png_name = f"img_{orig_idx}_inline.svg.png"
            png_path = os.path.join(self.img_dir, png_name)
            pil_img.save(png_path)
            
            return {
                "type": PageElementType.IMAGE.value,
                "local_path": os.path.join("images", png_name),
                "abs_path": os.path.abspath(png_path),
                "alt": alt_text, "caption": caption
            }
        except Exception as e:
            return {
                "type": PageElementType.IMAGE_FAILED.value,
                "alt": alt_text, "caption": caption,
                "error": f"SVG conversion failed: {str(e)}",
                "error_type": "SVGConversionError",
                "url": ""
            }
    
    def _compute_svg_dimensions(self, svg_elem) -> Tuple[int, int]:
        w_abs = ImageProcessor.parse_svg_dimension(svg_elem.get('width'))
        h_abs = ImageProcessor.parse_svg_dimension(svg_elem.get('height'))
        output_w = output_h = None
        if w_abs and h_abs:
            output_w, output_h = int(w_abs), int(h_abs)
        else:
            viewbox = svg_elem.get('viewbox') or svg_elem.get('viewBox')
            if viewbox:
                parts = viewbox.split()
                if len(parts) >= 4:
                    output_w = int(float(parts[2]))
                    output_h = int(float(parts[3]))
        if output_w is None or output_h is None:
            viewbox = svg_elem.get('viewbox') or svg_elem.get('viewBox')
            if viewbox and len(viewbox.split()) >= 4:
                parts = viewbox.split()
                aspect = float(parts[2]) / float(parts[3])
            elif w_abs and h_abs:
                aspect = w_abs / h_abs
            else:
                aspect = 1.0
            output_h = 100
            output_w = int(output_h * aspect)
        return output_w, output_h
    
    def _save_image_content(self, content: bytes, source_url: str, alt_text: str, caption: str, is_svg: bool) -> dict:
        try:
            base_name = f"img_{os.path.basename(source_url.split('?')[0])}"
            base_name = re.sub(r'[\\/*?:"<>|]', "", base_name)
            if is_svg:
                png_data = ImageProcessor.convert_svg_to_png(content)
                pil_img = PILImage.open(BytesIO(png_data))
                pil_img = ImageProcessor.scale_for_terminal(pil_img)
                png_name = os.path.splitext(base_name)[0] + ".png"
                png_path = os.path.join(self.img_dir, png_name)
                pil_img.save(png_path)
                return {
                    "type": PageElementType.IMAGE.value,
                    "local_path": os.path.join("images", png_name),
                    "abs_path": os.path.abspath(png_path),
                    "alt": alt_text, "caption": caption
                }
            else:
                img_path = os.path.join(self.img_dir, base_name)
                with open(img_path, "wb") as f:
                    f.write(content)
                with PILImage.open(img_path) as pil_img:
                    pil_img = ImageProcessor.scale_for_terminal(pil_img)
                pil_img.save(img_path)
                return {
                    "type": PageElementType.IMAGE.value,
                    "local_path": os.path.join("images", base_name),
                    "abs_path": os.path.abspath(img_path),
                    "alt": alt_text, "caption": caption
                }
        except Exception as e:
            return {
                "type": PageElementType.IMAGE_FAILED.value,
                "alt": alt_text, "caption": caption,
                "error": str(e), "error_type": type(e).__name__,
                "url": source_url
            }
    
    def process_images(self, image_tasks: List[Tuple]) -> Dict[int, dict]:
        total = len(image_tasks)
        if total == 0:
            return {}
        results = {}
        # Process image tasks (including posters)
        def process_task(task_idx, orig_idx, resource, alt, caption, placeholder_idx, is_inline_svg, is_poster):
            if is_inline_svg:
                return task_idx, self._process_inline_svg(resource, alt, caption, orig_idx)
            else:
                # For posters, we use the same remote image processing; the alt/caption are ignored
                return task_idx, self._process_remote_image(resource, alt if not is_poster else "", caption if not is_poster else "")
        self.status_callback(f"Status: Downloading {total} images (max {Config.MAX_DOWNLOAD_WORKERS} at once)...")
        with ThreadPoolExecutor(max_workers=Config.MAX_DOWNLOAD_WORKERS) as executor:
            futures = {
                executor.submit(process_task, t_idx, o_idx, res, alt, cap, placeholder_idx, is_inline, is_poster): t_idx
                for t_idx, o_idx, res, alt, cap, placeholder_idx, is_inline, is_poster in image_tasks
            }
            completed = 0
            for future in as_completed(futures):
                task_idx, result = future.result()
                results[task_idx] = result
                completed += 1
                self.status_callback(f"Status: Downloaded {completed}/{total} images...")
        return results


# ============================================================================
# MAIN APPLICATION
# ============================================================================

class WebManager(App):
    """Main TUI application for managing and viewing saved web pages."""
    
    CSS = """
    Screen { layout: vertical; }
    #input-container { height: 3; width: 100%; margin: 1; }
    #url-input { width: 1fr; }
    #save-btn { width: 12; margin-left: 1; }
    
    #status-label { 
        width: 35; 
        margin-left: 2; 
        content-align-vertical: middle; 
        color: #00ffaa; 
        text-style: bold;
    }
    
    #workspace { layout: horizontal; height: 1fr; margin: 1; }
    
    #page-list { width: 30; height: 100%; border: solid $primary; }
    
    #viewer-container { 
        width: 1fr; 
        height: 100%; 
        border: solid #00aaff; 
        background: #121212; 
    }
    
    .page-scroll {
        width: 100%;
        height: 100%;
        padding: 1 2;
    }
    
    .placeholder-label {
        padding: 1 2;
    }
    
    ListItem { padding: 1; background: $surface-darken-1; }
    
    .reader-h1 { text-style: bold; color: #ffffff; margin: 1 0; background: #0055ff; padding: 0 1; text-wrap: wrap; width: 100%; link-color: yellow; link-style: underline; }
    .reader-h2 { text-style: bold; color: #00ffaa; margin: 1 0; text-wrap: wrap; width: 100%; link-color: yellow; link-style: underline; }
    .reader-p  { color: #ffffff; margin-bottom: 1; text-wrap: wrap; width: 100%; link-color: yellow; link-style: underline; }
    
    .reader-caption {
        color: #888888;
        text-style: italic;
        margin-top: 0;
        margin-bottom: 1;
        text-wrap: wrap;
        width: 100%;
    }
    
    .image-wrapper {
        width: 100%;
        height: auto;
        align-horizontal: center;
        margin-top: 1;
        margin-bottom: 0;
    }
    
    .reader-image { 
        border: solid #00ffaa;
    }

    .list-item-row {
        align: left middle;
        height: auto;
        padding: 0 1;
    }
    
    .list-favicon {
        border: none;
        margin-right: 1;
    }

    .list-text-block {
        height: auto;
        width: 1fr;
        align-vertical: middle;
    }

    .list-title {
        color: $text;
        text-style: bold;
        width: 1fr;
    }

    .list-domain {
        color: $text 50%;
        text-style: italic;
        width: 1fr;
        margin-top: 0;
    }
    .video-play-link {
        color: $text;
        align: center middle;
        link-color: red;
        link-style: underline;
    }

    .video-preview {
        width: 100%;
        align: center middle;
        padding: 1 0;
        background: #1a1a1a;
        margin: 1 0;
    }
    .video-icon {
        color: red;
    }
    .video-title {
        color: $text;
        margin-top: 1;
    }

    .video-play-container {
        width: 100%;
        align: center middle;
        margin-top: 1;
    }

    """
    
    BINDINGS = [
        ("ctrl+v", "play_video", "Play video"),
        ("ctrl+q", "c_quit", "Quit"),
    ]

    class PageTitleIcon(Horizontal):
        def __init__(self, favicon_path: Optional[str], title: str, domain: str = ""):
            super().__init__(classes="list-item-row")
            self.favicon_path = favicon_path
            self.title = title
            self.domain = domain

        def compose(self) -> ComposeResult:
            if self.favicon_path and os.path.exists(self.favicon_path):
                icon = Image(self.favicon_path)
                icon.styles.width = 6
                icon.styles.height = 3
                icon.add_class("list-favicon")
                yield icon
            yield Vertical(classes="list-text-block")

        def on_mount(self):
            text_block = self.query_one(".list-text-block", Vertical)
            text_block.mount(Label(self.title, classes="list-title"))
            if self.domain:
                text_block.mount(Label(self.domain, classes="list-domain"))

    def __init__(self):
        super().__init__()
        self.db = PageDatabase(Config.DB_FILE)
        self.files_cache: List[SavedPageEntry] = []
        self._refresh_counter = 0
        self._page_views: Dict[str, VerticalScroll] = {}
        self._current_index_file: Optional[str] = None
        self._current_elements: Optional[List[dict]] = None  # holds elements of current page

    # ========== UI Layout ==========
    
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Horizontal(
            Input(placeholder="Enter URL to save...", id="url-input"),
            Button("Save Page", id="save-btn"),
            Label("Status: Ready", id="status-label"), 
            id="input-container"
        )
        with Horizontal(id="workspace"):
            yield ListView(id="page-list")
            with Container(id="viewer-container"):
                yield Label("Select a page from the left to read inside the terminal...", classes="placeholder-label")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_list()

    # ========== Status Management ==========
    
    def update_status(self, text: str) -> None:
        self.query_one("#status-label", Label).update(text)

    def action_open_link(self, link: str) -> None:
        self.notify(f"Opening browser: {link}")
        webbrowser.open(link)

    # ========== Database and List Management ==========
    
    def refresh_list(self) -> None:
        list_view = self.query_one("#page-list", ListView)
        list_view.clear()
        self.files_cache = list(reversed(self.db.load_all()))
        self._refresh_counter += 1
        for i, entry in enumerate(self.files_cache):
            favicon_path = None
            if entry.favicon:
                favicon_path = os.path.join(os.path.dirname(entry.path), entry.favicon)
            domain = ""
            if entry.url:
                parsed = urlparse(entry.url)
                domain = parsed.netloc.replace("www.", "")
            row = self.PageTitleIcon(favicon_path, entry.title, domain)
            list_view.append(ListItem(row, id=f"item_{i}_{self._refresh_counter}"))

    # ========== User Interactions ==========
    
    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.start_download(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        url = self.query_one("#url-input", Input).value
        self.start_download(url)

    def start_download(self, url: str) -> None:
        if not url:
            return
        self.query_one("#url-input", Input).value = ""
        self.save_page_worker(url)

    # ========== Page switching (persistent views) ==========

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        parts = event.item.id.split("_")
        index = int(parts[1])
        if index >= len(self.files_cache):
            self.notify("Invalid page selection", severity="error")
            return
        
        entry = self.files_cache[index]
        index_file = entry.path
        structure_file = os.path.join(os.path.dirname(index_file), "structure.json")
        if not os.path.exists(structure_file):
            self.notify("Structure file not found!", severity="error")
            return

        self._current_index_file = index_file

        # Create the page view if this page hasn't been opened yet
        if index_file not in self._page_views:
            self._create_page_view(index_file, structure_file)

        # Show the selected page, hide all others
        self._switch_to_page(index_file)
        self.title = f"Reading TUI: {entry.title}"

        # Load current elements for video lookup
        with open(structure_file, "r", encoding="utf-8") as f:
            self._current_elements = json.load(f)

    def _create_page_view(self, index_file: str, structure_file: str) -> None:
        viewer_container = self.query_one("#viewer-container")
        # Hide the placeholder label once we have at least one page
        placeholder = viewer_container.query_one(".placeholder-label")
        if placeholder:
            placeholder.display = False
        
        page_view = VerticalScroll(classes="page-scroll")
        viewer_container.mount(page_view)
        self._render_page_into(page_view, structure_file)
        self._page_views[index_file] = page_view

    def _switch_to_page(self, index_file: str) -> None:
        for path, view in self._page_views.items():
            view.display = (path == index_file)

    def _render_page_into(self, view: VerticalScroll, json_path: str) -> None:
        view.remove_children()
        with open(json_path, "r", encoding="utf-8") as f:
            elements = json.load(f)
        if elements and elements[0].get("type") == PageElementType.FAVICON.value:
            elements = elements[1:]
        for item in elements:
            self._render_element(view, item)

    # ========== Content Rendering ==========
    
    def _render_element(self, view: VerticalScroll, item: dict) -> None:
        elem_type = item.get("type")
        if elem_type == PageElementType.TEXT.value:
            self._render_text_element(view, item)
        elif elem_type == PageElementType.CAPTION.value:
            self._render_caption(view, item)
        elif elem_type in [PageElementType.IMAGE.value, PageElementType.IMAGE_FAILED.value]:
            self._render_image_element(view, item)
        elif elem_type == PageElementType.VIDEO.value:
            self._render_video_element(view, item)

    def _render_text_element(self, view: VerticalScroll, item: dict) -> None:
        tag_name = item.get("tag_name", "p")
        value = item.get("value", "")
        if tag_name == "h1":
            view.mount(Label(f" {value.upper()} ", classes="reader-h1", markup=True))
        elif tag_name in ["h2", "h3", "h4"]:
            view.mount(Label(value, classes="reader-h2", markup=True))
        else:
            view.mount(Label(value, classes="reader-p", markup=True))

    def _render_caption(self, view: VerticalScroll, item: dict) -> None:
        view.mount(Label(f"📝 {item.get('value', '')}", classes="reader-caption", markup=True))

    def _render_image_element(self, view: VerticalScroll, item: dict) -> None:
        wrapper = Container(classes="image-wrapper")
        view.mount(wrapper)
        if item.get("type") == PageElementType.IMAGE.value:
            abs_path = item.get("abs_path")
            if abs_path and os.path.exists(abs_path):
                try:
                    self._render_image_widget(wrapper, abs_path)
                except Exception:
                    self._render_image_widget(wrapper, abs_path, fallback=True)
            else:
                wrapper.mount(Label("🖼️ [Local Image File Missing]", classes="reader-caption"))
        else:
            wrapper.mount(Label("🖼️ [Image Failed to Load]", classes="reader-caption"))
        if item.get("alt"):
            view.mount(Label(f"[b]Alt Text:[/b] {item['alt']}", classes="reader-caption", markup=True))
        if item.get("caption"):
            view.mount(Label(f"[b]Caption:[/b] {item['caption']}", classes="reader-caption", markup=True))

    def _render_image_widget(self, container: Container, img_path: str, fallback: bool = False) -> None:
        img_widget = Image(img_path)
        img_widget.add_class("reader-image")
        if not fallback:
            try:
                with PILImage.open(img_path) as pil_img:
                    orig_w, orig_h = pil_img.size
                term_w = orig_w
                term_h = orig_h / 2
                scale = min(
                    Config.MAX_CELL_WIDTH / term_w,
                    Config.MAX_CELL_HEIGHT / term_h,
                    1.0
                )
                final_w = max(1, int(term_w * scale))
                final_h = max(1, int(term_h * scale))
                img_widget.styles.width = final_w
                img_widget.styles.height = final_h
            except Exception:
                fallback = True
        if fallback:
            img_widget.styles.width = 45
            img_widget.styles.height = 16
        container.mount(img_widget)

    # ========== Video Rendering ==========

    def _fallback_preview(self, view: VerticalScroll, alt_text: str) -> None:
        """Mount a film-reel emoji as a placeholder, directly into the view."""
        view.mount(Label("🎬", classes="video-icon"))
        if alt_text:
            view.mount(Label(alt_text, classes="video-title"))

    def _render_video_element(self, view: VerticalScroll, item: dict) -> None:
        url = item.get("url", "")
        poster_local = item.get("poster_local")
        alt = item.get("alt", "")

        # 1. Poster (exact same logic as regular images)
        if poster_local and self._current_index_file is not None:
            page_dir = os.path.dirname(self._current_index_file)
            abs_poster_path = os.path.join(page_dir, poster_local)
            if os.path.exists(abs_poster_path):
                wrapper = Container(classes="image-wrapper")
                view.mount(wrapper)
                self._render_image_widget(wrapper, abs_poster_path)
            else:
                self._fallback_preview(view, alt)
        else:
            self._fallback_preview(view, alt)

        # 2. Red play link (centered)
        safe_url = url.replace('"', '%22').replace("'", '%27')
        play_link = f"[@click=\"app.play_video('{safe_url}')\"]====-- ▶ Play Video --====[/]"
        play_container = Container(classes="video-play-container")
        view.mount(play_container)
        play_container.mount(Label(play_link, classes="video-play-link", markup=True))

    # ========== Video Playback ==========

    def get_current_video_url(self) -> Optional[str]:
        """Return the URL of the first video in the current page, if any."""
        if not self._current_elements:
            return None
        for item in self._current_elements:
            if item.get("type") == PageElementType.VIDEO.value:
                return item.get("url")
        return None

    def action_play_video(self, video_url: Optional[str] = None) -> None:
        """Play a video. Called from keybinding or clickable link."""
        self.notify("Trying to play video!", severity="information")   # <-- debug
        if video_url is None:
            video_url = self.get_current_video_url()
        if not video_url:
            self.notify("No video on this page", severity="warning")
            return
        if shutil.which("mpv"):
            self._play_with_mpv(video_url)
        else:
            self._play_in_browser(video_url)

    @staticmethod
    def _create_mpv_input_conf() -> str:
        """Create a temporary mpv input.conf that binds common keys to 'quit'."""
        import tempfile
        import string
        keys = list(string.printable)
        keys += ["q", "SPACE", "ENTER", "ESC", "UP", "DOWN", "LEFT", "RIGHT",
                "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9",
                "F10", "F11", "F12"]
        conf_content = "\n".join(f"{key} quit" for key in keys)
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False)
        tmp.write(conf_content)
        tmp.close()
        return tmp.name

    def _play_with_mpv(self, video_url: str) -> None:
        """Suspend TUI, play video, and restore terminal afterwards."""
        self.suspend()
        try:
            # Build a temporary input.conf that quits on any key
            conf_path = self._create_mpv_input_conf()
            # --term-reset: tell mpv to restore the terminal state when done
            subprocess.run(
                [
                    "mpv",
                    "--vo=tct",
                    "--really-quiet",
                    f"--input-conf={conf_path}",
                    video_url
                ],
                check=False,
            )
            # Clean up temp config
            try:
                os.unlink(conf_path)
            except Exception:
                pass
            # print("\033c", end="", flush=True)
        finally:
            # Textual resumes automatically after suspend()
            os.system("clear")


    def _play_in_browser(self, video_url: str) -> None:
        """Open the video URL in the default web browser."""
        webbrowser.open(video_url)
        self.notify("Opening video in browser...")

    # ========== Page Download and Parsing ==========
    
    @work(thread=True)
    def save_page_worker(self, url: str) -> None:
        try:
            if not url.startswith("http"):
                url = "https://" + url
            self.call_from_thread(self.update_status, "Status: Fetching webpage data...")
            resp = requests.get(url, headers=Config.HTTP_HEADERS, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, 'html.parser')
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            real_title = soup.title.string.strip() if soup.title else "Untitled"
            display_title = real_title[:50] + "..." if len(real_title) > 50 else real_title
            safe_title = "".join([c if c.isalnum() else "_" for c in real_title])[:20]
            article_dir = os.path.join(Config.BASE_DIR, f"{timestamp}_{safe_title}")
            os.makedirs(article_dir, exist_ok=True)
            
            extractor = HTMLContentExtractor(url, article_dir)
            extractor.clean_html(soup)
            
            self.call_from_thread(self.update_status, "Status: Extracting and downloading content...")
            page_structure = self._parse_page_content(soup, article_dir, url)
            
            favicon_filename = FaviconDownloader.download_and_save(
                FaviconDownloader.get_favicon_url(soup, url), article_dir
            )
            
            self.call_from_thread(self.update_status, "Status: Saving structural database...")
            structure_file = os.path.join(article_dir, "structure.json")
            with open(structure_file, "w", encoding="utf-8") as f:
                json.dump(page_structure, f, ensure_ascii=False, indent=4)
            
            index_file = os.path.join(article_dir, "index.html")
            with open(index_file, "w", encoding="utf-8") as f:
                f.write(str(soup))
            
            entry = SavedPageEntry(
                path=index_file, title=display_title,
                favicon=favicon_filename, url=url
            )
            self.db.add(entry)
            
            self.call_from_thread(self.refresh_list)
            self.call_from_thread(self.update_status, "Status: Ready")
            self.call_from_thread(self.notify, f"Saved & Parsed: {display_title}")
        except Exception as e:
            self.call_from_thread(self.update_status, "Status: Error!")
            self.call_from_thread(self.notify, str(e), title="Scrape Error", severity="error")

    def _parse_page_content(self, soup: BeautifulSoup, article_dir: str, base_url: str) -> List[dict]:
        extractor = HTMLContentExtractor(base_url, article_dir)
        img_dir = os.path.join(article_dir, "images")
        ordered_elements = []
        processed_tags = set()
        image_tasks = []         # now each task is (task_idx, orig_idx, resource, alt, caption, placeholder_idx, is_inline_svg, is_poster)
        task_idx = 0
        video_poster_tasks = []  # list of (video_element_index, poster_task_idx)

        tags = soup.find_all(extractor.SEARCH_TAGS)
        for orig_idx, tag in enumerate(tags):
            if tag in processed_tags:
                continue
            if tag.name == 'img':
                src = extractor._extract_image_source(tag)
                if not src or src.startswith("data:"):
                    processed_tags.add(tag)
                    continue
                alt_text = tag.get("alt", "").strip()
                img_url = urljoin(base_url, src)
                caption_text = ""
                next_sib = tag.find_next_sibling()
                if next_sib and next_sib.name in extractor.CAPTION_TAGS:
                    caption_text = next_sib.get_text(" ", strip=True)
                    processed_tags.add(next_sib)
                placeholder_index = len(ordered_elements)
                ordered_elements.append({"type": PageElementType.IMAGE_PENDING.value, "task_idx": task_idx})
                image_tasks.append((
                    task_idx, orig_idx, img_url, alt_text, caption_text,
                    placeholder_index, False, False   # is_inline_svg=False, is_poster=False
                ))                
                task_idx += 1
                processed_tags.add(tag)
            elif tag.name == 'svg':
                alt_text = tag.get("aria-label", "").strip() or tag.get("title", "").strip()
                svg_markup = str(tag)
                caption_text = ""
                next_sib = tag.find_next_sibling()
                if next_sib and next_sib.name in extractor.CAPTION_TAGS:
                    caption_text = next_sib.get_text(" ", strip=True)
                    processed_tags.add(next_sib)
                placeholder_index = len(ordered_elements)
                ordered_elements.append({"type": PageElementType.IMAGE_PENDING.value, "task_idx": task_idx})
                image_tasks.append((
                    task_idx, orig_idx, svg_markup, alt_text, caption_text,
                    placeholder_index, True, False    # is_inline_svg=True, is_poster=False
                ))                
                task_idx += 1
                processed_tags.add(tag)
            elif tag.name == 'video':
                video_src = extractor._extract_video_source(tag)
                if not video_src:
                    processed_tags.add(tag)
                    continue
                video_url = urljoin(base_url, video_src)
                poster_url = extractor._extract_video_poster(tag)
                alt_text = tag.get("title", "").strip() or tag.get("aria-label", "").strip()

                video_element = {
                    "type": PageElementType.VIDEO.value,
                    "url": video_url,
                    "poster_local": None,   # will be filled after download
                    "alt": alt_text
                }
                video_idx = len(ordered_elements)
                ordered_elements.append(video_element)

                # If poster exists, create an image task for it
                if poster_url:
                    placeholder_index = video_idx  # not used for posters; we'll update directly
                    image_tasks.append((
                        task_idx, orig_idx, poster_url, "", "", placeholder_index, False, True  # is_poster=True
                    ))
                    video_poster_tasks.append((video_idx, task_idx))
                    task_idx += 1

                processed_tags.add(tag)
            elif tag.name in extractor.CAPTION_TAGS:
                text = tag.get_text(" ", strip=True)
                if text:
                    ordered_elements.append({"type": PageElementType.CAPTION.value, "value": text})
                processed_tags.add(tag)
            elif tag.name in extractor.TEXT_TAGS:
                full_text = extractor._format_text_element(tag)
                if full_text:
                    ordered_elements.append({"type": PageElementType.TEXT.value, "tag_name": tag.name, "value": full_text})
                processed_tags.add(tag)
        if image_tasks:
            downloader = ImageDownloadManager(base_url, img_dir, lambda x: self.call_from_thread(self.update_status, x))
            results = downloader.process_images(image_tasks)

            # Update image placeholders
            for task_idx, result in results.items():
                # Find the corresponding task
                for t_idx, _, _, _, _, placeholder_idx, _, is_poster in image_tasks:
                    if t_idx == task_idx and not is_poster:   # don't replace video elements with poster images
                        ordered_elements[placeholder_idx] = result
                        break

            # Update video poster local paths
            for video_idx, poster_task_idx in video_poster_tasks:
                poster_result = results.get(poster_task_idx)
                if poster_result and poster_result.get("type") == PageElementType.IMAGE.value:
                    # poster_result is an image dict with local_path
                    ordered_elements[video_idx]["poster_local"] = poster_result["local_path"]
                # If poster download failed, poster_local remains None

        return ordered_elements
    
    def action_c_quit(self) -> None:
        os.system("clear" if os.name == "posix" else "cls")
        self.exit()


if __name__ == "__main__":
    WebManager().run()