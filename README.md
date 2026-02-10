# Feishu Group Message Watcher Bot

This project runs one service:

- `src`: uses `lark-oapi` long connection (WebSocket) to receive Feishu group message events, match a regex pattern, and execute a server-side script directly.

## 1) Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2) Configure

Create `.env` and fill values:

```env
# Bot service
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_VERIFICATION_TOKEN=xxx
FEISHU_ENCRYPT_KEY=
MATCH_PATTERN='(?ms)^Features update summary:\s*\d{4}-\d{2}-\d{2}\s+.*?^missed\s+0\s*$'
SCRIPT_COMMAND=python scripts/example_task.py
SCRIPT_TIMEOUT_SEC=30
LOG_LEVEL=INFO
```

## 3) Start services

Feishu SDK long connection worker:

```bash
python src/main.py
```

`src/main.py` loads `.env` automatically.

## 4) Configure Feishu app

1. Create a custom app in Feishu Open Platform.
2. In event subscription, select long connection mode (no public callback URL needed).
3. Subscribe to `im.message.receive_v1`.
4. Grant required message/event permissions and publish the app version.
5. Add bot to your target group chat.

## 5) How it works

1. This service receives Feishu events over long connection.
2. Bot checks only group text messages.
3. Bot applies `MATCH_PATTERN`.
4. If matched, bot sends a "script started" message to the same chat.
5. Bot executes `SCRIPT_COMMAND` locally.

Script environment variables:

- `TRIGGER_TEXT`
- `TRIGGER_CHAT_ID`
- `TRIGGER_SENDER_ID`
- `TRIGGER_MESSAGE_ID`
- `TRIGGER_MATCHED_TEXT`

## Security notes

- Keep `FEISHU_APP_SECRET` private.
- Keep `SCRIPT_COMMAND` fixed to trusted commands.
