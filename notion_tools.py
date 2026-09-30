"""Notion tool-calling support.

Provides the OpenAI-style tool/function definitions the LLM can call, and a
Python-side fallback implementation that talks directly to the Notion REST
API. This fallback is used whenever LM Studio does not expose Notion's MCP
tools natively through its API - the tool names match what an MCP server
would typically expose, so if LM Studio *does* surface MCP tools directly,
those calls will simply bypass this module and be handled upstream.
"""

import json
import time

import requests

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Filled in by configure() once CONFIG is loaded by main.py
_NOTION_API_KEY = None
_NOTION_DEFAULT_PARENT_ID = None
_NOTION_DEFAULT_PARENT_TYPE = "database_id"  # or "page_id"


def configure(api_key, default_parent_id=None, default_parent_type="database_id"):
    """Configure module-level Notion credentials/settings."""
    global _NOTION_API_KEY, _NOTION_DEFAULT_PARENT_ID, _NOTION_DEFAULT_PARENT_TYPE
    _NOTION_API_KEY = api_key
    _NOTION_DEFAULT_PARENT_ID = default_parent_id
    _NOTION_DEFAULT_PARENT_TYPE = default_parent_type or "database_id"


def is_enabled():
    """Whether the Python-side Notion fallback is usable."""
    return bool(_NOTION_API_KEY)


def _headers():
    return {
        "Authorization": "Bearer " + str(_NOTION_API_KEY),
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _text_prop(value):
    return {"rich_text": [{"text": {"content": str(value)}}]}


def _title_prop(value):
    return {"title": [{"text": {"content": str(value)}}]}


def _build_properties(title=None, properties=None):
    """Build a Notion `properties` payload from simple title/properties args."""
    props = {}
    if properties:
        for key, value in properties.items():
            if isinstance(value, dict):
                props[key] = value  # Already a Notion-shaped property
            else:
                props[key] = _text_prop(value)
    if title is not None and "Name" not in props and "title" not in props:
        props["Name"] = _title_prop(title)
    return props


# ================= TOOL IMPLEMENTATIONS =================
def notion_search(query="", filter_type=None, page_size=10):
    """Search Notion pages/databases by title text."""
    payload = {"query": query, "page_size": min(int(page_size or 10), 100)}
    if filter_type in ("page", "database"):
        payload["filter"] = {"value": filter_type, "property": "object"}

    resp = requests.post(f"{NOTION_API_BASE}/search", headers=_headers(), json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("results", []):
        results.append({
            "id": item.get("id"),
            "object": item.get("object"),
            "url": item.get("url"),
            "title": _extract_title(item),
        })
    return {"results": results, "count": len(results)}


def notion_create_page(parent_id=None, parent_type=None, title=None, properties=None, content=None):
    """Create a new Notion page/database item."""
    parent_id = parent_id or _NOTION_DEFAULT_PARENT_ID
    parent_type = parent_type or _NOTION_DEFAULT_PARENT_TYPE

    if not parent_id:
        raise ValueError("parent_id is required to create a Notion page (no default configured)")

    payload = {
        "parent": {parent_type: parent_id},
        "properties": _build_properties(title=title, properties=properties),
    }

    if content:
        payload["children"] = [{
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": str(content)}}]},
        }]

    resp = requests.post(f"{NOTION_API_BASE}/pages", headers=_headers(), json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return {"id": data.get("id"), "url": data.get("url"), "title": _extract_title(data)}


def notion_update_page(page_id, title=None, properties=None, archived=None):
    """Update properties of an existing Notion page/item."""
    if not page_id:
        raise ValueError("page_id is required")

    payload = {}
    props = _build_properties(title=title, properties=properties) if (title or properties) else {}
    if props:
        payload["properties"] = props
    if archived is not None:
        payload["archived"] = bool(archived)

    resp = requests.patch(f"{NOTION_API_BASE}/pages/{page_id}", headers=_headers(), json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return {"id": data.get("id"), "url": data.get("url"), "archived": data.get("archived", False)}


def notion_delete_page(page_id):
    """Archive (soft-delete) a Notion page/item."""
    return notion_update_page(page_id, archived=True)


def notion_get_page(page_id):
    """Fetch a single Notion page by ID."""
    if not page_id:
        raise ValueError("page_id is required")

    resp = requests.get(f"{NOTION_API_BASE}/pages/{page_id}", headers=_headers(), timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return {"id": data.get("id"), "url": data.get("url"), "title": _extract_title(data), "properties": data.get("properties", {})}


def _extract_title(item):
    """Best-effort extraction of a human-readable title from a Notion object."""
    props = item.get("properties") or {}
    for prop in props.values():
        if prop.get("type") == "title":
            parts = prop.get("title", [])
            return "".join(p.get("plain_text", "") for p in parts)
    # Databases use "title" directly at top level
    if "title" in item and isinstance(item["title"], list):
        return "".join(p.get("plain_text", "") for p in item["title"])
    return ""


# ================= TOOL DEFINITIONS (OpenAI function-calling schema) =================
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "notion_search",
            "description": "노션(Notion) 페이지나 데이터베이스 항목을 검색합니다. 제목 텍스트로 검색합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "검색할 텍스트"},
                    "filter_type": {
                        "type": "string",
                        "enum": ["page", "database"],
                        "description": "결과를 page 또는 database로만 제한 (선택 사항)",
                    },
                    "page_size": {"type": "integer", "description": "반환할 최대 결과 수 (기본 10)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notion_create_page",
            "description": "새 노션 페이지 또는 데이터베이스 항목을 생성합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "parent_id": {"type": "string", "description": "부모 데이터베이스/페이지 ID (생략 시 기본값 사용)"},
                    "parent_type": {
                        "type": "string",
                        "enum": ["database_id", "page_id"],
                        "description": "parent_id의 종류",
                    },
                    "title": {"type": "string", "description": "새 페이지/항목의 제목"},
                    "properties": {"type": "object", "description": "추가 속성 key-value (선택 사항)"},
                    "content": {"type": "string", "description": "본문에 추가할 텍스트 (선택 사항)"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notion_update_page",
            "description": "기존 노션 페이지/항목의 속성을 수정합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "수정할 페이지/항목의 ID"},
                    "title": {"type": "string", "description": "새 제목 (선택 사항)"},
                    "properties": {"type": "object", "description": "수정할 속성 key-value (선택 사항)"},
                },
                "required": ["page_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notion_delete_page",
            "description": "노션 페이지/항목을 삭제(보관 처리)합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "삭제(보관)할 페이지/항목의 ID"},
                },
                "required": ["page_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notion_get_page",
            "description": "ID로 특정 노션 페이지를 조회합니다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "조회할 페이지의 ID"},
                },
                "required": ["page_id"],
            },
        },
    },
]

_TOOL_FUNCTIONS = {
    "notion_search": notion_search,
    "notion_create_page": notion_create_page,
    "notion_update_page": notion_update_page,
    "notion_delete_page": notion_delete_page,
    "notion_get_page": notion_get_page,
}


def execute_tool(name, arguments):
    """Execute a Notion tool by name with the given arguments dict.

    Returns a JSON-serializable dict. Raises no exceptions - errors are
    captured and returned as {"error": "..."} so the calling loop can feed
    them back to the model as a tool result.
    """
    func = _TOOL_FUNCTIONS.get(name)
    if func is None:
        return {"error": f"Unknown tool: {name}"}

    if not is_enabled():
        return {"error": "Notion integration is not configured (missing NOTION_API_KEY)."}

    try:
        arguments = arguments or {}
        start = time.time()
        result = func(**arguments)
        duration = time.time() - start
        result_with_meta = dict(result) if isinstance(result, dict) else {"result": result}
        result_with_meta["_duration_s"] = round(duration, 3)
        return result_with_meta
    except requests.exceptions.RequestException as e:
        return {"error": f"Notion API request failed: {e}"}
    except Exception as e:
        return {"error": f"Notion tool execution failed: {e}"}


def tool_result_to_content(result):
    """Serialize a tool result dict to a string for the `tool` message content."""
    try:
        return json.dumps(result, ensure_ascii=False)
    except TypeError:
        return json.dumps({"result": str(result)}, ensure_ascii=False)
