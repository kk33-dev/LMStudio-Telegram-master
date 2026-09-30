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

## Notes
* Long messages are split to avoid Telegram limits.
* Compressed history keeps both assistant and user messages.
* `MAX_CONTEXT` refers to the number of **user messages** considered for context (roughly).

## ToDo
-  [ ] The script contains a mix of English and French
