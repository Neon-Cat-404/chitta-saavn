from __future__ import annotations

import re
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from flask import Flask, jsonify, render_template, request, send_from_directory, abort

BASE_DIR = Path(__file__).resolve().parent
# The original downloader writes to a relative Downloads/ path.
# Anchor its working directory to this project so files land in the right place.
import os
os.chdir(BASE_DIR)
DOWNLOAD_DIR = BASE_DIR / "Downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

SEARCH_API = "https://jiosaavn-api.tharunowner001.workers.dev/api/search"

app = Flask(__name__, template_folder="templates", static_folder="static")
download_lock = threading.Lock()
download_state = {
    "status": "idle",       # idle | running | error
    "message": "",
    "song_url": "",
}


def find_song_files():
    """Return supported audio files found recursively inside Downloads/."""
    extensions = {".m4a", ".mp3", ".aac", ".flac", ".ogg", ".wav"}
    return sorted(
        (path for path in DOWNLOAD_DIR.rglob("*")
         if path.is_file() and path.suffix.lower() in extensions),
        key=lambda path: path.name.lower(),
    )


def read_metadata(path: Path) -> dict:
    """Read common tags where possible; filename is the fallback."""
    title = path.stem
    artist = "Unknown artist"
    album = path.parent.name if path.parent != DOWNLOAD_DIR else "Unknown album"
    artwork = None
    duration = None

    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path, easy=True)
        if audio:
            title = (audio.get("title") or [title])[0]
            artist = (audio.get("artist") or audio.get("albumartist") or [artist])[0]
            album = (audio.get("album") or [album])[0]
            if audio.info and getattr(audio.info, "length", None):
                duration = round(audio.info.length)
    except Exception:
        # Metadata is optional; don't let one malformed file break the library.
        pass

    # jiosaavn-dl saves cover.jpg alongside tracks in each album folder.
    cover = path.parent / "cover.jpg"
    if cover.exists():
        artwork = f"/api/cover/{quote_relative(cover.relative_to(DOWNLOAD_DIR))}"

    relative_path = path.relative_to(DOWNLOAD_DIR).as_posix()
    return {
        "title": str(title),
        "artist": str(artist),
        "album": str(album),
        "filename": path.name,
        "path": relative_path,
        "audio_url": f"/api/audio/{quote_relative(relative_path)}",
        "artwork": artwork,
        "duration": duration,
    }


def quote_relative(value: Path | str) -> str:
    # Encode each path segment while preserving directory separators.
    from urllib.parse import quote
    return "/".join(quote(part) for part in str(value).replace("\\", "/").split("/"))


def resolve_download_path(relative_path: str) -> Path:
    """Resolve a requested file and prevent paths escaping Downloads/."""
    candidate = (DOWNLOAD_DIR / unquote(relative_path)).resolve()
    root = DOWNLOAD_DIR.resolve()
    if candidate != root and root not in candidate.parents:
        abort(404)
    if not candidate.is_file():
        abort(404)
    return candidate


@app.get("/")
def index():
    # Put index.html in a templates/ directory (recommended Flask layout).
    # If you prefer keeping it beside app.py, this fallback serves that file.
    template = BASE_DIR / "templates" / "index.html"
    if template.exists():
        return render_template("index.html")
    standalone = BASE_DIR / "index.html"
    if standalone.exists():
        return send_from_directory(BASE_DIR, "index.html")
    return "Place index.html beside app.py or inside templates/.", 404


@app.get("/api/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify(success=False, error="Missing search query (q)."), 400

    try:
        response = requests.get(
            SEARCH_API,
            params={"query": query},
            timeout=20,
            headers={"User-Agent": "SonicPulse/1.0"},
        )
        response.raise_for_status()
        return jsonify(response.json())
    except requests.RequestException as exc:
        return jsonify(success=False, error=f"Search API request failed: {exc}"), 502
    except ValueError:
        return jsonify(success=False, error="Search API returned invalid JSON."), 502


@app.get("/api/library")
def library():
    songs = [read_metadata(path) for path in find_song_files()]
    return jsonify(success=True, songs=songs, count=len(songs))


@app.get("/api/audio/<path:relative_path>")
def audio_file(relative_path):
    path = resolve_download_path(relative_path)
    return send_from_directory(path.parent, path.name, conditional=True)


@app.get("/api/cover/<path:relative_path>")
def cover_file(relative_path):
    path = resolve_download_path(relative_path)
    if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        abort(404)
    return send_from_directory(path.parent, path.name, conditional=True)


def run_download(song_url: str):
    """Run the existing jiosaavn-dl downloader in a background thread."""
    try:
        # This import will work after you add jiosaavn.py from your downloader repo.
        from jiosaavn import Jiosaavn

        match = re.search(
            r"https?://(?:www\.)?jiosaavn\.com/song/[^/]+/([^/?#]+)",
            song_url,
        )
        if not match:
            raise ValueError(
                "Expected an individual JioSaavn song URL, such as "
                "https://www.jiosaavn.com/song/song-name/SONG_ID"
            )

        song_id = match.group(1)
        downloader = Jiosaavn()
        downloader.processTrack(song_id)

        download_state["status"] = "idle"
        download_state["message"] = "Download finished."
    except Exception as exc:
        download_state["status"] = "error"
        download_state["message"] = str(exc)
    finally:
        download_state["song_url"] = ""
        download_lock.release()


@app.post("/api/download")
def download():
    body = request.get_json(silent=True) or {}
    song_url = str(body.get("url", "")).strip()

    parsed = urlparse(song_url)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
        "www.jiosaavn.com", "jiosaavn.com"
    }:
        return jsonify(success=False, error="Please provide a valid JioSaavn song URL."), 400

    if "/song/" not in parsed.path:
        return jsonify(success=False, error="Only individual song URLs are supported."), 400

    if not download_lock.acquire(blocking=False):
        return jsonify(success=False, error="A download is already running."), 409

    download_state.update({
        "status": "running",
        "message": "Download started.",
        "song_url": song_url,
    })
    thread = threading.Thread(target=run_download, args=(song_url,), daemon=True)
    thread.start()
    return jsonify(success=True, message="Download started."), 202


@app.get("/api/download/status")
def download_status():
    return jsonify(success=True, **download_state)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
