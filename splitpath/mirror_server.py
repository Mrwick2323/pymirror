#!/usr/bin/env python3
"""
mirror_server.py

Serves a local directory over HTTP. Whenever a request would 404 against
the local directory, it fetches the same path from a remote domain,
saves it into the local directory, and then serves it as normal.

Usage:
    python3 mirror_server.py -d https://example.com/ -f /path/of/folder/to/host [-p 8000]

Then open http://localhost:8000/ and browse around. Any path that isn't
found locally gets pulled from the remote domain and cached to disk.
"""

import argparse
import os
import sys
import posixpath
import urllib.request
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from functools import partial


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
        description="Serve a local folder and auto-download 404s from a remote domain."
    )
    parser.add_argument("-d", "--domain", required=True,
                         help="Base remote domain to fetch missing files from, e.g. https://example.com/")
    parser.add_argument("-f", "--folder", required=True,
                         help="Local folder to serve and save downloaded files into")
    parser.add_argument("-p", "--port", type=int, default=8000,
                         help="Local port to serve on (default: 8000)")
    args = parser.parse_args()

    root_dir = os.path.abspath(args.folder)
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


if __name__ == "__main__":
    main()
