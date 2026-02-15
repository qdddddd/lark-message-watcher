#!/usr/bin/env python3
"""Test script to verify message polling functionality."""

import sys
import os
import re
from datetime import datetime, timezone, timedelta

# Add src to path to import main module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from main import (
    _load_dotenv,
    _extract_text,
    FEISHU_APP_ID,
    FEISHU_APP_SECRET,
    POLLING_CHAT_IDS,
    MATCH_PATTERN,
    _compiled_pattern,
    _is_today,
)
import lark_oapi as lark

_load_dotenv()

# UTC+8 timezone
UTC_PLUS_8 = timezone(timedelta(hours=8))

def test_fetch_messages():
    """Test fetching messages from configured chat."""
    if not POLLING_CHAT_IDS:
        print("Error: POLLING_CHAT_IDS not configured in .env")
        return
    
    client = lark.Client.builder().app_id(FEISHU_APP_ID).app_secret(FEISHU_APP_SECRET).build()
    
    for chat_id in POLLING_CHAT_IDS:
        print(f"\n{'='*80}")
        print(f"Fetching messages from chat: {chat_id}")
        print(f"{'='*80}\n")
        
        request = (
            lark.im.v1.ListMessageRequest.builder()
            .container_id_type("chat")
            .container_id(chat_id)
            .page_size(20)
            .build()
        )
        
        response = client.im.v1.message.list(request)
        
        if not response.success():
            print(f"❌ Error: Failed to fetch messages")
            print(f"   Code: {response.code}")
            print(f"   Message: {response.msg}")
            continue
        
        if not response.data or not response.data.items:
            print("No messages found in this chat")
            continue
        
        print(f"Found {len(response.data.items)} messages\n")
        print(f"Pattern to match: {MATCH_PATTERN}\n")
        
        matched_count = 0
        today_count = 0
        
        for i, message in enumerate(reversed(response.data.items), 1):
            msg_id = message.message_id or "N/A"
            msg_timestamp = int(message.create_time) if message.create_time else 0
            msg_time = datetime.fromtimestamp(msg_timestamp / 1000, tz=UTC_PLUS_8)
            sender_id = message.sender.id if message.sender else "N/A"
            text = _extract_text(message.body.content if message.body else "")
            
            is_today_msg = _is_today(msg_timestamp)
            matches_pattern = bool(_compiled_pattern.search(text)) if text else False
            
            if is_today_msg:
                today_count += 1
            if matches_pattern:
                matched_count += 1
            
            print(f"Message #{i}:")
            print(f"  ID: {msg_id}")
            print(f"  Time: {msg_time.strftime('%Y-%m-%d %H:%M:%S UTC+8')}")
            print(f"  Sender: {sender_id}")
            print(f"  Is today: {'✓' if is_today_msg else '✗'}")
            print(f"  Matches pattern: {'✓' if matches_pattern else '✗'}")
            print(f"  Text preview: {text[:100]}..." if len(text) > 100 else f"  Text: {text}")
            
            if matches_pattern:
                print(f"  🎯 THIS MESSAGE WOULD TRIGGER THE SCRIPT!")
            
            print()
        
        print(f"\n{'='*80}")
        print(f"Summary:")
        print(f"  Total messages: {len(response.data.items)}")
        print(f"  Messages from today: {today_count}")
        print(f"  Messages matching pattern: {matched_count}")
        print(f"{'='*80}\n")

if __name__ == "__main__":
    test_fetch_messages()
