import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Callable

import lark_oapi as lark


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            if value and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]

            os.environ.setdefault(key, value)


_load_dotenv()


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("feishu-bot")

MATCH_PATTERN = os.getenv("MATCH_PATTERN", r"^/run\\s+.+")
SCRIPT_COMMAND = os.getenv("SCRIPT_COMMAND", "")
SCRIPT_TIMEOUT_SEC = int(os.getenv("SCRIPT_TIMEOUT_SEC", "7200"))
HEALTH_CHECK_INTERVAL_SEC = int(os.getenv("HEALTH_CHECK_INTERVAL_SEC", "600"))
POLLING_INTERVAL_FAST_SEC = int(os.getenv("POLLING_INTERVAL_FAST_SEC", "30"))
POLLING_INTERVAL_SLOW_SEC = int(os.getenv("POLLING_INTERVAL_SLOW_SEC", "300"))
POLLING_FAST_START_HOUR = int(os.getenv("POLLING_FAST_START_HOUR", "14"))
POLLING_FAST_END_HOUR = int(os.getenv("POLLING_FAST_END_HOUR", "15"))
POLLING_CHAT_IDS = [cid.strip() for cid in os.getenv("POLLING_CHAT_IDS", "").split(",") if cid.strip()]
STATE_FILE_PATH = os.getenv("STATE_FILE_PATH", ".message_state.json")
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_VERIFICATION_TOKEN = os.getenv("FEISHU_VERIFICATION_TOKEN", "")
FEISHU_ENCRYPT_KEY = os.getenv("FEISHU_ENCRYPT_KEY", "")

_compiled_pattern = re.compile(MATCH_PATTERN)
_feishu_client: Any = lark.Client.builder().app_id(FEISHU_APP_ID).app_secret(FEISHU_APP_SECRET).build()
_last_message_time: float = time.time()
_health_check_lock = threading.Lock()
_processed_message_ids: set[str] = set()
_processed_messages_lock = threading.Lock()
_last_processed_timestamps: dict[str, int] = {}
_last_match_date: str | None = None  # Date when pattern last matched (YYYY-MM-DD)
_state_lock = threading.Lock()

# UTC+8 timezone
UTC_PLUS_8 = timezone(timedelta(hours=8))


def _extract_text(content_raw: str) -> str:
    if not content_raw:
        return ""

    try:
        content = json.loads(content_raw)
    except json.JSONDecodeError:
        return ""

    return content.get("text", "")


def _load_state() -> None:
    """Load last processed timestamps from state file."""
    global _last_processed_timestamps, _last_match_date
    
    if not os.path.exists(STATE_FILE_PATH):
        logger.info("No state file found, starting fresh")
        return
    
    try:
        with open(STATE_FILE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
            with _state_lock:
                _last_processed_timestamps = state.get("last_processed_timestamps", {})
                _last_match_date = state.get("last_match_date")
            logger.info(
                "Loaded state from %s: %d chat(s) tracked, last match: %s",
                STATE_FILE_PATH,
                len(_last_processed_timestamps),
                _last_match_date or "never",
            )
    except Exception as e:
        logger.warning("Failed to load state file: %s", e)


def _save_state() -> None:
    """Save last processed timestamps to state file."""
    try:
        with _state_lock:
            state = {
                "last_processed_timestamps": _last_processed_timestamps,
                "last_match_date": _last_match_date,
            }
        
        with open(STATE_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        
        logger.debug("Saved state to %s", STATE_FILE_PATH)
    except Exception as e:
        logger.warning("Failed to save state file: %s", e)


def _is_today(timestamp_ms: int) -> bool:
    """Check if a timestamp (in milliseconds) is from today (UTC+8)."""
    if not timestamp_ms:
        return False
    
    msg_date = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC_PLUS_8).date()
    today = datetime.now(UTC_PLUS_8).date()
    return msg_date == today


def _run_script(trigger: dict[str, Any], on_started: Callable[[], None] | None = None) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["TRIGGER_TEXT"] = trigger.get("text") or ""
    env["TRIGGER_CHAT_ID"] = trigger.get("chat_id") or ""
    env["TRIGGER_SENDER_ID"] = trigger.get("sender_id") or ""
    env["TRIGGER_MESSAGE_ID"] = trigger.get("message_id") or ""
    env["TRIGGER_MATCHED_TEXT"] = trigger.get("matched_text") or ""

    process = subprocess.Popen(
        ["bash", "-lc", SCRIPT_COMMAND],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    if on_started:
        on_started()

    try:
        stdout, stderr = process.communicate(timeout=SCRIPT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise exc

    return process.returncode, stdout, stderr


def _send_text_to_chat(chat_id: str, text: str) -> None:
    if not chat_id:
        return

    request = (
        lark.im.v1.CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            lark.im.v1.CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )

    response = _feishu_client.im.v1.message.create(request)
    if not response.success():
        logger.warning(
            "Failed to send Feishu message: code=%s msg=%s",
            response.code,
            response.msg,
        )


def _process_message(message_id: str, chat_id: str, sender_id: str | None, text: str) -> None:
    """Process a message and execute script if pattern matches."""
    global _last_match_date
    
    # Check if already processed
    with _processed_messages_lock:
        if message_id in _processed_message_ids:
            return
        _processed_message_ids.add(message_id)
        # Keep set size manageable (last 1000 messages)
        if len(_processed_message_ids) > 1000:
            _processed_message_ids.pop()

    if not text:
        return

    matched = _compiled_pattern.search(text)
    if not matched:
        return

    logger.info(
        "Pattern matched: message_id=%s chat_id=%s matched_text=%r",
        message_id,
        chat_id,
        matched.group(0),
    )

    # Record match date to stop polling for the rest of the day
    today = datetime.now(UTC_PLUS_8).strftime("%Y-%m-%d")
    with _state_lock:
        _last_match_date = today
    _save_state()
    logger.info("Pattern matched today (%s), polling will stop until 14:00 tomorrow", today)

    notify_payload = {
        "message_id": message_id,
        "chat_id": chat_id,
        "sender_id": sender_id,
        "text": text,
        "matched_text": matched.group(0),
    }

    try:
        returncode, stdout, stderr = _run_script(
            notify_payload,
            on_started=lambda: _send_text_to_chat(
                chat_id,
                f"Executing update script triggered by message {message_id}",
            ),
        )
    except subprocess.TimeoutExpired:
        logger.exception("Script timed out after %ss", SCRIPT_TIMEOUT_SEC)
        return
    except Exception:
        logger.exception("Failed to execute script")
        return

    logger.info("Script finished: returncode=%s", returncode)
    if stdout:
        logger.info("Script stdout: %s", stdout.strip())
    if stderr:
        logger.warning("Script stderr: %s", stderr.strip())


def _handle_message_event(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
    global _last_message_time

    # Update last message time for health check
    with _health_check_lock:
        _last_message_time = time.time()

    event = getattr(data, "event", None)
    if not event or not event.message:
        return

    message = event.message
    sender = event.sender.sender_id if event.sender and event.sender.sender_id else None
    if message.chat_type != "group":
        text_preview = _extract_text(message.content or "")
        logger.debug(
            "Received message: chat_type=%s chat_id=%s sender_open_id=%s text=%r",
            message.chat_type,
            message.chat_id,
            sender.open_id if sender else None,
            text_preview,
        )
        return

    text = _extract_text(message.content or "")
    logger.debug(
        "Received message: chat_type=%s chat_id=%s sender_open_id=%s text=%r",
        message.chat_type,
        message.chat_id,
        sender.open_id if sender else None,
        text,
    )

    # Process the message using common logic
    _process_message(
        message_id=message.message_id,
        chat_id=message.chat_id,
        sender_id=sender.open_id if sender else None,
        text=text,
    )


def _build_dispatcher() -> lark.EventDispatcherHandler:
    return (
        lark.EventDispatcherHandler.builder(
            FEISHU_VERIFICATION_TOKEN,
            FEISHU_ENCRYPT_KEY,
        )
        .register_p2_im_message_receive_v1(_handle_message_event)
        .build()
    )


def _validate_env() -> None:
    missing = []
    if not FEISHU_APP_ID:
        missing.append("FEISHU_APP_ID")
    if not FEISHU_APP_SECRET:
        missing.append("FEISHU_APP_SECRET")
    if not SCRIPT_COMMAND:
        missing.append("SCRIPT_COMMAND")
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


def _health_check_loop() -> None:
    """Monitor connection health and exit if no messages received for too long."""
    while True:
        time.sleep(HEALTH_CHECK_INTERVAL_SEC)

        with _health_check_lock:
            elapsed = time.time() - _last_message_time

        if elapsed > HEALTH_CHECK_INTERVAL_SEC:
            logger.error(
                "Health check failed: no messages received for %.1f seconds (threshold: %d seconds). Exiting to trigger restart.",
                elapsed,
                HEALTH_CHECK_INTERVAL_SEC,
            )
            os._exit(1)
        else:
            logger.debug("Health check passed: last message received %.1f seconds ago", elapsed)


def _get_polling_interval() -> int | None:
    """
    Get the current polling interval based on time of day and pattern match status (UTC+8).
    Returns None if polling should be skipped.
    
    Schedule:
    - Before 14:00: No polling
    - 14:00-15:00: Fast polling (30 seconds)
    - After 15:00 (no match yet): Slow polling (5 minutes)
    - If pattern matched today: Stop polling until 14:00 tomorrow
    """
    now = datetime.now(UTC_PLUS_8)
    current_hour = now.hour
    today = now.strftime("%Y-%m-%d")
    
    # Check if pattern was matched today
    with _state_lock:
        matched_today = (_last_match_date == today)
    
    if matched_today:
        # Pattern already matched today, stop polling until tomorrow
        return None
    
    if current_hour < POLLING_FAST_START_HOUR:
        # Before 14:00 - no polling
        return None
    elif POLLING_FAST_START_HOUR <= current_hour < POLLING_FAST_END_HOUR:
        # 14:00-15:00 - fast polling (30s)
        return POLLING_INTERVAL_FAST_SEC
    else:
        # After 15:00 and no match yet - slow polling (5 minutes)
        return POLLING_INTERVAL_SLOW_SEC


def _poll_messages_loop() -> None:
    """Poll message history from configured chat groups to catch bot messages."""
    if not POLLING_CHAT_IDS:
        logger.info("Message polling disabled (POLLING_CHAT_IDS not configured)")
        return

    logger.info(
        "Starting message polling for %d chat(s) with time-based intervals",
        len(POLLING_CHAT_IDS),
    )
    logger.info(
        "Polling schedule: %d:00-%d:00=%ds, after %d:00=%ds, stops after pattern match",
        POLLING_FAST_START_HOUR,
        POLLING_FAST_END_HOUR,
        POLLING_INTERVAL_FAST_SEC,
        POLLING_FAST_END_HOUR,
        POLLING_INTERVAL_SLOW_SEC,
    )

    last_logged_state = None
    
    while True:
        try:
            now = datetime.now(UTC_PLUS_8)
            today = now.strftime("%Y-%m-%d")
            current_hour = now.hour
            
            with _state_lock:
                matched_today = (_last_match_date == today)
            
            interval = _get_polling_interval()
            
            # Determine current state for logging
            if matched_today:
                current_state = "matched_today"
            elif current_hour < POLLING_FAST_START_HOUR:
                current_state = "before_14"
            elif POLLING_FAST_START_HOUR <= current_hour < POLLING_FAST_END_HOUR:
                current_state = f"fast_{interval}s"
            else:
                current_state = f"slow_{interval}s"
            
            # Log state changes
            if current_state != last_logged_state:
                if matched_today:
                    logger.info(
                        "Polling stopped: pattern already matched today (%s), will resume at %d:00 tomorrow",
                        today,
                        POLLING_FAST_START_HOUR,
                    )
                elif current_hour < POLLING_FAST_START_HOUR:
                    logger.info(
                        "Polling disabled: before %d:00 (current time: %s)",
                        POLLING_FAST_START_HOUR,
                        now.strftime("%H:%M:%S"),
                    )
                elif POLLING_FAST_START_HOUR <= current_hour < POLLING_FAST_END_HOUR:
                    logger.info(
                        "Fast polling active: %ds interval (current time: %s)",
                        interval,
                        now.strftime("%H:%M:%S"),
                    )
                else:
                    logger.info(
                        "Slow polling active: %ds interval (no match yet, current time: %s)",
                        interval,
                        now.strftime("%H:%M:%S"),
                    )
                last_logged_state = current_state
            
            if interval is not None:
                # Polling is active
                for chat_id in POLLING_CHAT_IDS:
                    _poll_chat_messages(chat_id)
                time.sleep(interval)
            else:
                # Polling is disabled, check every minute if we should start
                time.sleep(60)
                
        except Exception as e:
            logger.error("Error during message polling: %s", e)
            time.sleep(60)
                
        except Exception as e:
            logger.error("Error during message polling: %s", e)
            time.sleep(60)


def _poll_chat_messages(chat_id: str) -> None:
    """Fetch recent messages from a chat and process them."""
    try:
        request = (
            lark.im.v1.ListMessageRequest.builder()
            .container_id_type("chat")
            .container_id(chat_id)
            .page_size(20)  # Fetch last 20 messages
            .build()
        )

        response = _feishu_client.im.v1.message.list(request)

        if not response.success():
            logger.warning(
                "Failed to fetch messages from chat %s: code=%s msg=%s",
                chat_id,
                response.code,
                response.msg,
            )
            return

        # Update last message time for health check
        global _last_message_time
        with _health_check_lock:
            _last_message_time = time.time()

        if not response.data or not response.data.items:
            logger.debug("No messages found in chat %s", chat_id)
            return

        # Get last processed timestamp for this chat
        with _state_lock:
            last_timestamp = _last_processed_timestamps.get(chat_id, 0)

        max_timestamp = last_timestamp
        processed_count = 0

        # Process messages in chronological order (oldest first)
        for message in reversed(response.data.items):
            if not message.message_id:
                continue

            # Parse message timestamp (in milliseconds)
            msg_timestamp = int(message.create_time) if message.create_time else 0

            # Skip if not from today
            if not _is_today(msg_timestamp):
                logger.debug(
                    "Skipping message %s: not from today (timestamp: %s)",
                    message.message_id,
                    msg_timestamp,
                )
                continue

            # Skip if already processed (timestamp <= last processed)
            if msg_timestamp <= last_timestamp:
                logger.debug(
                    "Skipping message %s: already processed (timestamp: %s <= %s)",
                    message.message_id,
                    msg_timestamp,
                    last_timestamp,
                )
                continue

            text = _extract_text(message.body.content if message.body else "")
            sender_id = message.sender.id if message.sender else None

            logger.debug(
                "Polled message: message_id=%s chat_id=%s sender_id=%s timestamp=%s text=%r",
                message.message_id,
                chat_id,
                sender_id,
                msg_timestamp,
                text,
            )

            _process_message(
                message_id=message.message_id,
                chat_id=chat_id,
                sender_id=sender_id,
                text=text,
            )

            processed_count += 1
            max_timestamp = max(max_timestamp, msg_timestamp)

        # Update last processed timestamp if we processed any messages
        if processed_count > 0:
            with _state_lock:
                _last_processed_timestamps[chat_id] = max_timestamp
            _save_state()
            logger.info(
                "Processed %d new message(s) from chat %s (last timestamp: %s)",
                processed_count,
                chat_id,
                max_timestamp,
            )

    except Exception as e:
        logger.error("Error polling messages from chat %s: %s", chat_id, e)


def main() -> None:
    _validate_env()
    _load_state()

    dispatcher = _build_dispatcher()
    ws_client = lark.ws.Client(
        FEISHU_APP_ID,
        FEISHU_APP_SECRET,
        event_handler=dispatcher,
        log_level=lark.LogLevel.INFO,
    )

    def _stop_handler(signum: int, frame: Any) -> None:
        logger.info("Received signal %s, exiting", signum)
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop_handler)
    signal.signal(signal.SIGTERM, _stop_handler)

    # Start health check thread
    health_check_thread = threading.Thread(target=_health_check_loop, daemon=True)
    health_check_thread.start()
    logger.info("Started health check monitor (interval: %d seconds)", HEALTH_CHECK_INTERVAL_SEC)

    # Start message polling thread
    polling_thread = threading.Thread(target=_poll_messages_loop, daemon=True)
    polling_thread.start()

    logger.info("Starting Feishu long-connection client")
    ws_client.start()


if __name__ == "__main__":
    main()
