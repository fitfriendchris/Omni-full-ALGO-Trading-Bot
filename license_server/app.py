"""
OMNI-ICT License Server
=======================
Deploy this on a cheap VPS ($5/mo) or Railway free tier.
Handles:
  - License key validation (called by every user's bot on startup)
  - Stripe webhooks (auto-provision keys on payment, revoke on cancel)
  - Admin API (create/list/revoke keys manually)

Setup:
  1. pip install flask stripe
  2. Set environment variables (see .env.example below)
  3. python app.py

Environment variables:
  STRIPE_SECRET_KEY       — from Stripe dashboard
  STRIPE_WEBHOOK_SECRET   — from Stripe webhook settings
  ADMIN_TOKEN             — a secret you choose for admin API calls
  PORT                    — port to listen on (default 5000)

Stripe products to create:
  - Starter ($49/mo)  → metadata: plan=starter
  - Pro     ($99/mo)  → metadata: plan=pro
  - Elite   ($199/mo) → metadata: plan=elite
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request, abort

# Optional Stripe — only needed for webhook handling
try:
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_AVAILABLE = bool(stripe.api_key)
except ImportError:
    STRIPE_AVAILABLE = False

app = Flask(__name__)

# Load .env if present (local dev)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("license_server")

DB_PATH    = Path(os.getenv("DB_PATH", "/app/licenses.db"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-this-secret")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")


# ── Database ──────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                key          TEXT PRIMARY KEY,
                email        TEXT,
                plan         TEXT NOT NULL DEFAULT 'starter',
                status       TEXT NOT NULL DEFAULT 'active',
                stripe_sub_id TEXT,
                created_at   REAL NOT NULL,
                expires_at   REAL,
                last_check   REAL,
                notes        TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS check_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                key        TEXT NOT NULL,
                ts         REAL NOT NULL,
                ip         TEXT,
                result     TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS client_sessions (
                license_key      TEXT PRIMARY KEY,
                telegram_chat_id INTEGER,
                mt5_login        TEXT,
                mt5_server       TEXT,
                mt5_password     TEXT,
                risk_mode        TEXT DEFAULT 'MODERATE',
                onboarding_step  TEXT DEFAULT 'awaiting_key',
                created_at       REAL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS client_relay (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key  TEXT NOT NULL,
                direction    TEXT NOT NULL,
                content      TEXT NOT NULL,
                ts           REAL NOT NULL,
                delivered    INTEGER DEFAULT 0
            )
        """)
        db.commit()
    log.info("Database ready: %s", DB_PATH)


def generate_key() -> str:
    raw = secrets.token_hex(6).upper()
    return f"OMNI-{raw[:4]}-{raw[4:8]}-{raw[8:12]}"


# ── Auth decorator ────────────────────────────────────────────

def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("X-Admin-Token") or request.args.get("token")
        if token != ADMIN_TOKEN:
            abort(401)
        return f(*args, **kwargs)
    return decorated


# ── Public: validate a key ────────────────────────────────────

@app.route("/validate")
def validate():
    key = request.args.get("key", "").strip().upper()
    ip  = request.remote_addr

    if not key:
        return jsonify({"valid": False, "message": "No key provided"}), 400

    with get_db() as db:
        row = db.execute(
            "SELECT * FROM licenses WHERE key = ?", (key,)
        ).fetchone()

        if not row:
            _log_check(db, key, ip, "not_found")
            return jsonify({"valid": False, "message": "License key not found. "
                           "Subscribe at https://omni-ict.com"}), 404

        if row["status"] != "active":
            _log_check(db, key, ip, f"inactive:{row['status']}")
            return jsonify({"valid": False,
                           "message": f"License is {row['status']}. "
                                      "Visit https://omni-ict.com to renew."}), 403

        if row["expires_at"] and time.time() > row["expires_at"]:
            db.execute("UPDATE licenses SET status='expired' WHERE key=?", (key,))
            db.commit()
            _log_check(db, key, ip, "expired")
            return jsonify({"valid": False,
                           "message": "License expired. Renew at https://omni-ict.com"}), 403

        db.execute("UPDATE licenses SET last_check=? WHERE key=?", (time.time(), key))
        db.commit()
        _log_check(db, key, ip, "ok")

        exp = ""
        if row["expires_at"]:
            exp = datetime.fromtimestamp(row["expires_at"], tz=timezone.utc).isoformat()

        return jsonify({
            "valid":      True,
            "plan":       row["plan"],
            "email":      row["email"] or "",
            "expires_at": exp,
            "message":    f"License valid — {row['plan']} plan",
        })


def _log_check(db, key, ip, result):
    db.execute("INSERT INTO check_log (key, ts, ip, result) VALUES (?,?,?,?)",
               (key, time.time(), ip, result))


# ── Stripe webhooks ───────────────────────────────────────────

@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    if not STRIPE_AVAILABLE:
        return jsonify({"error": "Stripe not configured"}), 500

    payload = request.get_data()
    sig     = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except Exception as e:
        log.warning("Webhook signature error: %s", e)
        return jsonify({"error": str(e)}), 400

    etype = event["type"]
    log.info("Stripe event: %s", etype)

    if etype == "customer.subscription.created":
        _handle_sub_created(event["data"]["object"])
    elif etype in ("customer.subscription.deleted", "customer.subscription.paused"):
        _handle_sub_cancelled(event["data"]["object"])
    elif etype == "customer.subscription.updated":
        _handle_sub_updated(event["data"]["object"])
    elif etype == "invoice.payment_failed":
        _handle_payment_failed(event["data"]["object"])

    return jsonify({"received": True})


def _get_email_from_sub(sub) -> str:
    try:
        customer = stripe.Customer.retrieve(sub["customer"])
        return customer.get("email", "")
    except Exception:
        return ""


def _plan_from_sub(sub) -> str:
    try:
        price_id  = sub["items"]["data"][0]["price"]["id"]
        price_obj = stripe.Price.retrieve(price_id, expand=["product"])
        return price_obj["product"].get("metadata", {}).get("plan", "starter")
    except Exception:
        return "starter"


def _handle_sub_created(sub):
    email  = _get_email_from_sub(sub)
    plan   = _plan_from_sub(sub)
    sub_id = sub["id"]
    key    = generate_key()
    # 31 days from now
    expires = time.time() + (31 * 86400)

    with get_db() as db:
        db.execute(
            "INSERT INTO licenses (key, email, plan, status, stripe_sub_id, created_at, expires_at) "
            "VALUES (?,?,?,'active',?,?,?)",
            (key, email, plan, sub_id, time.time(), expires)
        )
        db.commit()

    log.info("New license: %s %s plan=%s email=%s", key, sub_id, plan, email)
    _send_welcome_email(email, key, plan)


def _handle_sub_cancelled(sub):
    with get_db() as db:
        db.execute(
            "UPDATE licenses SET status='cancelled' WHERE stripe_sub_id=?",
            (sub["id"],)
        )
        db.commit()
    log.info("Subscription cancelled: %s", sub["id"])


def _handle_sub_updated(sub):
    plan = _plan_from_sub(sub)
    with get_db() as db:
        db.execute(
            "UPDATE licenses SET plan=? WHERE stripe_sub_id=?",
            (plan, sub["id"])
        )
        db.commit()


def _handle_payment_failed(invoice):
    sub_id = invoice.get("subscription")
    if sub_id:
        with get_db() as db:
            db.execute(
                "UPDATE licenses SET status='payment_failed' WHERE stripe_sub_id=?",
                (sub_id,)
            )
            db.commit()
        log.warning("Payment failed for subscription: %s", sub_id)


def _send_welcome_email(email: str, key: str, plan: str):
    """Send the license key email via SendGrid (if configured)."""
    log.info("License key for %s: %s  plan=%s", email, key, plan)
    sendgrid_key = os.getenv("SENDGRID_API_KEY", "")
    from_email   = os.getenv("FROM_EMAIL", "noreply@omni-ict.com")
    if not sendgrid_key or not email:
        log.warning("SendGrid not configured or no email — key not emailed: %s", key)
        return
    try:
        import urllib.request as _ur
        body = json.dumps({
            "personalizations": [{"to": [{"email": email}]}],
            "from": {"email": from_email, "name": "OMNI-ICT"},
            "subject": "Your OMNI-ICT License Key",
            "content": [{"type": "text/html", "value": f"""
<h2>Welcome to OMNI-ICT! 🚀</h2>
<p>Thank you for subscribing to the <strong>{plan.title()}</strong> plan.</p>

<h3>Your License Key</h3>
<p style="font-size:20px;font-family:monospace;background:#f4f4f4;padding:12px;border-radius:6px;">
  <strong>{key}</strong>
</p>

<h3>Get Started in 3 Steps</h3>
<ol>
  <li><strong>Open MT5 through our recommended broker</strong> (if you haven't already):<br>
      <a href="https://www.midasfx.com/?ib=1128101">https://www.midasfx.com/?ib=1128101</a></li>
  <li><strong>Message our Telegram bot</strong> to complete setup — no technical knowledge needed:<br>
      <a href="https://t.me/OMNI_ICT_setup_bot">t.me/OMNI_ICT_setup_bot</a><br>
      The bot will guide you step by step and generate your personalized install command.</li>
  <li><strong>Run the one-line install command</strong> on your VPS — the bot gives it to you.</li>
</ol>

<p>
  <a href="https://t.me/OMNI_ICT_setup_bot"
     style="display:inline-block;background:#0088cc;color:#fff;font-weight:bold;
            padding:12px 24px;border-radius:8px;text-decoration:none;font-size:16px;">
    👉 Start Setup on Telegram
  </a>
</p>

<p>Need help? Join the community: <a href="https://t.me/OMNI_ICT_community">t.me/OMNI_ICT_community</a></p>
<p style="color:#888;font-size:12px;">Keep this key private. Your subscription renews automatically each month.</p>
"""}]
        }).encode()
        req = _ur.Request(
            "https://api.sendgrid.com/v3/mail/send",
            data=body,
            headers={
                "Authorization": f"Bearer {sendgrid_key}",
                "Content-Type": "application/json",
            }
        )
        with _ur.urlopen(req, timeout=10) as r:
            log.info("Welcome email sent to %s (status %s)", email, r.status)
    except Exception as e:
        log.error("Failed to send welcome email to %s: %s", email, e)


# ── Admin API ─────────────────────────────────────────────────

@app.route("/admin/keys", methods=["GET"])
@require_admin
def admin_list_keys():
    with get_db() as db:
        rows = db.execute(
            "SELECT key, email, plan, status, created_at, expires_at, last_check "
            "FROM licenses ORDER BY created_at DESC"
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/admin/keys", methods=["POST"])
@require_admin
def admin_create_key():
    data   = request.json or {}
    email  = data.get("email", "")
    plan   = data.get("plan", "starter")
    days   = int(data.get("days", 31))
    key    = data.get("key") or generate_key()
    expires = time.time() + (days * 86400)

    with get_db() as db:
        db.execute(
            "INSERT INTO licenses (key, email, plan, status, created_at, expires_at) "
            "VALUES (?,?,?,'active',?,?)",
            (key, email, plan, time.time(), expires)
        )
        db.commit()

    log.info("Admin created key: %s email=%s plan=%s days=%d", key, email, plan, days)
    return jsonify({"key": key, "plan": plan, "email": email, "expires_days": days}), 201


@app.route("/admin/keys/<key>", methods=["DELETE"])
@require_admin
def admin_revoke_key(key):
    with get_db() as db:
        db.execute("UPDATE licenses SET status='revoked' WHERE key=?", (key.upper(),))
        db.commit()
    return jsonify({"revoked": key.upper()})


@app.route("/admin/keys/<key>/extend", methods=["POST"])
@require_admin
def admin_extend_key(key):
    data = request.json or {}
    days = int(data.get("days", 31))
    with get_db() as db:
        db.execute(
            "UPDATE licenses SET expires_at = MAX(expires_at, ?) + ?, status='active' WHERE key=?",
            (time.time(), days * 86400, key.upper())
        )
        db.commit()
    return jsonify({"extended": key.upper(), "days": days})


@app.route("/admin/stats", methods=["GET"])
@require_admin
def admin_stats():
    with get_db() as db:
        total  = db.execute("SELECT COUNT(*) FROM licenses").fetchone()[0]
        active = db.execute("SELECT COUNT(*) FROM licenses WHERE status='active'").fetchone()[0]
        today  = db.execute(
            "SELECT COUNT(*) FROM check_log WHERE ts > ? AND result='ok'",
            (time.time() - 86400,)
        ).fetchone()[0]
        by_plan = db.execute(
            "SELECT plan, COUNT(*) as n FROM licenses WHERE status='active' GROUP BY plan"
        ).fetchall()
    return jsonify({
        "total_keys":  total,
        "active_keys": active,
        "checks_24h":  today,
        "by_plan":     {r["plan"]: r["n"] for r in by_plan},
    })


# ── Health check ──────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": time.time()})


# ── Client relay API (used by omni_bridge.py on client VPS) ───

@app.route("/client/notify", methods=["POST"])
def client_notify():
    """Bridge posts trade alerts / status updates here."""
    data = request.json or {}
    key  = (data.get("key") or "").strip().upper()
    msg  = data.get("message", "")
    if not key or not msg:
        return jsonify({"error": "key and message required"}), 400
    with get_db() as db:
        row = db.execute("SELECT status FROM licenses WHERE key=?", (key,)).fetchone()
        if not row or row["status"] != "active":
            return jsonify({"error": "invalid key"}), 403
        db.execute("INSERT INTO client_relay (license_key, direction, content, ts) VALUES (?,?,?,?)",
                   (key, "from_client", msg, time.time()))
        db.commit()
    return jsonify({"ok": True})


@app.route("/client/poll/<key>", methods=["GET"])
def client_poll(key):
    """Bridge polls here for pending commands from the community bot."""
    key = key.strip().upper()
    with get_db() as db:
        row = db.execute("SELECT status FROM licenses WHERE key=?", (key,)).fetchone()
        if not row or row["status"] != "active":
            return jsonify({"error": "invalid key"}), 403
        cmds = db.execute(
            "SELECT id, content FROM client_relay WHERE license_key=? AND direction='to_client' AND delivered=0 ORDER BY ts",
            (key,)
        ).fetchall()
        ids = [r["id"] for r in cmds]
        if ids:
            db.execute(f"UPDATE client_relay SET delivered=1 WHERE id IN ({','.join('?'*len(ids))})", ids)
            db.commit()
    return jsonify({"commands": [{"id": r["id"], "text": r["content"]} for r in cmds]})


@app.route("/client/respond", methods=["POST"])
def client_respond():
    """Bridge posts command results back here; community bot relays to user."""
    data = request.json or {}
    key  = (data.get("key") or "").strip().upper()
    msg  = data.get("result", "")
    if not key or not msg:
        return jsonify({"error": "key and result required"}), 400
    with get_db() as db:
        db.execute("INSERT INTO client_relay (license_key, direction, content, ts) VALUES (?,?,?,?)",
                   (key, "from_client", msg, time.time()))
        db.commit()
    return jsonify({"ok": True})


@app.route("/client/session", methods=["POST"])
def client_session():
    """Community bot registers/updates a client session."""
    data = request.json or {}
    key  = (data.get("key") or "").strip().upper()
    if not key:
        return jsonify({"error": "key required"}), 400
    with get_db() as db:
        row = db.execute("SELECT status FROM licenses WHERE key=?", (key,)).fetchone()
        if not row or row["status"] != "active":
            return jsonify({"error": "invalid or inactive key"}), 403
        existing = db.execute("SELECT license_key FROM client_sessions WHERE license_key=?", (key,)).fetchone()
        fields = {k: v for k, v in data.items() if k != "key" and v is not None}
        if existing:
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                db.execute(f"UPDATE client_sessions SET {sets} WHERE license_key=?",
                           list(fields.values()) + [key])
        else:
            db.execute(
                "INSERT INTO client_sessions (license_key, telegram_chat_id, onboarding_step, created_at) VALUES (?,?,?,?)",
                (key, data.get("telegram_chat_id"), data.get("onboarding_step", "awaiting_login"), time.time())
            )
        db.commit()
    return jsonify({"ok": True})


@app.route("/client/session/<key>", methods=["GET"])
def get_client_session(key):
    key = key.strip().upper()
    with get_db() as db:
        row = db.execute("SELECT * FROM client_sessions WHERE license_key=?", (key,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        return jsonify(dict(row))


@app.route("/client/messages/<key>", methods=["GET"])
def client_messages(key):
    """Community bot polls here for client-side updates to relay."""
    key = key.strip().upper()
    token = request.headers.get("X-Admin-Token") or request.args.get("token")
    if token != ADMIN_TOKEN:
        abort(401)
    with get_db() as db:
        msgs = db.execute(
            "SELECT id, content FROM client_relay WHERE license_key=? AND direction='from_client' AND delivered=0 ORDER BY ts",
            (key,)
        ).fetchall()
        ids = [r["id"] for r in msgs]
        if ids:
            db.execute(f"UPDATE client_relay SET delivered=1 WHERE id IN ({','.join('?'*len(ids))})", ids)
            db.commit()
    return jsonify({"messages": [{"id": r["id"], "text": r["content"]} for r in msgs]})


@app.route("/install/<key>")
def install_script(key):
    """Returns a personalized bash install script for the client."""
    key = key.strip().upper()
    with get_db() as db:
        lic = db.execute("SELECT * FROM licenses WHERE key=?", (key,)).fetchone()
        ses = db.execute("SELECT * FROM client_sessions WHERE license_key=?", (key,)).fetchone()

    if not lic or lic["status"] != "active":
        return "echo 'Invalid or inactive license key.'", 400, {"Content-Type": "text/plain"}

    mt5_login    = (ses and ses["mt5_login"])    or "YOUR_MT5_LOGIN"
    mt5_server   = (ses and ses["mt5_server"])   or "YOUR_MT5_SERVER"
    mt5_password = (ses and ses["mt5_password"]) or "YOUR_MT5_PASSWORD"
    risk_mode    = (ses and ses["risk_mode"])     or "MODERATE"
    server_url   = request.host_url.rstrip("/")

    script = f"""#!/usr/bin/env bash
set -e
echo ""
echo "═══════════════════════════════════════════"
echo "  OMNI-ICT Auto Trader — Personalized Setup"
echo "═══════════════════════════════════════════"
echo ""

# Install deps
if ! command -v python3 &>/dev/null; then
  apt-get update -qq && apt-get install -y python3 python3-pip python3-venv git curl
fi

# Clone or update
if [ -d omni-ict ]; then
  cd omni-ict && git pull -q
else
  git clone -q https://github.com/fitfriendchris/Omni-full-ALGO-Trading-Bot.git omni-ict
  cd omni-ict
fi

# Create venv
python3 -m venv venv
venv/bin/pip install -q -r python/requirements.txt

# Write .env
cat > .env << 'ENVEOF'
OMNI_LICENSE_KEY={key}
OMNI_LICENSE_SERVER={server_url}
OMNI_MT5_LOGIN={mt5_login}
OMNI_MT5_SERVER={mt5_server}
OMNI_MT5_PASSWORD={mt5_password}
OMNI_RISK_MODE={risk_mode}
OMNI_PAPER_MODE=true
ENVEOF

echo ""
echo "✓ Installed and configured."
echo ""
echo "NEXT STEPS:"
echo "  1. Open MT5 and attach the OMNI EA to your chart"
echo "     (File → open data folder → MQL5/Experts → paste OMNI_EA.mq5)"
echo "  2. Start the bot:"
echo "     cd omni-ict && venv/bin/python python/watchdog.py"
echo ""
echo "  Your bot will send updates via @OMNI_ICT_setup_bot on Telegram."
echo ""
"""
    return script, 200, {
        "Content-Type": "text/plain",
        "Content-Disposition": f"inline; filename=install_{key}.sh"
    }


# ── Init on import (works with gunicorn) ──────────────────────
init_db()


# ── Start community bot in background ─────────────────────────
def _start_community_bot():
    import threading
    bot_token = os.getenv("COMMUNITY_BOT_TOKEN", "")
    if not bot_token:
        log.warning("COMMUNITY_BOT_TOKEN not set — community bot disabled")
        return
    try:
        from community_bot import run_bot
        t = threading.Thread(target=run_bot, daemon=True, name="community_bot")
        t.start()
        log.info("Community bot started in background thread")
    except Exception as e:
        log.error("Failed to start community bot: %s", e)

_start_community_bot()


# ── Main ──────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info("License server starting on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
