#!/usr/bin/env python3
"""
ClipForge: a local clipping studio.

Serves the web app (index.html) and does the heavy lifting:
  * ingests links with yt-dlp (YouTube, Twitch VODs and clips, and 1,000+ other sites)
  * accepts local video files
  * Hype Finder: reads the audio with ffmpeg, ranks the loudest moments and snaps
    cut points to quiet spots so clips do not start or stop mid-word
  * renders clips with ffmpeg: 16:9 original, 9:16 Shorts/Reels, or 1:1 square
  * publishes to YouTube (OAuth upload) and Instagram (Reels via the Graph API)

Standard library only. Needs ffmpeg and ffprobe on PATH, plus yt-dlp for links.

Run:  python3 clipforge.py      then open http://localhost:8787
"""

import json
import base64
import math
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from bisect import bisect_left
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("CLIPFORGE_DATA", HERE / "data"))
MEDIA_DIR = DATA / "media"
CLIP_DIR = DATA / "clips"
LIBRARY_FILE = DATA / "library.json"
TOKEN_FILE = DATA / "youtube_tokens.json"

HOST = os.environ.get("CLIPFORGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", os.environ.get("CLIPFORGE_PORT", "8787")))
PUBLIC_URL = os.environ.get("CLIPFORGE_PUBLIC_URL", "").rstrip("/")
ALLOWED_ORIGINS = {
    origin.strip().rstrip("/") for origin in os.environ.get(
        "CLIPFORGE_ALLOWED_ORIGINS", "https://clipforge78.netlify.app"
    ).split(",") if origin.strip()
}

YT_CLIENT_ID = os.environ.get("YOUTUBE_CLIENT_ID", "")
YT_CLIENT_SECRET = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
GOOGLE_AUTH_URL = os.environ.get("GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
GOOGLE_TOKEN_URL = os.environ.get("GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
YT_UPLOAD_URL = os.environ.get("YOUTUBE_UPLOAD_URL", "https://www.googleapis.com/upload/youtube/v3/videos")
YT_SCOPE = "https://www.googleapis.com/auth/youtube.upload"

IG_USER_ID = os.environ.get("INSTAGRAM_USER_ID", "")
IG_TOKEN = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("CLIPFORGE_VISION_MODEL", "gpt-4o-mini")
GRAPH_URL = os.environ.get(
    "GRAPH_BASE_URL",
    "https://graph.facebook.com/" + os.environ.get("INSTAGRAM_GRAPH_VERSION", "v21.0"),
)

MAX_UPLOAD = 8 * 1024 ** 3
ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
FORMATS = {
    "original": "scale=trunc(iw/2)*2:trunc(ih/2)*2",
    "shorts": "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
    "square": "scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080",
}
RENDER_SLOTS = threading.Semaphore(2)  # at most two ffmpeg renders at once

MEDIA_DIR.mkdir(parents=True, exist_ok=True)
CLIP_DIR.mkdir(parents=True, exist_ok=True)

LOCK = threading.RLock()
LIB = {"sources": {}, "clips": {}}
JOBS = {}
OAUTH_STATES = set()


class BadRequest(Exception):
    """A problem with the request the user made. Shown to them as-is."""


# ----------------------------------------------------------------- persistence

def load_library():
    if not LIBRARY_FILE.exists():
        return
    try:
        data = json.loads(LIBRARY_FILE.read_text())
    except (OSError, ValueError):
        return
    LIB["sources"] = data.get("sources", {})
    LIB["clips"] = data.get("clips", {})
    for clip in LIB["clips"].values():
        if clip.get("status") == "rendering":
            clip["status"] = "error"
            clip["error"] = "The server stopped before this clip finished rendering."


def save_library():
    with LOCK:
        tmp = LIBRARY_FILE.parent / (LIBRARY_FILE.name + ".tmp")
        tmp.write_text(json.dumps(LIB))
        tmp.replace(LIBRARY_FILE)


# ----------------------------------------------------------------- helpers

def new_id():
    return uuid.uuid4().hex[:12]


def now():
    return time.time()


def have(cmd):
    return shutil.which(cmd) is not None


def run(cmd, timeout=None):
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-600:]
        raise RuntimeError(f"{cmd[0]} failed: {detail or 'no output'}")
    return proc


def media_path(sid):
    for p in sorted(MEDIA_DIR.glob(f"{sid}.*")):
        if not p.name.endswith((".part", ".ytdl", ".tmp")):
            return p
    return None


def fmt_time(t):
    m, s = divmod(int(t), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def get_source(sid):
    with LOCK:
        src = LIB["sources"].get(sid)
    if not src:
        raise BadRequest("That source no longer exists.")
    return src


def get_clip(cid, ready=False):
    with LOCK:
        clip = LIB["clips"].get(cid)
    if not clip:
        raise BadRequest("That clip no longer exists.")
    if ready and clip.get("status") != "ready":
        raise BadRequest("This clip is not ready yet.")
    return clip


def probe(path):
    if not have("ffprobe"):
        raise RuntimeError("ffprobe is not installed. Install ffmpeg, which includes it.")
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)]).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {
        "duration": round(float(data.get("format", {}).get("duration") or 0), 2),
        "width": video.get("width"),
        "height": video.get("height"),
        "hasAudio": any(s.get("codec_type") == "audio" for s in streams),
    }


def add_source(sid, path, title, kind, url="", site=""):
    meta = probe(path)
    if not meta["duration"]:
        raise RuntimeError("Could not read the length of this file. Is it a video?")
    src = {
        "id": sid, "title": (title or "Untitled")[:160], "kind": kind,
        "url": url, "site": site, "addedAt": now(), "analysis": None, **meta,
    }
    with LOCK:
        LIB["sources"][sid] = src
    save_library()
    return src


# ----------------------------------------------------------------- jobs

def start_job(kind, fn, *args):
    jid = new_id()
    JOBS[jid] = {"id": jid, "kind": kind, "status": "running",
                 "message": "Working…", "result": None, "error": None}

    def runner():
        try:
            JOBS[jid]["result"] = fn(jid, *args)
            JOBS[jid]["status"] = "done"
        except Exception as exc:  # every failure is reported back to the UI
            JOBS[jid]["status"] = "error"
            JOBS[jid]["error"] = str(exc)[:900]

    threading.Thread(target=runner, daemon=True).start()
    return jid


def set_msg(jid, text):
    JOBS[jid]["message"] = text


# ----------------------------------------------------------------- ingest

def job_ingest(jid, url):
    if not have("yt-dlp"):
        raise RuntimeError("yt-dlp is not installed. Run: pip install -r requirements.txt")
    sid = new_id()
    set_msg(jid, "Downloading with yt-dlp…")
    proc = run([
        "yt-dlp", "--no-playlist", "--no-warnings", "--newline", "--print-json",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format", "mp4",
        "-o", str(MEDIA_DIR / f"{sid}.%(ext)s"),
        url,
    ])
    info = {}
    for line in proc.stdout.splitlines():
        if line.startswith("{"):
            try:
                info = json.loads(line)
                break
            except ValueError:
                continue
    path = media_path(sid)
    if path is None:
        raise RuntimeError("yt-dlp finished, but no video file was written.")
    set_msg(jid, "Reading the video…")
    title = info.get("title") or url
    site = info.get("extractor_key") or info.get("extractor") or ""
    return add_source(sid, path, title, "link", url=url, site=site)


# ----------------------------------------------------------------- Hype Finder

def audio_levels(path, duration):
    """One loudness reading (RMS dB) per second of audio, via ffmpeg's astats filter.

    Readings are placed by their timestamp, so the timeline stays aligned with the
    video even if ffmpeg chunks the audio differently than expected.
    """
    af = ("aresample=8000,aformat=channel_layouts=mono,asetnsamples=n=8000:p=0,"
          "astats=metadata=1:reset=1,"
          "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-")
    proc = run(["ffmpeg", "-v", "error", "-nostats", "-i", str(path), "-vn",
                "-af", af, "-f", "null", "-"], timeout=3 * 3600)
    prefix = "lavfi.astats.Overall.RMS_level="
    slots = [-90.0] * (int(math.ceil(duration)) + 1)
    frame_time = None
    for line in proc.stdout.splitlines():
        if line.startswith("frame:"):
            m = re.search(r"pts_time:([\d.]+)", line)
            frame_time = float(m.group(1)) if m else None
        elif line.startswith(prefix) and frame_time is not None:
            try:
                value = float(line[len(prefix):])
            except ValueError:
                value = -90.0
            if math.isnan(value) or value == float("-inf"):
                value = -90.0
            index = min(len(slots) - 1, int(frame_time + 0.5))
            slots[index] = max(-90.0, min(0.0, value))
            frame_time = None
    return slots


def clip_bounds(peak, levels, duration):
    """Start about 12 s before the peak and end about 18 s after it, nudged to quiet points."""
    n = len(levels)

    def quietest(center):
        center = max(0, min(n - 1, center))
        lo, hi = max(0, center - 3), min(n - 1, center + 3)
        return min(range(lo, hi + 1), key=lambda k: levels[k])

    start = max(0, min(quietest(peak - 12), peak - 6))
    end = min(duration, max(quietest(peak + 18), peak + 8))
    return float(start), float(end)


def find_moments(levels, duration, want=12):
    n = len(levels)
    if n < 15:
        return []
    gap = int(max(8, min(25, duration / 8)))  # short videos allow closer moments
    smooth = []
    for i in range(n):
        window = levels[max(0, i - 1):i + 2]
        smooth.append(sum(window) / len(window))
    mean = sum(smooth) / n
    spread = math.sqrt(sum((x - mean) ** 2 for x in smooth) / n) or 1.0

    score = []
    for i in range(n):
        before = levels[max(0, i - 10):max(0, i - 1)] or [smooth[i]]
        rise = max(0.0, smooth[i] - sum(before) / len(before)) / spread
        score.append((smooth[i] - mean) / spread + 0.8 * rise)

    chosen = []
    for i in sorted(range(n), key=lambda k: score[k], reverse=True):
        if score[i] < 1.0:  # only real spikes, not ordinary loudness
            break
        if all(abs(i - c) >= gap for c in chosen):
            chosen.append(i)
            if len(chosen) == want:
                break

    ranked = sorted(score)
    moments = []
    for peak in chosen:
        start, end = clip_bounds(peak, levels, duration)
        moments.append({
            "id": f"m{peak}",
            "peak": peak,
            "start": round(start, 2),
            "end": round(end, 2),
            "score": round(100 * bisect_left(ranked, score[peak]) / n),
            "level": round(levels[peak], 1),
            "label": "Loudest moment" if not moments else "Energy spike",
        })
    return moments


def job_hype(jid, sid):
    src = get_source(sid)
    path = media_path(sid)
    if path is None:
        raise RuntimeError("The media file for this source is missing.")
    if not src.get("hasAudio"):
        raise RuntimeError("This video has no audio track, so Hype Finder has nothing to listen to.")
    set_msg(jid, "Listening to the audio…")
    levels = audio_levels(path, src["duration"])
    if len(levels) < 15:
        raise RuntimeError("There is not enough audio to analyse.")
    set_msg(jid, "Ranking the loudest moments…")
    analysis = {
        "levels": levels,
        "suggestions": find_moments(levels, src["duration"]),
        "analysedAt": now(),
    }
    with LOCK:
        src["analysis"] = analysis
    save_library()
    return analysis




# ----------------------------------------------------------------- visual AI + Autopilot


def transcribe_video_audio(jid, path):
    """Create a compact timestamped speech transcript to help AI recognise spoken humour and punchlines."""
    audio = CLIP_DIR / f".transcript-{uuid.uuid4().hex}.mp3"
    try:
        run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vn",
             "-ac", "1", "-ar", "16000", "-b:a", "24k", str(audio)], timeout=3 * 3600)
        if not audio.exists() or audio.stat().st_size == 0:
            return ""
        if audio.stat().st_size > 24 * 1024 * 1024:
            return ""
        boundary = "----ClipForge" + uuid.uuid4().hex
        chunks = []
        def field(name, value):
            chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        field("model", "whisper-1")
        field("response_format", "verbose_json")
        field("timestamp_granularities[]", "segment")
        chunks.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                       f"filename=\"audio.mp3\"\r\nContent-Type: audio/mpeg\r\n\r\n").encode())
        chunks.append(audio.read_bytes())
        chunks.append(f"\r\n--{boundary}--\r\n".encode())
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/transcriptions",
            data=b"".join(chunks),
            headers={"Authorization": "Bearer " + OPENAI_API_KEY,
                     "Content-Type": "multipart/form-data; boundary=" + boundary},
            method="POST",
        )
        raw, _ = open_url(req, timeout=600)
        data = json.loads(raw)
        lines = []
        for seg in data.get("segments", [])[:1800]:
            text_value = str(seg.get("text", "")).strip()
            if text_value:
                lines.append(f"[{float(seg.get('start', 0)):.1f}s] {text_value}")
        return "\n".join(lines)[:24000]
    except Exception:
        # Visual analysis should still work if transcription is unavailable.
        return ""
    finally:
        audio.unlink(missing_ok=True)


def vision_highlights(jid, sid):
    """Use timestamped video frames to identify meaningful, self-contained highlights."""
    if not OPENAI_API_KEY:
        raise RuntimeError("Visual AI is not configured. Set OPENAI_API_KEY in your environment and restart ClipForge.")
    src = get_source(sid)
    path = media_path(sid)
    if path is None:
        raise RuntimeError("The source video file is missing.")
    duration = float(src["duration"])
    if duration < 3:
        raise RuntimeError("This video is too short to analyse.")
    transcript = ""
    if src.get("hasAudio"):
        set_msg(jid, "Listening for dialogue, jokes, reactions and punchlines…")
        transcript = transcribe_video_audio(jid, path)
    # Sample a bounded number of frames across the entire video. Frames are temporary.
    frame_count = min(20, max(6, int(duration / 15) + 1))
    timestamps = sorted(set(round((duration - 0.1) * i / max(1, frame_count - 1), 2)
                            for i in range(frame_count)))
    content = [{
        "type": "text",
        "text": (
            "You are an expert short-form entertainment editor. Review timestamped frames in chronological "
            "order. Prioritise genuinely funny, entertaining, surprising, satisfying, high-energy, emotionally "
            "engaging and highly rewatchable moments: punchlines, banter, reactions, unexpected fails, clever "
            "reveals, impressive skill, dramatic turns and satisfying payoffs. Judge setup AND payoff, not just "
            "a loud frame. Rank moments by hook strength, clarity without excessive context, emotional payoff, "
            "shareability and replay value. Prefer an immediate opening hook, tight pacing and a clean ending. "
            "Never manufacture suspense, misrepresent context or invent events. Return only JSON with a "
            "'highlights' array. Each item must contain start, end (seconds), title, reason, score (1-100), "
            "caption and description. Choose up to 8 distinct real moments, preferably 12-45 seconds, and "
            "estimate boundaries from timestamps. If fewer moments are supported, return fewer. Captions should "
            "be punchy and accurate, not deceptive clickbait; descriptions should include context and hashtags."
        )
    }]
    if transcript:
        content.append({"type": "text", "text": "TIMESTAMPED AUDIO TRANSCRIPT (use this to understand dialogue, jokes, banter, reactions and punchlines; timestamps are seconds):\n" + transcript})
    set_msg(jid, "Sampling frames across the video…")
    for stamp in timestamps:
        frame = CLIP_DIR / f".vision-{uuid.uuid4().hex}.jpg"
        try:
            run(["ffmpeg", "-y", "-v", "error", "-ss", str(stamp), "-i", str(path),
                 "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "6", str(frame)], timeout=90)
            if not frame.exists():
                continue
            encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
            content.append({"type": "text", "text": f"FRAME TIMESTAMP: {stamp:.2f} seconds"})
            content.append({"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + encoded, "detail": "low"
            }})
        finally:
            frame.unlink(missing_ok=True)
    if len(content) < 5:
        raise RuntimeError("Could not extract enough frames for visual analysis.")
    set_msg(jid, "AI is understanding the video and ranking its best moments…")
    request_data = {
        "model": OPENAI_MODEL,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": content}],
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(request_data).encode("utf-8"),
        headers={"Authorization": "Bearer " + OPENAI_API_KEY,
                 "Content-Type": "application/json"},
        method="POST",
    )
    raw, _ = open_url(req, timeout=300)
    try:
        result = json.loads(raw)["choices"][0]["message"]["content"]
        highlights = json.loads(result).get("highlights", [])
    except (ValueError, KeyError, TypeError, IndexError):
        raise RuntimeError("The visual AI response could not be read.")
    clean = []
    for idx, item in enumerate(highlights[:8]):
        try:
            start = max(0.0, float(item["start"]))
            end = min(duration, float(item["end"]))
            if end - start < 3:
                continue
            clean.append({
                "id": f"ai{idx}", "start": round(start, 2), "end": round(end, 2),
                "peak": round((start + end) / 2, 2),
                "score": max(1, min(100, int(item.get("score", 70)))),
                "title": str(item.get("title", f"Highlight {idx + 1}"))[:100],
                "label": str(item.get("reason", "AI-identified moment"))[:260],
                "caption": str(item.get("caption", ""))[:2200],
                "description": str(item.get("description", ""))[:4900],
            })
        except (KeyError, TypeError, ValueError):
            continue
    if not clean:
        raise RuntimeError("AI did not find any reliable highlight moments in this video.")
    with LOCK:
        src["aiAnalysis"] = {"suggestions": clean, "analysedAt": now(), "model": OPENAI_MODEL}
    save_library()
    return {"suggestions": clean, "model": OPENAI_MODEL}


def job_autopilot(jid, sid):
    """Analyse, render and publish each highlight to every configured connected platform."""
    tokens = yt_tokens()
    youtube_ready = bool(yt_configured() and tokens and tokens.get("refresh_token"))
    instagram_ready = bool(IG_USER_ID and IG_TOKEN and PUBLIC_URL)
    highlights = vision_highlights(jid, sid)["suggestions"]
    completed, failures = [], []
    for index, moment in enumerate(highlights, 1):
        title = moment["title"][:100]
        clip_id = new_id()
        clip = {
            "id": clip_id, "sourceId": sid, "title": title,
            "start": moment["start"], "end": moment["end"], "format": "shorts",
            "status": "rendering", "error": "", "publish": {}, "createdAt": now(),
            "ai": {k: moment.get(k, "") for k in ("caption", "description", "label", "score")},
        }
        with LOCK:
            LIB["clips"][clip_id] = clip
        save_library()
        render_job = "auto" + uuid.uuid4().hex[:10]
        JOBS[render_job] = {"id": render_job, "kind": "render", "status": "running",
                            "message": f"Rendering highlight {index} of {len(highlights)}…",
                            "result": None, "error": None}
        set_msg(jid, f"Rendering highlight {index} of {len(highlights)}: {title}")
        job_render(render_job, clip_id)
        platform_results = {}
        if youtube_ready:
            try:
                set_msg(jid, f"Publishing {index}/{len(highlights)} to YouTube…")
                platform_results["youtube"] = job_publish_youtube(
                    jid, clip_id, title, moment.get("description") or moment.get("caption", ""), "public")
            except Exception as exc:
                platform_results["youtube_error"] = str(exc)[:300]
        if instagram_ready:
            try:
                set_msg(jid, f"Publishing {index}/{len(highlights)} to Instagram…")
                caption = moment.get("caption") or title
                platform_results["instagram"] = job_publish_instagram(jid, clip_id, caption)
            except Exception as exc:
                platform_results["instagram_error"] = str(exc)[:300]
        if not any(k in platform_results for k in ("youtube", "instagram")):
            failures.append({"title": title, "errors": platform_results})
        completed.append({"clipId": clip_id, "title": title, "platforms": platform_results})
        save_library()
    return {"highlights": len(highlights), "completed": completed, "failures": failures}



def job_channel_autopilot(jid, channel_url, limit):
    """Fetch recent public channel videos, import them, then autonomously clip the strongest moments."""
    if not have("yt-dlp"):
        raise RuntimeError("yt-dlp is not installed. Run: pip install -r requirements.txt")
    if not OPENAI_API_KEY:
        raise RuntimeError("Visual AI is not configured. Set OPENAI_API_KEY and restart ClipForge.")
    set_msg(jid, "Discovering recent public videos on the channel…")
    proc = run([
        "yt-dlp", "--flat-playlist", "--dump-single-json", "--playlist-end", str(limit),
        "--no-warnings", channel_url,
    ], timeout=300)
    try:
        listing = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError("Could not read the channel video list. Check that the channel URL is public and supported.")
    entries = listing.get("entries") or []
    if not entries:
        raise RuntimeError("No public videos were found on that channel.")
    imported, processed, errors, skipped = [], [], [], []
    for index, entry in enumerate(entries[:limit], 1):
        video_url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url") or ""
        if video_url and not video_url.startswith(("http://", "https://")):
            extractor = (entry.get("ie_key") or listing.get("extractor_key") or "").lower()
            if "youtube" in extractor:
                video_url = "https://www.youtube.com/watch?v=" + video_url
            else:
                errors.append({"title": entry.get("title", "Unknown video"),
                               "error": "The extractor did not provide a usable video URL."})
                continue
        if not re.fullmatch(r"https?://\S+", video_url):
            continue
        with LOCK:
            existing = next((s for s in LIB["sources"].values() if s.get("url") == video_url), None)
        if existing:
            sid = existing["id"]
        else:
            set_msg(jid, f"Importing channel video {index}/{min(len(entries), limit)}: {entry.get('title') or 'video'}…")
            try:
                src = job_ingest(jid, video_url)
                sid = src["id"]
                imported.append({"sourceId": sid, "title": src.get("title")})
            except Exception as exc:
                errors.append({"title": entry.get("title", video_url), "error": str(exc)[:300]})
                continue
        set_msg(jid, f"AI judging channel video {index}/{min(len(entries), limit)}: {entry.get('title') or 'video'}…")
        try:
            result = job_autopilot(jid, sid)
            processed.append({"sourceId": sid, "title": get_source(sid).get("title"), **result})
            if not result.get("highlights"):
                skipped.append({"sourceId": sid})
        except Exception as exc:
            errors.append({"sourceId": sid, "title": entry.get("title"), "error": str(exc)[:300]})
    return {"channel": listing.get("title") or channel_url, "videosDiscovered": len(entries[:limit]),
            "videosImported": len(imported), "processed": processed, "skipped": skipped, "errors": errors}



def job_channels_autopilot(jid, channel_urls, limit):
    """Run channel discovery and autonomous clipping for each supplied public channel."""
    results = []
    for index, url in enumerate(channel_urls, 1):
        set_msg(jid, f"Processing channel {index}/{len(channel_urls)}…")
        try:
            results.append(job_channel_autopilot(jid, url, limit))
        except Exception as exc:
            results.append({"channel": url, "error": str(exc)[:400], "processed": [], "errors": []})
    return {"channelsReviewed": len(channel_urls), "results": results}


def job_autopilot_all(jid):
    """Process every imported source; AI decides which moments are worth clipping."""
    with LOCK:
        source_ids = list(LIB["sources"].keys())
    if not source_ids:
        raise RuntimeError("Add at least one video source first.")
    results, skipped, errors = [], [], []
    for index, sid in enumerate(source_ids, 1):
        src = get_source(sid)
        set_msg(jid, f"AI reviewing source {index}/{len(source_ids)}: {src.get('title', 'Video')}…")
        try:
            result = job_autopilot(jid, sid)
            if result.get("highlights"):
                results.append({"sourceId": sid, "title": src.get("title"), **result})
            else:
                skipped.append({"sourceId": sid, "title": src.get("title")})
        except Exception as exc:
            errors.append({"sourceId": sid, "title": src.get("title"), "error": str(exc)[:300]})
    return {"sourcesReviewed": len(source_ids), "processed": results, "skipped": skipped, "errors": errors}


# ----------------------------------------------------------------- clips

def create_clip(body):
    src = get_source(str(body.get("sourceId", "")))
    try:
        start = float(body["start"])
        end = float(body["end"])
    except (KeyError, TypeError, ValueError):
        raise BadRequest("Start and end times are required.")
    fmt = body.get("format", "shorts")
    if fmt not in FORMATS:
        raise BadRequest("Unknown output format.")
    end = min(end, src["duration"])
    if not (0 <= start < end):
        raise BadRequest("Those times are outside the video.")
    if end - start < 1:
        raise BadRequest("A clip needs at least one second.")
    cid = new_id()
    title = str(body.get("title") or "").strip()[:120] or f"{src['title']} · {fmt_time(start)}"
    clip = {
        "id": cid, "sourceId": src["id"], "title": title,
        "start": round(start, 2), "end": round(end, 2), "format": fmt,
        "status": "rendering", "error": "", "publish": {}, "createdAt": now(),
    }
    with LOCK:
        LIB["clips"][cid] = clip
    save_library()
    jid = start_job("render", job_render, cid)
    return {"clip": clip, "jobId": jid}


def job_render(jid, clip_id):
    clip = get_clip(clip_id)
    src = get_source(clip["sourceId"])
    path = media_path(src["id"])
    out = CLIP_DIR / f"{clip_id}.mp4"
    try:
        if path is None:
            raise RuntimeError("The media file for this clip is missing.")
        set_msg(jid, "Waiting for a render slot…")
        with RENDER_SLOTS:
            set_msg(jid, "Rendering…")
            length = clip["end"] - clip["start"]
            run([
                "ffmpeg", "-y", "-v", "error",
                "-ss", f"{clip['start']:.3f}", "-i", str(path), "-t", f"{length:.3f}",
                "-vf", FORMATS[clip["format"]],
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                str(out),
            ], timeout=6 * 3600)
    except Exception as exc:
        clip["status"], clip["error"] = "error", str(exc)[:500]
        save_library()
        raise
    clip["status"], clip["error"] = "ready", ""
    save_library()
    return clip


def delete_clip(cid):
    with LOCK:
        clip = LIB["clips"].pop(cid, None)
    if not clip:
        raise BadRequest("That clip no longer exists.")
    (CLIP_DIR / f"{cid}.mp4").unlink(missing_ok=True)
    save_library()


# ----------------------------------------------------------------- HTTP helpers

def open_url(req, timeout=120):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"The service said no ({exc.code}): {detail}") from None


def post_form(url, fields):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    raw, _ = open_url(req)
    return json.loads(raw or b"{}")


# ----------------------------------------------------------------- YouTube

def yt_configured():
    return bool(YT_CLIENT_ID and YT_CLIENT_SECRET)


def redirect_uri():
    configured = os.environ.get("YOUTUBE_REDIRECT_URI", "").strip()
    if configured:
        return configured
    if PUBLIC_URL:
        return f"{PUBLIC_URL}/auth/youtube/callback"
    return f"http://localhost:{PORT}/auth/youtube/callback"


def yt_tokens():
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except ValueError:
        return None


def yt_save_tokens(tokens):
    TOKEN_FILE.write_text(json.dumps(tokens))
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


def yt_access_token():
    tokens = yt_tokens()
    if not tokens or not tokens.get("refresh_token"):
        raise RuntimeError("YouTube is not connected. Click Connect YouTube first.")
    if tokens.get("expires_at", 0) > now() + 60:
        return tokens["access_token"]
    fresh = post_form(GOOGLE_TOKEN_URL, {
        "client_id": YT_CLIENT_ID, "client_secret": YT_CLIENT_SECRET,
        "refresh_token": tokens["refresh_token"], "grant_type": "refresh_token",
    })
    tokens["access_token"] = fresh["access_token"]
    tokens["expires_at"] = now() + int(fresh.get("expires_in", 3600))
    yt_save_tokens(tokens)
    return tokens["access_token"]


def youtube_login_url():
    state = secrets.token_urlsafe(16)
    OAUTH_STATES.add(state)
    params = {
        "client_id": YT_CLIENT_ID, "redirect_uri": redirect_uri(),
        "response_type": "code", "scope": YT_SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
    }
    return GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)


def job_publish_youtube(jid, clip_id, title, description, privacy):
    clip = get_clip(clip_id, ready=True)
    path = CLIP_DIR / f"{clip_id}.mp4"
    token = yt_access_token()
    if clip["format"] == "shorts" and "#shorts" not in f"{title} {description}".lower():
        description = (description + "\n\n#Shorts").strip()
    meta = {
        "snippet": {"title": title[:100], "description": description[:4900], "categoryId": "17"},
        "status": {
            "privacyStatus": privacy if privacy in ("private", "unlisted", "public") else "private",
            "selfDeclaredMadeForKids": False,
        },
    }
    size = path.stat().st_size
    set_msg(jid, "Opening the YouTube upload…")
    init = urllib.request.Request(
        YT_UPLOAD_URL + "?uploadType=resumable&part=snippet,status",
        data=json.dumps(meta).encode(), method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/mp4",
        },
    )
    _, headers = open_url(init)
    location = headers.get("Location")
    if not location:
        raise RuntimeError("YouTube did not return an upload address.")
    set_msg(jid, "Uploading to YouTube…")
    with open(path, "rb") as f:
        put = urllib.request.Request(
            location, data=f, method="PUT",
            headers={"Content-Type": "video/mp4", "Content-Length": str(size)},
        )
        raw, _ = open_url(put, timeout=6 * 3600)
    video_id = json.loads(raw or b"{}").get("id")
    if not video_id:
        raise RuntimeError("YouTube accepted the upload but returned no video id.")
    result = {"id": video_id, "url": f"https://youtu.be/{video_id}"}
    with LOCK:
        clip.setdefault("publish", {})["youtube"] = result
    save_library()
    return result


# ----------------------------------------------------------------- Instagram

def graph(method, path, params):
    params = dict(params, access_token=IG_TOKEN)
    url = f"{GRAPH_URL}/{path}"
    if method == "GET":
        req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params))
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), method="POST")
    raw, _ = open_url(req)
    return json.loads(raw or b"{}")


def job_publish_instagram(jid, clip_id, caption):
    clip = get_clip(clip_id, ready=True)
    if not (IG_USER_ID and IG_TOKEN):
        raise RuntimeError("Instagram is not set up. Add INSTAGRAM_USER_ID and INSTAGRAM_ACCESS_TOKEN.")
    if not PUBLIC_URL:
        raise RuntimeError("Instagram fetches the video from a public address. Set CLIPFORGE_PUBLIC_URL "
                           "to an https address that reaches this server, such as a tunnel.")
    length = clip["end"] - clip["start"]
    if not 3 <= length <= 90:
        raise RuntimeError(f"Reels must be 3 to 90 seconds long. This clip is {length:.0f}s.")
    set_msg(jid, "Creating the Reel…")
    container = graph("POST", f"{IG_USER_ID}/media", {
        "media_type": "REELS",
        "video_url": f"{PUBLIC_URL}/clips/{clip_id}.mp4",
        "caption": caption[:2200],
    })
    creation_id = container.get("id")
    if not creation_id:
        raise RuntimeError("Instagram did not accept the video.")
    for _ in range(90):
        code = graph("GET", creation_id, {"fields": "status_code"}).get("status_code", "")
        set_msg(jid, f"Instagram is processing the video ({code or 'pending'})…")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram could not process the video ({code}).")
        time.sleep(4)
    else:
        raise RuntimeError("Instagram is still processing the video. Check the Instagram app in a few minutes.")
    media = graph("POST", f"{IG_USER_ID}/media_publish", {"creation_id": creation_id})
    result = {"id": media.get("id", creation_id)}
    with LOCK:
        clip.setdefault("publish", {})["instagram"] = result
    save_library()
    return result


# ----------------------------------------------------------------- state for the UI

def status_payload():
    tokens = yt_tokens()
    return {
        "ffmpeg": have("ffmpeg") and have("ffprobe"),
        "ytdlp": have("yt-dlp"),
        "youtube": {"configured": yt_configured(),
                    "connected": bool(tokens and tokens.get("refresh_token"))},
        "instagram": {"configured": bool(IG_USER_ID and IG_TOKEN), "publicUrl": bool(PUBLIC_URL)},
        "visualAI": bool(OPENAI_API_KEY), "visionModel": OPENAI_MODEL,
    }


def library_payload():
    with LOCK:
        data = {
            "sources": sorted(LIB["sources"].values(), key=lambda s: s.get("addedAt", 0), reverse=True),
            "clips": sorted(LIB["clips"].values(), key=lambda c: c.get("createdAt", 0), reverse=True),
        }
        return json.loads(json.dumps(data))


# ----------------------------------------------------------------- HTTP server

class Handler(BaseHTTPRequestHandler):
    server_version = "ClipForge/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("CLIPFORGE_LOG"):
            super().log_message(fmt, *args)

    # responses
    def cors_headers(self):
        origin = (self.headers.get("Origin") or "").rstrip("/")
        headers = {}
        if origin in ALLOWED_ORIGINS:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
            headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            headers["Access-Control-Allow-Headers"] = "Content-Type, X-Filename, Authorization, Range"
            headers["Access-Control-Expose-Headers"] = "Content-Length, Content-Range, Accept-Ranges"
        return headers

    def send_bytes(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in {**self.cors_headers(), **(extra or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        self.send_bytes(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def send_error_json(self, code, msg):
        self.send_json({"error": msg}, code)

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_media(self, path):
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end, code = 0, size - 1, 200
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                for key, value in self.cors_headers().items(): self.send_header(key, value)
                self.end_headers()
                return
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        for key, value in self.cors_headers().items(): self.send_header(key, value)
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 16, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def send_clip(self, path):
        clip_file = CLIP_DIR / Path(path).name
        if clip_file.exists():
            return self.send_media(clip_file)
        self.send_error_json(404, "Clip not found")

    # requests
    def is_local(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in ("localhost", "127.0.0.1")

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise BadRequest("Request too large.")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise BadRequest("Invalid request data.")

    def handle_upload(self):
        name_header = self.headers.get("X-Filename")
        if not name_header:
            raise BadRequest("Missing file name.")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            raise BadRequest("The file is empty.")
        if n > MAX_UPLOAD:
            raise BadRequest("That file is larger than 8 GB.")
        name = urllib.parse.unquote(name_header)
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_EXT:
            raise BadRequest("Unsupported file type. Use MP4, MOV, MKV, WEBM, M4V or AVI.")
        sid = new_id()
        dest = MEDIA_DIR / f"{sid}{ext}"
        remaining = n
        with open(dest, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining:
            dest.unlink(missing_ok=True)
            raise BadRequest("The upload was cut off. Try again.")
        try:
            src = add_source(sid, dest, Path(name).stem, "file")
        except (RuntimeError, ValueError) as exc:
            dest.unlink(missing_ok=True)
            raise BadRequest(f"Could not read this file as a video. {exc}")
        self.send_json(src)

    def youtube_callback(self, query):
        if "error" in query:
            return self.redirect("/?youtube=denied")
        state = (query.get("state") or [""])[0]
        code = (query.get("code") or [""])[0]
        if not code or state not in OAUTH_STATES:
            raise BadRequest("That YouTube login link has expired. Click Connect YouTube again.")
        OAUTH_STATES.discard(state)
        data = post_form(GOOGLE_TOKEN_URL, {
            "code": code, "client_id": YT_CLIENT_ID, "client_secret": YT_CLIENT_SECRET,
            "redirect_uri": redirect_uri(), "grant_type": "authorization_code",
        })
        old = yt_tokens() or {}
        refresh = data.get("refresh_token") or old.get("refresh_token")
        if not refresh:
            raise BadRequest("Google did not return a refresh token. Remove ClipForge from your Google "
                             "account's third-party access, then connect again.")
        yt_save_tokens({
            "access_token": data["access_token"],
            "refresh_token": refresh,
            "expires_at": now() + int(data.get("expires_in", 3600)),
        })
        self.redirect("/?connected=YouTube")

    def route_get(self, path, query):
        if path == "/health":
            return self.send_json({"ok": True, "service": "ClipForge"})
        if path in ("/", "/index.html"):
            return self.send_bytes(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/status":
            return self.send_json(status_payload())
        if path == "/api/library":
            return self.send_json(library_payload())
        m = re.fullmatch(r"/api/jobs/(\w+)", path)
        if m:
            job = JOBS.get(m.group(1))
            return self.send_json(job) if job else self.send_error_json(404, "Unknown job")
        m = re.fullmatch(r"/media/(\w+)", path)
        if m:
            p = media_path(m.group(1))
            return self.send_media(p) if p else self.send_error_json(404, "Media not found")
        if re.fullmatch(r"/clips/\w+\.mp4", path):
            return self.send_clip(path)
        if path == "/auth/youtube":
            if not yt_configured():
                return self.send_error_json(400, "Add YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET first. See the README.")
            return self.redirect(youtube_login_url())
        if path == "/auth/youtube/callback":
            return self.youtube_callback(query)
        self.send_error_json(404, "Not found")

    def route_post(self, path):
        if path == "/api/upload":
            return self.handle_upload()
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise BadRequest("Requests must be sent as JSON.")
        body = self.read_json()

        if path == "/api/ingest":
            url = str(body.get("url", "")).strip()
            if not re.fullmatch(r"https?://\S+", url):
                raise BadRequest("Paste a full link that starts with https://")
            return self.send_json({"jobId": start_job("ingest", job_ingest, url)})
        if path == "/api/channel-autopilot":
            raw_urls = body.get("urls") or [body.get("url", "")]
            if isinstance(raw_urls, str):
                raw_urls = re.split(r"[\n,]+", raw_urls)
            channel_urls = list(dict.fromkeys(str(u).strip() for u in raw_urls
                                              if re.fullmatch(r"https?://\S+", str(u).strip())))
            if not channel_urls:
                raise BadRequest("Paste at least one public channel URL beginning with https://")
            if len(channel_urls) > 10:
                raise BadRequest("Add up to 10 channels per run.")
            try:
                limit = max(1, min(30, int(body.get("limit", 10))))
            except (TypeError, ValueError):
                limit = 10
            return self.send_json({"jobId": start_job("channel-autopilot", job_channels_autopilot, channel_urls, limit)})
        if path == "/api/visual-analyse":
            src = get_source(str(body.get("sourceId", "")))
            return self.send_json({"jobId": start_job("visual-analysis", vision_highlights, src["id"])})
        if path == "/api/autopilot":
            src = get_source(str(body.get("sourceId", "")))
            return self.send_json({"jobId": start_job("autopilot", job_autopilot, src["id"])})
        if path == "/api/autopilot-all":
            return self.send_json({"jobId": start_job("autopilot-all", job_autopilot_all)})
        if path == "/api/hype":
            src = get_source(str(body.get("sourceId", "")))
            if not src.get("hasAudio"):
                raise BadRequest("This video has no audio track to analyse.")
            return self.send_json({"jobId": start_job("hype", job_hype, src["id"])})
        if path == "/api/clips":
            return self.send_json(create_clip(body))
        if path == "/api/clips/delete":
            delete_clip(str(body.get("id", "")))
            return self.send_json({"ok": True})
        if path == "/api/publish/youtube":
            clip = get_clip(str(body.get("clipId", "")), ready=True)
            if not yt_configured():
                raise BadRequest("Add YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET first. See the README.")
            title = str(body.get("title", "")).strip()[:100]
            if not title:
                raise BadRequest("Give the video a title.")
            jid = start_job("youtube", job_publish_youtube, clip["id"], title,
                            str(body.get("description", "")), str(body.get("privacy", "private")))
            return self.send_json({"jobId": jid})
        if path == "/api/publish/instagram":
            clip = get_clip(str(body.get("clipId", "")), ready=True)
            if not (IG_USER_ID and IG_TOKEN):
                raise BadRequest("Instagram is not set up. Add INSTAGRAM_USER_ID and INSTAGRAM_ACCESS_TOKEN.")
            if not PUBLIC_URL:
                raise BadRequest("Instagram needs a public address for the video. Set CLIPFORGE_PUBLIC_URL, "
                                 "for example to a tunnel that reaches this server.")
            jid = start_job("instagram", job_publish_instagram, clip["id"], str(body.get("caption", "")))
            return self.send_json({"jobId": jid})
        if path == "/api/disconnect/youtube":
            TOKEN_FILE.unlink(missing_ok=True)
            return self.send_json({"ok": True})
        self.send_error_json(404, "Not found")

    def dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            origin = (self.headers.get("Origin") or "").rstrip("/")
            if origin and origin not in ALLOWED_ORIGINS:
                return self.send_error_json(403, "This website is not allowed to access ClipForge. Add its exact URL to CLIPFORGE_ALLOWED_ORIGINS.")
            if method == "OPTIONS":
                self.send_response(204)
                for key, value in self.cors_headers().items(): self.send_header(key, value)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "GET":
                return self.route_get(path, query)
            return self.route_post(path)
        except BadRequest as exc:
            self.send_error_json(400, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.send_error_json(500, str(exc)[:500])

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_OPTIONS(self):
        self.dispatch("OPTIONS")


def main():
    load_library()
    print("ClipForge")
    print(f"  ffmpeg:  {'ok' if have('ffmpeg') and have('ffprobe') else 'MISSING, install ffmpeg'}")
    print(f"  links:   {'ok (yt-dlp)' if have('yt-dlp') else 'yt-dlp not installed, run: pip install -r requirements.txt'}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"  host:    {HOST}:{PORT}")
    print(f"  allowed origins: {', '.join(sorted(ALLOWED_ORIGINS))}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
