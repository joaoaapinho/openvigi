"""
Send alerts to Telegram, with the snapshot attached.
"""

from pathlib import Path

import requests

from . import config

LOGO = Path(__file__).parent / "logo.png"


def call(method, **kwargs):
    """Call a Telegram bot API method and return its result."""
    token = config.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Telegram: no bot token set")
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", timeout=30, **kwargs)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        raise RuntimeError(f"Telegram: {str(e).replace(token, '***')}") from None
    if not data.get("ok"):
        raise RuntimeError(f"Telegram: {data.get('description', r.status_code)}")
    return data["result"]


def send_photo(image, caption, filename="alert.jpg"):
    """Send an image to the configured chat."""
    call("sendPhoto", data={"chat_id": config.get("TELEGRAM_CHAT_ID"), "caption": caption[:1024]},  # Telegram's caption limit.
         files={"photo": (filename, image)})


def send_alert(frame, result):
    """Send an alert snapshot with the verdict and Qwen's description. Works as watch()'s on_alert."""
    caption = (f"OpenVigi ALERT at {frame.when}\n"
               f"{result['situation']} (threat {result['confidence']:.2f})\n\n"
               f"{result['description']}")
    send_photo(frame.jpeg, caption)


def send_test():
    """Send the OpenVigi logo, so you can see where alerts will arrive."""
    send_photo(LOGO.read_bytes(), "OpenVigi test. Alerts will arrive here.", "openvigi.png")


def find_chats():
    """Return {chat id: name} for everyone who has recently messaged the bot."""
    chats = {}
    for update in call("getUpdates"):
        chat = update.get("message", {}).get("chat")
        if chat:
            name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("username")]))
            chats[chat["id"]] = name
    return chats


def ready():
    """True once both the bot token and the chat id are set."""
    return bool(config.get("TELEGRAM_BOT_TOKEN") and config.get("TELEGRAM_CHAT_ID"))


def require_telegram():
    """Stop with a helpful message if Telegram isn't set up yet."""
    if not ready():
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first. Run `openvigi telegram` for help.")


def setup():
    """Walk through openvigi telegram: find the chat id first, then send a test photo."""
    if not config.get("TELEGRAM_BOT_TOKEN"):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN first: message @BotFather in Telegram, send /newbot and copy the token.\n"
                         "Then send your new bot any message and run `openvigi telegram` again.")
    if not config.get("TELEGRAM_CHAT_ID"):
        chats = find_chats()
        if not chats:
            raise SystemExit("No messages found. Send your bot any message in Telegram, then run this again.")
        for chat_id, name in chats.items():
            print(f"chat id {chat_id}: {name}")
        print("\nnow: export TELEGRAM_CHAT_ID=<the id above>  and run `openvigi telegram` again")
        return
    send_test()
    print("test photo sent, check Telegram")
