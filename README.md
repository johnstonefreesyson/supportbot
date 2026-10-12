# Depixfy – Swap Depix Bot

A Telegram bot that allows users to swap DePix to USDT across multiple blockchain networks.

## Features
- Convert DePix → USDT
- Network selection: Ethereum, Polygon, BSC, Solana, Tron
- 1% fee deducted before quoting (minimum 1 DePix)
- Wallet address collection
- English language interface

## Local Setup

```bash
pip install -r requirements.txt
export BOT_TOKEN=your_telegram_bot_token
python bot.py
```

## Deploy on Railway

1. Push this folder to a GitHub repository
2. Go to [railway.app](https://railway.app) and create a new project
3. Click **"Deploy from GitHub repo"** and select your repo
4. Go to your service → **Variables** tab
5. Add this environment variable:
   - `BOT_TOKEN` = your Telegram bot token
6. Railway will automatically deploy your bot as a background worker

## Environment Variables

| Variable | Description |
|----------|-------------|
| `BOT_TOKEN` | Your Telegram bot token from @BotFather |

## Customization

- **Exchange rates**: Edit `EXCHANGE_RATES` in `bot.py` to set real rates or integrate an API
- **Fee**: Change `FEE_PERCENT` and `MIN_FEE` in `bot.py`
- **Swap logic**: Replace the TODO section in `enter_wallet()` with your actual swap/payment backend

## Commands

- `/start` – Begin a new swap
- `/cancel` – Cancel current swap
