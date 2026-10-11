"""Run every clip in videos/ through Qwen + Jev and score the N-in-a-row alert.

Put clips in benchmarks/videos/threat/ and benchmarks/videos/safe/.

python benchmarks/eval_videos.py # run and score
python benchmarks/eval_videos.py --score-only  # re-print the scores from cache, no GPU or API call

Depending on GPU, PARALLEL_VIDEOS can run multiple clips at once (start Ollama with OLLAMA_NUM_PARALLEL=x).
"""
import csv
import json
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2

from openvigi.describe import describe, people_count
from openvigi.judge import ask, THRESHOLD
from openvigi.monitor import INTERVAL  # check frames at the same rate as the live monitor
from openvigi.sources import VIDEO_TYPES
from stats import PRICE_PER_M, wilson, poisson_upper

HERE = Path(__file__).parent
VIDEO_DIR = HERE / "videos"
WORKERS = 8  # Jev requests in flight per video
PARALLEL_VIDEOS = int(os.getenv("PARALLEL_VIDEOS", "1"))
COOLDOWN = 60  # seconds after an alert before another one counts as a new false alarm

# test diff how many threat frames in a row it takes to alert 
SYSTEMS = {
    "single frame": 1,
    "2 in a row": 2,
    "3 in a row": 3,
}


def find_videos():
    """List the clips in videos/threat and videos/safe, with robbery start and end times if given."""
    times = {}
    times_file = VIDEO_DIR / "threat_times.csv"
    if times_file.exists():
        with open(times_file) as f:
            for row in csv.DictReader(f):
                times[row["file"]] = row

    videos = []
    # folders match benchmarks/snapshots/ inside the script they're scored as robbery / normal
    for folder, label in (("threat", "robbery"), ("safe", "normal")):
        for path in sorted((VIDEO_DIR / folder).glob("*")):
            if path.suffix.lower() not in VIDEO_TYPES:
                continue
            t = times.get(path.name, {})
            videos.append({
                "name": path.stem,
                "path": path,
                "label": label,
                "cache": HERE / "cache" / label / (path.stem + ".jsonl"),
                "start": float(t["start_s"]) if t.get("start_s") else None,
                "end": float(t["end_s"]) if t.get("end_s") else None,
            })
    return videos


def ask_with_retry(desc, tries=4):
    """Ask Jev, retrying with backoff. After the last try, record the frame as an API error instead of crashing."""
    for i in range(tries):
        try:
            return ask(desc)
        except Exception as e:
            if i == tries - 1:
                return {"p": None, "situation": "api_error", "tokens": None, "error": str(e)[:200]}
            time.sleep(2 ** i)


def run_video(video, pool):
    """Check 1 clip every INTERVAL seconds, appending each result to the clip's cache file. Qwen runs, 1 frame at a time, while Jev calls go to `pool` so they overlap."""
    cache = video["cache"]
    cache.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if cache.exists():
        with open(cache) as f:
            done = {json.loads(line)["t"] for line in f}

    cap = cv2.VideoCapture(str(video["path"]))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    step = max(1, round(fps * INTERVAL))
    out = open(cache, "a")
    lock = threading.Lock()
    futures = []

    # runs in the pool so Qwen can move on to the next frame while Jev answers
    def finish(t, desc, t0):
        answer = ask_with_retry(desc)
        line = {"t": t, "people": people_count(desc), "latency_s": round(time.time() - t0, 2), **answer, "description": desc}
        with lock:
            out.write(json.dumps(line) + "\n")
            out.flush()

    started = time.time()
    idx = 0
    new = 0
    while cap.grab():  # grab() skips frames without fully decoding them
        if idx % step == 0:
            t = round(idx / fps, 1)
            if t not in done:
                ok, frame = cap.retrieve()
                if ok:
                    t0 = time.time()
                    try:
                        desc = describe(cv2.imencode(".jpg", frame)[1].tobytes())
                    except Exception as e:
                        print(f"  Qwen failed at {t}s: {e}")
                    else:
                        futures.append(pool.submit(finish, t, desc, t0))
                        new += 1
        idx += 1

    for fut in futures:
        fut.result()
    out.close()

    secs = time.time() - started
    if new:
        print(f"done {video['name']}: {new} new frames in {secs:.0f}s ({secs / new:.1f}s per frame)")
    else:
        print(f"done {video['name']}: 0 new frames in {secs:.0f}s (all cached)")


def load_frames(video):
    """Read a clip's cached results, sorted by time."""
    if not video["cache"].exists():
        return []
    with open(video["cache"]) as f:
        frames = [json.loads(line) for line in f]
    return sorted(frames, key=lambda fr: fr["t"])


def alerts(frames, needed):
    """Times at which the alarm would go off with an N-in-a-row rule."""
    streak = 0
    last = -1e9
    fired = []
    for fr in frames:
        if (fr["p"] or 0) >= THRESHOLD:
            streak += 1
        else:
            streak = 0
        # same rule as monitor.watch(): fire once when the streak reaches needed, not again until it breaks
        if streak == needed and fr["t"] - last >= COOLDOWN:
            fired.append(fr["t"])
            last = fr["t"]
    return fired


def format_times(times):
    """Seconds into the clip at which the alarm went off."""
    return " ".join(f"{t:g}" for t in times)


def score(videos):
    """Score every alert rule in SYSTEMS on the cached results and write video_results.csv."""
    data = [(v, load_frames(v)) for v in videos]
    data = [(v, frames) for v, frames in data if frames]
    if not data:
        print("nothing cached yet - run without --score-only")
        return

    robberies = [(v, frames) for v, frames in data if v["label"] == "robbery"]

    # normal footage = the normal clips plus whatever comes before the robbery in robbery clips
    normal_parts = [frames for v, frames in data if v["label"] == "normal"]
    for v, frames in robberies:
        if v["start"] is not None:
            normal_parts.append([fr for fr in frames if fr["t"] < v["start"]])
    normal_frames = [fr for part in normal_parts for fr in part]
    hours = len(normal_frames) * INTERVAL / 3600
    occupied_hours = sum(fr["people"] != 0 for fr in normal_frames) * INTERVAL / 3600

    all_frames = [fr for _, frames in data for fr in frames]
    errors = sum(fr["situation"] == "api_error" for fr in all_frames)
    tokens = [fr["tokens"] for fr in all_frames if fr["tokens"]]

    print(f"\n{len(robberies)} robbery videos, {len(data) - len(robberies)} normal videos")
    print(f"normal footage: {hours:.1f} h ({occupied_hours:.1f} h with people in frame), "
          f"{len(all_frames)} frames checked, {errors} API errors")
    if tokens:
        print(f"Jev cost: ${sum(tokens) * PRICE_PER_M / 1e6:.2f} total, {statistics.mean(tokens):.0f} tokens/frame")

    results = []
    print(f"\n{'system':24}{'robberies caught':>26}{'median delay':>14}{'false alarms':>14}{'per 100 occupied h (95% max)':>32}")
    for name, needed in SYSTEMS.items():
        caught = 0
        delays = []
        for v, frames in robberies:
            start = v["start"] or 0
            end = v["end"] if v["end"] is not None else frames[-1]["t"]
            # score the robbery window on its own, so a false alarm just before it
            # (and its cooldown) can't hide the real alert
            window = [fr for fr in frames if start - needed * INTERVAL < fr["t"] <= end]
            hits = [t for t in alerts(window, needed) if t >= start]
            if hits:
                caught += 1
                delays.append(hits[0] - start)
            results.append({"system": name, "video": v["name"], "label": "robbery", "caught": bool(hits),
                            "delay_s": hits[0] - start if hits else None,
                            "alarm_times_s": format_times(alerts(frames, needed))})

        false_alarms = sum(len(alerts(part, needed)) for part in normal_parts)
        for v, frames in data:
            if v["label"] == "normal":
                fired = alerts(frames, needed)
                results.append({"system": name, "video": v["name"], "label": "normal",
                                "false_alarms": len(fired), "alarm_times_s": format_times(fired)})

        n = len(robberies)
        if n:
            lo, hi = wilson(caught, n)
            recall = f"{caught}/{n} = {caught / n:.0%} [{lo:.0%}-{hi:.0%}]"
        else:
            recall = "-"
        delay = f"{statistics.median(delays):.0f}s" if delays else "-"
        if occupied_hours:
            per_100h = 100 * false_alarms / occupied_hours
            upper = 100 * poisson_upper(false_alarms) / occupied_hours
            fa_rate = f"{per_100h:.2f} (<{upper:.2f})"
        else:
            fa_rate = "-"
        print(f"{name:24}{recall:>26}{delay:>14}{false_alarms:>14}{fa_rate:>32}")

    with open(HERE / "video_results.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["system", "video", "label", "caught", "delay_s", "false_alarms", "alarm_times_s"])
        writer.writeheader()
        writer.writerows(results)
    print("\n[brackets] = 95% confidence interval. Per-video details saved to benchmarks/video_results.csv")


if __name__ == "__main__":
    videos = find_videos()
    print(f"{len(videos)} videos")
    if not videos:
        print(f"Put clips in {VIDEO_DIR}/threat/ and {VIDEO_DIR}/safe/")
        sys.exit()

    if "--score-only" not in sys.argv:
        with ThreadPoolExecutor(WORKERS * PARALLEL_VIDEOS) as pool, ThreadPoolExecutor(PARALLEL_VIDEOS) as runner:
            jobs = [runner.submit(run_video, v, pool) for v in videos]
            for job in jobs:
                job.result()
    score(videos)
