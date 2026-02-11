import json
import logging
import os
import re
import signal
import subprocess
import sys
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
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
FEISHU_VERIFICATION_TOKEN = os.getenv("FEISHU_VERIFICATION_TOKEN", "")
FEISHU_ENCRYPT_KEY = os.getenv("FEISHU_ENCRYPT_KEY", "")

_compiled_pattern = re.compile(MATCH_PATTERN)
_feishu_client: Any = lark.Client.builder().app_id(FEISHU_APP_ID).app_secret(FEISHU_APP_SECRET).build()


def _extract_text(content_raw: str) -> str:
    if not content_raw:
        return ""

    try:
        content = json.loads(content_raw)
    except json.JSONDecodeError:
        return ""

    return content.get("text", "")


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


def _handle_message_event(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
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
    if not text:
        return

    matched = _compiled_pattern.search(text)
    if not matched:
        return

    logger.info(
        "Pattern matched: message_id=%s chat_id=%s matched_text=%r",
        message.message_id,
        message.chat_id,
        matched.group(0),
    )

    notify_payload = {
        "message_id": message.message_id,
        "chat_id": message.chat_id,
        "sender_id": sender.open_id if sender else None,
        "text": text,
        "matched_text": matched.group(0),
    }

    try:
        returncode, stdout, stderr = _run_script(
            notify_payload,
            on_started=lambda: _send_text_to_chat(
                message.chat_id,
                f"Executing update script triggered by message {message.message_id}",
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


def main() -> None:
    _validate_env()

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

    logger.info("Starting Feishu long-connection client")
    ws_client.start()


if __name__ == "__main__":
    main()
