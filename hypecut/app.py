"""HypeCut desktop app.

`hypecut-app` (or `hypecut app`) starts a small local server and opens the app in its own window
(pywebview) or, if that's not installed, in your browser. Everything runs on your computer.
Each clipping job runs the normal CLI in a background process, so the app stays responsive.
"""
from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
WEB = HERE / "web"
HOME = Path.home() / ".hypecut"
CONFIG = HOME / "config.json"
DEFAULT_OUT = Path.home() / "Desktop" / "HypeCut clips" if (Path.home() / "Desktop").exists() else Path.home() / "HypeCut clips"


# ---------------------------------------------------------------- config
def load_config() -> dict:
    cfg = {"gemini_key": "", "out_dir": str(DEFAULT_OUT), "whisper_model": "base", "whisper_cpp": "",
           "whisper_cpp_model": "", "language": "", "max_clips": 6, "max_clip": 30, "z": 3.0,
           "caption_style": "bold", "layout": "auto", "hinglish": False, "effects": True,
           "youtube_client_id": "", "youtube_client_secret": "", "youtube_visibility": "public",
           "youtube_category": "20", "youtube_auto_upload": True,
           "youtube_schedule": True, "youtube_slots": "12:00,15:00,18:00,21:30",
           "youtube_min_gap": 45}
    try:
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError):
        pass
    # pick up env vars set outside the app (first run)
    cfg["whisper_cpp"] = cfg["whisper_cpp"] or os.environ.get("HYPECUT_WHISPER_CPP", "")
    if not cfg["whisper_cpp_model"] and os.environ.get("HYPECUT_WHISPER_MODEL", "").endswith(".bin"):
        cfg["whisper_cpp_model"] = os.environ["HYPECUT_WHISPER_MODEL"]
    cfg["gemini_key"] = cfg["gemini_key"] or os.environ.get("GEMINI_API_KEY", "")
    return cfg


def save_config(cfg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def public_config(cfg: dict) -> dict:
    c = dict(cfg)
    k = c.pop("gemini_key", "")
    c["gemini_set"] = bool(k)
    c["gemini_hint"] = (k[:4] + "..." + k[-4:]) if len(k) > 10 else ""
    c["youtube_client_secret"] = ""  # never send secrets to the web view
    c["youtube_secret_set"] = bool(cfg.get("youtube_client_secret"))
    cid = c.get("youtube_client_id", "")
    c["youtube_client_hint"] = (cid[:4] + "..." + cid[-4:]) if len(cid) > 10 else ""
    return c


# ---------------------------------------------------------------- jobs
class Job:
    def __init__(self, kind: str, label: str, out: Path, cmd: list[str], env: dict):
        self.id = uuid.uuid4().hex[:8]
        self.kind, self.label, self.out = kind, label, out
        self.started = time.time()
        self.log: list[str] = []
        self.status = "running"
        out.mkdir(parents=True, exist_ok=True)
        (HOME / "work").mkdir(parents=True, exist_ok=True)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(cmd, cwd=str(HOME / "work"), env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                     bufsize=1, creationflags=flags)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.rstrip()
            if line:
                # live mode prints a status line with \r; keep the latest part
                self.log.append(line.split("\r")[-1])
                self.log = self.log[-400:]
        code = self.proc.wait()
        try:  # charge credits for the clips this run actually made
            from . import control

            control.spend(self.kind, len(list(self.out.rglob("clip_*.mp4"))))
        except Exception:  # noqa: BLE001
            pass
        if self.status == "stopping":
            self.status = "stopped"
        elif code == 0:
            self.status = "done"
            # auto-upload to YouTube when the setting is on and we're connected
            try:
                cfg = load_config()
                if cfg.get("youtube_auto_upload"):
                    from . import youtube
                    if not youtube.is_connected():
                        self.log.append("[youtube] auto-upload is ON but YouTube is not connected (Settings -> YouTube -> Connect)")
                    else:
                        def _upload_all():
                            try:
                                r = youtube.upload_new_clips(self.out,
                                                             visibility=cfg.get("youtube_visibility", "public"),
                                                             category=cfg.get("youtube_category", "20"),
                                                             schedule=bool(cfg.get("youtube_schedule")),
                                                             slots=cfg.get("youtube_slots", ""),
                                                             min_gap=int(cfg.get("youtube_min_gap") or 45),
                                                             log=self.log)
                                self.log.append(f"[youtube] uploaded {sum(1 for x in r if x.get('ok'))}/{len(r)} clip(s)")
                            except Exception as e:  # noqa: BLE001
                                self.log.append(f"[youtube] auto-upload failed: {e}")
                        threading.Thread(target=_upload_all, daemon=True).start()
            except Exception:  # noqa: BLE001
                pass
        else:
            self.status = "error"

    def stop(self):
        if self.status == "running":
            self.status = "stopping"
            if self.kind == "channel":
                (self.out / "STOP").write_text("stop", encoding="utf-8")  # live loop exits cleanly
                threading.Timer(15, lambda: self.proc.poll() is None and self.proc.kill()).start()
            else:
                self.proc.kill()

    def progress(self) -> tuple[int, str]:
        """Turn the log into a % and a plain-English stage."""
        import re

        if self.status == "done":
            return 100, "Done"
        if self.status in ("error", "stopped"):
            err = next((l for l in reversed(self.log) if l.startswith("[error]")), "")
            return 100, (err[8:] if err else ("Stopped" if self.status == "stopped" else "Something went wrong"))
        pct, stage = 3, "Starting ..."
        estimate = ""
        n = 0
        rendered = 0
        for line in self.log:
            if line.startswith("[estimate]"):
                estimate = line[11:]
            elif line.startswith("[demo]"):
                pct, stage = 5, "Building the demo stream ..."
            elif line.startswith("[chat]") or line.startswith("[vod]"):
                pct, stage = max(pct, 8), "Downloading the video / chat ..."
            elif line.startswith("[analyse]"):
                pct, stage = max(pct, 12), "Listening to the video ..."
            elif line.startswith("[mode]"):
                pct, stage = max(pct, 15), line[7:].split(":")[0].title()
            elif m := re.match(r"\[whisper\] transcribing (\d+)-(\d+) min of (\d+)", line):
                a, b, tot = map(int, m.groups())
                pct, stage = max(pct, 15 + int(30 * b / max(tot, 1))), f"Transcribing the video ({b}/{tot} min) ..."
            elif m := re.match(r"\[spikes\] picked (\d+)", line):
                n = int(m.group(1))
                pct, stage = max(pct, 45), f"Found {n} moments"
            elif m := re.match(r"\[progress\] analysing clip (\d+)/(\d+)", line):
                i, n = map(int, m.groups())
                pct, stage = max(pct, 45 + int(30 * (i - 1) / n)), f"Finding the best cut in clip {i} of {n} ..."
            elif line.startswith("[render] rendering"):
                pct, stage = max(pct, 75), "Making the vertical clips ..."
            elif line.startswith("[render] done"):
                rendered += 1
                pct = max(pct, 75 + int(23 * rendered / max(n, 1)))
                stage = f"Rendered {rendered} of {n} clips ..."
            elif line.startswith("[live] watching"):
                pct, stage = 50, "Watching live chat ..."
            elif line.startswith("[live] SPIKE"):
                stage = "Chat exploded: clipping it ..."
        pct = min(pct, 99)
        elapsed = time.time() - self.started
        if self.kind != "channel":
            if pct >= 20 and elapsed > 20:
                left = elapsed * (100 - pct) / pct / 60
                stage += f"  ·  about {max(1, round(left))} min left"
            elif estimate:
                stage += f"  ·  {estimate}"
        return pct, stage

    def as_dict(self):
        return {"id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
                "out": str(self.out), "started": self.started, "log": self.log[-60:],
                "pct": self.progress()[0], "stage": self.progress()[1],
                "clips": len(list(self.out.rglob("clip_*.mp4"))) if self.out.exists() else 0}


JOBS: dict[str, Job] = {}
WATCHER = None  # live alerts (started in main)


def _auto_clip(channel: dict) -> None:
    """Called by the watcher when a watched channel goes live and auto-clip is on."""
    url = channel.get("live_url") or channel["url"]
    if any(j.kind == "channel" and j.status == "running" and j.label == url for j in JOBS.values()):
        return
    start_job({"kind": "channel", "url": url})


def start_job(req: dict) -> Job:
    from . import control

    cfg = load_config()
    kind = req.get("kind")
    want = int((req.get("options") or {}).get("max_clips") or cfg["max_clips"])
    ok, why, allowed = control.check(kind, want)
    if not ok:
        raise PermissionError(why)
    root = Path(cfg["out_dir"])
    stamp = time.strftime("%Y-%m-%d_%H-%M")
    args: list[str] = []
    if kind == "video":
        url = req["url"].strip()
        args = [url, "--mode", "vod"]
        label = url
    elif kind == "channel":
        url = req["url"].strip()
        args = [url, "--mode", "live"]
        label = url
    elif kind == "file":
        args = [req["video"]] + (["--chat", req["chat"]] if req.get("chat") else [])
        label = Path(req["video"]).name
    elif kind == "demo":
        args = ["--demo", "--language", "en"]
        label = "Demo stream"
    else:
        raise ValueError("unknown job type")
    slug = "".join(ch if ch.isalnum() else "_" for ch in label.split("/")[-1])[:40].strip("_") or kind
    out = root / f"{stamp}_{kind}_{slug}"
    args += ["--out", str(out)]

    o = {**cfg, **(req.get("options") or {})}
    o["max_clips"] = allowed
    args += ["--max-clips", str(int(o["max_clips"])), "--max-clip", str(float(o["max_clip"])), "--z", str(float(o["z"])),
             "--caption-style", o["caption_style"], "--layout", o["layout"]]
    if o.get("language") and kind != "demo":
        args += ["--language", o["language"]]
    if o.get("hinglish"):
        args.append("--hinglish")
    if not o.get("effects", True):
        args.append("--no-effects")
    if o.get("allow_repeats"):
        args.append("--allow-repeats")

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    if cfg.get("gemini_key"):
        env["GEMINI_API_KEY"] = cfg["gemini_key"]
    if cfg.get("whisper_cpp") and cfg.get("whisper_cpp_model"):
        env["HYPECUT_WHISPER_CPP"] = cfg["whisper_cpp"]
        env["HYPECUT_WHISPER_MODEL"] = cfg["whisper_cpp_model"]
    else:
        env.pop("HYPECUT_WHISPER_CPP", None)
        env["HYPECUT_WHISPER_MODEL"] = cfg.get("whisper_model") or "base"
    job = Job(kind, label, out, [sys.executable, "-u", "-m", "hypecut", *args], env)
    JOBS[job.id] = job
    control.record_run(kind)
    return job


def _to_trash(path: Path) -> None:
    """Recycle Bin on Windows; elsewhere a .hypecut-trash folder next to the clips (recoverable)."""
    if os.name == "nt":
        ps = ("Add-Type -AssemblyName Microsoft.VisualBasic;"
              f"[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile('{str(path).replace(chr(39), chr(39) * 2)}',"
              "'OnlyErrorDialogs','SendToRecycleBin')")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode == 0:
            return
    trash = Path(load_config()["out_dir"]) / ".hypecut-trash"
    trash.mkdir(parents=True, exist_ok=True)
    path.rename(trash / f"{int(time.time())}_{path.name}")


def delete_clip(rel: str) -> int:
    root = Path(load_config()["out_dir"]).resolve()
    mp4 = (root / rel).resolve()
    if root not in mp4.parents or not mp4.name.startswith("clip_") or mp4.suffix != ".mp4":
        raise ValueError("not a HypeCut clip")
    n = 0
    for ext in (".mp4", ".json", ".md", ".ass"):
        f = mp4.with_suffix(ext)
        if f.exists():
            _to_trash(f)
            n += 1
    return n


REPO = HERE.parent  # the project folder (only meaningful on the owner's laptop)


def dev_api(path: str, body: dict) -> dict:
    """Developer tab (owner's laptop only): edit control.json and publish it to GitHub."""
    from . import control

    if not control.developer():
        raise PermissionError("Developer mode is only on the owner's copy.")
    f = REPO / "control.json"
    if path == "/api/dev/get":
        data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else dict(control.DEFAULT)
        remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=REPO, capture_output=True, text=True)
        return {"control": data, "github": remote.stdout.strip(), "control_url": control.CONTROL_URL}
    if path == "/api/dev/save":
        data = body["control"]
        f.write_text(json.dumps(data, indent=2, ensure_ascii=False) + chr(10), encoding="utf-8")
        if control.CACHE.exists():  # see the change in this app right away
            control.CACHE.write_text(json.dumps({"_fetched": time.time(), "data": data}), encoding="utf-8")
        return {"ok": True, "message": "Saved on this laptop."}
    if path == "/api/dev/publish":
        remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=REPO, capture_output=True, text=True)
        if remote.returncode != 0:
            return {"ok": False, "message": "Not on GitHub yet. Publish the repo first; then this button updates every user's app."}
        subprocess.run(["git", "add", "control.json"], cwd=REPO, check=True)
        subprocess.run(["git", "commit", "-m", "Update app control settings"], cwd=REPO, capture_output=True)
        r = subprocess.run(["git", "push"], cwd=REPO, capture_output=True, text=True)
        ok = r.returncode == 0
        return {"ok": ok, "message": "Published: every user's app updates within an hour." if ok else r.stderr[-300:]}
    if path == "/api/dev/preview":
        cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
        cfg["preview_user"] = bool(body.get("on"))
        save_config({**load_config(), **cfg})
        return control.status()
    raise ValueError("unknown developer action")


def list_clips() -> list[dict]:
    root = Path(load_config()["out_dir"])
    clips = []
    if not root.exists():
        return clips
    for js in root.glob("*/**/clip_*.json"):
        if ".hypecut-trash" in js.parts:
            continue
        try:
            c = json.loads(js.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        mp4 = js.with_suffix(".mp4")
        if not mp4.exists():
            continue
        rel = mp4.relative_to(root).as_posix()
        clips.append({"rel": rel, "folder": js.parent.relative_to(root).as_posix(), "id": c.get("id"),
                      "title": c.get("title"), "hook": c.get("hook"), "caption": c.get("caption", ""),
                      "hashtags": c.get("hashtags", []), "viral": c.get("viral_score"), "z": c.get("z"),
                      "duration": c.get("duration"), "transcript": c.get("transcript", ""),
                      "flow": c.get("flow_prompt", ""), "youtube": c.get("youtube"),
                      "mtime": mp4.stat().st_mtime})
    clips.sort(key=lambda c: (-c["mtime"], -(c["viral"] or 0)))
    return clips


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _reqlog(self, note: str = ""):
        """Append every request to ~/.hypecut/requests.log for debugging."""
        try:
            with open(HOME / "requests.log", "a", encoding="utf-8") as f:
                f.write(f"{time.time():.0f} {self.command} {self.path} -> {note}\n")
        except Exception:  # noqa: BLE001
            pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _html(self, text: str, code: int = 200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path):
        if not path.is_file():
            return self._json({"error": "not found"}, 404)
        size = path.stat().st_size
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else 0
            end = int(b) if b else size - 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (ConnectionError, OSError):
                pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        self._reqlog()
        if p in ("/", "/index.html"):
            return self._file(WEB / "index.html")
        if p.startswith("/static/"):
            f = (WEB / p[8:]).resolve()
            return self._file(f) if WEB.resolve() in f.parents else self._json({}, 404)
        if p == "/oauth2callback":  # Google redirects here after the OAuth consent
            from . import youtube

            q = urllib.parse.parse_qs(u.query)
            err = q.get("error", [""])[0]
            if err:
                return self._html(f"<h2>Authorization failed ({err})</h2><p>Close this tab and try again.</p>")
            code = q.get("code", [""])[0]
            state = q.get("state", [""])[0]
            try:
                youtube.finish_connect(code, state)
                html = ("<body style='font-family:system-ui;background:#0b0b0d;color:#fff;"
                        "display:grid;place-items:center;height:100vh'>"
                        "<div style='text-align:center'><h2 style='color:#3ddc97'>✓ Connected to YouTube!</h2>"
                        "<p>You can close this tab and go back to HypeCut.</p></div></body>")
            except Exception as e:  # noqa: BLE001
                html = f"<h2>Connection failed</h2><p>{e}</p>"
            return self._html(html)
        if p == "/api/youtube/status":
            from . import youtube

            return self._json(youtube.status())
        if p == "/api/state":
            from . import control

            from . import watch

            wd = watch.load()
            return self._json({"config": public_config(load_config()), "account": control.status(),
                               "alerts": [a for a in wd["alerts"] if not a.get("seen")][:5],
                               "live_now": sum(1 for c in wd["channels"] if c.get("live")),
                               "jobs": [j.as_dict() for j in sorted(JOBS.values(), key=lambda j: -j.started)]})
        if p == "/api/clips":
            return self._json(list_clips())
        if p.startswith("/clips/"):
            root = Path(load_config()["out_dir"]).resolve()
            f = (root / urllib.parse.unquote(p[7:])).resolve()
            return self._file(f) if root in f.parents else self._json({}, 404)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        self._reqlog()
        try:
            if p == "/api/run":
                return self._json(start_job(self._body()).as_dict())
            if p.startswith("/api/stop/"):
                j = JOBS.get(p.rsplit("/", 1)[-1])
                if j:
                    j.stop()
                return self._json({"ok": True})
            if p == "/api/config":
                cfg = load_config()
                new = self._body()
                # allow pasting the whole client_secret_*.json (Google's downloaded file) into
                # either YouTube field: parse it and fill both values
                for f in ("youtube_client_id", "youtube_client_secret"):
                    v = new.get(f, "")
                    if isinstance(v, str) and v.strip().startswith("{") and "client_secret" in v:
                        try:
                            j = json.loads(v)
                            inst = j.get("installed") or j
                            if inst.get("client_id"):
                                new["youtube_client_id"] = inst["client_id"]
                            if inst.get("client_secret"):
                                new["youtube_client_secret"] = inst["client_secret"]
                        except ValueError:
                            pass
                for k, v in new.items():
                    if k == "gemini_key" and v == "__keep__":
                        continue
                    if k in cfg:
                        cfg[k] = v
                save_config(cfg)
                if "youtube_client_id" in new or "youtube_client_secret" in new:
                    from . import youtube

                    youtube.save_client_creds(cfg.get("youtube_client_id", ""),
                                             cfg.get("youtube_client_secret", ""))
                return self._json(public_config(cfg))
            if p == "/api/youtube/connect":
                from . import youtube

                cfg = load_config()
                if not cfg.get("youtube_client_id") or not cfg.get("youtube_client_secret"):
                    self._reqlog("connect: missing creds")
                    return self._json({"error": "Set your Google OAuth client ID and secret in Settings -> YouTube first."}, 400)
                try:
                    url, state = youtube.start_connect(self.server.server_address[1])
                except Exception as e:  # noqa: BLE001
                    self._reqlog(f"connect: start_connect failed: {e}")
                    raise
                # open the system browser from the server: popup blockers in the app window
                # can silently swallow window.open(), which users read as "nothing happened"
                import threading as _t

                import webbrowser as _wb

                _t.Thread(target=lambda: _wb.open(url), daemon=True).start()
                self._reqlog(f"connect: ok state={state[:8]}")
                return self._json({"url": url, "state": state})
            if p == "/api/youtube/logout":
                from . import youtube

                youtube.logout()
                return self._json({"ok": True})
            if p == "/api/youtube/upload":
                from . import youtube

                cfg = load_config()
                b = self._body()
                rel = b.get("rel", "")
                root = Path(load_config()["out_dir"]).resolve()
                mp4 = (root / rel).resolve()
                if root not in mp4.parents or mp4.suffix != ".mp4" or not mp4.name.startswith("clip_"):
                    raise ValueError("not a HypeCut clip")
                js = mp4.with_suffix(".json")
                clip = json.loads(js.read_text(encoding="utf-8")) if js.exists() else {"id": mp4.stem, "title": mp4.stem}
                info = youtube.upload_clip(clip, mp4,
                                           visibility=cfg.get("youtube_visibility", "public"),
                                           category=cfg.get("youtube_category", "20"),
                                           schedule=bool(cfg.get("youtube_schedule")),
                                           slots=cfg.get("youtube_slots", ""),
                                           min_gap=int(cfg.get("youtube_min_gap") or 45))
                if js.exists():
                    youtube.mark_uploaded(js, info)
                return self._json({"youtube": info})
            if p == "/api/open":
                raw = self._body().get("path") or ""
                root = Path(load_config()["out_dir"])
                target = Path(raw) if raw and Path(raw).is_absolute() else root / raw
                target.mkdir(parents=True, exist_ok=True)
                if os.name == "nt":
                    os.startfile(str(target))  # noqa: S606
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(target)])
                else:
                    subprocess.Popen(["xdg-open", str(target)])
                return self._json({"ok": True})
            if p.startswith("/api/dev"):
                return self._json(dev_api(p, self._body()))
            if p == "/api/delete":
                return self._json({"deleted": delete_clip(self._body().get("rel", ""))})
            if p == "/api/signup":
                from . import control

                b = self._body()
                if "@" not in (b.get("email") or ""):
                    return self._json({"error": "Enter a valid email"}, 400)
                control.signup(b.get("name", ""), b["email"], b.get("instagram", ""))
                return self._json(control.status())
            if p.startswith("/api/watch/"):
                from . import watch

                b = self._body()
                act = p.rsplit("/", 1)[-1]
                if act == "add":
                    if not (b.get("url") or "").strip():
                        return self._json({"error": "Paste a channel link first"}, 400)
                    watch.add(b["url"], bool(b.get("auto_clip")))
                    WATCHER and WATCHER.check_now()
                elif act == "remove":
                    watch.remove(b["url"])
                elif act == "auto":
                    watch.set_auto(b["url"], bool(b.get("on")))
                elif act == "check":
                    WATCHER and WATCHER.check_now()
                elif act == "seen":
                    d = watch.load()
                    for a in d["alerts"]:
                        a["seen"] = True
                    watch.save(d)
                return self._json(watch.load())
            if p == "/api/clipboard":
                return self._json({"text": read_clipboard()})
            if p == "/api/pick":
                kind = self._body().get("kind", "video")
                return self._json({"path": pick_file(kind)})
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)}, 400)
        return self._json({"error": "not found"}, 404)


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # never share a port with another HypeCut copy
    daemon_threads = True


_WINDOW = None


def read_clipboard() -> str:
    """Clipboard text for the Paste buttons (works even where the web view blocks clipboard reads)."""
    try:
        if os.name == "nt":
            r = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                               capture_output=True, text=True, timeout=10)
        elif sys.platform == "darwin":
            r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=10)
        else:
            r = subprocess.run(["xclip", "-selection", "clipboard", "-o"], capture_output=True, text=True, timeout=10)
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def pick_file(kind: str) -> str:
    """Native file picker (pywebview window), else tkinter."""
    types = ("Video files (*.mp4;*.mkv;*.mov;*.ts;*.webm)", "All files (*.*)") if kind == "video" else \
        ("Chat logs (*.jsonl;*.json;*.csv;*.txt)", "All files (*.*)")
    if _WINDOW is not None:
        import webview  # type: ignore

        res = _WINDOW.create_file_dialog(webview.OPEN_DIALOG, file_types=types)
        return res[0] if res else ""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename()
        root.destroy()
        return path or ""
    except Exception:  # noqa: BLE001
        return ""


def main(port: int = 7860, browser: bool = False, headless: bool = False) -> int:
    global _WINDOW
    server = None
    for p in range(port, port + 20):
        try:
            server = Server(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if server is None:
        print("No free port for the HypeCut app.")
        return 1
    url = f"http://127.0.0.1:{port}/"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    global WATCHER
    from .watch import Watcher

    WATCHER = Watcher(on_live=_auto_clip)
    print(f"HypeCut app running at {url}")
    if headless:  # server only (for testing / remote use)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0
    try:
        if browser:
            raise ImportError
        import webview  # type: ignore

        webview.settings["SHOW_DEFAULT_MENUS"] = True   # right-click Paste / Copy works
        webview.settings["ALLOW_DOWNLOADS"] = True      # Download button saves the MP4
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        _WINDOW = webview.create_window("HypeCut - AI viral clips auto-editor", url, width=1280, height=860,
                                        min_size=(900, 640), maximized=True, background_color="#0b0b0d")
        webview.start()
    except ImportError:
        webbrowser.open(url)
        print("Close this window to quit HypeCut.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    for j in JOBS.values():
        j.stop()
    server.shutdown()
    return 0


def make_shortcut() -> int:
    """Put a HypeCut icon on the Desktop (Windows: .lnk with the logo; macOS/Linux: a launcher script)."""
    desk = Path.home() / "Desktop"
    desk.mkdir(exist_ok=True)
    exe = Path(sys.executable).with_name("pythonw.exe")
    exe = exe if exe.exists() else Path(sys.executable)
    if os.name == "nt":
        lnk = desk / "HypeCut.lnk"
        ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
              f"$s.TargetPath='{exe}';$s.Arguments='-m hypecut app';"
              f"$s.WorkingDirectory='{Path.home()}';$s.IconLocation='{WEB / 'icon.ico'}';"
              "$s.Description='HypeCut - AI viral clips auto-editor';$s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
        print(f"Created {lnk}")
    else:
        sh = desk / ("HypeCut.command" if sys.platform == "darwin" else "HypeCut.sh")
        sh.write_text(f'#!/bin/sh\n"{sys.executable}" -m hypecut app\n', encoding="utf-8")
        sh.chmod(0o755)
        print(f"Created {sh}")
    return 0


def run_app():
    try:
        code = main(browser="--browser" in sys.argv, headless="--headless" in sys.argv)
    except Exception:  # noqa: BLE001 - desktop app has no console: keep a crash log
        import traceback

        HOME.mkdir(parents=True, exist_ok=True)
        (HOME / "app_error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    sys.exit(code)


if __name__ == "__main__":
    run_app()
