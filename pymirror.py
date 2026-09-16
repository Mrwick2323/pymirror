#!/usr/bin/env python3
"""
site_mirror.py — a simple single-file website mirroring tool.

Given a starting URL, this script downloads the page and every asset it
references (CSS, JS, images, fonts, etc.), rewrites links so the mirror
works offline, and saves everything to disk preserving the site's
directory structure (similar to `wget --mirror` / "mirrify").

Usage:
    python3 site_mirror.py https://example.com/ -o ./mirror
    python3 site_mirror.py https://example.com/some/page.html --depth 1

Notes:
- By default this only follows *asset* references (css, js, img, font,
  etc.) needed to render the page(s) you give it, not every link on the
  site. Use --depth to also crawl same-domain <a href> links N levels deep.
- Only same-domain (or explicitly allowed) hosts are downloaded, so a
  page linking to a hundred external sites won't pull the whole internet.
- Respect the target site's terms of service and robots.txt when mirroring.
"""

import requests
import argparse
import os
import re
import sys
import time
import urllib.parse as urlparse
from collections import deque
import posixpath
import urllib.request
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from functools import partial
from bs4 import BeautifulSoup
DEFAULT_UA = (
        "Mozilla/5.0 (compatible; SiteMirror/1.0; "
        "+https://example.com/site-mirror-bot)"
    )

    # Tags/attributes that reference an external resource we should fetch.
ASSET_ATTRS = [
        ("img", "src"),
        ("img", "data-src"),
        ("script", "src"),
        ("link", "href"),          # stylesheets, icons, manifest, preload
        ("source", "src"),
        ("source", "srcset"),
        ("img", "srcset"),
        ("video", "src"),
        ("video", "poster"),
        ("audio", "src"),
        ("embed", "src"),
        ("iframe", "src"),
        ("object", "data"),
        ("track", "src"),
    ]

CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.IGNORECASE)
CSS_IMPORT_RE = re.compile(r"""@import\s+(?:url\()?['"]?([^'")]+)['"]?\)?""", re.IGNORECASE)
def site_mirror():
    def normalize_url(url):
        """Strip fragment; leave query string intact."""
        parts = urlparse.urlsplit(url)
        return urlparse.urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


    def url_to_local_path(url, out_dir):
        """
        Map a URL to a local filesystem path that preserves the site's
        directory structure, e.g.:
            https://example.com/css/main.css      -> out_dir/example.com/css/main.css
            https://example.com/                  -> out_dir/example.com/index.html
            https://example.com/about             -> out_dir/example.com/about/index.html
                (no extension + no trailing slash treated as a directory route)
            https://example.com/api/data?x=1      -> out_dir/example.com/api/data?x=1... (sanitized)
        """
        parts = urlparse.urlsplit(url)
        host = parts.netloc
        path = parts.path

        if path == "" or path == "/":
            path = "/index.html"
        elif path.endswith("/"):
            path = path + "index.html"
        else:
            # If the last segment has no file extension, treat it as a
            # "pretty URL" route and store it as .../segment/index.html
            last_segment = path.rsplit("/", 1)[-1]
            if "." not in last_segment:
                path = path + "/index.html"

        # Fold query string into the filename so distinct queries don't collide.
        if parts.query:
            safe_query = re.sub(r"[^A-Za-z0-9_\-=.]", "_", parts.query)
            root, ext = os.path.splitext(path)
            path = f"{root}__{safe_query}{ext or '.html'}"

        # Sanitize each path segment (defensively) and strip leading slash.
        segments = [seg for seg in path.split("/") if seg not in ("", ".", "..")]
        local_path = os.path.join(out_dir, host, *segments)
        return local_path


    def rel_path(from_file, to_file):
        """Relative path from one saved file to another, for link rewriting."""
        from_dir = os.path.dirname(from_file)
        rel = os.path.relpath(to_file, start=from_dir)
        return rel.replace(os.sep, "/")


    class SiteMirror:
        def __init__(self, out_dir, allowed_hosts=None, delay=0.0,
                    timeout=15, user_agent=DEFAULT_UA, max_assets=2000,
                    verbose=True):
            self.out_dir = out_dir
            self.allowed_hosts = allowed_hosts  # set of hosts, or None = same-host-as-seed
            self.delay = delay
            self.timeout = timeout
            self.max_assets = max_assets
            self.verbose = verbose

            self.session = requests.Session()
            self.session.headers.update({"User-Agent": user_agent})

            self.downloaded = {}   # normalized url -> local file path
            self.failed = set()

        def log(self, msg):
            if self.verbose:
                print(msg)

        def host_allowed(self, url):
            host = urlparse.urlsplit(url).netloc
            return host in self.allowed_hosts

        def fetch(self, url):
            """GET a URL, return (content_bytes, content_type) or (None, None)."""
            try:
                resp = self.session.get(url, timeout=self.timeout)
                resp.raise_for_status()
                return resp.content, resp.headers.get("Content-Type", "")
            except requests.RequestException as e:
                self.log(f"  ! failed: {url} ({e})")
                self.failed.add(url)
                return None, None

        def save_asset(self, url):
            """
            Download a non-HTML asset (image/css/js/font/etc.) verbatim and
            save it to its mapped local path. CSS files get their internal
            url()/@import references recursively resolved too.
            Returns the local file path, or None on failure.
            """
            norm = normalize_url(url)
            if norm in self.downloaded:
                return self.downloaded[norm]
            if norm in self.failed:
                return None
            if len(self.downloaded) >= self.max_assets:
                return None
            if not self.host_allowed(norm):
                return None

            content, ctype = self.fetch(url)
            if content is None:
                return None

            local_path = url_to_local_path(norm, self.out_dir)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            is_css = "css" in (ctype or "") or url.lower().split("?")[0].endswith(".css")
            if is_css:
                text = content.decode("utf-8", errors="replace")
                text = self.process_css(text, base_url=norm, local_path=local_path)
                with open(local_path, "w", encoding="utf-8") as f:
                    f.write(text)
            else:
                with open(local_path, "wb") as f:
                    f.write(content)

            self.downloaded[norm] = local_path
            self.log(f"  + saved asset: {norm} -> {os.path.relpath(local_path, self.out_dir)}")

            if self.delay:
                time.sleep(self.delay)
            return local_path

        def process_css(self, css_text, base_url, local_path):
            """Find url(...) and @import references in CSS, download them,
            and rewrite the CSS to point at local copies."""

            def replace_url(match):
                quote = match.group(1)
                ref = match.group(2)
                return self._rewrite_css_ref(ref, quote, base_url, local_path, f"url({{0}})")

            def replace_import(match):
                ref = match.group(1)
                return self._rewrite_css_ref(ref, "", base_url, local_path, "@import url({0})")

            css_text = CSS_URL_RE.sub(replace_url, css_text)
            css_text = CSS_IMPORT_RE.sub(replace_import, css_text)
            return css_text

        def _rewrite_css_ref(self, ref, quote, base_url, local_path, template):
            ref_stripped = ref.strip()
            if ref_stripped.startswith("data:") or ref_stripped.startswith("#"):
                return template.format(f"{quote}{ref}{quote}" if quote else ref)
            absolute = urlparse.urljoin(base_url, ref_stripped)
            saved_path = self.save_asset(absolute)
            if saved_path:
                new_ref = rel_path(local_path, saved_path)
            else:
                new_ref = ref_stripped
            return template.format(f"{quote}{new_ref}{quote}" if quote else new_ref)

        def mirror_page(self, url, depth=0, visited_pages=None):
            """
            Download an HTML page, download every asset it references,
            rewrite links to point at local copies, and (if depth > 0)
            recurse into same-host <a href> links.
            """
            if visited_pages is None:
                visited_pages = set()

            norm = normalize_url(url)
            if norm in visited_pages:
                return
            visited_pages.add(norm)

            if not self.host_allowed(norm):
                self.log(f"  (skipping out-of-scope page: {norm})")
                return

            self.log(f"Fetching page: {norm}")
            content, ctype = self.fetch(url)
            if content is None:
                return

            if "html" not in (ctype or "") and not norm.lower().endswith((".html", ".htm", "/")):
                # Not actually HTML (e.g. a PDF linked with no extension) -- save as asset.
                self.save_asset(url)
                return

            local_path = url_to_local_path(norm, self.out_dir)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            self.downloaded[norm] = local_path

            html = content.decode("utf-8", errors="replace")
            soup = BeautifulSoup(html, "html.parser")

            # --- download & rewrite standard asset attributes ---
            for tag_name, attr in ASSET_ATTRS:
                for tag in soup.find_all(tag_name):
                    if not tag.has_attr(attr):
                        continue
                    if attr == "srcset":
                        tag[attr] = self._rewrite_srcset(tag[attr], norm, local_path)
                    else:
                        raw = tag[attr].strip()
                        if not raw or raw.startswith("data:") or raw.startswith("javascript:"):
                            continue
                        absolute = urlparse.urljoin(norm, raw)
                        saved_path = self.save_asset(absolute)
                        if saved_path:
                            tag[attr] = rel_path(local_path, saved_path)

            # --- inline <style> blocks: resolve url()s the same way as CSS files ---
            for style_tag in soup.find_all("style"):
                if style_tag.string:
                    style_tag.string.replace_with(
                        self.process_css(style_tag.string, base_url=norm, local_path=local_path)
                    )

            # --- inline style="" attributes ---
            for tag in soup.find_all(style=True):
                tag["style"] = self.process_css(tag["style"], base_url=norm, local_path=local_path)

            # --- <a href> links: rewrite to local copies if we mirror them, ---
            #     or leave/optionally crawl them if depth allows.
            page_links = []
            for tag in soup.find_all("a", href=True):
                raw = tag["href"].strip()
                if not raw or raw.startswith(("mailto:", "tel:", "javascript:", "#")):
                    continue
                absolute = urlparse.urljoin(norm, raw)
                if self.host_allowed(absolute):
                    page_links.append(absolute)
                    target_local = url_to_local_path(normalize_url(absolute), self.out_dir)
                    tag["href"] = rel_path(local_path, target_local)

            with open(local_path, "w", encoding="utf-8") as f:
                f.write(str(soup))
            self.log(f"  + saved page: {norm} -> {os.path.relpath(local_path, self.out_dir)}")

            if self.delay:
                time.sleep(self.delay)

            if depth > 0:
                for link in page_links:
                    self.mirror_page(link, depth=depth - 1, visited_pages=visited_pages)

        def _rewrite_srcset(self, srcset_value, base_url, local_path):
            """srcset="a.jpg 1x, b.jpg 2x" -> download each, rewrite paths."""
            parts = []
            for candidate in srcset_value.split(","):
                candidate = candidate.strip()
                if not candidate:
                    continue
                bits = candidate.split()
                url_part = bits[0]
                descriptor = " ".join(bits[1:])
                absolute = urlparse.urljoin(base_url, url_part)
                saved_path = self.save_asset(absolute)
                new_url = rel_path(local_path, saved_path) if saved_path else url_part
                parts.append(f"{new_url} {descriptor}".strip())
            return ", ".join(parts)


    def main():
        parser = argparse.ArgumentParser(
            description="Mirror a webpage and all its referenced assets to disk, "
                        "preserving the site's directory structure."
        )
        parser.add_argument("-d", "--domain", required=True,
                            help="Starting URL or domain to mirror (e.g. https://example.com/)")
        parser.add_argument("-f", "--folder", required=True,
                            help="Output directory")
        parser.add_argument("--depth", type=int, default=0,
                            help="Also crawl same-host <a href> links this many levels deep "
                                "(default: 0 = only mirror the given page + its assets)")
        parser.add_argument("--allow-host", action="append", default=[],
                            help="Additional host(s) to allow downloading from "
                                "(repeatable). By default only the seed URL's host is allowed.")
        parser.add_argument("--delay", type=float, default=0.0,
                            help="Seconds to wait between requests (be polite to servers)")
        parser.add_argument("--timeout", type=float, default=15,
                            help="Per-request timeout in seconds")
        parser.add_argument("--max-assets", type=int, default=2000,
                            help="Safety cap on number of assets downloaded")
        parser.add_argument("--user-agent", default=DEFAULT_UA)
        parser.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")
        parser.add_argument("-p", "--port", type=int, default=8000,
                         help="Local port to serve on (default: 8000)")
        args = parser.parse_args()

        url = args.domain
        if not urlparse.urlsplit(url).scheme:
            url = "https://" + url

        seed_host = urlparse.urlsplit(url).netloc
        allowed_hosts = {seed_host} | set(args.allow_host)

        mirror = SiteMirror(
            out_dir=args.folder,
            allowed_hosts=allowed_hosts,
            delay=args.delay,
            timeout=args.timeout,
            user_agent=args.user_agent,
            max_assets=args.max_assets,
            verbose=not args.quiet,
        )

        mirror.mirror_page(url, depth=args.depth)

        print(f"\nDone. {len(mirror.downloaded)} file(s) saved to: "
            f"{os.path.abspath(args.folder)}")
        if mirror.failed:
            print(f"{len(mirror.failed)} URL(s) failed to download.")
    main()
    
def mirror_server():
    def safe_local_path(root, url_path):
        """
        Turn a URL path into a safe local filesystem path under root,
        preventing directory traversal (../../etc).
        """
        # Strip query string / fragment, unquote percent-encoding
        path = urllib.parse.urlsplit(url_path).path
        path = urllib.parse.unquote(path)

        # Normalize using posixpath semantics (URL paths are always /-separated)
        path = posixpath.normpath(path)
        # normpath("/") -> "/", normpath("/../x") -> "/x" (leading slash kept)
        parts = [p for p in path.split("/") if p and p != "."]

        # Drop any leftover traversal components defensively
        parts = [p for p in parts if p != ".."]

        return os.path.join(root, *parts) if parts else root


    def fetch_remote(domain, url_path, dest_path, timeout=15):
        """
        Try to download domain + url_path into dest_path.
        Returns True on success, False if the remote also has nothing (404/error).
        """
        remote_url = urllib.parse.urljoin(domain, url_path.lstrip("/"))
        try:
            req = urllib.request.Request(
                remote_url,
                headers={"User-Agent": "mirror-server/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            print(f"  remote {e.code} for {remote_url}")
            return False
        except urllib.error.URLError as e:
            print(f"  remote fetch failed for {remote_url}: {e.reason}")
            return False
        except Exception as e:
            print(f"  remote fetch error for {remote_url}: {e}")
            return False

        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
        print(f"  downloaded {remote_url} -> {dest_path} ({len(data)} bytes)")
        return True


    class MirrorHandler(BaseHTTPRequestHandler):
        # These are set via functools.partial when the server is created
        root_dir = None
        domain = None

        def _resolve_and_maybe_fetch(self):
            """
            Returns a local filesystem path to serve, downloading it from
            the remote domain first if it's missing locally.
            """
            local_path = safe_local_path(self.root_dir, self.path)

            # If it's a directory, look for an index.html inside it (basic case)
            candidate = local_path
            if os.path.isdir(candidate):
                index_candidate = os.path.join(candidate, "index.html")
                if os.path.exists(index_candidate):
                    candidate = index_candidate

            if os.path.exists(candidate) and not os.path.isdir(candidate):
                return candidate

            # Not found locally -> try to pull it from the remote domain
            print(f"404 locally: {self.path} -- attempting remote fetch")
            if fetch_remote(self.domain, self.path, local_path):
                return local_path

            return None

        def do_GET(self):
            local_path = self._resolve_and_maybe_fetch()

            if local_path is None:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"404 Not Found: {self.path}".encode("utf-8"))
                return

            try:
                with open(local_path, "rb") as f:
                    data = f.read()
            except OSError as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f"Error reading file: {e}".encode("utf-8"))
                return

            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(local_path))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def guess_type(self, path):
            import mimetypes
            mime, _ = mimetypes.guess_type(path)
            return mime or "application/octet-stream"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


    def main():
        parser = argparse.ArgumentParser(
            description="Mirror a webpage and all its referenced assets to disk, "
                        "preserving the site's directory structure."
        )
        parser.add_argument("-d", "--domain", required=True,
                            help="Starting URL or domain to mirror (e.g. https://example.com/)")
        parser.add_argument("-f", "--folder", required=True,
                            help="Output directory (default: ./mirror)")
        parser.add_argument("--depth", type=int, default=0,
                            help="Also crawl same-host <a href> links this many levels deep "
                                "(default: 0 = only mirror the given page + its assets)")
        parser.add_argument("--allow-host", action="append", default=[],
                            help="Additional host(s) to allow downloading from "
                                "(repeatable). By default only the seed URL's host is allowed.")
        parser.add_argument("--delay", type=float, default=0.0,
                            help="Seconds to wait between requests (be polite to servers)")
        parser.add_argument("--timeout", type=float, default=15,
                            help="Per-request timeout in seconds")
        parser.add_argument("--max-assets", type=int, default=2000,
                            help="Safety cap on number of assets downloaded")
        parser.add_argument("--user-agent", default=DEFAULT_UA)
        parser.add_argument("-p", "--port", type=int, default=8000,
                            help="Local port to serve on (default: 8000)")
        parser.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")
        args = parser.parse_args()
        # 1. Safely parse the domain, regardless of http/https protocol
        tomain=args.domain
        if args.domain[-10:]=='index.html':
            tomain=args.domain[:-10]
        if args.domain[-11:]=='index.html/':
            tomain=args.domain[:-11]  
        furl = urlparse.urlparse(tomain)
        furl = furl.netloc + furl.path
        
        # 2. Use os.path.join to handle the trailing slashes automatically
        root_dir = os.path.abspath(os.path.join(args.folder, furl))
        os.makedirs(root_dir, exist_ok=True)

        domain = args.domain
        if not domain.endswith("/"):
            domain += "/"

        handler_cls = partial(MirrorHandler)
        # Bind class attributes since BaseHTTPRequestHandler subclasses are
        # instantiated per-request by the server.
        MirrorHandler.root_dir = root_dir
        MirrorHandler.domain = domain

        server = ThreadingHTTPServer(("localhost", args.port), MirrorHandler)
        print(f"Serving '{root_dir}' on http://localhost:{args.port}/")
        print(f"Missing files will be pulled from: {domain}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down.")
            server.shutdown()
    main()
def main():
    site_mirror()
    mirror_server()
if __name__ == "__main__":
    sys.exit(main())
