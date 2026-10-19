#!/usr/bin/env python3
import re
import time
import json
from urllib.parse import urljoin, quote, unquote
import requests
from flask import Flask, request, Response, jsonify

app = Flask(__name__)

CHAT_DB = {}
CACHE = {}
CACHE_TTL = 900 

UPSTREAM_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
PROXY_PREFIX = "/p/"
TIMEOUT = 15
MAX_BYTES = 8 * 1024 * 1024

session = requests.Session()
session.trust_env = False
session.headers.update({"User-Agent": UPSTREAM_UA, "Accept-Language": "en-US,en;q=0.9"})

HOOK_JS = """
<script>
(function () {
  var P = '/p/';
  function enc(u) { return P + encodeURIComponent(u); }
  function looksHttp(u) { return /^https?:\\/\\//i.test(u); }
  var _fetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      if (typeof input === "string" && looksHttp(input)) input = enc(input);
      else if (input && input.url && looksHttp(input.url)) input = new Request(enc(input.url), input);
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
"""

ATTR_REGEX = re.compile(r'(href|src|action)=["\'](?!(?:https?:|data:|javascript:|mailto:|#|/p/))(.*?)["\']', re.IGNORECASE)
CSS_REGEX = re.compile(r'url\(\s*[\'"]?(?!(?:https?:|data:))(.*?)[\'"]?\s*\)', re.IGNORECASE)

def rewrite_fast(body, base):
    def repl_attr(m):
        absolute = urljoin(base, m.group(2))
        return f'{m.group(1)}="{PROXY_PREFIX}{quote(absolute, safe="")}"'
    body = ATTR_REGEX.sub(repl_attr, body)
    base_tag = f'<base href="{PROXY_PREFIX}{quote(base, safe="")}">'
    injection = base_tag + HOOK_JS
    if '<head>' in body.lower():
        body = re.sub(r'<head>', f'<head>{injection}', body, count=1, flags=re.IGNORECASE)
    else:
        body = injection + body
    return body

def rewrite_css_fast(css, base):
    def repl_css(m):
        absolute = urljoin(base, m.group(1))
        return f'url("{PROXY_PREFIX}{quote(absolute, safe="")}")'
    return CSS_REGEX.sub(repl_css, css)

@app.route("/api/search_yt")
def search_yt():
    q = request.args.get("q", "")
    if not q: return jsonify([])
    
    # Try multiple instances in case one is down
    apis = [
        f"https://pipedapi.kavin.rocks/search?q={quote(q)}&filter=videos",
        f"https://pipedapi.adminforge.de/search?q={quote(q)}&filter=videos",
        f"https://invidious.privacyredirect.com/api/v1/search?q={quote(q)}&type=video"
    ]
    
    for api in apis:
        try:
            r = requests.get(api, timeout=5)
            data = r.json()
            results = []
            # Piped API format
            if "items" in data:
                for v in data["items"][:12]:
                    if v.get("type") == "stream":
                        results.append({"title": v["title"], "id": v["url"].split("?v=")[1], "author": v["uploaderName"], "thumb": v["thumbnail"]})
                if results: return jsonify(results)
            # Invidious API format
            elif isinstance(data, list):
                for v in data[:12]:
                    results.append({"title": v["title"], "id": v["videoId"], "author": v["author"], "thumb": v["videoThumbnails"][0]["url"]})
                if results: return jsonify(results)
        except Exception:
            continue
            
    return jsonify({"error": "All search APIs are currently down. Please try again later."})

@app.route("/api/tt")
def get_tt():
    url = request.args.get("url", "")
    if not url: return jsonify({"error": "No URL"})
    try:
        r = requests.get(f"https://www.tikwm.com/api/?url={quote(url)}", timeout=10)
        data = r.json()
        if data.get("code") == 0:
            return jsonify({"video": data["data"]["play"], "title": data["data"]["title"], "author": data["data"]["author"]["nickname"]})
        return jsonify({"error": "Failed to extract"})
    except Exception as e: return jsonify({"error": str(e)})

@app.route("/api/chat/<room>", methods=["GET", "POST"])
def chat_api(room):
    if request.method == "POST":
        data = request.json
        if room not in CHAT_DB: CHAT_DB[room] = []
        CHAT_DB[room].append({"user": data.get("user", "Anon"), "text": data.get("text", ""), "time": time.strftime("%H:%M")})
        if len(CHAT_DB[room]) > 50: CHAT_DB[room].pop(0)
        return {"status": "ok"}
    return {"messages": CHAT_DB.get(room, [])}

@app.route("/")
def index():
    with open("index.html", "r", encoding="utf-8") as f: return f.read()

@app.route("/p/<path:encoded>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def proxy(encoded):
    target = unquote(encoded)
    if not target.startswith(("http://", "https://")): return Response("bad url", status=400)

    if request.method == "GET" and target in CACHE:
        ts, body, ctype = CACHE[target]
        if time.time() - ts < CACHE_TTL: return Response(body, mimetype=ctype)

    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "referer", "origin", "cookie", "accept-encoding", "content-length", "connection")}
    ref = request.headers.get("Referer", "")
    if ref.startswith(PROXY_PREFIX): fwd_headers["Referer"] = unquote(ref[len(PROXY_PREFIX):])
    
    data = None
    if request.method in ("POST", "PUT", "PATCH"):
        data = request.get_data()
        fwd_headers["Content-Type"] = request.headers.get("Content-Type", "application/x-www-form-urlencoded")

    try:
        upstream = session.request(request.method, target, headers=fwd_headers, data=data, timeout=TIMEOUT, allow_redirects=True, stream=True)
    except requests.RequestException as e: return Response(f"Error: {e}", status=502)

    ctype = upstream.headers.get("Content-Type", "").lower()
    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in ("content-security-policy", "x-frame-options", "strict-transport-security", "content-encoding", "content-length")}
    if "Location" in upstream.headers: out_headers["Location"] = PROXY_PREFIX + quote(urljoin(target, upstream.headers["Location"]), safe="")

    body_bytes = b""
    try:
        for chunk in upstream.iter_content(chunk_size=8192):
            body_bytes += chunk
            if len(body_bytes) > MAX_BYTES: break
    except Exception: pass

    if "text/html" in ctype:
        raw = rewrite_fast(body_bytes.decode(upstream.encoding or "utf-8", "replace"), upstream.url)
        if request.method == "GET": CACHE[target] = (time.time(), raw, "text/html")
        return Response(raw, status=upstream.status_code, headers=out_headers, mimetype="text/html")
    if "text/css" in ctype:
        raw = rewrite_css_fast(body_bytes.decode(upstream.encoding or "utf-8", "replace"), upstream.url)
        if request.method == "GET": CACHE[target] = (time.time(), raw, "text/css")
        return Response(raw, status=upstream.status_code, headers=out_headers, mimetype="text/css")
        
    if request.method == "GET": CACHE[target] = (time.time(), body_bytes, ctype)
    return Response(body_bytes, status=upstream.status_code, headers=out_headers, mimetype=ctype or None)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)