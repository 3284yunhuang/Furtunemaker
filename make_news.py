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
import socket
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
ANCHOR = ROOT / "anchor.mp4" if (ROOT / "anchor.mp4").exists() else ROOT / "assets" / "anchor.mp4"

today = dt.datetime.now(zoneinfo.ZoneInfo("America/New_York")).date()
stamp = today.isoformat()


def log(msg):
    print(f"[news] {msg}", flush=True)


def fail(title, msg):
    """Show a clear error on the GitHub run page, then stop."""
    print(f"::error title={title}::{msg}", flush=True)
    print(f"[news] FAILED - {title}: {msg}", flush=True)
    sys.exit(1)


# ---------------------------------------------------------------- 0. checks before spending anything
def preflight():
    missing = [k for k in ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "FTP_HOST", "FTP_USER", "FTP_PASSWORD")
               if not os.environ.get(k, "").strip()]
    if missing:
        fail("Missing GitHub secret", "These secrets are empty or misspelled: " + ", ".join(missing)
             + ". Check Settings > Secrets and variables > Actions (names must match exactly).")
    if not ANCHOR.exists():
        fail("Missing anchor video", "anchor.mp4 was not found in the repository. Upload it next to make_news.py.")

    # ElevenLabs key + voice
    try:
        r = requests.get(f"https://api.elevenlabs.io/v1/voices/{VOICE_ID}",
                         headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"].strip()}, timeout=30)
    except requests.RequestException as e:
        fail("ElevenLabs unreachable", str(e))
    if r.status_code == 401:
        fail("ElevenLabs key rejected", "ELEVENLABS_API_KEY is wrong or lacks permission (needs Text to Speech and Voices read). "
             + r.text[:200])
    if r.status_code != 200:
        fail("ElevenLabs voice not available", f"Voice {VOICE_ID} (Riley) returned {r.status_code}. "
             "In ElevenLabs open Voice Library, find Riley, click 'Add to My Voices'. " + r.text[:200])
    log("ElevenLabs key and voice OK")

    # FTP login
    try:
        ftp = ftp_connect()
        ftp.quit()
    except Exception as e:
        fail("Hostinger FTP login failed", f"{type(e).__name__}: {e}. Check FTP_HOST (e.g. ftp.furtunemaker.com or the IP shown in hPanel), "
             "FTP_USER and FTP_PASSWORD.")
    log("Hostinger FTP login OK")


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
            "x-api-key": os.environ["ANTHROPIC_API_KEY"].strip(),
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
    if r.status_code != 200:
        detail = r.text[:300]
        if r.status_code == 401:
            fail("Claude API key rejected", "ANTHROPIC_API_KEY is wrong. Create a new key at platform.claude.com. " + detail)
        if "credit" in detail.lower():
            fail("Claude API has no credit", "Add credit under Billing at platform.claude.com. " + detail)
        fail(f"Claude API error {r.status_code}", detail)
    blocks = r.json()["content"]
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    m = re.search(r"<json>(.*?)</json>", text, re.S) or re.search(r"(\{.*\})", text, re.S)
    if not m:
        fail("Could not read Claude's reply", text[:300].replace("\n", " "))
    data = json.loads(m.group(1))
    assert data.get("script") and data.get("headlines"), "Claude reply is missing script/headlines"
    return data


# ---------------------------------------------------------------- 2. voice
def make_voice(script, path):
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}",
        params={"output_format": "mp3_44100_128"},
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"].strip(), "content-type": "application/json"},
        json={"text": script, "model_id": TTS_MODEL},
        timeout=300,
    )
    if r.status_code != 200:
        fail(f"ElevenLabs speech error {r.status_code}", r.text[:300])
    path.write_bytes(r.content)


# ---------------------------------------------------------------- 3. video
def make_video(voice, path):
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-stream_loop", "-1", "-i", str(ANCHOR),
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
    host = os.environ["FTP_HOST"].strip().removeprefix("ftp://").removeprefix("ftps://").rstrip("/")
    port = 21
    if host.count(":") == 1:  # "host:port"
        host, port = host.split(":")[0], int(host.split(":")[1])
    user, pw = os.environ["FTP_USER"].strip(), os.environ["FTP_PASSWORD"].strip("\r\n")
    # GitHub's servers have no IPv6, so always connect over IPv4.
    try:
        ip = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except socket.gaierror:
        raise OSError(f"'{host}' has no IPv4 address. Put the IPv4 'FTP IP' from Hostinger hPanel "
                      "(looks like 123.45.67.89) in the FTP_HOST secret")
    log(f"FTP connecting to {host} ({ip}) port {port}")
    last = None
    for tls in (True, False):
        try:
            ftp = ftplib.FTP_TLS(timeout=60) if tls else ftplib.FTP(timeout=60)
            ftp.connect(ip, port)
            ftp.login(user, pw)
            if tls:
                ftp.prot_p()
            ftp.set_pasv(True)
            return ftp
        except ftplib.error_perm as e:
            if str(e).startswith("530"):
                raise OSError(f"FTP username or password is wrong ({e})")
            last = e
        except Exception as e:
            last = e
    raise last


def ftp_cd(ftp, path):
    parts = [p for p in path.split("/") if p]
    # Some FTP accounts already start inside public_html; don't create public_html/public_html.
    if parts and parts[0] == "public_html":
        try:
            names = [n.rsplit("/", 1)[-1] for n in ftp.nlst()]
        except Exception:
            names = []
        if "public_html" not in names:
            log("FTP account already starts inside public_html")
            parts = parts[1:]
    for part in parts:
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
    if os.environ.get("DRY_RUN") != "1":
        preflight()
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
    try:
        upload(video, entry)
    except Exception as e:
        fail("Upload to Hostinger failed", f"{type(e).__name__}: {e}. If the folder is wrong, set variable FTP_DIR (now '{FTP_DIR}').")
    log(f"Uploaded to {FTP_DIR}/{video.name}")


if __name__ == "__main__":
    main()
