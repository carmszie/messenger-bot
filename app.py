import os
import requests
import gspread
from flask import Flask, request, jsonify
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ============================================================
#  CONFIGURATION — fill these in with your own values
# ============================================================

PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
VERIFY_TOKEN      = os.getenv("VERIFY_TOKEN")
SPREADSHEET_ID    = os.getenv("SPREADSHEET_ID")
CREDENTIALS_FILE  = "credentials.json"

FALLBACK_MESSAGE  = "Sorry, I didn't quite understand that. 😅 Please choose from the options below or contact us directly!"

# ============================================================
#  GREETING KEYWORDS
#  Messages that trigger the welcome message + quick reply buttons
# ============================================================

GREETING_KEYWORDS = ["hi", "hello", "hey", "start", "help", "menu"]

# ============================================================
#  QUICK REPLY BUTTONS
#  These appear as floating button suggestions in Messenger.
#  - title:   what the user sees (max 20 characters)
#  - payload: the internal code sent when the user taps the button
#
#  IMPORTANT: The payload values here must match the keys
#  in PAYLOAD_REPLIES below.
# ============================================================

QUICK_REPLY_BUTTONS = [
    {"title": "💰 Pricing",    "payload": "PRICE"},
    {"title": "🕐 Our Hours",  "payload": "HOURS"},
    {"title": "📍 Location",   "payload": "LOCATION"},
    {"title": "📞 Contact Us", "payload": "CONTACT"},
]

# ============================================================
#  PAYLOAD REPLIES
#  What to say when a quick reply button is tapped.
#  Each key matches a "payload" value from QUICK_REPLY_BUTTONS.
#  Edit the replies here to match your business.
# ============================================================

PAYLOAD_REPLIES = {
    "PRICE":    "💰 Our pricing starts at $99/month. Visit our website at yourwebsite.com for full details!",
    "HOURS":    "🕐 We're open Monday to Friday, 9:00 AM – 6:00 PM.",
    "LOCATION": "📍 You can find us at 123 Main Street, Manila. We'd love to see you!",
    "CONTACT":  "📞 You can reach us at hello@yourbusiness.com or call +63 900 000 0000.",
}


# ============================================================
#  GOOGLE SHEETS — KEYWORD LOOKUP
# ============================================================

def get_sheet_data():
    """
    Reads keyword-reply pairs from Google Sheets.

    Your Sheet1 should have two columns (with a header row):
      Column A: Keyword
      Column B: Reply

    Example:
      Keyword     | Reply
      ------------|-----------------------------------------------
      price       | Our pricing starts at $99/month...
      hours       | We're open Mon-Fri, 9am to 6pm.
      location    | We're at 123 Main St, Manila!
    """
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds  = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    sheet  = client.open_by_key(SPREADSHEET_ID).sheet1
    rows   = sheet.get_all_values()
    return rows[1:]  # Skip the header row


def find_reply_from_sheet(user_message):
    """
    Scans the Google Sheet for a keyword that appears in
    the user's message. Returns the reply if found, or None.
    """
    try:
        rows = get_sheet_data()
        user_message_lower = user_message.lower()

        for row in rows:
            if len(row) < 2:
                continue
            keyword = row[0].strip().lower()
            reply   = row[1].strip()
            if keyword and keyword in user_message_lower:
                return reply

    except Exception as e:
        print(f"[ERROR] Google Sheets lookup failed: {e}")

    return None  # No match found


# ============================================================
#  FACEBOOK GRAPH API — GET USER'S FIRST NAME
# ============================================================

def get_user_name(sender_id):
    """
    Fetches the sender's first name from Facebook's Graph API.
    Falls back to 'there' if the name can't be retrieved,
    so the greeting still reads naturally: "Hi, there!"
    """
    url    = f"https://graph.facebook.com/{sender_id}"
    params = {
        "fields":       "first_name",
        "access_token": PAGE_ACCESS_TOKEN
    }
    try:
        response = requests.get(url, params=params)
        if response.status_code == 200:
            return response.json().get("first_name", "there")
    except Exception as e:
        print(f"[ERROR] Could not fetch user name: {e}")

    return "there"


# ============================================================
#  MESSENGER SEND API
# ============================================================

def send_message(recipient_id, message_text, quick_replies=None):
    """
    Sends a text message to the user via the Messenger Send API.
    Optionally attaches quick reply buttons if provided.

    quick_replies format:
      [{"title": "Button Label", "payload": "PAYLOAD_KEY"}, ...]
    """
    url     = "https://graph.facebook.com/v19.0/me/messages"
    headers = {"Content-Type": "application/json"}
    params  = {"access_token": PAGE_ACCESS_TOKEN}

    message = {"text": message_text}

    if quick_replies:
        message["quick_replies"] = [
            {
                "content_type": "text",
                "title":        qr["title"],
                "payload":      qr["payload"]
            }
            for qr in quick_replies
        ]

    payload = {
        "recipient":      {"id": recipient_id},
        "message":        message,
        "messaging_type": "RESPONSE"
    }

    response = requests.post(url, headers=headers, json=payload, params=params)

    if response.status_code != 200:
        print(f"[ERROR] Failed to send message: {response.status_code} — {response.text}")
    else:
        print(f"[OK] Replied to {recipient_id}: {message_text[:60]}...")


def send_welcome(recipient_id):
    """
    Sends a personalized welcome message with quick reply buttons.
    Fetches the user's first name first for a personal touch.
    """
    name = get_user_name(recipient_id)
    send_message(
        recipient_id,
        f"👋 Hi, {name}! How can we help you today? Choose an option below or type your question:",
        quick_replies=QUICK_REPLY_BUTTONS
    )


# ============================================================
#  WEBHOOK ENDPOINTS
# ============================================================

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """
    Meta calls this once when you register your webhook URL.
    It checks that the verify token matches before activating.
    """
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("[OK] Webhook verified!")
        return challenge, 200

    print("[ERROR] Webhook verification failed.")
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def handle_message():
    """
    All incoming Messenger events arrive here as POST requests.

    Flow:
      1. Extract sender ID + message from the payload
      2. If it's a quick reply button tap → use PAYLOAD_REPLIES
      3. If it's a greeting → send welcome message with buttons
      4. Otherwise → look up a reply in Google Sheets
      5. If nothing matches → send the fallback message (with buttons)
    """
    data = request.get_json()

    if data.get("object") != "page":
        return "Not a page event", 404

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):

            sender_id = event["sender"]["id"]

            if "message" not in event:
                continue

            msg = event["message"]

            # Ignore echoes of our own outgoing messages
            if msg.get("is_echo"):
                continue

            # ── CASE 1: User tapped a Quick Reply button ──────────────
            if "quick_reply" in msg:
                button_payload = msg["quick_reply"]["payload"]
                reply = PAYLOAD_REPLIES.get(button_payload, FALLBACK_MESSAGE)
                # After answering, show buttons again so user can keep exploring
                send_message(sender_id, reply, quick_replies=QUICK_REPLY_BUTTONS)
                continue

            # ── CASE 2: Text message ───────────────────────────────────
            user_text = msg.get("text")
            if not user_text:
                continue

            print(f"[MSG] From {sender_id}: {user_text}")
            user_text_lower = user_text.lower().strip()

            # Check if it's a greeting → send personalized welcome
            if any(keyword in user_text_lower for keyword in GREETING_KEYWORDS):
                send_welcome(sender_id)
                continue

            # Look up a reply in Google Sheets
            sheet_reply = find_reply_from_sheet(user_text)
            if sheet_reply:
                send_message(sender_id, sheet_reply, quick_replies=QUICK_REPLY_BUTTONS)
                continue

            # Nothing matched → send fallback with buttons
            send_message(sender_id, FALLBACK_MESSAGE, quick_replies=QUICK_REPLY_BUTTONS)

    return jsonify({"status": "ok"}), 200


# ============================================================
#  RUN SERVER
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)