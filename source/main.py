"""
Terminal Page Saver and Viewer
A TUI application for saving, managing, and reading web pages in the terminal.
"""

import os
import json
import re
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


@dataclass
class SavedPageEntry:
    """Represents a saved page entry in the database."""
    path: str
    title: str
    favicon: Optional[str] = None
    url: Optional[str] = None
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "path": self.path,
            "title": self.title,
            "favicon": self.favicon,
            "url": self.url
        }
    
    @staticmethod
    def from_dict(data: dict) -> "SavedPageEntry":
        """Create from dictionary, handling legacy string format."""
        if isinstance(data, str):
            # Legacy format: just a path string
            folder = os.path.basename(os.path.dirname(data))
            title = folder.split("_", 1)[-1].replace("_", " ") if "_" in folder else folder
            return SavedPageEntry(path=data, title=title, favicon=None)
        return SavedPageEntry(
            path=data["path"],
            title=data.get("title", "Untitled"),
            favicon=data.get("favicon"),
            url=data.get("url")
        )


@dataclass
class ScrollPosition:
    """Manages scroll position as a fraction (0.0 to 1.0) for persistence."""
    index_file: str
    fraction: float
    
    def apply_to_viewer(self, viewer: VerticalScroll) -> bool:
        """
        Apply the stored scroll fraction to a viewer widget.
        
        Returns:
            bool: True if successfully applied, False if viewer not ready yet.
        """
        max_y = viewer.max_scroll_y
        if max_y > 0:
            viewer.scroll_y = int(self.fraction * max_y)
            return True
        return False


# ============================================================================
# DATABASE MANAGEMENT
# ============================================================================

class PageDatabase:
    """Manages persistence of saved pages to JSON database."""
    
    def __init__(self, db_file: str):
        self.db_file = db_file
        self._ensure_db_exists()
    
    def _ensure_db_exists(self) -> None:
        """Create database file if it doesn't exist."""
        os.makedirs(os.path.dirname(self.db_file) or ".", exist_ok=True)
        if not os.path.exists(self.db_file):
            with open(self.db_file, "w") as f:
                json.dump([], f)
    
    def load_all(self) -> List[SavedPageEntry]:
        """Load all saved pages from database."""
        with open(self.db_file, "r") as f:
            data = json.load(f)
        return [SavedPageEntry.from_dict(item) for item in data]
    
    def add(self, entry: SavedPageEntry) -> bool:
        """
        Add a page to the database if not already present.
        
        Returns:
            bool: True if added, False if already exists.
        """
        all_entries = self.load_all()
        if any(item.path == entry.path for item in all_entries):
            return False
        
        all_entries.append(entry)
        self._save_all(all_entries)
        return True
    
    def _save_all(self, entries: List[SavedPageEntry]) -> None:
        """Save all entries to database file."""
        with open(self.db_file, "w") as f:
            json.dump([e.to_dict() for e in entries], f, indent=2)


# ============================================================================
# IMAGE AND CONTENT PROCESSING
# ============================================================================

class ImageProcessor:
    """Handles image downloading, conversion, and scaling."""
    
    @staticmethod
    def scale_for_terminal(pil_img: PILImage.Image) -> PILImage.Image:
        """
        Scale an image to fit within terminal display limits.
        
        Accounts for terminal cell aspect ratio (characters are ~2x as tall as wide).
        """
        orig_w, orig_h = pil_img.size
        term_w = orig_w
        term_h = orig_h / 2.0  # Adjust for terminal cell aspect ratio
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
        """
        Parse an SVG dimension string (e.g., "100px", "10em") to a float.
        Returns None if value is percentage-based or empty.
        """
        if value is None:
            return None
        
        value = value.lower().strip()
        if '%' in value or value == '':
            return None
        
        # Remove known unit suffixes
        for suffix in ['px', 'em', 'ex', 'pt', 'pc', 'in', 'cm', 'mm']:
            if value.endswith(suffix):
                value = value[:-len(suffix)]
                break
        
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    
    @staticmethod
    def convert_svg_to_png(svg_content: bytes, width: int = 100, height: int = 100) -> bytes:
        """Convert SVG bytes to PNG bytes using cairosvg."""
        return cairosvg.svg2png(
            bytestring=svg_content,
            output_width=width,
            output_height=height
        )


class FaviconDownloader:
    """Handles favicon detection and download."""
    
    @staticmethod
    def get_favicon_url(soup: BeautifulSoup, base_url: str) -> str:
        """Extract favicon URL from page, or fall back to /favicon.ico."""
        icon_link = soup.find('link', rel=lambda r: r and 'icon' in r.lower())
        if icon_link and icon_link.get('href'):
            return urljoin(base_url, icon_link['href'])
        return urljoin(base_url, '/favicon.ico')
    
    @staticmethod
    def download_and_save(favicon_url: str, save_dir: str) -> Optional[str]:
        """
        Download favicon and save as PNG.
        
        Returns:
            Optional[str]: Filename (e.g., "favicon.png") if successful, None otherwise.
        """
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
            
            # Convert SVG to PNG if needed
            if is_svg:
                png_data = ImageProcessor.convert_svg_to_png(
                    content, width=64, height=64
                )
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
    """Extracts and structures content from HTML."""
    
    # Tags to remove entirely from parsing
    REMOVE_TAGS = ["script", "style", "iframe"]
    
    # Text tags to parse
    TEXT_TAGS = ["p", "h1", "h2", "h3", "h4"]
    
    # Caption tags
    CAPTION_TAGS = ["caption", "figcaption"]
    
    # All tags to search for
    SEARCH_TAGS = TEXT_TAGS + ["img"] + CAPTION_TAGS + ["svg"]
    
    def __init__(self, base_url: str, save_dir: str):
        self.base_url = base_url
        self.save_dir = save_dir
        self.img_dir = os.path.join(save_dir, "images")
        os.makedirs(self.img_dir, exist_ok=True)
    
    def clean_html(self, soup: BeautifulSoup) -> None:
        """Remove unwanted tags from the soup."""
        for tag in soup(self.REMOVE_TAGS):
            tag.decompose()
        for noscript_tag in soup("noscript"):
            noscript_tag.unwrap()
    
    def _extract_image_source(self, img_tag) -> Optional[str]:
        """Extract the best available image source from an img tag."""
        # Try standard and lazy-loading attributes
        src = (
            img_tag.get("src") or
            img_tag.get("data-src") or
            img_tag.get("data-original") or
            img_tag.get("data-lazy-src")
        )
        
        # Fall back to srcset if src not found
        if not src and img_tag.get("srcset"):
            src = img_tag.get("srcset").split(",")[0].strip().split(" ")[0]
        
        return src
    
    def _format_text_element(self, tag) -> str:
        """
        Convert a text tag's children to formatted text.
        Handles nested tags like <a>, <b>, <i>, etc.
        """
        parts = []
        for child in tag.children:
            if child.name is None:
                # Text node
                if child.string:
                    parts.append(str(child.string))
            elif child.name == 'a':
                # Convert links to Textual markup with click handler
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
        # Normalize whitespace
        full_text = re.sub(r'\s+', ' ', full_text).strip()
        return full_text


# ============================================================================
# IMAGE DOWNLOAD WORKER
# ============================================================================

class ImageDownloadManager:
    """Manages parallel downloading and processing of images."""
    
    def __init__(self, base_url: str, img_dir: str, status_callback):
        self.base_url = base_url
        self.img_dir = img_dir
        self.status_callback = status_callback
        self.thread_local = threading.local()
    
    def _get_session(self) -> requests.Session:
        """Get or create a thread-local HTTP session."""
        if not hasattr(self.thread_local, "session"):
            session = requests.Session()
            session.headers.update(Config.HTTP_HEADERS)
            session.headers["Referer"] = self.base_url
            self.thread_local.session = session
        return self.thread_local.session
    
    def _process_remote_image(self, img_url: str, alt_text: str, caption: str) -> dict:
        """Download and process a remote image."""
        session = self._get_session()
        
        try:
            resp = session.get(img_url, timeout=Config.REQUEST_TIMEOUT)
            
            # Handle rate limiting
            if resp.status_code == 429:
                return {
                    "type": PageElementType.IMAGE_FAILED.value,
                    "alt": alt_text,
                    "caption": caption,
                    "error": "HTTP 429 (Rate Limited)",
                    "error_type": "RateLimitError",
                    "url": img_url,
                    "status_code": 429
                }
            
            # Handle other HTTP errors
            if resp.status_code != 200:
                return {
                    "type": PageElementType.IMAGE_FAILED.value,
                    "alt": alt_text,
                    "caption": caption,
                    "error": f"HTTP {resp.status_code}",
                    "error_type": "HTTPError",
                    "url": img_url,
                    "status_code": resp.status_code
                }
            
            content = resp.content
            content_type = resp.headers.get('Content-Type', '')
            is_svg = 'svg' in content_type or img_url.lower().endswith('.svg')
            
            return self._save_image_content(
                content, img_url, alt_text, caption, is_svg
            )
            
        except Exception as e:
            return {
                "type": PageElementType.IMAGE_FAILED.value,
                "alt": alt_text,
                "caption": caption,
                "error": str(e),
                "error_type": type(e).__name__,
                "url": img_url
            }
    
    def _process_inline_svg(self, svg_markup: str, alt_text: str, caption: str) -> dict:
        """Process an inline SVG element."""
        try:
            svg_soup = BeautifulSoup(svg_markup, 'html.parser')
            svg_elem = svg_soup.find('svg')
            if not svg_elem:
                raise ValueError("No <svg> element found")
            
            # Ensure white color for visibility on dark terminal
            existing_style = str(svg_elem.get('style', ''))
            if 'color:' not in existing_style.lower():
                new_style = (existing_style + '; color: white;').strip('; ')
                svg_elem['style'] = new_style
            
            # Extract or compute SVG dimensions
            output_w, output_h = self._compute_svg_dimensions(svg_elem)
            
            # Remove old dimension attributes
            for attr in ['width', 'height']:
                if attr in svg_elem.attrs:
                    del svg_elem[attr]
            
            # Set new dimensions
            svg_elem['width'] = str(output_w)
            svg_elem['height'] = str(output_h)
            
            # Ensure viewBox is set
            if not svg_elem.get('viewBox') and not svg_elem.get('viewbox'):
                svg_elem['viewBox'] = f"0 0 {output_w} {output_h}"
            
            # Convert to PNG
            png_data = ImageProcessor.convert_svg_to_png(
                str(svg_elem).encode('utf-8'),
                width=output_w,
                height=output_h
            )
            
            pil_img = PILImage.open(BytesIO(png_data))
            pil_img = ImageProcessor.scale_for_terminal(pil_img)
            
            png_name = "inline_svg.png"
            png_path = os.path.join(self.img_dir, png_name)
            pil_img.save(png_path)
            
            return {
                "type": PageElementType.IMAGE.value,
                "local_path": os.path.join("images", png_name),
                "abs_path": os.path.abspath(png_path),
                "alt": alt_text,
                "caption": caption
            }
            
        except Exception as e:
            return {
                "type": PageElementType.IMAGE_FAILED.value,
                "alt": alt_text,
                "caption": caption,
                "error": f"SVG conversion failed: {str(e)}",
                "error_type": "SVGConversionError",
                "url": ""
            }
    
    def _compute_svg_dimensions(self, svg_elem) -> Tuple[int, int]:
        """Extract or compute SVG output dimensions."""
        w_abs = ImageProcessor.parse_svg_dimension(svg_elem.get('width'))
        h_abs = ImageProcessor.parse_svg_dimension(svg_elem.get('height'))
        
        output_w = output_h = None
        
        # Try absolute dimensions first
        if w_abs and h_abs:
            output_w, output_h = int(w_abs), int(h_abs)
        else:
            # Try viewBox
            viewbox = svg_elem.get('viewbox') or svg_elem.get('viewBox')
            if viewbox:
                parts = viewbox.split()
                if len(parts) >= 4:
                    output_w = int(float(parts[2]))
                    output_h = int(float(parts[3]))
        
        # Compute from aspect ratio if still missing
        if output_w is None or output_h is None:
            viewbox = svg_elem.get('viewbox') or svg_elem.get('viewBox')
            if viewbox:
                parts = viewbox.split()
                if len(parts) >= 4:
                    aspect = float(parts[2]) / float(parts[3])
                else:
                    aspect = 1.0
            elif w_abs and h_abs:
                aspect = w_abs / h_abs
            else:
                aspect = 1.0
            
            output_h = 100
            output_w = int(output_h * aspect)
        
        return output_w, output_h
    
    def _save_image_content(
        self,
        content: bytes,
        source_url: str,
        alt_text: str,
        caption: str,
        is_svg: bool
    ) -> dict:
        """Save downloaded image content to disk."""
        try:
            base_name = f"img_{os.path.basename(source_url.split('?')[0])}"
            base_name = re.sub(r'[\\/*?:"<>|]', "", base_name)
            
            if is_svg:
                # Convert SVG to PNG
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
                    "alt": alt_text,
                    "caption": caption
                }
            else:
                # Save as-is, then optimize
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
                    "alt": alt_text,
                    "caption": caption
                }
                
        except Exception as e:
            return {
                "type": PageElementType.IMAGE_FAILED.value,
                "alt": alt_text,
                "caption": caption,
                "error": str(e),
                "error_type": type(e).__name__,
                "url": source_url
            }
    
    def process_images(
        self,
        image_tasks: List[Tuple]
    ) -> Dict[int, dict]:
        """
        Download and process images in parallel.
        
        Args:
            image_tasks: List of tuples (task_idx, orig_idx, resource, alt, caption, is_inline_svg)
        
        Returns:
            Dict mapping task_idx to processed image results.
        """
        total = len(image_tasks)
        if total == 0:
            return {}
        
        results = {}
        
        def process_task(task_idx, orig_idx, resource, alt, caption, is_inline_svg):
            if is_inline_svg:
                return task_idx, self._process_inline_svg(resource, alt, caption)
            else:
                return task_idx, self._process_remote_image(resource, alt, caption)
        
        self.status_callback(
            f"Status: Downloading {total} images (max {Config.MAX_DOWNLOAD_WORKERS} at once)..."
        )
        
        with ThreadPoolExecutor(max_workers=Config.MAX_DOWNLOAD_WORKERS) as executor:
            futures = {
                executor.submit(process_task, t_idx, o_idx, res, alt, cap, is_inline): t_idx
                for t_idx, o_idx, res, alt, cap, is_inline in image_tasks
            }
            
            completed = 0
            for future in as_completed(futures):
                task_idx, result = future.result()
                results[task_idx] = result
                completed += 1
                self.status_callback(
                    f"Status: Downloaded {completed}/{total} images..."
                )
        
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
        padding: 1 2; 
        background: #121212; 
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
    """
    
    class PageTitleIcon(Horizontal):
        """Display component: favicon + title + domain for list items."""
        def __init__(self, favicon_path: Optional[str], title: str, domain: str = ""):
            super().__init__(classes="list-item-row")
            self.favicon_path = favicon_path
            self.title = title
            self.domain = domain

        def compose(self) -> ComposeResult:
            # Render favicon if available
            if self.favicon_path and os.path.exists(self.favicon_path):
                icon = Image(self.favicon_path)
                icon.styles.width = 6
                icon.styles.height = 3
                icon.add_class("list-favicon")
                yield icon
            
            # Text block for title and domain
            yield Vertical(classes="list-text-block")

        def on_mount(self):
            """Mount title and domain labels after layout."""
            text_block = self.query_one(".list-text-block", Vertical)
            title_label = Label(self.title, classes="list-title")
            text_block.mount(title_label)
            if self.domain:
                domain_label = Label(self.domain, classes="list-domain")
                text_block.mount(domain_label)

    def __init__(self):
        super().__init__()
        # Initialize database and state
        self.db = PageDatabase(Config.DB_FILE)
        self.files_cache: List[SavedPageEntry] = []
        self._refresh_counter = 0
        
        # Scroll position management
        self._scroll_positions: Dict[str, ScrollPosition] = {}
        self._current_index_file: Optional[str] = None
        self._pending_scroll_position: Optional[ScrollPosition] = None

    # ========== UI Layout ==========
    
    def compose(self) -> ComposeResult:
        """Compose the main UI layout."""
        yield Header(show_clock=True)
        yield Horizontal(
            Input(placeholder="Enter URL to save...", id="url-input"),
            Button("Save Page", id="save-btn"),
            Label("Status: Ready", id="status-label"), 
            id="input-container"
        )
        with Horizontal(id="workspace"):
            yield ListView(id="page-list")
            with VerticalScroll(id="viewer-container"):
                yield Label("Select a page from the left to read inside the terminal...")
        yield Footer()

    def on_mount(self) -> None:
        """Initialize app after mounting."""
        self.refresh_list()
        
        # Watch for content changes and restore scroll position
        viewer = self.query_one("#viewer-container", VerticalScroll)
        viewer.watch(self, "virtual_size", self._on_viewer_layout_changed)

    def _on_viewer_layout_changed(self, old_size, new_size):
        """
        Called when viewer content/layout changes.
        Attempts to restore saved scroll position.
        """
        self._restore_scroll_position()

    # ========== Status Management ==========
    
    def update_status(self, text: str) -> None:
        """Update the status label."""
        self.query_one("#status-label", Label).update(text)

    def action_open_link(self, link: str) -> None:
        """Open a link in the system browser."""
        self.notify(f"Opening browser: {link}")
        webbrowser.open(link)

    # ========== Database and List Management ==========
    
    def refresh_list(self) -> None:
        """Refresh the page list from database."""
        list_view = self.query_one("#page-list", ListView)
        list_view.clear()
        
        # Load from database (reversed to show newest first)
        self.files_cache = list(reversed(self.db.load_all()))
        self._refresh_counter += 1

        # Populate list with page entries
        for i, entry in enumerate(self.files_cache):
            favicon_path = None
            if entry.favicon:
                favicon_path = os.path.join(
                    os.path.dirname(entry.path),
                    entry.favicon
                )
            
            domain = ""
            if entry.url:
                parsed = urlparse(entry.url)
                domain = parsed.netloc.replace("www.", "")
            
            row = self.PageTitleIcon(favicon_path, entry.title, domain)
            list_item = ListItem(row, id=f"item_{i}_{self._refresh_counter}")
            list_view.append(list_item)

    # ========== User Interactions ==========
    
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle URL submission via Enter key."""
        self.start_download(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle Save button press."""
        url = self.query_one("#url-input", Input).value
        self.start_download(url)

    def start_download(self, url: str) -> None:
        """Start downloading a page."""
        if not url:
            return
        self.query_one("#url-input", Input).value = ""
        self.save_page_worker(url)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle page selection from list."""
        viewer = self.query_one("#viewer-container", VerticalScroll)

        # Save current scroll position before switching
        if self._current_index_file is not None:
            self._save_current_scroll_position(viewer)

        # Parse list item ID to get index
        parts = event.item.id.split("_")
        index = int(parts[1])
        
        if index >= len(self.files_cache):
            self.notify("Invalid page selection", severity="error")
            return
        
        entry = self.files_cache[index]
        index_file = entry.path
        structure_file = os.path.join(
            os.path.dirname(index_file),
            "structure.json"
        )

        # Validate structure file exists
        if not os.path.exists(structure_file):
            self.notify("Structure file not found!", severity="error")
            self._current_index_file = None
            return

        # Load and render new page
        self._current_index_file = index_file
        self.render_page_content(structure_file)
        self.title = f"Reading TUI: {entry.title}"
        
        # Prepare scroll position restoration
        if index_file in self._scroll_positions:
            self._pending_scroll_position = self._scroll_positions[index_file]
        else:
            self._pending_scroll_position = None
        
        # Attempt immediate restoration
        self._restore_scroll_position()

    def _save_current_scroll_position(self, viewer: VerticalScroll) -> None:
        """Save the current scroll position."""
        if self._current_index_file is None:
            return
        
        max_y = viewer.max_scroll_y
        fraction = viewer.scroll_y / max_y if max_y > 0 else 0.0
        
        self._scroll_positions[self._current_index_file] = ScrollPosition(
            index_file=self._current_index_file,
            fraction=fraction
        )

    def _restore_scroll_position(self) -> None:
        """
        Restore saved scroll position if available.
        Only applies if viewer layout is ready (max_scroll_y > 0).
        """
        if self._pending_scroll_position is None:
            return
        
        viewer = self.query_one("#viewer-container", VerticalScroll)
        
        if self._pending_scroll_position.apply_to_viewer(viewer):
            # Successfully applied
            self._pending_scroll_position = None

    # ========== Content Rendering ==========
    
    def render_page_content(self, json_path: str) -> None:
        """
        Render saved page structure to the viewer.
        
        Reads structure.json and mounts appropriate widgets for each element.
        """
        viewer = self.query_one("#viewer-container", VerticalScroll)
        
        # Clear previous content
        self.query("#viewer-container > *").remove()

        # Load structure from file
        with open(json_path, "r", encoding="utf-8") as f:
            elements = json.load(f)

        # Skip favicon element if present
        if elements and elements[0].get("type") == PageElementType.FAVICON.value:
            elements = elements[1:]

        # Render each element
        for item in elements:
            self._render_element(viewer, item)

    def _render_element(self, viewer: VerticalScroll, item: dict) -> None:
        """Render a single content element to the viewer."""
        elem_type = item.get("type")
        
        if elem_type == PageElementType.TEXT.value:
            self._render_text_element(viewer, item)
        elif elem_type == PageElementType.CAPTION.value:
            self._render_caption(viewer, item)
        elif elem_type in [PageElementType.IMAGE.value, PageElementType.IMAGE_FAILED.value]:
            self._render_image_element(viewer, item)

    def _render_text_element(self, viewer: VerticalScroll, item: dict) -> None:
        """Render a text element (heading or paragraph)."""
        tag_name = item.get("tag_name", "p")
        value = item.get("value", "")
        
        if tag_name == "h1":
            viewer.mount(Label(
                f" {value.upper()} ",
                classes="reader-h1",
                markup=True
            ))
        elif tag_name in ["h2", "h3", "h4"]:
            viewer.mount(Label(value, classes="reader-h2", markup=True))
        else:
            viewer.mount(Label(value, classes="reader-p", markup=True))

    def _render_caption(self, viewer: VerticalScroll, item: dict) -> None:
        """Render a caption element."""
        viewer.mount(Label(
            f"📝 {item.get('value', '')}",
            classes="reader-caption",
            markup=True
        ))

    def _render_image_element(self, viewer: VerticalScroll, item: dict) -> None:
        """Render an image or failed image element."""
        wrapper = Container(classes="image-wrapper")
        viewer.mount(wrapper)
        
        # Render image if file exists
        if item.get("type") == PageElementType.IMAGE.value:
            abs_path = item.get("abs_path")
            if abs_path and os.path.exists(abs_path):
                try:
                    self._render_image_widget(wrapper, abs_path)
                except Exception:
                    # Fallback: render with default size
                    self._render_image_widget(wrapper, abs_path, fallback=True)
            else:
                wrapper.mount(Label(
                    "🖼️ [Local Image File Missing]",
                    classes="reader-caption"
                ))
        else:
            # Image failed to download
            wrapper.mount(Label(
                "🖼️ [Image Failed to Load]",
                classes="reader-caption"
            ))
        
        # Render alt text and caption
        if item.get("alt"):
            viewer.mount(Label(
                f"[b]Alt Text:[/b] {item['alt']}",
                classes="reader-caption",
                markup=True
            ))
        if item.get("caption"):
            viewer.mount(Label(
                f"[b]Caption:[/b] {item['caption']}",
                classes="reader-caption",
                markup=True
            ))

    def _render_image_widget(
        self,
        container: Container,
        img_path: str,
        fallback: bool = False
    ) -> None:
        """Mount an image widget with appropriate sizing."""
        img_widget = Image(img_path)
        img_widget.add_class("reader-image")
        
        if not fallback:
            # Calculate optimal size
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
            # Use default fallback size
            img_widget.styles.width = 45
            img_widget.styles.height = 16
        
        container.mount(img_widget)

    # ========== Page Download and Parsing ==========
    
    @work(thread=True)
    def save_page_worker(self, url: str) -> None:
        """
        Worker thread: Download, parse, and save a web page.
        """
        try:
            # Ensure URL has a protocol
            if not url.startswith("http"):
                url = "https://" + url
            
            self.call_from_thread(self.update_status, "Status: Fetching webpage data...")
            
            # Fetch page
            resp = requests.get(url, headers=Config.HTTP_HEADERS, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, 'html.parser')
            
            # Generate page directory and metadata
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            real_title = soup.title.string.strip() if soup.title else "Untitled"
            display_title = (
                real_title[:50] + "..." if len(real_title) > 50 else real_title
            )
            safe_title = "".join(
                [c if c.isalnum() else "_" for c in real_title]
            )[:20]
            article_dir = os.path.join(Config.BASE_DIR, f"{timestamp}_{safe_title}")
            os.makedirs(article_dir, exist_ok=True)
            
            # Clean HTML
            extractor = HTMLContentExtractor(url, article_dir)
            extractor.clean_html(soup)
            
            # Parse content and download images
            self.call_from_thread(
                self.update_status,
                "Status: Extracting and downloading content..."
            )
            page_structure = self._parse_page_content(soup, article_dir, url)
            
            # Download favicon
            favicon_filename = FaviconDownloader.download_and_save(
                FaviconDownloader.get_favicon_url(soup, url),
                article_dir
            )
            
            # Save structure to JSON
            self.call_from_thread(
                self.update_status,
                "Status: Saving structural database..."
            )
            structure_file = os.path.join(article_dir, "structure.json")
            with open(structure_file, "w", encoding="utf-8") as f:
                json.dump(page_structure, f, ensure_ascii=False, indent=4)
            
            # Save original HTML
            index_file = os.path.join(article_dir, "index.html")
            with open(index_file, "w", encoding="utf-8") as f:
                f.write(str(soup))
            
            # Save to database
            entry = SavedPageEntry(
                path=index_file,
                title=display_title,
                favicon=favicon_filename,
                url=url
            )
            self.db.add(entry)
            
            # Update UI
            self.call_from_thread(self.refresh_list)
            self.call_from_thread(self.update_status, "Status: Ready")
            self.call_from_thread(
                self.notify,
                f"Saved & Parsed: {display_title}"
            )
            
        except Exception as e:
            self.call_from_thread(self.update_status, "Status: Error!")
            self.call_from_thread(
                self.notify,
                str(e),
                title="Scrape Error",
                severity="error"
            )

    def _parse_page_content(
        self,
        soup: BeautifulSoup,
        article_dir: str,
        base_url: str
    ) -> List[dict]:
        """
        Parse page content and download images.
        
        Returns:
            List of structured page elements.
        """
        extractor = HTMLContentExtractor(base_url, article_dir)
        img_dir = os.path.join(article_dir, "images")
        
        ordered_elements = []
        processed_tags = set()
        image_tasks = []
        task_idx = 0
        
        # Find all relevant tags
        tags = soup.find_all(extractor.SEARCH_TAGS)
        
        for orig_idx, tag in enumerate(tags):
            if tag in processed_tags:
                continue
            
            if tag.name == 'img':
                # Process image tag
                src = extractor._extract_image_source(tag)
                if not src or src.startswith("data:"):
                    processed_tags.add(tag)
                    continue
                
                alt_text = tag.get("alt", "").strip()
                img_url = urljoin(base_url, src)
                caption_text = ""
                
                # Try to find associated caption
                next_sib = tag.find_next_sibling()
                if next_sib and next_sib.name in extractor.CAPTION_TAGS:
                    caption_text = next_sib.get_text(" ", strip=True)
                    processed_tags.add(next_sib)
                
                # Add placeholder and queue download
                placeholder_index = len(ordered_elements)
                ordered_elements.append({
                    "type": PageElementType.IMAGE_PENDING.value,
                    "task_idx": task_idx
                })
                image_tasks.append((
                    task_idx, orig_idx, img_url, alt_text,
                    caption_text, placeholder_index, False
                ))
                task_idx += 1
                processed_tags.add(tag)
                
            elif tag.name == 'svg':
                # Process inline SVG
                alt_text = (
                    tag.get("aria-label", "").strip() or
                    tag.get("title", "").strip()
                )
                svg_markup = str(tag)
                caption_text = ""
                
                # Try to find associated caption
                next_sib = tag.find_next_sibling()
                if next_sib and next_sib.name in extractor.CAPTION_TAGS:
                    caption_text = next_sib.get_text(" ", strip=True)
                    processed_tags.add(next_sib)
                
                # Add placeholder and queue processing
                placeholder_index = len(ordered_elements)
                ordered_elements.append({
                    "type": PageElementType.IMAGE_PENDING.value,
                    "task_idx": task_idx
                })
                image_tasks.append((
                    task_idx, orig_idx, svg_markup, alt_text,
                    caption_text, placeholder_index, True
                ))
                task_idx += 1
                processed_tags.add(tag)
                
            elif tag.name in extractor.CAPTION_TAGS:
                # Standalone caption
                text = tag.get_text(" ", strip=True)
                if text:
                    ordered_elements.append({
                        "type": PageElementType.CAPTION.value,
                        "value": text
                    })
                processed_tags.add(tag)
                
            elif tag.name in extractor.TEXT_TAGS:
                # Text element (paragraph or heading)
                full_text = extractor._format_text_element(tag)
                if full_text:
                    ordered_elements.append({
                        "type": PageElementType.TEXT.value,
                        "tag_name": tag.name,
                        "value": full_text
                    })
                processed_tags.add(tag)
        
        # Download images if any
        if image_tasks:
            downloader = ImageDownloadManager(
                base_url,
                img_dir,
                self.call_from_thread(self.update_status)
            )
            results = downloader.process_images(image_tasks)
            
            # Replace placeholders with actual results
            for task_idx, result in results.items():
                for task_t_idx, _, _, _, _, placeholder_idx, _ in image_tasks:
                    if task_t_idx == task_idx:
                        ordered_elements[placeholder_idx] = result
                        break
        
        return ordered_elements


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    WebManager().run()
