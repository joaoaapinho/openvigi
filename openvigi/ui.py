"""
UI behind openvigi ui, served at http://127.0.0.1:8080.
"""

import json
import shutil
import subprocess
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import cv2

from . import config, notify
from .monitor import INTERVAL, save_snapshot, watch
from .sources import VIDEO_TYPES, SnapshotSource, SourceError, open_source

PAGE = Path(__file__).parent / "ui.html"
LOGO = notify.LOGO
ALERTS = Path("alerts")
UPLOADS = Path.home() / ".cache" / "openvigi" / "uploads"


class LiveView:
    """Wraps a camera so the page can show live video between checks.

    A thread reads the camera a few times a second and passes each frame to on_frame for
    the live view. When watch() asks for a frame, it gets the newest one.
    """
    live = True

    def __init__(self, source, on_frame):
        self.source, self.on_frame = source, on_frame
        self.fps = 1 if isinstance(source, SnapshotSource) else 5  # don't hammer snapshot URLs
        self.latest = None  # (frame, when it arrived)
        self.error = "no frame from the camera yet"
        self.got_frame = threading.Event()
        self.stopped = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stopped.is_set():
            try:
                frame = self.source.read()
            except SourceError as e:
                self.error = str(e)
                self.stopped.wait(1)
                continue
            self.latest = (frame, time.monotonic())
            self.on_frame(frame.jpeg)
            self.got_frame.set()
            self.stopped.wait(1 / self.fps)

    def read(self):
        """Return the newest frame, as long as it's under 10 seconds old."""
        self.got_frame.wait(10)
        if self.latest and time.monotonic() - self.latest[1] < 10:
            return self.latest[0]
        raise SourceError(self.error)

    def close(self):
        self.stopped.set()
        self.source.close()


class FileView:
    """Wraps a video file so the page shows each frame as it gets checked."""
    live = False

    def __init__(self, source, on_frame):
        self.source, self.on_frame = source, on_frame

    def read(self):
        frame = self.source.read()
        if frame:
            self.on_frame(frame.jpeg)
        return frame

    def close(self):
        self.source.close()


class Runner:
    """Starts and stops the watch() loop for the page, and keeps what the page displays."""

    def __init__(self):
        self.thread = None
        self.stop_event = threading.Event()
        self.source_name = ""
        self.telegram = False
        self.frame = None  # newest JPEG, for the live view
        self.checks = deque(maxlen=40)
        self.log = deque(maxlen=40)
        self.alert_info = {}  # snapshot file name: "situation confidence"

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def show(self, jpeg):
        """Put a frame in the live view."""
        self.frame = jpeg

    def note(self, message):
        """Add a line to the Messages box (and the terminal)."""
        print(message)
        self.log.appendleft(f"{datetime.now():%H:%M:%S}  {message}")

    def start(self, spec, telegram):
        """Open the source and start watching it. Stops whatever was running first."""
        self.stop()
        source = open_source(spec, INTERVAL)
        view = LiveView(source, self.show) if source.live else FileView(source, self.show)
        self.stop_event = threading.Event()
        self.source_name, self.telegram = source.name, telegram
        self.checks.clear()
        self.thread = threading.Thread(target=self._run, args=(view, self.stop_event), daemon=True)
        self.thread.start()
        self.note(f"watching {source.name}" + (" - alerts go to Telegram" if telegram else ""))

    def _run(self, view, stop):
        try:
            watch(view, on_result=self.on_result, on_alert=self.on_alert, stop=stop, log=self.note)
        except BaseException as e:  # e.g. the Jev key was deleted mid-run, which raises SystemExit
            self.note(f"stopped: {e}")
        finally:
            view.close()

    def stop(self):
        """Stop watching. Waits for the check in progress to finish."""
        if self.running:
            self.stop_event.set()
            self.thread.join(timeout=30)
            self.note("stopped")
        self.thread = None

    def on_result(self, frame, result):
        self.checks.appendleft({key: result[key] for key in ("threat", "confidence", "situation", "description", "latency_s")}
                               | {"when": frame.when})

    def on_alert(self, frame, result):
        """Save the snapshot and, if the page asked for it, send it to Telegram."""
        path = save_snapshot(frame, result, ALERTS)
        self.alert_info[path.name] = f"{result['situation']} {result['confidence']:.2f}"
        self.note(f"ALERT {result['situation']} ({result['confidence']:.2f}) - saved {path}")
        if self.telegram:
            try:
                notify.send_alert(frame, result)
                self.note("alert sent to Telegram")
            except RuntimeError as e:
                self.note(f"could not send to Telegram: {e}")

    def state(self):
        """Everything the page needs to redraw itself. It asks for this once a second."""
        settings = {key: {"set": bool(config.get(key)), "from": config.where(key)} for key in config.KEYS}
        snapshots = sorted(ALERTS.glob("*.jpg"), reverse=True)[:12] if ALERTS.is_dir() else []
        return {
            "running": self.running,
            "source": self.source_name,
            "telegram_ready": notify.ready(),
            "checks": list(self.checks),
            "log": list(self.log),
            "settings": settings,
            "alerts": [{"file": p.name, "when": snapshot_time(p), "info": self.alert_info.get(p.name, "")} for p in snapshots],
        }


def snapshot_time(path):
    """Turn a snapshot's file name back into a readable date, e.g. "05 Oct 21:59:26"."""
    try:
        return f"{datetime.strptime(path.stem[:15], '%Y%m%d_%H%M%S'):%d %b %H:%M:%S}"
    except ValueError:
        return path.stem


def find_webcams(limit=6):
    """List the webcams that actually return a picture, e.g. [{"index": 0, "label": "Integrated Camera (0)"}].

    Infrared cameras used for face login show up here too; they give grayscale frames.
    """
    cams = []
    for i in range(limit):
        cap = cv2.VideoCapture(i)
        ok = cap.isOpened() and cap.read()[0]
        cap.release()
        if ok:
            name_file = Path(f"/sys/class/video4linux/video{i}/name")  # Linux only, gives a friendlier label
            label = name_file.read_text().strip() if name_file.exists() else ""
            cams.append({"index": i, "label": f"{label} ({i})" if label else f"Webcam {i}"})
    return cams


class Handler(BaseHTTPRequestHandler):
    """Answers the page's requests. serve() gives it the Runner to work with."""
    runner = None
    webcams = None  # looked up on first use, and again when the page's Refresh button is pressed

    def log_message(self, *args):
        pass  # keep the terminal for OpenVigi's own messages

    def send(self, body, content_type="application/json", status=200):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def error(self, message, status=400):
        self.send({"error": message}, status=status)

    def body_json(self):
        """The request body, parsed as JSON. Read once and kept, since the stream can't be re-read."""
        if not hasattr(self, "_body"):
            length = int(self.headers.get("Content-Length") or 0)
            self._body = json.loads(self.rfile.read(length) or b"{}")
        return self._body

    def from_this_computer(self):
        """Refuse requests that could come from another website open in the same browser.

        Checking the Host header blocks DNS rebinding. Requiring our own header on POSTs
        blocks cross-site form posts, because a browser won't let another site add it.
        """
        host = self.headers.get("Host", "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost") and (self.command == "GET" or self.headers.get("X-OpenVigi") == "1")

    def do_GET(self):
        if not self.from_this_computer():
            return self.error("forbidden", 403)
        path = self.path.split("?")[0]
        if path == "/":
            self.send(PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/logo.png":
            self.send(LOGO.read_bytes(), "image/png")
        elif path == "/api/state":
            self.send(self.runner.state())
        elif path == "/api/webcams":
            if Handler.webcams is None or ("refresh" in self.path and not self.runner.running):
                Handler.webcams = find_webcams()
            self.send(Handler.webcams)
        elif path == "/live.mjpg":
            self.stream()
        elif path.startswith("/alerts/"):
            file = ALERTS / Path(unquote(path[len("/alerts/"):])).name  # .name strips any ../
            if file.suffix == ".jpg" and file.is_file():
                self.send(file.read_bytes(), "image/jpeg")
            else:
                self.error("not found", 404)
        else:
            self.error("not found", 404)

    def do_POST(self):
        if not self.from_this_computer():
            return self.error("forbidden", 403)
        routes = {
            "/api/test": self.test_camera,
            "/api/start": self.start,
            "/api/stop": self.stop,
            "/api/upload": self.upload,
            "/api/settings": self.save_settings,
            "/api/telegram/chats": self.telegram_chats,
            "/api/telegram/test": self.telegram_test,
        }
        route = routes.get(self.path)
        if not route:
            return self.error("not found", 404)
        try:
            if route != self.upload:
                self.body_json()  # read the body even if the route ignores it: closing with unread data resets the connection
            route()
        except (SourceError, RuntimeError) as e:
            self.error(str(e))

    def stream(self):
        """Serve the live view as an MJPEG stream, which an <img> tag can show directly."""
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = None
        try:
            while True:
                jpeg = self.runner.frame
                if jpeg is not None and jpeg is not last:
                    last = jpeg
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpeg) + jpeg + b"\r\n")
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the page was closed

    def test_camera(self):
        """Grab one frame from the chosen source and send it back, so the user can see it works."""
        if self.runner.running:
            return self.error("Stop watching first - the camera is in use")
        source = open_source(self.body_json()["source"], INTERVAL)
        try:
            frame = source.read()
        finally:
            source.close()
        if frame is None:
            return self.error("that video has no frames")
        self.runner.show(frame.jpeg)
        self.send(frame.jpeg, "image/jpeg")

    def start(self):
        """Start watching, once the settings it needs are in place."""
        body = self.body_json()
        if not config.get("JEV_API_KEY"):
            return self.error("Add your Jev API key in Settings first")
        if body.get("telegram") and not notify.ready():
            return self.error("Set up Telegram in Settings first, or untick 'Send alerts to Telegram'")
        self.runner.start(body["source"], bool(body.get("telegram")))
        self.send({"ok": True})

    def stop(self):
        """Stop watching."""
        self.runner.stop()
        self.send({"ok": True})

    def upload(self):
        """Save a video picked on the page to ~/.cache/openvigi/uploads, so it can be watched like any file."""
        name = Path(unquote(self.headers.get("X-Filename", "video.mp4"))).name
        if Path(name).suffix.lower() not in VIDEO_TYPES:
            return self.error(f"{name} is not a video ({', '.join(sorted(VIDEO_TYPES))})")
        UPLOADS.mkdir(parents=True, exist_ok=True)
        dest = UPLOADS / name
        left = int(self.headers.get("Content-Length") or 0)
        with open(dest, "wb") as f:
            while left > 0:
                chunk = self.rfile.read(min(left, 1 << 20))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        self.send({"path": str(dest)})

    def save_settings(self):
        """Save the keys typed on the page. Empty fields leave the saved value alone."""
        config.save({key: value for key, value in self.body_json().items() if value.strip()})
        self.send({"ok": True})

    def telegram_chats(self):
        """List the chats that have messaged the bot, for the "Find it" button."""
        chats = notify.find_chats()
        if not chats:
            return self.error("No messages found. Send your bot any message in Telegram, then try again.")
        self.send([{"id": chat_id, "name": name} for chat_id, name in chats.items()])

    def telegram_test(self):
        """Send a test photo to Telegram."""
        if not notify.ready():
            return self.error("Save your bot token and chat ID first")
        notify.send_test()
        self.send({"ok": True})


def open_in_browser(url):
    """Open the page in the default browser.

    On Linux we go through xdg-open with its output discarded. Otherwise a browser that
    wasn't already running prints its own debug messages into our terminal.
    """
    if shutil.which("xdg-open"):  # Linux
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    else:
        webbrowser.open(url)


def serve(port=8080, open_browser=True):
    """Run the UI until Ctrl+C."""
    runner = Runner()
    server = ThreadingHTTPServer(("127.0.0.1", port), type("BoundHandler", (Handler,), {"runner": runner}))
    url = f"http://127.0.0.1:{port}"
    print(f"OpenVigi is running at {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.5, open_in_browser, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        runner.stop()
        server.server_close()
