"""
OpenVigi command line: openvigi ui | snapshot | check | watch | telegram.
Run `openvigi -h` for help.
"""

import argparse
import sys
from pathlib import Path

from . import monitor
from .sources import SourceError, open_source

EXAMPLES = """examples:
  openvigi ui                                                   # open the web ui
  openvigi snapshot rtsp://user:pass@192.168.1.20:554/stream1   # test the camera URL
  openvigi check frame.jpg                                      # judge one image
  openvigi watch rtsp://user:pass@192.168.1.20:554/stream1      # watch a camera
  openvigi watch 0                                              # watch a webcam
  openvigi watch clip.mp4                                       # try it on a recording
  openvigi watch 0 --telegram                                   # also send alerts to Telegram
"""


def snapshot(args):
    """Save one frame from the source. Needs no GPU or API key."""
    source = open_source(args.source)
    try:
        frame = source.read()
    finally:
        source.close()
    if frame is None:
        sys.exit("no frame in that video")
    Path(args.out).write_bytes(frame.jpeg)
    print(f"saved {args.out} ({len(frame.jpeg) // 1024} KB) from {source.name}. Open it to check it's the right camera.")


def check(args):
    """Run one image through Qwen and Jev and print everything they said."""
    from .judge import classify, require_api_key
    require_api_key()
    r = classify(Path(args.image).read_bytes())
    print("DESCRIPTION:\n" + r["description"] + "\n")
    print(f"THREAT:    {r['p']:.2f}  ->  {'ALERT' if r['threat'] else 'ok'}")
    print(f"SITUATION: {r['situation']}  (confidence {r['situation_confidence']:.2f})")
    probs = sorted(r["situation_probs"].items(), key=lambda x: -x[1])
    print("           " + ", ".join(f"{k} {v:.2f}" for k, v in probs))
    print(f"took {r['latency_s']}s, {r['tokens']} Jev tokens")


def watch(args):
    """Watch a source until Ctrl+C (or the end of a video), saving and optionally sending alerts."""
    from .judge import require_api_key
    from . import notify
    require_api_key()
    if args.telegram:
        notify.require_telegram()

    def alert(frame, result):
        monitor.save_snapshot(frame, result, folder=args.alerts_dir)  # keep a local copy even if Telegram fails
        if args.telegram:
            try:
                notify.send_alert(frame, result)
                print("    sent to Telegram")
            except RuntimeError as e:
                print("    could not send:", e)

    source = open_source(args.source, args.interval)
    print(f"watching {source.name} every {args.interval:g}s, alert after {args.needed} threat frames in a row (Ctrl+C to stop)")
    try:
        monitor.watch(source, args.interval, args.needed, on_alert=alert)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        source.close()


def ui(args):
    """Start the web UI."""
    from .ui import serve
    try:
        serve(args.port, open_browser=not args.no_browser)
    except OSError as e:
        sys.exit(f"can't start on port {args.port} ({e.strerror}). Try: openvigi ui --port {args.port + 1}")


def telegram(args):
    """Help set up Telegram alerts."""
    from . import notify
    notify.setup()


def main():
    parser = argparse.ArgumentParser(prog="openvigi", description=__doc__.split(":")[0], epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True, metavar="command")

    p = sub.add_parser("ui", help="open the OpenVigi UI in your browser")
    p.add_argument("--port", type=int, default=8080, help="default: 8080")
    p.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    p.set_defaults(func=ui)

    p = sub.add_parser("snapshot", help="save one frame from a camera, to test the URL")
    p.add_argument("source", help="webcam number, camera URL or video file")
    p.add_argument("-o", "--out", default="snapshot.jpg", help="where to save it (default: snapshot.jpg)")
    p.set_defaults(func=snapshot)

    p = sub.add_parser("check", help="describe and judge one image")
    p.add_argument("image")
    p.set_defaults(func=check)

    p = sub.add_parser("watch", help="watch a camera and alert on threats")
    p.add_argument("source", help="webcam number, camera URL or video file")
    p.add_argument("--interval", type=float, default=monitor.INTERVAL, help=f"seconds between checks (default: {monitor.INTERVAL:g})")
    p.add_argument("--needed", type=int, default=monitor.NEEDED, help=f"threat frames in a row before alerting (default: {monitor.NEEDED})")
    p.add_argument("--alerts-dir", default="alerts", help="where alert snapshots go (default: alerts/)")
    p.add_argument("--telegram", action="store_true", help="also send alerts to Telegram (see `openvigi telegram`)")
    p.set_defaults(func=watch)

    p = sub.add_parser("telegram", help="set up Telegram alerts and send a test photo")
    p.set_defaults(func=telegram)

    args = parser.parse_args()
    try:
        args.func(args)
    except SourceError as e:
        sys.exit(f"camera: {e}")
    except RuntimeError as e:  # e.g. Telegram or Jev rejecting a key
        sys.exit(str(e))


if __name__ == "__main__":
    main()
