# Telegram LM Studio Bot

Bot for Telegram using LM Studio with anti-spam and conversation history compression.

**Made 50% by ChatGPT, 50% by Claude, 50% by @darthjahus.**

## Features
- Responds to Telegram messages via LM Studio model.
- Saves conversation history and compresses it automatically.
- Summarizes long blocks of messages to limit context sent to the model.
- Anti-spam: temporary mute if too many messages are sent in a short time.
- Priority queue to manage multiple users.

## Installation
1. Clone the repo
2. Install dependencies
3. Create `config.json` with these keys:

```json
{
  "BOT_TOKEN": "<your_token>",
  "SYSTEM_PROMPT": "file:system_prompt.txt",
  "SUMMARY_PROMPT": "file:summary_prompt.txt",
  "ADMIN_ID": 0,
  "DATA_DIR": "data",
  "LM_STUDIO_URL": "http://localhost:8080/api/v1/generate",
  "MODEL_NAME": "openai/gpt-oss-20b",
  "MAX_CONTEXT": 20,
  "ERROR_LIMIT": 5,
  "POLL_INTERVAL": 1
}
```
4. Create or edit `system_prompt.txt` and `summary_prompt.txt` with your model instructions.

## Notion tool-calling (optional)
The bot can call Notion tools (search, create, update, archive/delete, get page) while
chatting. To enable it:
1. Create a Notion internal integration and share the relevant pages/databases with it.
2. Add these optional keys to `config.json` (or set `NOTION_API_KEY` as an environment
   variable instead, which takes precedence):

```json
{
  "NOTION_API_KEY": "secret_xxx",
  "NOTION_DEFAULT_PARENT_ID": "<database-or-page-id>",
  "NOTION_DEFAULT_PARENT_TYPE": "database_id",
  "TOOLS_ENABLED": true
}
```

If LM Studio exposes Notion's MCP tools natively, those are used automatically. Otherwise,
the bot falls back to a built-in Python implementation that calls the Notion REST API
directly. Set `"TOOLS_ENABLED": false` to fully disable tool definitions and restore the
plain chat-only behavior.

## Notes
* Long messages are split to avoid Telegram limits.
* Compressed history keeps both assistant and user messages.
* `MAX_CONTEXT` refers to the number of **user messages** considered for context (roughly).

## ToDo
-  [ ] The script contains a mix of English and French
