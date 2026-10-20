#!/usr/bin/env python3
import re
import time
import yt_dlp
from urllib.parse import urljoin, quote, unquote
import requests
from flask import Flask, request, Response, jsonify, stream_with_context

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

@app.route("/api/extract")
def extract_media():
    url = request.args.get("url")
    is_audio = request.args.get("audio") == "true"
    if not url: return jsonify({"error": "No URL provided"})
    
    if "tiktok.com" in url and not is_audio:
        try:
            r = requests.get(f"https://www.tikwm.com/api/?url={quote(url)}", timeout=10)
            data = r.json()
            if data.get("code") == 0:
                return jsonify({
                    "title": data["data"]["title"] or "TikTok Video",
                    "url": data["data"]["play"],
                    "thumbnail": data["data"]["cover"],
                    "ext": "mp4",
                    "uploader": data["data"]["author"]["nickname"]
                })
        except Exception:
            pass

    # Audio or Fallback extraction
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['tv', 'mweb', 'ios'],
                'player_skip': ['webpage', 'configs'],
            }
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        }
    }
    
    if is_audio:
        ydl_opts['format'] = 'bestaudio/best'
    else:
        ydl_opts['format'] = 'best[ext=mp4]/best'
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            direct_url = info.get('url')
            
            if not direct_url and 'formats' in info:
                for f in reversed(info['formats']):
                    if f.get('url'):
                        direct_url = f['url']
                        break

            if direct_url:
                ext = info.get("ext", "mp4") if not is_audio else "m4a"
                return jsonify({
                    "title": info.get("title", "Media"),
                    "url": direct_url,
                    "thumbnail": info.get("thumbnail", ""),
                    "ext": ext,
                    "uploader": info.get("uploader", "Unknown")
                })
            return jsonify({"error": "Could not find a direct link."})
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/download")
def download_file():
    url = request.args.get("url")
    filename = request.args.get("filename", "minxy_download")
    if not url: return "No URL", 400
    try:
        req = session.get(url, stream=True, timeout=15)
        if '.' not in filename: filename += '.mp4'
        return Response(
            stream_with_context(req.iter_content(chunk_size=8192)),
            content_type=req.headers.get('content-type', 'video/mp4'),
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return str(e), 500

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)