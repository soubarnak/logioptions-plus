"""A local HTTP server that imitates the parts of github.com the update check uses.

`/releases/latest` redirects to the newest stable tag (never followed by the
agent); `/releases.atom` lists every release including pre-releases."""

import http.server
import threading

PREFIX = "optionsplus-linux-v"


class FakeReleaseServer:
    def __init__(self):
        self.stable_tag = None          # e.g. "optionsplus-linux-v0.1.0"; None -> 404
        self.atom_tags = []             # newest first, pre-releases included
        self.requests = []              # (method, path, headers dict)
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self, body_allowed):
                outer.requests.append((self.command, self.path, dict(self.headers)))
                if self.path == "/releases/latest":
                    if outer.stable_tag is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    self.send_response(302)
                    self.send_header("Location", "%s/releases/tag/%s" % (outer.base, outer.stable_tag))
                    self.end_headers()
                elif self.path == "/releases.atom":
                    body = outer.atom().encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/atom+xml")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if body_allowed:
                        self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_HEAD(self):
                self._serve(False)

            def do_GET(self):
                self._serve(True)

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self._httpd.server_address[1]
        self.base_url = self.base + "/releases"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def atom(self):
        entries = "".join(
            '<entry><id>tag:github.com,2008:Repository/1/%s</id>'
            '<link rel="alternate" type="text/html" href="%s/releases/tag/%s"/>'
            '<title>%s</title></entry>' % (t, self.base, t, t) for t in self.atom_tags)
        return ('<?xml version="1.0" encoding="UTF-8"?>'
                '<feed xmlns="http://www.w3.org/2005/Atom"><title>Releases</title>%s</feed>' % entries)

    def publish(self, version, prerelease=False):
        tag = PREFIX + version
        self.atom_tags.insert(0, tag)
        if not prerelease:
            self.stable_tag = tag

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
