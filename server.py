#!/usr/bin/env python3
# server.py — a small "web proxy" website.
# The school sees only YOUR domain on HTTPS. Your server does the fetch.
# Install: pip install flask requests beautifulsoup4 lxml
# Run:     python server.py   ->  http://0.0.0.0:8080

import re
from urllib.parse import urljoin, quote, unquote

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, Response

app = Flask(__name__)

# -----------------------------------------------------------------------------
# config
# -----------------------------------------------------------------------------
UPSTREAM_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
PROXY_PREFIX = "/p/"
TIMEOUT = 15
MAX_BYTES = 8 * 1024 * 1024  # 8 MB cap — refuse giant binaries

session = requests.Session()
session.trust_env = False  # don't loop through a host-level HTTP_PROXY
session.headers.update({
    "User-Agent": UPSTREAM_UA,
    "Accept-Language": "en-US,en;q=0.9",
})

# injected into every HTML page — routes fetch/XHR through the proxy
HOOK_JS = """
<script>
(function () {
  var P = %r;
  function enc(u) { return P + encodeURIComponent(u); }
  function looksHttp(u) { return /^https?:\\/\\//i.test(u); }
  var _fetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      if (typeof input === "string" && looksHttp(input)) input = enc(input);
      else if (input && input.url && looksHttp(input.url))
        input = new Request(enc(input.url), input);
    } catch (e) {}
    return _fetch.call(this, input, init);
  };
  var _open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u) {
    try { if (looksHttp(u)) u = enc(u); } catch (e) {}
    return _open.apply(this, [m, u].concat([].slice.call(arguments, 2)));
  };
})();
</script>
""" % PROXY_PREFIX

CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)(.*?)\1\s*\)""", re.I)
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "content-encoding", "content-length",
}

# -----------------------------------------------------------------------------
# url + css rewriting
# -----------------------------------------------------------------------------
def enc(url: str) -> str:
    return quote(url, safe="")


def rewrite_attr(base: str, value: str) -> str:
    if not value:
        return value
    v = value.strip()
    if v.startswith(("data:", "javascript:", "mailto:", "tel:", "blob:", "#")):
        return value
    absolute = urljoin(base, v)
    if absolute.startswith(("http://", "https://")):
        return PROXY_PREFIX + enc(absolute)
    return value


def rewrite_css(css: str, base: str) -> str:
    return CSS_URL_RE.sub(
        lambda m: "url(%s)" % rewrite_attr(base, m.group(2)),
        css,
    )


def rewrite_html(body: str, base: str) -> str:
    soup = BeautifulSoup(body, "html.parser")

    for tag, attr in [
        ("a", "href"), ("link", "href"), ("area", "href"),
        ("script", "src"), ("img", "src"), ("iframe", "src"),
        ("frame", "src"), ("video", "src"), ("audio", "src"),
        ("source", "src"), ("track", "src"), ("embed", "src"),
        ("input", "src"), ("form", "action"),
    ]:
        for el in soup.find_all(tag):
            if el.has_attr(attr):
                el[attr] = rewrite_attr(base, el[attr])

    # srcset
    for el in soup.find_all(attrs={"srcset": True}):
        parts = []
        for chunk in el["srcset"].split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            bits = chunk.split()
            bits[0] = rewrite_attr(base, bits[0])
            parts.append(" ".join(bits))
        el["srcset"] = ", ".join(parts)

    # inline style + <style> blocks
    for el in soup.find_all(style=True):
        el["style"] = rewrite_css(el["style"], base)
    for el in soup.find_all("style"):
        if el.string:
            el.string = rewrite_css(el.string, base)

    # SRI + crossorigin can't survive rewriting
    for el in soup.find_all(attrs={"integrity": True}):
        del el["integrity"]
    for el in soup.find_all(attrs={"crossorigin": True}):
        del el["crossorigin"]

    html = str(soup)
    if "</head>" in html:
        html = html.replace("</head>", HOOK_JS + "</head>", 1)
    elif "<body" in html:
        html = html.replace("<body", HOOK_JS + "<body", 1)
    else:
        html = HOOK_JS + html
    return html

# -----------------------------------------------------------------------------
# fetching
# -----------------------------------------------------------------------------
def forward(url: str) -> Response:
    method = request.method

    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in HOP_HEADERS
        and k.lower() not in ("host", "referer", "origin", "cookie")
    }

    # if the browser sent a Referer that was itself proxied, un-proxy it upstream
    ref = request.headers.get("Referer", "")
    if ref.startswith(PROXY_PREFIX):
        fwd_headers["Referer"] = unquote(ref[len(PROXY_PREFIX):])

    data = None
    if method in ("POST", "PUT", "PATCH"):
        data = request.get_data()
        fwd_headers["Content-Type"] = request.headers.get(
            "Content-Type", "application/x-www-form-urlencoded"
        )

    try:
        upstream = session.request(
            method, url, headers=fwd_headers, data=data,
            timeout=TIMEOUT, allow_redirects=True, stream=True,
        )
    except requests.RequestException as e:
        return Response(f"Upstream error: {e}", status=502, mimetype="text/plain")

    ctype = upstream.headers.get("Content-Type", "").lower()

    out_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in HOP_HEADERS
        and k.lower() not in (
            "content-security-policy",
            "content-security-policy-report-only",
            "x-frame-options",
            "strict-transport-security",
        )
    }

    loc = upstream.headers.get("Location")
    if loc:
        out_headers["Location"] = rewrite_attr(url, loc)

    if "text/html" in ctype:
        raw = upstream.raw.read(MAX_BYTES, decode_content=True).decode(
            upstream.encoding or "utf-8", "replace"
        )
        return Response(
            rewrite_html(raw, upstream.url),
            status=upstream.status_code,
            headers=out_headers,
            mimetype="text/html",
        )

    if "text/css" in ctype:
        raw = upstream.raw.read(MAX_BYTES, decode_content=True).decode(
            upstream.encoding or "utf-8", "replace"
        )
        return Response(
            rewrite_css(raw, upstream.url),
            status=upstream.status_code,
            headers=out_headers,
            mimetype="text/css",
        )

    body = upstream.raw.read(MAX_BYTES, decode_content=True)
    return Response(
        body,
        status=upstream.status_code,
        headers=out_headers,
        mimetype=ctype or None,
    )

# -----------------------------------------------------------------------------
# routes
# -----------------------------------------------------------------------------
LAUNCHER = """<!doctype html>
<title>proxy</title>
<body style="font:16px system-ui;max-width:640px;margin:4rem auto;padding:0 1rem;">
  <h1 style="margin:0 0 1rem;">proxy</h1>
  <form onsubmit="location.href='/p/'+encodeURIComponent(this.u.value);return false;">
    <input name="u" placeholder="https://example.com"
           style="width:100%;padding:10px;font-size:16px;box-sizing:border-box;">
  </form>
</body>
"""


@app.route("/")
def index():
    return Response(LAUNCHER, mimetype="text/html")


@app.route("/p/<path:encoded>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def proxy(encoded):
    target = unquote(encoded)
    if not target.startswith(("http://", "https://")):
        return Response("bad url", status=400)
    return forward(target)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)