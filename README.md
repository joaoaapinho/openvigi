<p align="center">
  <img src="openvigi/logo.png" alt="OpenVigi" width="25%">
</p>

<h1 align="left">OpenVigi</h1>
<h3 align="left">CCTV AI Threat Detection via Local Vision Model + Jev Judgment</h3>

<p align="left">
  <img src="https://img.shields.io/badge/Python-3.10+-3776ab?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/OpenCV-4+-5c3ee8?logo=opencv&logoColor=white" alt="OpenCV">
  <img src="https://img.shields.io/badge/Ollama-Qwen3--VL%208B-000000?logo=ollama&logoColor=white" alt="Ollama Qwen3-VL">
  <img src="https://img.shields.io/badge/Jev-API-2e8b57" alt="Jev API">
  <img src="https://img.shields.io/badge/Telegram-alerts-26a5e4?logo=telegram&logoColor=white" alt="Telegram">
  <img src="https://img.shields.io/badge/License-Apache--2.0-blue" alt="License">
</p>

OpenVigi connects to a camera you already own (an IP camera, an NVR, a webcam) and checks a frame every 3 seconds. A vision model running **on your own machine** (Qwen3-VL in Ollama) describes what it sees, and [Jev](https://typesafe.ai) API judges from that context if there's any danger: a weapon, violence, threatened people, a ransacked shop. After 3 threat frames in a row, it saves the snapshot and can send an alarm notification to your phone.

The two models split the work: Qwen is the witness that turns pixels into words without the image ever leaving your machine, and Jev, which works on text, judges what those words mean and gives a threat probability that can be tuned and tested.

⚠️ Experimental system. It should not serve as a replacement for an alarm system, or to contact the police without a person's confirmation.

---

## Scope


<p align="center"><img src="misc/demo_openvigi.gif" alt="OpenVigi web page" width="70%"></p>

| Part | What it does |
|---|---|
| Camera input | RTSP stream, HTTP snapshot URL, MJPEG stream, USB webcam or a video file.|
| Describe | Qwen3-VL 8B (local, via Ollama) writes a focused short description of the frame. |
| Judge | Jev turns the description into a threat probability and a situation label. |
| Alert rule | Fires after 3 threat frames in a row, to ride out single misreadings. |
| Alerts | Snapshot saved locally and optionally sent to Telegram with the verdict and description. |
| Web UI | `openvigi ui`: live view with the current verdict, every check, alert snapshots and settings. |
| Command line | `snapshot`, `check`, `watch` and `telegram` for scripting and headless machines. |
| Benchmarks | Score photos and clips. |

---

## Methodology

<p align="center"><img src="misc/design.png" alt="OpenVigi pipeline diagram" width="90%"></p>


### 1. Frame sampling

A frame is checked every $\Delta = 3$ s. For a live camera, the next check starts at

$$t_{i+1} = \max\left(t_i + \Delta,\; t_i + \text{check time}_i\right)$$

so a slow check doesn't push every following one back. Video streams keep sending frames whether you read them or not, and reading one every few seconds leaves you judging footage that gets older with every check. `StreamSource` avoids this by draining the stream on a background thread and decoding a frame only when a check asks for one. For video files, frames are sampled every $\Delta$ seconds of footage.

### 2. Description

Qwen answers a short form about the frame (`prompts/qwen_prompt.txt`): how many people, what is happening, hidden faces, unusual behaviour, the state of the shop and the lighting. If anyone is in frame, it is then asked two focused questions on their own (`prompts/qwen_checks.txt`): is anyone holding a weapon, and is anyone being threatened. Qwen spots weapons far more reliably when that is the only thing it is asked. Temperature is set to 0, so the same frame gets the same description.

### 3. Judgment

The description $d_t$ goes to Jev with two questions (`prompts/jev_questions.json`):

$$p_t = P(\text{someone is or could soon be in danger} \mid d_t)$$

$$s_t \in \{\text{armed\_threat},\ \text{violence},\ \text{ransacked},\ \text{person\_down},\ \text{strange\_behaviour},\ \text{normal}\}$$

A frame counts as a threat when $p_t \geq \tau$, with $\tau = 0.7$. Theft without violence, and covered faces on their own, are deliberately not threats.

### 4. Alert rule

$$\text{streak}_t = \begin{cases} \text{streak}_{t-1} + 1 & \text{if } p_t \geq \tau \\\\ 0 & \text{otherwise} \end{cases}$$

An alert fires when $\text{streak}_t = k$, with $k = 3$. This adds at least $(k-1)\Delta = 6$ s of delay in exchange for fewer false alarms.

### 5. Evaluation

Detection rates use the Wilson interval, which stays sensible near 0% and 100%:

$$\frac{\hat{p} + \frac{z^2}{2n} \pm z\sqrt{\frac{\hat{p}(1-\hat{p})}{n} + \frac{z^2}{4n^2}}}{1 + \frac{z^2}{n}}, \qquad z = 1.96$$

False alarms are counted per 100 hours of footage with people in it (an empty shop at night is easy and would flatter the rate). Alerts within 60 s of each other count once. The upper bound uses the exact Poisson interval: $\lambda_{\text{up}}$ such that $P(X \leq k \mid \lambda_{\text{up}}) = 0.05$.

---

## Results

| Test | Result |
|---|---|
| 100 photos (50 robbery, 50 normal) | **47/50** caught (94%, CI 84-98%), **1/50** false alarms (2%, CI 0-10%) |
| 10 clips (5 robbery, 5 normal), single frame rule | 4/5 caught, median delay 20 s, 2 false alarms |
| 10 clips, 3-in-a-row rule (default) | **4/5** caught (80%, CI 38-96%), median delay **26 s**, 1 false alarm |
| Speed | 3-4 s per frame on an NVIDIA RTX 3070 Laptop GPU (8 GB) |
| Jev cost | ~820 input tokens per frame, about **$1 per camera per day** |

**1. Single frames are mostly read correctly, and most misses start in the description |**
On still photos the pipeline gets 96 of 100 right. In two of the three missed robberies, Qwen described an ordinary scene, so Jev had nothing to go on. The third was rated *ransacked* at 0.63, just under $\tau$. The one false alarm was a person standing on a counter, which the Jev questions deliberately count as risky.

**2. Requiring three frames in a row cut false alarms from 2 to 1 for 6 seconds of delay |**
On the clips, the 3-in-a-row rule kept the same detections as single frames, cut false alarms from 2 to 1, and moved the median delay from 20 s to 26 s.

**3. One robbery was never seen at all |**
In one clip, every frame was judged normal, so no alert rule could have caught it. Better descriptions, not a different alert rule, are what would fix that.
The footage itself is the main cause: it is low resolution and filmed from a poor angle, so the robbery is hard to make out even for a person. A modern high-definition camera pointed at the counter should make this kind of miss much rarer.

**4. The false-alarm rate per day is still unknown |**
There are only a few minutes of normal footage in the benchmark, so the 95% upper bound on false alarms is far too wide to be useful. This is the number that most needs more data: run the benchmarks on your own camera before trusting it.

---

## Run locally

A machine running on the same network as your camera is required, with an **NVIDIA GPU (min ~ 8 GB+ of video memory)** or an **Apple Silicon Mac**, Python 3.10+, and a Jev API key from [typesafe.ai](https://typesafe.ai). This system was developed and tested on Linux, but although untested macOS and Windows should work as well.

```bash
# 1. Install Ollama (https://ollama.com/download) + download the vision model (~6 GB)
ollama pull qwen3-vl:8b-instruct

# 2. Install OpenVigi (pipx: https://pipx.pypa.io)
pipx install git+https://github.com/joaoaapinho/openvigi

# 3. Run
openvigi ui
```

App serves at **http://127.0.0.1:8080** and opens in your browser. Paste your Jev API key in **Settings**, pick a camera (or try a **Video file** first), press **Test camera**, then **Start watching**. The first check takes longer while the model loads. Ollama has to be running whenever OpenVigi is - its installers will start it automatically.

Settings are saved in `~/.config/openvigi/config.json`, readable only by you, and alert snapshots go to `alerts/` in the folder you ran it from. The page only answers on your own computer, because it handles your keys and camera passwords.

To work on the code instead:

```bash
git clone https://github.com/joaoaapinho/openvigi && cd openvigi
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

---

## Connecting your camera

| Type | Example | Notes |
|---|---|---|
| RTSP stream | `rtsp://user:pass@192.168.1.20:554/...` | Most IP cameras and NVRs should work. |
| HTTP snapshot | `http://user:pass@192.168.1.20/snapshot.jpg` | Best if your camera has one: lighter, and doesn't use a stream slot. Basic and Digest auth both work. |
| MJPEG stream | `http://192.168.1.20/video.mjpg` | Detected automatically. |
| USB webcam | `0` | |
| Video file | `clip.mp4` | Checked every 3s of footage. |

Paths vary by model and firmware, the [iSpy camera database](https://www.ispyconnect.com/cameras) can be a good source for helping you find yours. Use the main stream, since low-resolution sub-streams can hide a weapon. URL-encode special characters in passwords (`@` is `%40`).

---

## Telegram alerts

Each install uses its own bot:

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token.
2. Open your new bot, press **Start** and send it any message.
3. On the openvigi UI: paste the token in **Settings**, press **Find it** to fill in your chat ID, **Save**, then **Send test photo**.
4. Tick **Send alerts to Telegram** before starting.

From the CLI, the same steps are `export TELEGRAM_BOT_TOKEN=...`, `openvigi telegram` (prints your chat ID), `export TELEGRAM_CHAT_ID=...`, `openvigi telegram` (sends a test photo), then `openvigi watch 0 --telegram`. To alert a group, add the bot to it, send `/start@your_bot_username` there, and use the group's chat ID.

---

## Command line

```
openvigi ui [--port 8080] [--no-browser]   the web page
openvigi snapshot SOURCE [-o file.jpg]     save one frame, to test a camera URL (no GPU or API needed)
openvigi check IMAGE                       describe and judge one image, with full details
openvigi telegram                          set up Telegram and send a test photo
openvigi watch SOURCE [--interval 3] [--needed 3] [--alerts-dir alerts] [--telegram]
```

| Var | Default | |
|---|---|---|
| `JEV_API_KEY` | - | Required. |
| `JEV_THRESHOLD` | `0.7` | Threat probability $\tau$. |
| `JEV_MODEL` / `JEV_API_URL` | `jev-latest` / `https://api.typesafe.ai/v1/systemone` | |
| `QWEN_MODEL` | `qwen3-vl:8b-instruct` | Any specs eligible vision model. |
| `OLLAMA_URL` | `http://localhost:11434` | Where Ollama is running. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | - | Telegram alerts. |
| `OPENVIGI_CONFIG` | `~/.config/openvigi/config.json` | Where the web page saves settings locally. |

Env vars take priority over the saved settings.

---

## Building on top of it

```
openvigi/
  sources.py    camera in        open_source() picks a source class from what the user typed
  describe.py   image to text    Qwen-VL via Ollama
  judge.py      text to verdict  Jev - classify() runs describe + judge on one image
  monitor.py    the loop         watch() - checks, the N-in-a-row rule, alert callbacks
  notify.py     alerts out       telegram
  config.py     settings         env vars, then the saved config file
  ui.py         web page         small local server around watch(), plus ui.html
  prompts/      what Qwen and Jev are asked - edit these to look for something else
benchmarks/     scores any change on your own photos and clips
```

**Send alerts somewhere new** by passing your own `on_alert` to `watch()`:

```python
import requests
from openvigi import open_source, watch

def post_alert(frame, result):
    requests.post("https://example.com/hooks/openvigi", timeout=10,
                  files={"snapshot": ("frame.jpg", frame.jpeg, "image/jpeg")},
                  data={"situation": result["situation"], "confidence": result["confidence"],
                        "description": result["description"], "when": frame.when})

watch(open_source("rtsp://user:pass@192.168.1.20:554/stream1"), on_alert=post_alert)
```

**Add a new kind of camera** with any class that has `live`, `read()` (returns a `Frame`, `None` when finished, or raises `SourceError` to retry) and `close()`:

```python
from openvigi import Frame, SourceError
from openvigi.sources import now

class FolderSource:
    """The newest image in a folder, e.g. one an NVR uploads to over FTP."""
    live = True

    def __init__(self, folder):
        self.folder = folder

    def read(self):
        images = sorted(self.folder.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
        if not images:
            raise SourceError("no images yet")
        return Frame(images[-1].read_bytes(), now())

    def close(self):
        pass
```

**Measure every change** to the prompts or threshold with the benchmarks, and delete `benchmarks/cache/` and `benchmarks/results.csv` first, since the old answers no longer apply:

```bash
python benchmarks/evaluate.py                  # photos in benchmarks/snapshots/{threat,safe}/
python benchmarks/eval_videos.py               # clips in benchmarks/videos/{threat,safe}/
python benchmarks/eval_videos.py --score-only  # re-score from cache, no GPU or API
```
---

## Limitations

- **Small benchmark |** It has 100 photos and 10 clips. Proving a low false-alarm rate needs hundreds of hours of real footage, which is hard to find, so test on your own camera before relying on it.
- **Gaps between checks |** One frame is checked every 3 seconds, so a very quick event between two checks can be missed.
- **Only what Qwen sees |** Hidden weapons (a gun under a shirt or jacket), low-resolution footage and poor camera angles leave too few visible signs to describe, and Jev can only judge what is in the description.
- **Built-in delay |** Alerts need three threat frames in a row, which adds at least 6 seconds.
- **Hardware-bound |** Qwen-VL needs a GPU with about 8 GB of memory, so stronger GPUs should allow larger vision models, with better descriptions and shorter checks.
- **Shops only, so far |** The prompts are written for shops. Homes, offices and outdoor spaces have not been tested.
- **One camera on the web page |** It watches one camera at a time, and one consumer GPU handles about one camera anyway.
- **Telegram gets the snapshot |** Otherwise images stay on your computer (Jev only receives text), but a Telegram alert sends the photo to Telegram's servers.

---

## Credits

- **Benchmark photos and clips |** taken from the [UCF-Crime dataset](https://www.crcv.ucf.edu/projects/real-world/) (Sultani, Chen and Shah, *Real-world Anomaly Detection in Surveillance Videos*, CVPR 2018). They remain under the dataset's own terms.
- **Project trigger clip |** [this video](https://www.youtube.com/watch?v=ify0B8RgEFo&t=1s) from my home country 🇵🇹 is what prompted this project.

---

## License

[Apache-2.0](LICENSE). Provided as-is, with no guarantee that it will detect anything. Recording and analysing people is regulated in many places (for example under the GDPR in the EU), so make sure your camera use is lawful and signposted.
