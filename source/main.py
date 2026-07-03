import os
import json
import re
import threading
import requests
import webbrowser
from datetime import datetime
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from bs4 import BeautifulSoup
from PIL import Image as PILImage
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, ListView, ListItem, Label, Input, Button
from textual.containers import Horizontal, Vertical, VerticalScroll, Container
from textual_image.widget import Image
from textual import work
import cairosvg
from urllib.parse import urlparse

# ---------- constants ----------
DATA_DIR = "browser_data"
BASE_DIR = os.path.join(DATA_DIR, "saved_pages")
DB_FILE = os.path.join(DATA_DIR, "db.json")

HEADERS = {
    "User-Agent": "TerminalPageSaver/1.0 (https://github.com/mhrohani1385/terminal-page-saver; m.h.rohani1385@gmail.com)"
}
MAX_CELL_WIDTH = 55
MAX_CELL_HEIGHT = 22
# -------------------------------

os.makedirs(BASE_DIR, exist_ok=True)
if not os.path.exists(DB_FILE):
    with open(DB_FILE, "w") as f:
        json.dump([], f)


class WebManager(App):
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

    /* Left panel list item row: favicon + title */
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
        """A horizontal row with favicon, title, and domain, vertically centred."""
        def __init__(self, favicon_path: str | None, title: str, domain: str = ""):
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
            title_label = Label(self.title, classes="list-title")
            text_block.mount(title_label)
            if self.domain:
                domain_label = Label(self.domain, classes="list-domain")
                text_block.mount(domain_label)

    def __init__(self):
        super().__init__()
        self.files_cache = []
        self._refresh_counter = 0
        self._scroll_fractions = {}          # key = index_file, value = float 0..1
        self._current_index_file = None      # currently displayed page's index.html
        self._pending_scroll_fraction = None # (index_file, fraction) to apply when layout ready

    # ---------- UI layout ----------
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
            with VerticalScroll(id="viewer-container"):
                yield Label("Select a page from the left to read inside the terminal...")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_list()
        # Watch the viewer's virtual size to re-apply scroll fraction when content changes
        viewer = self.query_one("#viewer-container", VerticalScroll)
        viewer.watch(self, "virtual_size", self._on_viewer_virtual_size_changed)

    def _on_viewer_virtual_size_changed(self, old_size, new_size):
        self._restore_scroll_fraction()

    def update_status(self, text: str) -> None:
        self.query_one("#status-label", Label).update(text)

    def action_open_link(self, link: str) -> None:
        self.notify(f"Opening browser: {link}")
        webbrowser.open(link)

    # ---------- Database helpers ----------
    def get_db(self):
        with open(DB_FILE, "r") as f:
            data = json.load(f)
        fixed = []
        for item in data:
            if isinstance(item, str):
                folder = os.path.basename(os.path.dirname(item))
                title = folder.split("_", 1)[-1].replace("_", " ") if "_" in folder else folder
                fixed.append({"path": item, "title": title, "favicon": None})
            else:
                fixed.append(item)
        return fixed

    def save_to_db(self, entry: dict):
        db = self.get_db()
        if not any(item['path'] == entry['path'] for item in db):
            db.append(entry)
            with open(DB_FILE, "w") as f:
                json.dump(db, f, indent=2)

    def refresh_list(self):
        list_view = self.query_one("#page-list", ListView)
        list_view.clear()
        db = self.get_db()
        self.files_cache = list(reversed(db))
        self._refresh_counter += 1

        for i, entry in enumerate(self.files_cache):
            favicon_path = None
            if entry.get("favicon"):
                favicon_path = os.path.join(os.path.dirname(entry["path"]), entry["favicon"])
            
            title_text = entry.get("title", os.path.basename(os.path.dirname(entry["path"])))
            domain = ""
            if entry.get("url"):
                parsed = urlparse(entry["url"])
                domain = parsed.netloc.replace("www.", "")
            
            row = self.PageTitleIcon(favicon_path, title_text, domain)
            list_item = ListItem(row, id=f"item_{i}_{self._refresh_counter}")
            list_view.append(list_item)

    # ---------- User interactions ----------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.start_download(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        url = self.query_one("#url-input", Input).value
        self.start_download(url)

    def start_download(self, url):
        if not url: return
        self.query_one("#url-input", Input).value = ""
        self.save_page_worker(url)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        viewer = self.query_one("#viewer-container", VerticalScroll)

        # ----- Save current scroll fraction -----
        if self._current_index_file is not None:
            max_y = viewer.max_scroll_y
            fraction = viewer.scroll_y / max_y if max_y > 0 else 0.0
            self._scroll_fractions[self._current_index_file] = fraction

        # ----- Load new page -----
        parts = event.item.id.split("_")
        index = int(parts[1])
        entry = self.files_cache[index]
        index_file = entry["path"]
        structure_file = os.path.join(os.path.dirname(index_file), "structure.json")

        if not os.path.exists(structure_file):
            self.notify("Structure file not found!", severity="error")
            self._current_index_file = None
            return

        self._current_index_file = index_file
        self.render_page_content(structure_file)

        page_title = entry.get("title", os.path.basename(os.path.dirname(index_file)))
        self.title = f"Reading TUI: {page_title}"

        # ----- Prepare pending scroll fraction -----
        if index_file in self._scroll_fractions:
            saved_fraction = self._scroll_fractions[index_file]
            self._pending_scroll_fraction = (index_file, saved_fraction)
        else:
            self._pending_scroll_fraction = None

        # Attempt to restore immediately (if layout already done)
        self._restore_scroll_fraction()

    def _restore_scroll_fraction(self):
        """Apply pending scroll fraction if the viewer has a valid max_scroll_y."""
        if self._pending_scroll_fraction is None:
            return
        idx_file, fraction = self._pending_scroll_fraction
        viewer = self.query_one("#viewer-container", VerticalScroll)
        max_y = viewer.max_scroll_y
        if max_y > 0:
            viewer.scroll_y = int(fraction * max_y)
            self._pending_scroll_fraction = None   # done

    # ---------- Page rendering (unchanged) ----------
    def render_page_content(self, json_path):
        viewer = self.query_one("#viewer-container", VerticalScroll)
        self.query("#viewer-container > *").remove()

        with open(json_path, "r", encoding="utf-8") as f:
            elements = json.load(f)

        if elements and elements[0].get("type") == "favicon":
            elements = elements[1:]

        for item in elements:
            if item["type"] == "text":
                if item["tag_name"] == "h1":
                    viewer.mount(Label(f" {item['value'].upper()} ", classes="reader-h1", markup=True))
                elif item["tag_name"] in ["h2", "h3", "h4"]:
                    viewer.mount(Label(item["value"], classes="reader-h2", markup=True))
                else:
                    viewer.mount(Label(item["value"], classes="reader-p", markup=True))
            elif item["type"] == "caption":
                viewer.mount(Label(f"📝 {item['value']}", classes="reader-caption", markup=True))
            elif item["type"] in ["image", "image_failed"]:
                if item["type"] == "image" and os.path.exists(item["abs_path"]):
                    wrapper = Container(classes="image-wrapper")
                    viewer.mount(wrapper)
                    try:
                        with PILImage.open(item["abs_path"]) as pil_img:
                            orig_w, orig_h = pil_img.size
                        term_w = orig_w
                        term_h = orig_h / 2
                        scale = min(MAX_CELL_WIDTH / term_w, MAX_CELL_HEIGHT / term_h, 1.0)
                        final_w = max(1, int(term_w * scale))
                        final_h = max(1, int(term_h * scale))
                        img_widget = Image(item["abs_path"])
                        img_widget.add_class("reader-image")
                        img_widget.styles.width = final_w
                        img_widget.styles.height = final_h
                        wrapper.mount(img_widget)
                    except Exception:
                        try:
                            img_widget = Image(item["abs_path"])
                            img_widget.add_class("reader-image")
                            img_widget.styles.width = 45
                            img_widget.styles.height = 16
                            wrapper.mount(img_widget)
                        except:
                            viewer.mount(Label("🖼️ [Image Layout Exception]", classes="reader-caption"))
                else:
                    viewer.mount(Label("🖼️ [Local Image File Missing or Failed]", classes="reader-caption"))
                if item.get("alt"):
                    viewer.mount(Label(f"[b]Alt Text:[/b] {item['alt']}", classes="reader-caption", markup=True))
                if item.get("caption"):
                    viewer.mount(Label(f"[b]Caption:[/b] {item['caption']}", classes="reader-caption", markup=True))

    # ---------- Image scaling helper ----------
    @staticmethod
    def _scale_to_terminal_limits(pil_img):
        orig_w, orig_h = pil_img.size
        term_w = orig_w
        term_h = orig_h / 2.0
        scale = min(MAX_CELL_WIDTH / term_w, MAX_CELL_HEIGHT / term_h, 1.0)
        final_cell_w = max(1, int(term_w * scale))
        final_cell_h = max(1, int(term_h * scale))
        return pil_img.resize((final_cell_w, final_cell_h * 2), PILImage.LANCZOS)

    # ---------- Favicon download helper ----------
    def _get_favicon(self, soup, base_url, article_dir):
        icon_link = soup.find('link', rel=lambda r: r and 'icon' in r.lower())
        if icon_link and icon_link.get('href'):
            favicon_url = urljoin(base_url, icon_link['href'])
        else:
            favicon_url = urljoin(base_url, '/favicon.ico')
        try:
            resp = requests.get(favicon_url, headers=HEADERS, timeout=5)
            if resp.status_code != 200:
                return None
            content = resp.content
            content_type = resp.headers.get('Content-Type', '')
            is_svg = 'svg' in content_type or favicon_url.lower().endswith('.svg')
            if is_svg:
                png_data = cairosvg.svg2png(bytestring=content, output_width=64, output_height=64)
                img = PILImage.open(BytesIO(png_data))
            else:
                img = PILImage.open(BytesIO(content))
            img = img.convert('RGBA')
            img.thumbnail((64, 64), PILImage.LANCZOS)
            favicon_path = os.path.join(article_dir, 'favicon.png')
            img.save(favicon_path, 'PNG')
            return 'favicon.png'
        except Exception:
            return None

    # ---------- Main HTML parser (unchanged) ----------
    def parse_and_save_ordered_content(self, soup, page_dir, base_url, use_stream=True):
        ordered_elements = []
        img_dir = os.path.join(page_dir, "images")
        os.makedirs(img_dir, exist_ok=True)

        tags = soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'img', 'caption', 'figcaption', 'svg'])
        processed_tags = set()

        image_tasks = []
        task_idx = 0

        for orig_idx, tag in enumerate(tags):
            if tag in processed_tags:
                continue

            if tag.name == 'img':
                src = tag.get("src") or tag.get("data-src") or tag.get("data-original") or tag.get("data-lazy-src")
                if not src and tag.get("srcset"):
                    src = tag.get("srcset").split(",")[0].strip().split(" ")[0]
                alt_text = tag.get("alt", "").strip()
                if not src or src.startswith("data:"):
                    processed_tags.add(tag)
                    continue
                img_url = urljoin(base_url, src)
                caption_text = ""
                next_sib = tag.find_next_sibling()
                if next_sib and next_sib.name in ['caption', 'figcaption']:
                    caption_text = next_sib.get_text(" ", strip=True)
                    processed_tags.add(next_sib)

                placeholder = {"type": "image_pending", "task_idx": task_idx, "alt": alt_text, "caption": caption_text}
                placeholder_index = len(ordered_elements)
                ordered_elements.append(placeholder)
                image_tasks.append((task_idx, orig_idx, img_url, alt_text, caption_text, placeholder_index, False))
                task_idx += 1
                processed_tags.add(tag)

            elif tag.name == 'svg':
                alt_text = tag.get("aria-label", "").strip() or tag.get("title", "").strip()
                svg_markup = str(tag)
                caption_text = ""
                next_sib = tag.find_next_sibling()
                if next_sib and next_sib.name in ['caption', 'figcaption']:
                    caption_text = next_sib.get_text(" ", strip=True)
                    processed_tags.add(next_sib)

                placeholder = {"type": "image_pending", "task_idx": task_idx, "alt": alt_text, "caption": caption_text}
                placeholder_index = len(ordered_elements)
                ordered_elements.append(placeholder)
                image_tasks.append((task_idx, orig_idx, svg_markup, alt_text, caption_text, placeholder_index, True))
                task_idx += 1
                processed_tags.add(tag)

            elif tag.name in ['caption', 'figcaption']:
                text = tag.get_text(" ", strip=True)
                if text:
                    ordered_elements.append({"type": "caption", "value": text})
                processed_tags.add(tag)

            elif tag.name in ['p', 'h1', 'h2', 'h3', 'h4']:
                parts = []
                for child in tag.children:
                    if child.name is None:
                        parts.append(child.string)
                    elif child.name == 'a':
                        link_text = child.get_text()
                        href = child.get("href")
                        if href:
                            full_url = urljoin(base_url, href)
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
                full_text = re.sub(r'\s+', ' ', full_text).strip()
                if full_text:
                    ordered_elements.append({"type": "text", "tag_name": tag.name, "value": full_text})
                processed_tags.add(tag)

        total_imgs = len(image_tasks)
        if total_imgs == 0:
            return ordered_elements

        thread_local = threading.local()

        def get_session():
            if not hasattr(thread_local, "session"):
                s = requests.Session()
                s.headers.update(HEADERS)
                s.headers["Referer"] = base_url
                thread_local.session = s
            return thread_local.session

        def download_one(task_idx, orig_idx, resource, alt, caption, is_inline_svg):
            if is_inline_svg:
                try:
                    svg_soup = BeautifulSoup(resource, 'html.parser')
                    svg_elem = svg_soup.find('svg')
                    if not svg_elem:
                        raise ValueError("No <svg> element")

                    existing_style = str(svg_elem.get('style', ''))
                    if 'color:' not in existing_style.lower():
                        new_style = (existing_style + '; color: white;').strip('; ')
                        svg_elem['style'] = new_style

                    def parse_dim(val):
                        if val is None:
                            return None
                        val = val.lower().strip()
                        if '%' in val or val == '':
                            return None
                        for suf in ['px', 'em', 'ex', 'pt', 'pc', 'in', 'cm', 'mm']:
                            if val.endswith(suf):
                                val = val[:-len(suf)]
                                break
                        try:
                            return float(val)
                        except:
                            return None

                    w_abs = parse_dim(svg_elem.get('width'))
                    h_abs = parse_dim(svg_elem.get('height'))
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
                            if viewbox:
                                parts = viewbox.split()
                                vw, vh = float(parts[2]), float(parts[3])
                                aspect = vw / vh
                            elif w_abs and h_abs:
                                aspect = w_abs / h_abs
                            else:
                                aspect = 1.0
                            output_h = 100
                            output_w = int(output_h * aspect)

                    for attr in ['width', 'height']:
                        if attr in svg_elem.attrs:
                            del svg_elem[attr]
                    svg_elem['width'] = str(output_w)
                    svg_elem['height'] = str(output_h)
                    if not svg_elem.get('viewBox') and not svg_elem.get('viewbox'):
                        svg_elem['viewBox'] = f"0 0 {output_w} {output_h}"

                    png_data = cairosvg.svg2png(
                        bytestring=str(svg_elem).encode('utf-8'),
                        output_width=output_w,
                        output_height=output_h
                    )
                    pil_img = PILImage.open(BytesIO(png_data))
                    pil_img = self._scale_to_terminal_limits(pil_img)

                    png_name = f"img_{orig_idx}_inline.svg.png"
                    png_path = os.path.join(img_dir, png_name)
                    pil_img.save(png_path)

                    return task_idx, {
                        "type": "image",
                        "local_path": os.path.join("images", png_name),
                        "abs_path": os.path.abspath(png_path),
                        "alt": alt, "caption": caption
                    }
                except Exception as e:
                    return task_idx, {
                        "type": "image_failed",
                        "alt": alt, "caption": caption,
                        "error": f"SVG conversion failed: {str(e)}",
                        "error_type": "SVGConversionError",
                        "url": ""
                    }
            else:
                session = get_session()
                try:
                    resp = session.get(resource, timeout=10)
                    if resp.status_code == 429:
                        return task_idx, {
                            "type": "image_failed", "alt": alt, "caption": caption,
                            "error": "HTTP 429", "error_type": "HTTPError",
                            "url": resource, "status_code": 429
                        }
                    if resp.status_code != 200:
                        return task_idx, {
                            "type": "image_failed", "alt": alt, "caption": caption,
                            "error": f"HTTP {resp.status_code}", "error_type": "HTTPError",
                            "url": resource, "status_code": resp.status_code
                        }

                    content = resp.content
                    content_type = resp.headers.get('Content-Type', '')
                    is_svg = 'svg' in content_type or resource.lower().endswith('.svg')

                    base_name = f"img_{orig_idx}_{os.path.basename(resource.split('?')[0])}"
                    base_name = re.sub(r'[\\/*?:"<>|]', "", base_name)

                    if is_svg:
                        png_data = cairosvg.svg2png(bytestring=content)
                        pil_img = PILImage.open(BytesIO(png_data))
                        pil_img = self._scale_to_terminal_limits(pil_img)
                        png_name = os.path.splitext(base_name)[0] + ".png"
                        png_path = os.path.join(img_dir, png_name)
                        pil_img.save(png_path)
                        return task_idx, {
                            "type": "image",
                            "local_path": os.path.join("images", png_name),
                            "abs_path": os.path.abspath(png_path),
                            "alt": alt, "caption": caption
                        }
                    else:
                        img_path = os.path.join(img_dir, base_name)
                        with open(img_path, "wb") as f:
                            f.write(content)
                        with PILImage.open(img_path) as pil_img:
                            pil_img = self._scale_to_terminal_limits(pil_img)
                        pil_img.save(img_path)
                        return task_idx, {
                            "type": "image",
                            "local_path": os.path.join("images", base_name),
                            "abs_path": os.path.abspath(img_path),
                            "alt": alt, "caption": caption
                        }
                except Exception as e:
                    return task_idx, {
                        "type": "image_failed", "alt": alt, "caption": caption,
                        "error": str(e), "error_type": type(e).__name__,
                        "url": resource
                    }

        MAX_WORKERS = 3
        idx_to_placeholder_pos = {t_idx: pos for t_idx, _, _, _, _, pos, _ in image_tasks}

        self.call_from_thread(self.update_status,
                              f"Status: Downloading {total_imgs} images (max {MAX_WORKERS} at once)...")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_task_idx = {}
            for t_idx, o_idx, res, alt, cap, pos, is_inline in image_tasks:
                future = executor.submit(download_one, t_idx, o_idx, res, alt, cap, is_inline)
                future_to_task_idx[future] = t_idx

            completed = 0
            for future in as_completed(future_to_task_idx):
                t_idx, result = future.result()
                pos = idx_to_placeholder_pos[t_idx]
                ordered_elements[pos] = result
                completed += 1
                self.call_from_thread(self.update_status,
                                      f"Status: Downloaded {completed}/{total_imgs} images...")

        return ordered_elements

    # ---------- Page download worker ----------
    @work(thread=True)
    def save_page_worker(self, url):
        try:
            if not url.startswith("http"):
                url = "https://" + url
            self.call_from_thread(self.update_status, "Status: Fetching webpage data...")
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, 'html.parser')

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            real_title = soup.title.string.strip() if soup.title else "Untitled"
            display_title = real_title[:50] + "..." if len(real_title) > 50 else real_title
            safe_title = "".join([c if c.isalnum() else "_" for c in real_title])[:20]
            article_dir = os.path.join(BASE_DIR, f"{timestamp}_{safe_title}")
            os.makedirs(article_dir, exist_ok=True)

            for tag in soup(["script", "style", "iframe"]):
                tag.decompose()
            for noscript_tag in soup("noscript"):
                noscript_tag.unwrap()

            page_structure = self.parse_and_save_ordered_content(soup, article_dir, url)

            favicon_rel = self._get_favicon(soup, url, article_dir)

            self.call_from_thread(self.update_status, "Status: Saving structural database...")
            structure_file = os.path.join(article_dir, "structure.json")
            with open(structure_file, "w", encoding="utf-8") as f:
                json.dump(page_structure, f, ensure_ascii=False, indent=4)

            index_file = os.path.join(article_dir, "index.html")
            with open(index_file, "w", encoding="utf-8") as f:
                f.write(str(soup))

            entry = {
                "path": index_file,
                "title": display_title,
                "favicon": favicon_rel,
                "url": url
            }
            self.save_to_db(entry)
            self.call_from_thread(self.refresh_list)
            self.call_from_thread(self.update_status, "Status: Ready")
            self.call_from_thread(self.notify, f"Saved & Parsed: {display_title}")
        except Exception as e:
            self.call_from_thread(self.update_status, "Status: Error!")
            self.call_from_thread(self.notify, str(e), title="Scrape Error", severity="error")


if __name__ == "__main__":
    WebManager().run()
