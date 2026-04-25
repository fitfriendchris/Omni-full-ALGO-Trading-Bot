# OMNI-ICT Auto Trader — Startup Guide

> **Estimated setup time: 20–30 minutes**
> Paper mode is on by default — no real money at risk until you explicitly turn it off.

---

## Table of Contents

1. [What You Need](#1-what-you-need)
2. [Open Your MT5 Account](#2-open-your-mt5-account)
3. [Get a VPS (Recommended)](#3-get-a-vps-recommended)
4. [Install the Bot](#4-install-the-bot)
5. [Install MetaTrader 5 & the EA](#5-install-metatrader-5--the-ea)
6. [Create Your Telegram Bot](#6-create-your-telegram-bot)
7. [Run the Setup Wizard](#7-run-the-setup-wizard)
8. [Go Live Checklist](#8-go-live-checklist)
9. [Telegram Commands Reference](#9-telegram-commands-reference)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. What You Need

| Item | Cost | Required |
|------|------|----------|
| OMNI-ICT license key | Your subscription | ✅ Yes |
| MT5 trading account (MidasFX) | Free | ✅ Yes |
| Telegram account | Free | ✅ Yes |
| VPS or always-on PC | $10–15/mo | Recommended |
| Anthropic API key (AI analysis) | Free tier | Optional |

---

## 2. Open Your MT5 Account

**Use our recommended broker to get the best spreads and execution:**

## 👉 [Open Account at MidasFX](https://www.midasfx.com/?ib=1128101)

Steps:
1. Click the link above and register
2. Choose **MetaTrader 5** as your platform
3. Start with a **Demo Account** (free, no risk) to test the bot
4. Once you're confident, open a **Live Account**
5. Note down your:
   - **Account number** (e.g. `5049231810`)
   - **Password**
   - **Server name** (e.g. `MetaQuotes-Demo` or `MidasFX-Live01`)

> 💡 A demo account has all the same features as live — perfect for running the bot safely first.

---

## 3. Get a VPS (Recommended)

The bot needs to run 24/7. Running it on your personal computer means it stops when you close the lid. A VPS (Virtual Private Server) is a cheap cloud computer that runs all the time.

**Recommended VPS providers:**

| Provider | Price | OS | Link |
|----------|-------|----|------|
| **Contabo** | ~$7/mo | Ubuntu 22.04 | [contabo.com](https://contabo.com) |
| **DigitalOcean** | $12/mo | Ubuntu 22.04 | [digitalocean.com](https://digitalocean.com) |
| **Vultr** | $12/mo | Ubuntu 22.04 | [vultr.com](https://vultr.com) |
| **Hetzner** | €4/mo | Ubuntu 22.04 | [hetzner.com](https://hetzner.com) |

**Minimum specs:** 2 CPU, 2GB RAM, 20GB SSD — any $10/month plan works.

**Operating system:** Choose **Ubuntu 22.04 LTS** when you create the VPS.

> ⚠️ If you're on Windows and prefer not to get a VPS, you can run the bot directly on your PC — just make sure it stays on 24/7.

---

## 4. Install the Bot

### Option A — Docker (Easiest, Recommended)

Docker packages everything into one command. No Python setup needed.

**On your VPS / Ubuntu machine:**

```bash
# 1. Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# 2. Clone the bot
git clone https://github.com/YOUR_REPO/omni-ict.git
cd omni-ict

# 3. Run the setup wizard
bash setup.sh
```

### Option B — Direct Python

If you prefer to run without Docker:

```bash
# 1. Install Python 3.11+
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git

# 2. Clone the bot
git clone https://github.com/YOUR_REPO/omni-ict.git
cd omni-ict

# 3. Run the setup wizard
bash setup.sh
```

The setup wizard will ask for your license key, Telegram token, and MT5 credentials, then start everything automatically.

---

## 5. Install MetaTrader 5 & the EA

The bot communicates with MT5 through a bridge file. You need MT5 running with the OMNI Expert Advisor (EA) attached.

### On Windows (or Windows VPS):

1. Download MT5 from your broker: [MidasFX Downloads](https://www.midasfx.com/?ib=1128101)
2. Install and log in with your account credentials
3. Copy the EA file from the `mql5/` folder in this repo:
   - File: `mql5/OMNI_Bridge.ex5`
   - Destination: `C:\Users\YourName\AppData\Roaming\MetaQuotes\Terminal\YOUR_TERMINAL_ID\MQL5\Experts\`
4. In MT5:
   - Open any chart (e.g. EURUSD H1)
   - Drag **OMNI_Bridge** from the Navigator panel onto the chart
   - Click **OK** on the EA settings
   - Make sure **AutoTrading** is enabled (button in the toolbar)
5. The EA will create `omni_data.json` in `Terminal\Common\Files\`
6. Set `MT5_DATA_DIR` in your `.env` to that path

### On Linux VPS (using Wine):

```bash
# Install Wine + MT5
sudo dpkg --add-architecture i386
sudo apt update && sudo apt install -y wine wine32 wine64 winetricks
winetricks dotnet48

# Download and install MT5
wget https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe
wine mt5setup.exe
```

Then follow the same steps as Windows above, using the Wine path for the data directory.

---

## 6. Create Your Telegram Bot

1. Open Telegram on your phone
2. Search for **@BotFather** and start a chat
3. Send: `/newbot`
4. Choose a name (e.g. `My OMNI Trader`)
5. Choose a username (must end in `bot`, e.g. `my_omni_trader_bot`)
6. BotFather will give you a token like: `123456789:AABBccDDeeFF...`
7. Copy that token — you'll need it in the setup wizard

> 🔒 Keep your bot token private. Anyone with it can control your bot.

---

## 7. Run the Setup Wizard

If you haven't already:

```bash
cd omni-ict
bash setup.sh
```

The wizard will:
- Ask for your license key, Telegram token, and MT5 credentials
- Write your `.env` file automatically
- Start all bot services
- Confirm everything is running

**After setup, open Telegram:**
1. Find your bot (the one you created with BotFather)
2. Send `/start`
3. You should see a welcome message with all available commands
4. Send `/dashboard` to see your live account status

---

## 8. Go Live Checklist

Complete all of these before enabling live trading:

- [ ] Bot is running and responding to `/dashboard` in Telegram
- [ ] `/equity` shows your correct account balance
- [ ] MT5 is running with the EA attached (check `/status` — server should be ✅)
- [ ] At least 7 days of paper trading completed
- [ ] Paper trade results reviewed (use `/performance`)
- [ ] Risk settings configured (use `/settings`)
- [ ] Daily loss limit set conservatively (use `/set daily_loss 2.0`)

**To enable live trading:**
```
/set paper off
```
Then restart auto_trader:
```
/restart auto_trader
```

> ⚠️ **Only trade with money you can afford to lose.** Past paper trading performance does not guarantee future live results.

---

## 9. Telegram Commands Reference

### 📊 Dashboard
| Command | Description |
|---------|-------------|
| `/dashboard` | Full snapshot — equity, trades, signals, services |
| `/equity` | Live balance & equity from MT5 |
| `/trades` | All open positions with entry, SL, TP |
| `/pnl` | Today's P&L, drawdown, streaks |
| `/signals` | Latest ICT signals |
| `/performance` | Win rate, R-multiple, expectancy |
| `/risk` | Current risk settings |
| `/status` | Service health & PIDs |
| `/log auto_trader` | Last 20 lines of any service log |

### ⚙️ Settings
| Command | Description |
|---------|-------------|
| `/settings` | View all settings |
| `/set risk MODERATE` | LOW / MODERATE / HIGH |
| `/set freq NORMAL` | CONSERVATIVE / NORMAL / AGGRESSIVE |
| `/set base_risk 1.0` | Risk % per trade |
| `/set daily_loss 3.0` | Max daily loss before halt |
| `/set max_dd 10.0` | Max drawdown before halt |
| `/set min_conf 65` | Min signal confidence |
| `/set max_trades 3` | Max open positions |
| `/set paper on\|off` | Toggle paper mode |

### 🎛 Control
| Command | Description |
|---------|-------------|
| `/halt` | Stop new entries immediately |
| `/resume` | Lift the halt |
| `/restart auto_trader` | Restart a service |

### 🏦 Accounts
| Command | Description |
|---------|-------------|
| `/accounts` | List all accounts |
| `/account` | Show active account |
| `/switch demo` | Change active account |
| `/addaccount` | Add new MT5 account (guided) |

---

## 10. Troubleshooting

### Bot doesn't respond in Telegram
- Check it's running: look in your VPS terminal for the process
- Check the log: `tail -f logs/telegram_bot.log`
- Make sure your bot token is correct in `.env`
- Send `/start` first — the bot won't respond until you register

### `/equity` shows $0 or old data
- MT5 must be running with the EA attached
- Check MT5 is logged in and AutoTrading is enabled
- Verify `MT5_DATA_DIR` in `.env` points to the right folder
- Check `omni_data.json` exists in that folder

### Services keep restarting
- Check the log: `docker compose logs auto_trader` or `tail -f logs/auto_trader.log`
- Common cause: wrong MT5 credentials in `.env`
- Common cause: MT5 data file missing (EA not running)

### License validation failed
- Double-check your key in `.env` — no spaces, correct format
- Make sure your subscription is active at https://omni-ict.com
- Check your internet connection on the VPS

### MT5 connection lost
- The bot will automatically retry and halt trading if disconnected
- Restart MT5 and make sure the EA is attached
- Use `/resume` in Telegram after MT5 reconnects

---

## Support

- **Telegram support group:** [Join OMNI-ICT Community](https://t.me/omni_ict_community)
- **Email:** support@omni-ict.com
- **Docs:** https://omni-ict.com/docs

---

*OMNI-ICT does not guarantee trading profits. All trading involves risk. Never trade with money you cannot afford to lose. Past performance is not indicative of future results.*
