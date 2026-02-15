#!/usr/bin/env python3
"""Script to find chat_id for a group by name."""

import sys
import os

# Add src to path to import main module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from main import _load_dotenv, FEISHU_APP_ID, FEISHU_APP_SECRET
import lark_oapi as lark

_load_dotenv()

def find_chat_by_name(name_prefix: str):
    """Find chat IDs for groups whose name starts with the given prefix."""
    client = lark.Client.builder().app_id(FEISHU_APP_ID).app_secret(FEISHU_APP_SECRET).build()
    
    print(f"Searching for chats with name starting with '{name_prefix}'...\n")
    
    # List all chats
    request = lark.im.v1.ListChatRequest.builder().page_size(100).build()
    response = client.im.v1.chat.list(request)
    
    if not response.success():
        print(f"Error: Failed to list chats: code={response.code} msg={response.msg}")
        return
    
    if not response.data or not response.data.items:
        print("No chats found")
        return
    
    matches = []
    for chat in response.data.items:
        chat_name = chat.name or ""
        chat_id = chat.chat_id or ""
        
        if chat_name.startswith(name_prefix):
            matches.append((chat_id, chat_name))
            print(f"✓ Found: {chat_name}")
            print(f"  Chat ID: {chat_id}")
            print()
    
    if not matches:
        print(f"No chats found with name starting with '{name_prefix}'")
        print("\nAll available chats:")
        for chat in response.data.items:
            print(f"  - {chat.name} (ID: {chat.chat_id})")
    else:
        print(f"\nTotal matches: {len(matches)}")
        print("\nAdd to your .env file:")
        chat_ids = ",".join([chat_id for chat_id, _ in matches])
        print(f"POLLING_CHAT_IDS={chat_ids}")

if __name__ == "__main__":
    find_chat_by_name("Kronos")
