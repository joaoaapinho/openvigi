"""
Score Qwen + Jev on single photos.

Put robbery frames in benchmarks/snapshots/threat/ and normal ones in benchmarks/snapshots/safe/, then run
python benchmarks/evaluate.py

Each prediction is saved to results.csv, along with Qwen's description and Jev's answers.
"""
import csv
import json
from pathlib import Path

from openvigi.judge import classify
from openvigi.monitor import INTERVAL
from stats import PRICE_PER_M, wilson

HERE = Path(__file__).parent

IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}
FIELDS = ["file", "actual", "predicted", "confidence", "situation", "situation_probs", "latency_s", "tokens", "description"]


def load_results(path):
    """Read the predictions saved by earlier runs, so finished photos are skipped."""
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def rate(k, n):
    """Format k out of n as "k/n = x%  [95% CI lo-hi]"."""
    if n == 0:
        return "-"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {k / n:.0%}  [95% CI {lo:.0%}-{hi:.0%}]"


def main(folder=HERE / "snapshots", out_path=HERE / "results.csv"):
    """Classify every photo not yet in results.csv, then print the scores for all of them."""
    out_path = Path(out_path)
    rows = load_results(out_path)
    done = {r["file"] for r in rows}
    write_header = not out_path.exists() or out_path.stat().st_size == 0
    errors = 0

    with open(out_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            writer.writeheader()

        for actual in ("threat", "safe"):
            for img in sorted(Path(folder, actual).glob("*")):
                name = f"{actual}/{img.name}"
                if img.suffix.lower() not in IMAGE_TYPES or name in done:
                    continue
                try:
                    r = classify(img.read_bytes())
                except Exception as e:
                    # not saved, so the next run tries this image again
                    print(f"ERR  {actual:6} {img.name}: {str(e)[:100]}")
                    errors += 1
                    continue

                predicted = "threat" if r["threat"] else "safe"
                mark = "ok  " if predicted == actual else "MISS"
                print(f"{mark} {actual:6} -> {predicted:6} {r['confidence']:.2f} {r['situation']:18} {img.name}")

                row = {
                    "file": name,
                    "actual": actual,
                    "predicted": predicted,
                    "confidence": r["confidence"],
                    "situation": r["situation"],
                    "situation_probs": json.dumps(r.get("situation_probs", {})),
                    "latency_s": r["latency_s"],
                    "tokens": r["tokens"],
                    "description": r["description"],
                }
                writer.writerow(row)
                f.flush()  # so a crash halfway doesn't lose what's done
                rows.append(row)

    if not rows:
        print(f"No images found. Add some to {folder}/threat/ and {folder}/safe/")
        return

    def count(actual, predicted):
        return sum(r["actual"] == actual and r["predicted"] == predicted for r in rows)

    tp, fn = count("threat", "threat"), count("threat", "safe")
    fp, tn = count("safe", "threat"), count("safe", "safe")
    # rows loaded back from the csv hold strings, new ones hold numbers
    latencies = [float(r["latency_s"]) for r in rows if r["latency_s"] not in (None, "")]
    tokens = [float(r["tokens"]) for r in rows if r["tokens"] not in (None, "") and float(r["tokens"]) > 0]

    print("\n--- summary ---")
    print(f"robberies caught: {rate(tp, tp + fn)}   (recall)")
    print(f"false alarms: {rate(fp, fp + tn)}")
    print(f"alerts that were real: {rate(tp, tp + fp)}   (precision)")
    print(f"accuracy: {rate(tp + tn, len(rows))}")
    if errors:
        print(f"errors: {errors} images failed and are NOT in these numbers - rerun to retry them")
    if latencies:
        print(f"avg latency: {sum(latencies) / len(latencies):.2f}s")
    if tokens:
        per_frame = sum(tokens) / len(tokens) * PRICE_PER_M / 1e6
        per_day = per_frame * 86400 / INTERVAL
        print(f"cost per frame: ${per_frame:.6f}  (~${per_day:.2f}/day at 1 frame every {INTERVAL:g}s)")
    print(f"details saved to {out_path}")


if __name__ == "__main__":
    main()
