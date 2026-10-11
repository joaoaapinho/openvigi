"""Frame sources: the different ways OpenVigi can get a picture from a camera.

open_source() picks the right class from what the user types:

0, 1, ...                      USB webcam              StreamSource
rtsp://user:pass@ip:554/...    IP camera or NVR        StreamSource
http://ip/snapshot.jpg         camera snapshot URL     SnapshotSource
http://ip/video.mjpg           MJPEG stream            StreamSource
clip.mp4                       recorded video          FileSource

To add a new kind of source (a cloud API, Home Assistant, a folder of images), write a
class with a live attribute and read() and close() methods, and pass it to monitor.watch().
"""

import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import cv2
import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

VIDEO_TYPES = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".ts"}


@dataclass
class Frame:
    jpeg: bytes
    when: str


class SourceError(Exception):
    """The source couldn't give us a frame right now, e.g. the camera is offline. Worth retrying."""


def redact(url):
    """Hide the password in a URL so it can be printed or logged."""
    parts = urlsplit(str(url))
    if parts.password is None:
        return str(url)
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=f"{parts.username}:***@{host}"))


def encode(frame):
    """Encode an OpenCV image as JPEG bytes."""
    ok, buf = cv2.imencode(".jpg", frame)
    if not ok:
        raise SourceError("could not encode frame as JPEG")
    return buf.tobytes()


def now():
    """The current time as HH:MM:SS, used to label frames from cameras."""
    return f"{datetime.now():%H:%M:%S}"


class StreamSource:
    """A live video stream (RTSP, MJPEG or a webcam), read on a background thread.

    Streams keep sending frames whether or not you read them. If you only read one every
    few seconds, the rest queue up in a buffer and you end up looking at footage from
    minutes ago. So the thread keeps calling grab() to drain the stream, and only decodes
    a frame when read() asks for one. It also reconnects on its own if the camera drops.
    """
    live = True

    def __init__(self, target, timeout=10):
        self.target = target  # an int for a webcam, otherwise a URL
        self.name = f"webcam {target}" if isinstance(target, int) else redact(target)
        self.timeout = timeout  # how long read() waits for a frame before giving up
        self._frame = None
        self._want = threading.Event()  # read() is waiting for a frame
        self._ready = threading.Event()  # the thread has put a fresh one in self._frame
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"source {self.name}", daemon=True)
        self._thread.start()

    def _run(self):
        backoff = 1
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.target)
            if not cap.isOpened():
                cap.release()
                print(f"[{self.name}] can't connect, retrying in {backoff}s")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)
                continue
            backoff = 1
            self._connected.set()

            while not self._stop.is_set():
                if not cap.grab():
                    break
                if self._want.is_set():
                    ok, frame = cap.retrieve()
                    if ok:
                        self._frame = frame
                        self._want.clear()
                        self._ready.set()

            cap.release()
            self._connected.clear()
            if not self._stop.is_set():
                print(f"[{self.name}] stream lost, reconnecting")
                self._stop.wait(2)

    def read(self):
        """Return the newest frame, or raise SourceError if none arrives within timeout."""
        self._ready.clear()
        self._want.set()
        if not self._ready.wait(self.timeout):
            self._want.clear()
            state = "no new frame" if self._connected.is_set() else "not connected"
            raise SourceError(f"{self.name}: {state} after {self.timeout}s")
        return Frame(encode(self._frame), now())

    def close(self):
        self._stop.set()
        self._thread.join(timeout=5)


class SnapshotSource:
    """A camera's HTTP snapshot URL. Each read is one GET that returns one JPEG.

    This is lighter than streaming, and it doesn't take up one of the camera's few
    streaming slots. Credentials can go in the URL (http://user:pass@ip/...).
    """
    live = True

    def __init__(self, url, timeout=10):
        parts = urlsplit(url)
        self.name = redact(url)
        self.timeout = timeout
        self.url = urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))  # credentials go in auth instead
        self.session = requests.Session()
        if parts.username:
            user, password = unquote(parts.username), unquote(parts.password or "")
            self._auths = [HTTPBasicAuth(user, password), HTTPDigestAuth(user, password)]
        else:
            self._auths = [None]

    def get(self, **kwargs):
        """GET the snapshot URL, trying each auth scheme until the camera accepts one."""
        for auth in self._auths:
            r = self.session.get(self.url, auth=auth, timeout=self.timeout, **kwargs)
            if r.status_code != 401:
                self._auths = [auth]
                return r
        return r

    def read(self):
        """Fetch a fresh snapshot."""
        try:
            r = self.get()
            r.raise_for_status()
        except requests.RequestException as e:
            raise SourceError(f"{self.name}: {e}") from e
        if not r.headers.get("Content-Type", "").startswith("image/"):
            raise SourceError(f"{self.name}: expected an image, got {r.headers.get('Content-Type')}")
        return Frame(r.content, now())

    def close(self):
        self.session.close()


class FileSource:
    """A recorded video, sampled every interval seconds of footage as fast as we can go. 
    Useful for trying OpenVigi on a clip before pointing it at a real camera.
    """
    live = False

    def __init__(self, path, interval):
        self.name = str(path)
        self.cap = cv2.VideoCapture(str(path))
        if not self.cap.isOpened():
            raise SourceError(f"can't open video {path}")
        self.step = max(1, round((self.cap.get(cv2.CAP_PROP_FPS) or 25) * interval))
        self.first = True

    def read(self):
        """Return the frame `interval` seconds after the previous one, or None at the end."""
        if not self.first:
            # Seeking is much faster than decoding every frame in between.
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, self.cap.get(cv2.CAP_PROP_POS_FRAMES) + self.step - 1)
        self.first = False
        ok, frame = self.cap.read()
        if not ok:
            return None
        return Frame(encode(frame), f"{self.cap.get(cv2.CAP_PROP_POS_MSEC) / 1000:.1f}s")

    def close(self):
        self.cap.release()


def is_snapshot_url(url, timeout=10):
    """Tell a snapshot URL (answers with one image) apart from an MJPEG stream."""
    probe = SnapshotSource(url, timeout)
    try:
        with probe.get(stream=True) as r:  # stream=True, or an MJPEG URL would download forever
            r.raise_for_status()
            return r.headers.get("Content-Type", "").startswith("image/")
    except requests.RequestException as e:
        raise SourceError(f"{redact(url)}: {e}") from e
    finally:
        probe.close()


def open_source(spec, interval=3.0):
    """Open whatever the user typed: a webcam number, a camera URL or a video file. interval is only used for video files, to decide how far apart the checked frames are."""
    spec = str(spec).strip()
    if spec.isdigit():
        return StreamSource(int(spec))

    path = Path(spec).expanduser()
    if path.is_file():
        if path.suffix.lower() not in VIDEO_TYPES:
            raise SourceError(f"{path} is not a video. To check a single image use: openvigi check {path}")
        return FileSource(path, interval)

    scheme = urlsplit(spec).scheme.lower()
    if scheme in ("http", "https"):
        return SnapshotSource(spec) if is_snapshot_url(spec) else StreamSource(spec)
    if scheme:  # rtsp, rtsps, rtmp and anything else OpenCV's FFmpeg backend can open
        return StreamSource(spec)
    raise SourceError(f"don't know how to open {spec!r}: use a webcam number, a camera URL or a video file")
