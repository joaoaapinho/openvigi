"""
The monitoring loop: check a frame every few seconds and alert after a run of threats.
"""

import threading
import time
from datetime import datetime
from pathlib import Path

from .judge import classify
from .sources import SourceError

INTERVAL = 3.0  # seconds between checks
NEEDED = 3  # threat frames in a row before an alert


def print_result(frame, result):
    """Print one line per check. This is what you see when running openvigi watch."""
    print(f"{frame.when} {'THREAT' if result['threat'] else 'ok':6} {result['confidence']:.2f} {result['situation']}")


def save_snapshot(frame, result, folder="alerts"):
    """Save the alert frame as a timestamped JPEG and return its path."""
    Path(folder).mkdir(parents=True, exist_ok=True)
    path = Path(folder, datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".jpg")
    path.write_bytes(frame.jpeg)
    print(f"!!! ALERT at {frame.when} {result['situation']} ({result['confidence']:.2f}), "
          f"snapshot {path}. Have a person confirm before calling the police.")
    return path


def watch(source, interval=INTERVAL, needed=NEEDED, on_result=print_result, on_alert=save_snapshot,
          stop=None, log=print):
    """Check frames from source until the video ends or stop is set.

    Args:
        source: anything with live, read() and close(), usually from open_source().
        interval: seconds between checks.
        needed: how many threat frames in a row trigger an alert.
        on_result: called as on_result(frame, result) after every check.
        on_alert: called as on_alert(frame, result) when an alert goes off.
        stop: threading.Event that ends the loop. Without one, cameras run until Ctrl+C.
        log: called with a message when something goes wrong, e.g. the camera drops or a check fails.

    result is the dict returned by judge.classify().
    """
    stop = stop or threading.Event()
    streak = 0
    while not stop.is_set():
        started = time.monotonic()
        try:
            frame = source.read()
        except SourceError as e:
            log(f"camera: {e}")
            stop.wait(2)
            continue
        if frame is None:
            log("end of video")
            return

        try:
            result = classify(frame.jpeg)
        except Exception as e:
            log(f"check failed: {e}")
            result = None

        if result:
            streak = streak + 1 if result["threat"] else 0
            on_result(frame, result)
            if streak == needed:
                on_alert(frame, result)

        if source.live:
            # Time the interval from the start of the check, so a slow check doesn't push the next one back.
            stop.wait(max(0.0, interval - (time.monotonic() - started)))
