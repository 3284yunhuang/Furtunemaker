"""
FurtuneMaker daily market-news video.

Steps:
  1. Ask Claude (with web search) for today's top 5 financial stories and a short anchor script.
  2. Turn the script into speech with ElevenLabs.
  3. Loop the silent anchor clip under the voice with ffmpeg.
  4. Upload the video + latest.json to the website over FTP (Hostinger).

Secrets come from environment variables (set them as GitHub Actions secrets):
  ANTHROPIC_API_KEY, ELEVENLABS_API_KEY, FTP_HOST, FTP_USER, FTP_PASSWORD
Optional settings:
  ELEVENLABS_VOICE_ID (default: Riley news anchor), ELEVENLABS_MODEL, CLAUDE_MODEL,
  FTP_DIR (default: public_html/news), SCRIPT_SECONDS (default: 45), DRY_RUN=1 (skip upload)
"""
import datetime as dt
import ftplib
import json
import os
import re
import subprocess
import sys
import zoneinfo
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "99Hm6N8psCBzc1JwEVR2")  # Riley - news anchor
TTS_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_multilingual_v2")
SCRIPT_SECONDS = int(os.environ.get("SCRIPT_SECONDS", "45"))
FTP_DIR = os.environ.get("FTP_DIR", "public_html/news")
KEEP_DAYS = 30

today = dt.datetime.now(zoneinfo.ZoneInfo("America/New_York")).date()
stamp = today.isoformat()


def log(msg):
    print(f"[news] {msg}", flush=True)


# ---------------------------------------------------------------- 1. news + script
def get_news():
    words = int(SCRIPT_SECONDS * 2.5)
    prompt = f"""Today is {today:%A, %B %d, %Y}. Search the web for today's US stock market close and the
5 most important financial news stories of the day (index moves, big movers, rates/Fed, oil, earnings, deals).
Use only facts you found in today's reporting; do not guess numbers.

Then reply with ONLY a JSON object between <json> and </json>, no other text, in this shape:
{{
  "headlines": ["5 short headlines, max 70 characters each"],
  "script": "A spoken news-anchor script of about {words} words. Start with 'Good evening, and welcome to your FurtuneMaker market close.' Cover the 5 stories in plain spoken English (write numbers the way they are said, e.g. 'four-tenths of a percent'). End with 'That's your market close. Thanks for watching.' No stage directions.",
  "sources": [{{"title": "...", "url": "https://..."}}]
}}
If US markets were closed today, say so in the script and cover the biggest financial stories instead."""
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 4000,
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=300,
    )
    r.raise_for_status()
    blocks = r.json()["content"]
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    m = re.search(r"<json>(.*?)</json>", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if not m:
        sys.exit(f"Could not find JSON in Claude reply:\n{text[:2000]}")
    data = json.loads(m.group(1))
    assert data.get("script") and data.get("headlines"), "Claude reply is missing script/headlines"
    return data


# ---------------------------------------------------------------- 2. voice
def make_voice(script, path):
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}",
        params={"output_format": "mp3_44100_128"},
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"], "content-type": "application/json"},
        json={"text": script, "model_id": TTS_MODEL},
        timeout=300,
    )
    if r.status_code != 200:
        sys.exit(f"ElevenLabs error {r.status_code}: {r.text[:500]}")
    path.write_bytes(r.content)


# ---------------------------------------------------------------- 3. video
def make_video(voice, path):
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-stream_loop", "-1", "-i", str(ROOT / "anchor.mp4" if (ROOT / "anchor.mp4").exists() else ROOT / "assets" / "anchor.mp4"),
            "-i", str(voice),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "medium", "-crf", "23", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k",
            "-shortest", "-movflags", "+faststart",
            str(path),
        ],
        check=True,
    )


# ---------------------------------------------------------------- 4. upload
def ftp_connect():
    host, user, pw = os.environ["FTP_HOST"], os.environ["FTP_USER"], os.environ["FTP_PASSWORD"]
    try:
        ftp = ftplib.FTP_TLS(host, timeout=60)
        ftp.login(user, pw)
        ftp.prot_p()
    except ftplib.all_errors:
        ftp = ftplib.FTP(host, timeout=60)
        ftp.login(user, pw)
    return ftp


def ftp_cd(ftp, path):
    for part in [p for p in path.split("/") if p]:
        try:
            ftp.cwd(part)
        except ftplib.error_perm:
            ftp.mkd(part)
            ftp.cwd(part)


def upload(video, entry):
    ftp = ftp_connect()
    ftp_cd(ftp, FTP_DIR)
    # previous index
    index = []
    try:
        buf = []
        ftp.retrbinary("RETR index.json", buf.append)
        index = json.loads(b"".join(buf))
    except Exception:
        pass
    index = [e for e in index if e.get("date") != entry["date"]]
    index.insert(0, entry)
    old, index = index[KEEP_DAYS:], index[:KEEP_DAYS]

    with open(video, "rb") as f:
        ftp.storbinary(f"STOR {entry['video']}", f)
    for name, obj in (("latest.json", entry), ("index.json", index)):
        p = OUT / name
        p.write_text(json.dumps(obj, indent=2))
        with open(p, "rb") as f:
            ftp.storbinary(f"STOR {name}", f)
    for e in old:  # tidy up videos older than KEEP_DAYS
        try:
            ftp.delete(e["video"])
        except Exception:
            pass
    ftp.quit()


def main():
    log(f"Making market news for {stamp}")
    news = get_news()
    log("Script: " + news["script"][:120] + "...")
    voice = OUT / f"voice-{stamp}.mp3"
    make_voice(news["script"], voice)
    log(f"Voice saved ({voice.stat().st_size // 1024} KB)")
    video = OUT / f"market-news-{stamp}.mp4"
    make_video(voice, video)
    log(f"Video saved ({video.stat().st_size // 1024} KB)")
    entry = {
        "date": stamp,
        "title": f"Market Close — {today:%B %d, %Y}",
        "video": video.name,
        "headlines": news["headlines"],
        "script": news["script"],
        "sources": news.get("sources", []),
    }
    (OUT / "latest.json").write_text(json.dumps(entry, indent=2))
    if os.environ.get("DRY_RUN") == "1":
        log("DRY_RUN=1, skipping upload")
        return
    upload(video, entry)
    log(f"Uploaded to {FTP_DIR}/{video.name}")


if __name__ == "__main__":
    main()
