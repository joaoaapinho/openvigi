"""
OpenVigi watches an existing camera and raises an alert when someone looks to be in danger.

If you want to build on it:

open_source("rtsp://...") - turns a webcam number, camera URL or video path into a frame source
classify(jpeg_bytes) - describes one frame with Qwen and asks Jev to judge it
watch(source, on_alert=f) - the monitoring loop that both the CLI and the UI run
"""
from .judge import classify
from .monitor import watch
from .sources import Frame, SourceError, open_source

__all__ = ["classify", "watch", "open_source", "Frame", "SourceError"]
