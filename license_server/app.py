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
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("license_server")

DB_PATH    = Path(os.getenv("DB_PATH", "licenses.db"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-this-secret")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")


# ── Database ──────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
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
<h3>Getting Started</h3>
<ol>
  <li>Open your MT5 account through our recommended broker:<br>
      <a href="https://www.midasfx.com/?ib=1128101">https://www.midasfx.com/?ib=1128101</a></li>
  <li>Download and run the setup wizard — full instructions at:<br>
      <a href="https://omni-ict.com/start">https://omni-ict.com/start</a></li>
  <li>Paste your license key when prompted</li>
</ol>
<p>Need help? Join our community: <a href="https://t.me/omni_ict_community">t.me/omni_ict_community</a></p>
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


# ── Main ──────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", 5000))
    log.info("License server starting on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
