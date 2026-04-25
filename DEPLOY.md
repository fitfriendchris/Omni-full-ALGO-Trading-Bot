# OMNI-ICT — Launch Checklist

Complete these in order. Estimated time: 45–60 minutes.

---

## Step 1 — Push Code to GitHub

```bash
gh auth login
# Choose: GitHub.com → HTTPS → Login with a web browser

git push -u origin main
```

Your code is now private at:
https://github.com/fitfriendchris/Omni-full-ALGO-Trading-Bot

---

## Step 2 — Deploy the License Server (Railway)

Railway gives you a free always-on server for the license validator.

```bash
# Install Railway CLI
npm install -g @railway/cli
# OR on Mac without npm:
brew install railway

# Login
railway login

# Deploy from the license_server folder
cd license_server
railway init        # create new project, name it "omni-ict-license"
railway up          # deploy

# Get your public URL
railway domain      # e.g. https://omni-ict-license.up.railway.app
```

Then set environment variables in the Railway dashboard:
- `ADMIN_TOKEN` → a long random string (your secret for admin API)
- `STRIPE_SECRET_KEY` → from Stripe (added in Step 3)
- `STRIPE_WEBHOOK_SECRET` → from Stripe (added in Step 3)
- `SENDGRID_API_KEY` → optional, for automatic emails

---

## Step 3 — Set Up Stripe

### 3a. Create your Stripe account
Go to https://stripe.com and sign up (free).

### 3b. Create products + webhook automatically
```bash
cd /Users/owner/omni-ict/license_server
pip install stripe
STRIPE_SECRET_KEY=sk_live_... \
LICENSE_SERVER_URL=https://your-railway-url.up.railway.app \
python stripe_setup.py
```

This creates:
- 3 subscription products ($49 / $99 / $199/mo)
- Webhook pointing to your license server
- Prints the `STRIPE_WEBHOOK_SECRET` to copy into Railway

### 3c. Create payment links (5 minutes)
In Stripe dashboard → Products → click each product → "Create payment link"
These are the URLs you'll put on your website/landing page.

### 3d. Update the license server with Stripe keys
In Railway dashboard → your project → Variables:
```
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...  (from stripe_setup.py output)
```

---

## Step 4 — Update the License Server URL in the Bot

```bash
# Edit python/license.py line 16:
LICENSE_SERVER = os.getenv("OMNI_LICENSE_SERVER", "https://YOUR-RAILWAY-URL.up.railway.app")
```

Then commit and push:
```bash
git add python/license.py
git commit -m "Set production license server URL"
git push
```

---

## Step 5 — Create Your First Release (Customer Download)

Tag a release — GitHub Actions builds the ZIP automatically:

```bash
git tag v1.0.0
git push origin v1.0.0
```

Wait ~2 minutes → go to https://github.com/fitfriendchris/Omni-full-ALGO-Trading-Bot/releases

Download the ZIP, test it yourself:
```bash
unzip omni-ict-release.zip
bash setup.sh
```

Copy the release ZIP download URL — this is what you send to customers.

---

## Step 6 — Test End-to-End

### Create a test license key manually:
```bash
curl -X POST https://your-railway-url.up.railway.app/admin/keys \
  -H "X-Admin-Token: your-admin-token" \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","plan":"starter","days":31}'
```

Response: `{"key": "OMNI-XXXX-XXXX-XXXX", ...}`

### Validate it:
```bash
curl "https://your-railway-url.up.railway.app/validate?key=OMNI-XXXX-XXXX-XXXX"
```

Should return: `{"valid": true, "plan": "starter", ...}`

### Test the bot with it:
```bash
cd /Users/owner/omni-ict
# Temporarily swap in the test key:
OMNI_LICENSE_KEY=OMNI-XXXX-XXXX-XXXX python python/watchdog.py --status
```

If it prints status instead of "License check failed" — it's working.

---

## Step 7 — Check License Server Stats

```bash
# How many active licenses, checks in last 24h, breakdown by plan:
curl -H "X-Admin-Token: your-admin-token" \
  https://your-railway-url.up.railway.app/admin/stats
```

---

## Step 8 — Optional: Custom Domain

Point `license.omni-ict.com` to your Railway URL:
- In Railway: Settings → Domains → Add custom domain
- In your DNS: add CNAME `license` → `your-project.up.railway.app`

Then update `python/license.py`:
```python
LICENSE_SERVER = os.getenv("OMNI_LICENSE_SERVER", "https://license.omni-ict.com")
```

---

## Admin API Quick Reference

All requests need header: `X-Admin-Token: your-admin-token`

| Action | Command |
|--------|---------|
| List all keys | `GET /admin/keys` |
| Create key | `POST /admin/keys` `{"email":"..","plan":"starter","days":31}` |
| Revoke key | `DELETE /admin/keys/OMNI-XXXX-XXXX-XXXX` |
| Extend key | `POST /admin/keys/OMNI-XXXX-XXXX-XXXX/extend` `{"days":31}` |
| Stats | `GET /admin/stats` |

---

## Revenue Streams

1. **Subscriptions** — Stripe recurring billing, automatic key provisioning
2. **Broker affiliate** — Every customer opens MT5 via https://www.midasfx.com/?ib=1128101
3. **Upsells** — Starter → Pro → Elite as users gain confidence

---

## Support Setup

Create a Telegram group: https://t.me/  
Name it `OMNI-ICT Community` — link it in the startup guide and welcome email.
Pin a message with the setup guide link and common fixes.
