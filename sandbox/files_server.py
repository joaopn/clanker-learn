#!/usr/bin/env python3
"""The files server of a sandbox: the workspace, read-only, over HTTP.

It is how a picture or an HTML file the tutor wrote reaches the browser: as a web app of the
sandbox, through the interface's per-origin proxy. So the web server never reads a workspace
file itself, and a link planted in one project's workspace cannot reach another project's
files, which are not in this container at all.

It serves files, not folders; answers GET and HEAD only; and refuses a path whose real
location is outside the workspace (a link that points out of it, a path with `..`). It runs as
the sandbox user.
"""
import mimetypes
import os
import shutil
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = "/workspace"
PORT = 7070


def resolve(root, url_path):
    """The file a request path names, or None: no such file, a folder, or a real location
    outside `root`."""
    raw = urllib.parse.unquote(urllib.parse.urlsplit(url_path).path)
    if "\0" in raw:
        return None
    real_root = os.path.realpath(root)
    candidate = os.path.realpath(os.path.join(real_root, raw.lstrip("/")))
    if not candidate.startswith(real_root + os.sep):
        return None
    return candidate if os.path.isfile(candidate) else None


class Files(BaseHTTPRequestHandler):
    root = ROOT

    def _answer(self, body_wanted):
        path = resolve(self.root, self.path)
        if path is None:
            self.send_error(404)
            return
        try:
            with open(path, "rb") as fh:
                kind = mimetypes.guess_type(path)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(os.fstat(fh.fileno()).st_size))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if body_wanted:
                    shutil.copyfileobj(fh, self.wfile)
        except (PermissionError, FileNotFoundError, IsADirectoryError):
            self.send_error(404)

    def do_GET(self):
        self._answer(True)

    def do_HEAD(self):
        self._answer(False)

    def log_message(self, *args):
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    ThreadingHTTPServer(("0.0.0.0", port), Files).serve_forever()


if __name__ == "__main__":
    main()
