"""YouTube auto-upload for finished HypeCut clips.

OAuth 2.0 (installed app, loopback redirect) + the YouTube Data API v3
resumable upload. Credentials live in ~/.hypecut/ (same place as the rest of
HypeCut's config). Nothing leaves the machine except the clip itself.

How to get a free OAuth client ID/secret once (5 minutes):
  1. https://console.cloud.google.com/apis/library/youtube.googleapis.com -> Enable
  2. https://console.cloud.google.com/apis/credentials -> Create credentials
     -> OAuth client ID -> Application type: "Desktop app" -> Create.
  3. Copy the client ID and client secret into the app's Settings -> YouTube.
"""
from __future__ import annotations

import datetime as _dt
import json
import time
from pathlib import Path

HOME = Path.home() / ".hypecut"
CRED_FILE = HOME / "youtube_credentials.json"
TOKEN_FILE = HOME / "youtube_token.json"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
REDIRECT_PATH = "/oauth2callback"

# state -> Flow kept for the short time between opening the browser and the callback
_PENDING: dict[str, "object"] = {}


class YoutubeNotConfiguredError(Exception):
    pass


class YoutubeNotConnectedError(Exception):
    pass


# ------------------------------------------------------------------ helpers
def save_client_creds(client_id: str, client_secret: str) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    CRED_FILE.write_text(json.dumps({
        "client_id": (client_id or "").strip(),
        "client_secret": (client_secret or "").strip(),
    }), encoding="utf-8")


def load_client_creds() -> dict:
    try:
        d = json.loads(CRED_FILE.read_text(encoding="utf-8"))
        return {"client_id": d.get("client_id", ""), "client_secret": d.get("client_secret", "")}
    except (OSError, ValueError):
        return {"client_id": "", "client_secret": ""}


def client_config() -> dict:
    c = load_client_creds()
    if not c["client_id"] or not c["client_secret"]:
        raise YoutubeNotConfiguredError("Set your Google OAuth client ID and secret in Settings -> YouTube first.")
    return {"installed": {
        "client_id": c["client_id"],
        "client_secret": c["client_secret"],
        "auth_uri": AUTH_URI,
        "token_uri": TOKEN_URI,
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "redirect_uris": ["http://localhost"],
    }}


def _save_token(data: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_token() -> dict:
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _creds():
    """Return an authorized Credentials object, or None when not connected."""
    tok = _load_token()
    if not tok.get("token") or not tok.get("client_id"):
        return None
    from google.oauth2.credentials import Credentials
    return Credentials(
        token=tok.get("token"),
        refresh_token=tok.get("refresh_token"),
        token_uri=TOKEN_URI,
        client_id=tok.get("client_id"),
        client_secret=tok.get("client_secret"),
        scopes=tok.get("scopes") or SCOPES,
    )


def is_configured() -> bool:
    c = load_client_creds()
    return bool(c["client_id"] and c["client_secret"])


def is_connected() -> bool:
    return _creds() is not None


def _service():
    creds = _creds()
    if creds is None:
        raise YoutubeNotConnectedError("YouTube is not connected. Open Settings -> YouTube -> Connect.")
    from googleapiclient.discovery import build
    return build("youtube", "v3", credentials=creds)


# ------------------------------------------------------------------ OAuth flow
def start_connect(port: int) -> tuple[str, str]:
    """Prepare the OAuth flow and return (authorization_url, state)."""
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(client_config(), scopes=SCOPES)
    flow.redirect_uri = f"http://127.0.0.1:{port}{REDIRECT_PATH}"
    url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    _PENDING[state] = flow
    return url, state


def finish_connect(code: str, state: str) -> dict:
    """Exchange the callback code for tokens and store them."""
    flow = _PENDING.pop(state, None)
    if flow is None:
        raise YoutubeNotConnectedError("Authorization state expired. Try connecting again.")
    flow.fetch_token(code=code)
    c = flow.credentials
    _save_token({
        "token": c.token,
        "refresh_token": c.refresh_token,
        "token_uri": c.token_uri,
        "client_id": c.client_id,
        "client_secret": c.client_secret,
        "scopes": list(c.scopes),
    })
    return {}


def logout() -> None:
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()


def status() -> dict:
    """Public-safe status for the app."""
    connected = is_connected()
    chan = ""
    if connected:
        try:
            r = _service().channels().list(part="snippet", mine=True).execute()
            items = r.get("items") or []
            chan = (items[0].get("snippet") or {}).get("title", "") if items else ""
        except Exception:  # noqa: BLE001 - stale token etc.
            connected = False
            chan = ""
    return {"connected": connected, "channel": chan, "configured": is_configured(),
            "client_hint": _mask(load_client_creds()["client_id"])}


def _mask(client_id: str) -> str:
    return (client_id[:4] + "…" + client_id[-4:]) if len(client_id) > 10 else ""


# ------------------------------------------------------------------ scheduling
DEFAULT_SLOTS = ["12:00", "15:00", "18:00", "21:30"]


def parse_slots(slots: str | list[str] | None) -> list[str]:
    if slots is None:
        return list(DEFAULT_SLOTS)
    if isinstance(slots, str):
        slots = slots.split(",")
    return [s.strip() for s in slots if s.strip() and ":" in s.strip()]


def next_publish_at(slots: list[str], min_gap_min: int = 45,
                    after: _dt.datetime | None = None) -> _dt.datetime:
    """The earliest upcoming slot (local time) at least min_gap_min after `after`.

    `after` is the previous clip's publish time, so clips in one run spread out
    across the day instead of all going up at the same instant.
    """
    base = after or _dt.datetime.now().astimezone()
    horizon = base + _dt.timedelta(minutes=max(10, int(min_gap_min)))
    clean = parse_slots(slots)
    if not clean:
        return horizon  # no slots configured: schedule right after the gap
    zone = base.tzinfo
    for days in range(0, 8):
        day = base.date() + _dt.timedelta(days=days)
        for s in clean:
            hh, mm = s.split(":", 1)
            try:
                t = _dt.datetime(day.year, day.month, day.day, int(hh), int(mm), tzinfo=zone)
            except ValueError:
                continue
            if t > horizon:
                return t
    return horizon


# ------------------------------------------------------------------ metadata
def build_title(c: dict) -> str:
    t = (c.get("title") or c.get("hook") or c.get("id") or "HypeCut clip").strip()
    return t[:100]


def build_description(c: dict) -> str:
    lines = []
    if c.get("caption"):
        lines.append(str(c["caption"]).strip())
    tags = c.get("hashtags") or []
    if isinstance(tags, str):
        tags = [tags]
    tags = [f"#{str(t).lstrip('#')}" for t in tags if str(t).strip()]
    if tags:
        lines.append(" ".join(tags))
    if c.get("source"):
        lines += ["", f"Source: {c['source']}"]
    lines += ["", "Generated with HypeCut — AI viral clip editor."]
    return "\n".join(lines)[:4900]


def build_tags(c: dict) -> list[str]:
    tags = []
    raw = c.get("hashtags") or []
    if isinstance(raw, str):
        raw = [raw]
    for t in raw:
        s = str(t).strip().lstrip("#")
        if s:
            tags.append(s)
    if not tags:
        tags = ["gaming", "clips"]
    if "shorts" not in tags:
        tags.append("shorts")
    return tags[:500]


# ------------------------------------------------------------------ upload
def upload_clip(clip: dict, video_path: Path, visibility: str = "public",
                category: str = "20", schedule: bool = False, slots: list[str] | None = None,
                min_gap: int = 45, publish_after: _dt.datetime | None = None) -> dict:
    """Upload one clip.

    With schedule=True the video is uploaded as **scheduled** (YouTube's
    publishAt + private): YouTube makes it public automatically at the chosen
    slot time. Returns {'video_id', 'url', 'publish_at', ...}.
    """
    from googleapiclient.http import MediaFileUpload

    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(f"clip file not found: {path}")
    if visibility not in ("private", "unlisted", "public"):
        visibility = "public"
    service = _service()
    status = {"selfDeclaredMadeForKids": False}
    publish_at = None
    if schedule:
        publish_at = next_publish_at(parse_slots(slots), min_gap, after=publish_after)
        status["privacyStatus"] = "private"  # scheduled = private + publishAt
        status["publishAt"] = publish_at.isoformat()
    else:
        status["privacyStatus"] = visibility
    body = {
        "snippet": {
            "title": build_title(clip),
            "description": build_description(clip),
            "tags": build_tags(clip),
            "categoryId": str(category or "20"),
        },
        "status": status,
    }
    media = MediaFileUpload(str(path), mimetype="video/mp4", resumable=True,
                            chunksize=8 * 1024 * 1024)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    last_status = None
    while response is None:
        last_status, response = request.next_chunk()
    video_id = response["id"]
    return {
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "visibility": "scheduled" if schedule else visibility,
        "publish_at": publish_at.isoformat() if publish_at else None,
        "title": body["snippet"]["title"],
        "uploaded_at": round(time.time(), 2),
    }


def mark_uploaded(json_path: Path, info: dict) -> None:
    try:
        c = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        c = {}
    c["youtube"] = info
    json_path.write_text(json.dumps(c, indent=2, ensure_ascii=False), encoding="utf-8")


def upload_new_clips(out_dir: Path, visibility: str = "public", category: str = "20",
                     log: object | None = None, schedule: bool = False,
                     slots: list[str] | None = None, min_gap: int = 45) -> list[dict]:
    """Upload every clip_*.mp4 in out_dir that isn't marked uploaded yet.

    Returns [{file, ok, url?, error?}, ...]. Never raises on a single clip's
    failure so one bad upload doesn't stop the rest (or the caller).
    """
    def say(msg: str) -> None:
        print(f"[youtube] {msg}", flush=True)
        if log is not None:
            try:
                log.append(f"[youtube] {msg}")
            except Exception:  # noqa: BLE001
                pass

    if not is_connected():
        say("not connected — run through Settings -> YouTube first")
        return [{"ok": False, "error": "not connected"}]
    results = []
    prev_publish: _dt.datetime | None = None
    for js in sorted(Path(out_dir).glob("clip_*.json")):
        try:
            c = json.loads(js.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if c.get("youtube", {}).get("video_id"):
            continue
        mp4 = js.with_suffix(".mp4")
        if not mp4.is_file():
            continue
        try:
            say(f"uploading {mp4.name} ...")
            info = upload_clip(c, mp4, visibility=visibility, category=category,
                               schedule=schedule, slots=slots, min_gap=min_gap,
                               publish_after=prev_publish)
            mark_uploaded(js, info)
            results.append({"file": mp4.name, "ok": True, **info})
            if info.get("publish_at"):
                prev_publish = _dt.datetime.fromisoformat(info["publish_at"])
                say(f"done {info['url']} (scheduled {info['publish_at']})")
            else:
                say(f"done {info['url']}")
        except Exception as e:  # noqa: BLE001
            results.append({"file": mp4.name, "ok": False, "error": str(e)})
            say(f"failed {mp4.name}: {e}")
    return results

