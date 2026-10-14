#!/usr/bin/env python3
"""
DeepSeek Web Local AI Proxy (OpenAI-compatible API Server for Coding Agents).
Connects to an open chat.deepseek.com tab running bridge.user.js over WebSocket.
"""
import asyncio
import json
import re
import sys
import time
import urllib.parse
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

PORT = 8765
app = FastAPI(title="DeepSeek Web Local AI")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 1. DeepSeek Web SSE Patch Stream Parser ──────────────────────────────────

class DeepSeekSseParser:
    """Normalizes DeepSeek Web SSE patch frames (response/fragments & direct) into (kind, text) tuples."""

    def __init__(self) -> None:
        self.event_name: str = ""
        self.data_lines: List[str] = []
        self.active_channel: str = "content"  # "thinking" | "content"
        self.response_message_id: Optional[int] = None
        self.session_id: Optional[str] = None

    def feed_lines(self, lines: List[str]) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        for raw_line in lines:
            line = raw_line.rstrip("\r")
            if not line:
                out.extend(self._flush_event())
                self.event_name = ""
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                out.extend(self._flush_event())
                self.event_name = line[6:].strip()
                continue
            if line.startswith("data:"):
                self.data_lines.append(line[5:].strip())
        return out

    def _flush_event(self) -> List[Tuple[str, str]]:
        if not self.data_lines:
            return []
        data_str = "\n".join(self.data_lines).strip()
        self.data_lines.clear()
        if not data_str or data_str == "[DONE]":
            return []
        try:
            payload = json.loads(data_str)
        except Exception:
            return []
        return self._handle_payload(payload, self.event_name)

    def _consume_fragments(self, frags: Any) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        if isinstance(frags, dict):
            frags = [frags]
        if not isinstance(frags, list):
            return out
        for frag in frags:
            if not isinstance(frag, dict):
                continue
            ftype = str(frag.get("type") or "RESPONSE").upper()
            self.active_channel = "thinking" if ftype == "THINK" else "content"
            content = frag.get("content")
            if isinstance(content, str) and content:
                out.append((self.active_channel, content))
        return out

    def _handle_payload(self, d: Any, event_name: str) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        if not isinstance(d, dict):
            return out

        if isinstance(d.get("response_message_id"), int):
            self.response_message_id = d["response_message_id"]

        if event_name == "title":
            return out
        if event_name == "toast" or d.get("type") == "error":
            msg = d.get("content") or d.get("message") or str(d)
            return [("error", str(msg))]

        # Standard OpenAI choices fallback
        if isinstance(d.get("choices"), list) and d["choices"]:
            delta = d["choices"][0].get("delta") or {}
            if delta.get("reasoning_content"):
                out.append(("thinking", delta["reasoning_content"]))
            if delta.get("content"):
                out.append(("content", delta["content"]))
            return out

        v = d.get("v")
        p = d.get("p")

        # 1) Full response snapshot: {"v": {"response": {...}}}
        if isinstance(v, dict) and isinstance(v.get("response"), dict):
            resp = v["response"]
            if isinstance(resp.get("message_id"), int):
                self.response_message_id = resp["message_id"]
            if isinstance(resp.get("fragments"), list):
                out.extend(self._consume_fragments(resp["fragments"]))
            elif isinstance(resp.get("content"), str) and resp["content"]:
                self.active_channel = "content"
                out.append(("content", resp["content"]))
            return out

        # 2) Path-addressed patch frame
        if isinstance(p, str):
            if p == "response/fragments":
                out.extend(self._consume_fragments(v))
            elif p == "response/fragments/-1/content" and isinstance(v, str) and v:
                out.append((self.active_channel, v))
            elif p == "response/thinking_content" and isinstance(v, str) and v:
                self.active_channel = "thinking"
                out.append(("thinking", v))
            elif p == "response/content" and isinstance(v, str) and v:
                self.active_channel = "content"
                out.append(("content", v))
            elif p == "response" and isinstance(v, list):
                for op in v:
                    if isinstance(op, dict) and op.get("p") == "fragments":
                        out.extend(self._consume_fragments(op.get("v")))
            return out

        # 3) Continuation frame without path: {"v": "..."} or {"o": "APPEND", "v": "..."}
        if isinstance(v, str) and v:
            out.append((self.active_channel, v))
        return out


# ── 2. Tool Calling Adapter & Stateful Session Chaining ──────────────────────

def _norm_role(role: Optional[str]) -> str:
    if role in ("system", "developer"):
        return "system"
    return role or "user"


def _msg_text(msg: Dict[str, Any]) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                itype = item.get("type")
                if itype == "text":
                    parts.append(str(item.get("text", "")))
                elif itype == "tool_use":
                    tname = item.get("name") or "tool"
                    targs = item.get("input") or {}
                    parts.append(f"```tool_call\n{json.dumps([{'name': tname, 'arguments': targs}], ensure_ascii=False)}\n```")
                elif itype == "tool_result":
                    tname = item.get("name") or item.get("tool_use_id") or "tool"
                    tcontent = item.get("content", "")
                    if isinstance(tcontent, list):
                        tcontent = " ".join(c.get("text", "") for c in tcontent if isinstance(c, dict) and c.get("type") == "text")
                    parts.append(f"[TOOL_RESULT ({tname})]\n{tcontent}")
            elif isinstance(item, str):
                parts.append(item)
        text = "\n".join(parts)
    else:
        text = ""

    # Include tool_calls if assistant message had them
    tcalls = msg.get("tool_calls")
    if isinstance(tcalls, list) and tcalls:
        calls_json = []
        for tc in tcalls:
            fn = tc.get("function") or tc.get("custom") or {}
            raw_args = fn.get("arguments") if "arguments" in fn else fn.get("input", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                args = raw_args or {}
            calls_json.append({"name": fn.get("name"), "arguments": args})
        text = (text + "\n" if text else "") + f"```tool_call\n{json.dumps(calls_json, ensure_ascii=False)}\n```"

    role = _norm_role(msg.get("role"))
    if role == "tool":
        tname = msg.get("name") or msg.get("tool_call_id") or "tool"
        return f"[TOOL_RESULT ({tname})]\n{text}"
    return text


def _build_tool_schema_map(tools: Optional[List[Dict[str, Any]]]) -> Dict[str, Dict[str, str]]:
    """Returns {tool_name: {param_name: json_schema_type}} for argument validation and coercion."""
    schema_map: Dict[str, Dict[str, str]] = {}
    if not tools:
        return schema_map
    for t in tools:
        fn = t.get("function") if t.get("type") == "function" else (t.get("custom") or t)
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        props = (fn.get("parameters") or {}).get("properties") or {}
        param_types: Dict[str, str] = {}
        for pname, pspec in props.items():
            if isinstance(pspec, dict) and isinstance(pspec.get("type"), str):
                param_types[pname] = pspec["type"]
        schema_map[fn["name"]] = param_types
    return schema_map


def format_tools_prompt(tools: Optional[List[Dict[str, Any]]]) -> str:
    if not tools:
        return ""
    specs = []
    for t in tools:
        fn = t.get("function") if t.get("type") == "function" else (t.get("custom") or t)
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        specs.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        })
    if not specs:
        return ""
    return (
        "## Tool Use Instructions\n"
        "You have access to the following tools. Whenever you want to execute one or more tools, "
        "you MUST output a fenced `tool_call` JSON block at the end of your reply in this exact format:\n"
        "```tool_call\n"
        '[{"name": "tool_name", "arguments": {"arg1": "val1"}}]\n'
        "```\n"
        "Rules:\n"
        "- Use exact tool names and parameter names from the definitions below.\n"
        "- Put all tool parameters inside the `\"arguments\"` object.\n"
        "- CRITICAL: Do NOT output bare commands, underline markers (___), or raw XML/DSML tags like <invoke> or <｜｜DSML｜｜>. "
        "You MUST ALWAYS encapsulate tool calls inside a fenced ```tool_call JSON block.\n"
        "- Stop immediately after closing the ```tool_call``` block and wait for `[TOOL_RESULT]`.\n\n"
        "Tool definitions:\n"
        f"{json.dumps(specs, ensure_ascii=False)}\n"
    )


TOOL_REMINDER_FOOTER = (
    "[TOOL CALL REMINDER: When invoking any tool (e.g. `bash`, `read`, `edit`, `write`), "
    "you MUST emit a fenced ```tool_call``` JSON block at the end of your message. "
    "Do NOT output bare text or <invoke> tags: "
    '```tool_call\n[{"name": "bash", "arguments": {"command": "..."}}]\n```]'
)

DSML_PREFIX = r"(?:[｜|]+\s*DSML\s*[｜|]+\s*)?"
DSML_ANY_TAG_RE = re.compile(rf"</?\s*[｜|]+\s*DSML\s*[｜|]+[^>]*>", re.DOTALL)
FENCE_START_RE = re.compile(r"```(tool_call|tool_calls|json)\b[ \t]*\r?\n?", re.IGNORECASE)
XML_INVOKE_RE = re.compile(
    rf"<\s*{DSML_PREFIX}invoke\s+name=[\"']([^\"']+)[\"'][^>]*>(.*?)(?:</\s*{DSML_PREFIX}invoke\s*>|(?=</\s*{DSML_PREFIX}(?:function_calls|calls)\s*>)|$)",
    re.DOTALL,
)
XML_PARAM_RE = re.compile(
    rf"<\s*{DSML_PREFIX}parameter\s+name=[\"']([^\"']+)[\"']([^>]*)>(.*?)(?:</\s*{DSML_PREFIX}parameter\s*>|(?=$|<\s*/?\s*{DSML_PREFIX}(?:parameter|invoke|function_calls|calls)\b))",
    re.DOTALL,
)


def _normalize_json_tool_item(
    it: Any, schema_map: Dict[str, Dict[str, str]], require_known_name: bool = False
) -> Optional[Dict[str, Any]]:
    if not isinstance(it, dict):
        return None
    if isinstance(it.get("function"), dict):
        fn = it["function"]
        name = fn.get("name")
        raw_args = fn.get("arguments", {})
    else:
        name = it.get("name") or it.get("tool") or it.get("tool_name")
        if not isinstance(name, str) or not name:
            return None
        if "arguments" in it:
            raw_args = it["arguments"]
        elif "parameters" in it:
            raw_args = it["parameters"]
        elif "input" in it:
            raw_args = it["input"]
        elif "args" in it:
            raw_args = it["args"]
        else:
            # Flat format emitted by DeepSeek Web: {"name": "bash", "command": "ls -la"}
            raw_args = {
                k: v for k, v in it.items()
                if k not in ("name", "tool", "tool_name", "type", "id")
            }

    if not isinstance(name, str) or not name:
        return None
    if require_known_name and schema_map and name not in schema_map:
        return None

    if isinstance(raw_args, str):
        parsed_raw, _ = _try_parse_json_prefix(raw_args)
        args_dict = parsed_raw if isinstance(parsed_raw, dict) else {}
    elif isinstance(raw_args, dict):
        args_dict = dict(raw_args)
    else:
        args_dict = {}

    expected_types = schema_map.get(name, {})
    # Unwrap accidental double-nested {"arguments": "{\"command\": ...}"}
    for wrapper_key in ("arguments", "parameters", "input", "args"):
        if wrapper_key in args_dict and wrapper_key not in expected_types and len(args_dict) == 1:
            inner = args_dict[wrapper_key]
            if isinstance(inner, dict):
                args_dict = dict(inner)
            elif isinstance(inner, str):
                parsed_inner, _ = _try_parse_json_prefix(inner)
                if isinstance(parsed_inner, dict):
                    args_dict = parsed_inner
            break

    for k, v in list(args_dict.items()):
        if isinstance(v, str) and expected_types.get(k) in ("number", "integer", "boolean", "array", "object"):
            try:
                args_dict[k] = json.loads(v)
            except Exception:
                pass

    return {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args_dict, ensure_ascii=False)},
    }


def _extract_dsml_params(body: str, expected_types: Dict[str, str]) -> Dict[str, Any]:
    args_dict: Dict[str, Any] = {}
    for pm in XML_PARAM_RE.finditer(body):
        pname = pm.group(1).strip()
        pattrs = pm.group(2) or ""
        pval = pm.group(3).strip()
        # Unwrap <parameter name="arguments">{"command": "..."}</parameter>
        if pname in ("arguments", "parameters", "input", "args") and pname not in expected_types:
            parsed_inner, _ = _try_parse_json_prefix(pval)
            if isinstance(parsed_inner, dict):
                args_dict.update(parsed_inner)
                continue
        is_non_str = 'string="false"' in pattrs or expected_types.get(pname) in (
            "number", "integer", "boolean", "array", "object"
        )
        if is_non_str:
            try:
                args_dict[pname] = json.loads(pval)
                continue
            except Exception:
                pass
        args_dict[pname] = pval
    return args_dict


def _try_parse_json_prefix(sub: str) -> Tuple[Optional[Any], int]:
    """Parses a JSON array/object from the start of `sub`, handling DSML truncation if needed."""
    stripped = sub.lstrip()
    lead_ws = len(sub) - len(stripped)
    if not stripped or stripped[0] not in "[{":
        return None, 0
    decoder = json.JSONDecoder()
    try:
        obj, end_rel = decoder.raw_decode(stripped)
        return obj, lead_ws + end_rel
    except Exception:
        # Cut at first DSML tag or closing fence and attempt bracket repair for truncated JSON
        dsml_m = DSML_ANY_TAG_RE.search(stripped)
        candidate = stripped[: dsml_m.start()] if dsml_m else stripped
        if "```" in candidate:
            candidate = candidate[: candidate.find("```")]
        candidate = candidate.strip().rstrip(",")
        for suffix in ("", "]", "}", "}]", '"}', '"}]', '"}}]'):
            try:
                obj = json.loads(candidate + suffix)
                consumed_rel = dsml_m.start() if dsml_m else len(stripped)
                return obj, lead_ws + consumed_rel
            except Exception:
                continue
        return None, 0


def extract_tool_calls(
    text: str, has_tools: bool, tools: Optional[List[Dict[str, Any]]] = None
) -> Tuple[str, List[Dict[str, Any]]]:
    """Extracts OpenAI tool_calls from model text (fenced JSON, flat JSON, hybrid DSML, or XML invoke)."""
    if not has_tools or not text:
        return text, []

    schema_map = _build_tool_schema_map(tools)
    calls: List[Dict[str, Any]] = []
    spans_to_remove: List[Tuple[int, int]] = []

    # 1. Fenced ```tool_call / ```tool_calls / ```json blocks (even if unclosed or hybrid DSML)
    pos = 0
    while pos < len(text):
        m = FENCE_START_RE.search(text, pos)
        if not m:
            break
        fence_tag = m.group(1).lower()
        body_start = m.end()
        parsed, consumed = _try_parse_json_prefix(text[body_start:])
        if parsed is not None:
            items = parsed if isinstance(parsed, list) else [parsed]
            require_known = fence_tag == "json"
            normalized = [
                tc for it in items
                if (tc := _normalize_json_tool_item(it, schema_map, require_known_name=require_known)) is not None
            ]
            if normalized:
                end_pos = body_start + consumed
                rest = text[end_pos:]
                # Check if DeepSeek emitted hybrid DSML <parameter> tags after the JSON prefix
                hybrid_m = re.match(
                    rf"^\s*(?:</?\s*[｜|]+\s*DSML\s*[｜|]+[^>]*>|\s|<parameter\b.*?</parameter>)*",
                    rest,
                    re.DOTALL,
                )
                # Consume everything up to closing ``` or final </...DSML...(calls|function_calls|invoke)>
                tail_dsml = list(DSML_ANY_TAG_RE.finditer(rest))
                if tail_dsml:
                    dsml_end = tail_dsml[-1].end()
                    extra_params = _extract_dsml_params(
                        rest[:dsml_end], schema_map.get(normalized[-1]["function"]["name"], {})
                    )
                    if extra_params:
                        merged = json.loads(normalized[-1]["function"]["arguments"])
                        merged.update(extra_params)
                        normalized[-1]["function"]["arguments"] = json.dumps(merged, ensure_ascii=False)
                    end_pos += dsml_end
                    rest = text[end_pos:]
                close_m = re.match(r"^\s*(?:```)?", rest, re.DOTALL)
                if close_m:
                    end_pos += close_m.end()
                calls.extend(normalized)
                spans_to_remove.append((m.start(), end_pos))
                pos = end_pos
                continue
        pos = m.end()

    # 2. Native DeepSeek DSML / XML <invoke name="..."> blocks
    if not calls:
        for match in XML_INVOKE_RE.finditer(text):
            name = match.group(1).strip()
            body = match.group(2)
            expected_types = schema_map.get(name, {})
            args_dict = _extract_dsml_params(body, expected_types)
            if not args_dict:
                parsed_body, _ = _try_parse_json_prefix(body)
                if isinstance(parsed_body, dict):
                    args_dict = parsed_body
            calls.append({
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args_dict, ensure_ascii=False)},
            })
            spans_to_remove.append((match.start(), match.end()))

    # 3. Unfenced JSON array/object immediately preceding a DSML closing tag
    if not calls and DSML_ANY_TAG_RE.search(text):
        dsml_m = DSML_ANY_TAG_RE.search(text)
        if dsml_m:
            prefix = text[: dsml_m.start()]
            for start_ch in ("[", "{"):
                idx = prefix.rfind(start_ch)
                if idx != -1:
                    parsed, _ = _try_parse_json_prefix(prefix[idx:])
                    if parsed is not None:
                        items = parsed if isinstance(parsed, list) else [parsed]
                        normalized = [
                            tc for it in items
                            if (tc := _normalize_json_tool_item(it, schema_map, require_known_name=False)) is not None
                        ]
                        if normalized:
                            calls.extend(normalized)
                            spans_to_remove.append((idx, len(text)))
                            break

    # 4. DeepSeek V4 Pro bare tool text preceding a DSML closing tag (e.g. bash command text)
    if not calls and (DSML_ANY_TAG_RE.search(text) or "</invoke>" in text or "<|DSML|>" in text):
        tag_match = re.search(rf"</?\s*{DSML_PREFIX}(?:invoke|calls|function_calls)>|</\s*invoke\s*>", text, re.IGNORECASE)
        if tag_match:
            pre_text = text[: tag_match.start()].strip()
            # If there is a bash / command-line tool defined and pre_text looks like a command invocation
            if pre_text and "bash" in schema_map:
                # Strip any stray leading markers or underlines
                cmd_cleaned = re.sub(r"^[_\s`]+", "", pre_text).strip()
                if cmd_cleaned:
                    calls.append({
                        "id": f"call_{uuid.uuid4().hex[:12]}",
                        "type": "function",
                        "function": {"name": "bash", "arguments": json.dumps({"command": cmd_cleaned}, ensure_ascii=False)},
                    })
                    spans_to_remove.append((0, len(text)))

    cleaned = text
    for start, end in reversed(spans_to_remove):
        cleaned = cleaned[:start] + cleaned[end:]

    if calls or DSML_ANY_TAG_RE.search(cleaned) or "</invoke>" in cleaned:
        cleaned = DSML_ANY_TAG_RE.sub("", cleaned)
        cleaned = re.sub(r"</?\s*(?:function_calls|calls|invoke)\s*>", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"```(?:tool_call|tool_calls|json)?\s*```", "", cleaned)
        cleaned = re.sub(r"```(?:tool_call|tool_calls)\s*$", "", cleaned)
        cleaned = re.sub(r"_{3,}[｜|]*.*$", "", cleaned)

    return cleaned.strip(), calls


class SessionChainManager:
    """
    Tracks active DeepSeek chat_session_id and parent_message_id so multi-step coding agents
    only send the incremental delta prompt on continuation turns.
    """

    def __init__(self) -> None:
        self.session_id: Optional[str] = None
        self.turns_in_session: int = 0
        self.last_response_id: Optional[int] = None
        self.synced_messages: List[Tuple[str, str]] = []  # [(role, text), ...]

    def prepare_turn(
        self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None
    ) -> Tuple[Optional[str], Optional[int], str, List[Tuple[str, str]]]:
        norm: List[Tuple[str, str]] = [(_norm_role(m.get("role")), _msg_text(m)) for m in messages]

        if self.turns_in_session >= 40:
            self.session_id = None
            self.turns_in_session = 0
            self.last_response_id = None
            self.synced_messages = []

        can_chain = (
            self.session_id is not None
            and self.last_response_id is not None
            and len(self.synced_messages) > 0
            and len(norm) > len(self.synced_messages)
        )
        if can_chain:
            for i, (r_old, t_old) in enumerate(self.synced_messages):
                r_new, t_new = norm[i]
                if r_old != r_new:
                    can_chain = False
                    break
                if r_old != "assistant" and t_old != t_new:
                    can_chain = False
                    break

        if can_chain:
            delta = norm[len(self.synced_messages) :]
            if delta and delta[0][0] == "assistant":
                delta = delta[1:]
            if delta:
                prompt = self._format_entries(delta, tools_block="", has_tools=bool(tools))
                return self.session_id, self.last_response_id, prompt, norm

        self.session_id = None
        self.turns_in_session = 0
        self.last_response_id = None
        tools_block = format_tools_prompt(tools)
        prompt = self._format_entries(norm, tools_block=tools_block, has_tools=bool(tools))
        return None, None, prompt, norm

    @staticmethod
    def _format_entries(entries: List[Tuple[str, str]], tools_block: str, has_tools: bool = False) -> str:
        parts: List[str] = []
        if tools_block:
            parts.append(tools_block)
        if len(entries) == 1 and entries[0][0] == "user" and not tools_block:
            return entries[0][1]
        has_tool_result = False
        for role, text in entries:
            if not text:
                continue
            if role == "tool":
                has_tool_result = True
                parts.append(text)
            else:
                tag = role.upper()
                parts.append(f"[{tag}]\n{text}" if len(entries) > 1 or role != "user" else text)
        if has_tool_result and has_tools:
            parts.append(
                "(Continue based on the tool result above. "
                'If calling another tool, emit a fenced ```tool_call``` JSON block: [{"name": "...", "arguments": {...}}])'
            )
        elif has_tools:
            parts.append(TOOL_REMINDER_FOOTER)
        return "\n\n".join(parts)

    def commit_turn(
        self,
        session_id: Optional[str],
        response_message_id: Optional[int],
        norm_messages: List[Tuple[str, str]],
        assistant_reply: str,
    ) -> None:
        if session_id:
            self.session_id = session_id
            self.turns_in_session += 1
        self.last_response_id = response_message_id
        self.synced_messages = list(norm_messages) + [("assistant", assistant_reply)]


# ── 3. WebSocket Bridge Hub & OpenAI HTTP Routes ─────────────────────────────

RATE_LIMIT_RE = re.compile(
    r"thường xuyên|thử lại sau|quá tải|frequent|too many|rate limit|try again later|频繁|稍后再试|busy|429",
    re.IGNORECASE,
)


class BridgeHub:
    def __init__(self) -> None:
        self.ws: Optional[WebSocket] = None
        self.has_token: bool = False
        self.device_id: bool = False
        self._lock: Optional[asyncio.Lock] = None
        self.queues: Dict[str, asyncio.Queue] = {}
        self.chain = SessionChainManager()
        self.last_turn_end_time: float = 0.0
        self.turn_timestamps: List[float] = []
        self.cooldown_until: float = 0.0
        self.waiting_count: int = 0

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def trigger_cooldown(self, wait_seconds: float) -> None:
        """Sets a global cooldown timestamp to shield all waiting subagents from burst limiter."""
        target = time.time() + wait_seconds
        if target > self.cooldown_until:
            self.cooldown_until = target

    async def pace_before_turn(self) -> None:
        """Paces rapid multi-step agent turns so DeepSeek Web's burst limiter doesn't trigger."""
        now = time.time()
        # Respect global rate-limit cooldown across all subagents
        if self.cooldown_until > now:
            cooldown_wait = self.cooldown_until - now
            print(f"[Local-AI] ⏳ Waiting {cooldown_wait:.1f}s for global rate-limit cooldown to expire...", flush=True)
            await asyncio.sleep(cooldown_wait)
            now = time.time()

        self.turn_timestamps = [t for t in self.turn_timestamps if now - t < 60.0]
        # When subagents run concurrently, increase spacing to avoid DeepSeek IP/session burst limiter
        n_recent = len(self.turn_timestamps)
        if n_recent >= 12:
            min_gap = 6.0
        elif n_recent >= 8:
            min_gap = 4.8
        elif n_recent >= 4:
            min_gap = 3.8
        else:
            min_gap = 2.8

        if self.last_turn_end_time > 0:
            elapsed = now - self.last_turn_end_time
            if elapsed < min_gap:
                wait_s = min_gap - elapsed
                print(f"[Local-AI] ⏱️ Pacing {wait_s:.1f}s between rapid agent turns ({n_recent} reqs/min)...", flush=True)
                await asyncio.sleep(wait_s)
        self.turn_timestamps.append(time.time())

    def record_turn_end(self) -> None:
        self.last_turn_end_time = time.time()

hub = BridgeHub()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    hub.ws = ws
    print("[Local-AI] 🟢 Chrome tab connected to /ws", flush=True)
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype in ("hello", "pong"):
                hub.has_token = bool(msg.get("has_token"))
                hub.device_id = bool(msg.get("device_id"))
            elif mtype == "log":
                print(f"  [Bridge] {msg.get('message')}", flush=True)
            elif mtype in ("sse_lines", "done", "error", "eval_result"):
                req_id = msg.get("req_id") or msg.get("id")
                q = hub.queues.get(req_id)
                if q:
                    await q.put(msg)
    except WebSocketDisconnect:
        print("[Local-AI] ⚪ Chrome tab disconnected", flush=True)
    finally:
        if hub.ws is ws:
            hub.ws = None


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": hub.ws is not None,
        "bridge_connected": hub.ws is not None,
        "authenticated": hub.has_token,
        "session_id": hub.chain.session_id,
        "turns_in_session": hub.chain.turns_in_session,
        "queue_waiting": hub.waiting_count,
    }


KNOWN_MODELS = [
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-v3",
    "deepseek-v3.2",
    "deepseek-r1",
    "deepseek-coder",
    "deepseek-flash",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-chat-search",
    "deepseek-reasoner-search",
]


@app.get("/v1/models")
@app.get("/models")
async def list_models() -> Dict[str, Any]:
    now = int(time.time())
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": now, "owned_by": "deepseek-web"} for m in KNOWN_MODELS],
    }


@app.get("/v1/models/{model_id:path}")
@app.get("/models/{model_id:path}")
async def retrieve_model(model_id: str) -> Dict[str, Any]:
    now = int(time.time())
    return {
        "id": model_id,
        "object": "model",
        "created": now,
        "owned_by": "deepseek-web",
        "permission": [],
        "root": model_id,
        "parent": None,
    }


REASONING_EFFORT_HINTS: Dict[str, str] = {
    "minimal": "[REASONING EFFORT: MINIMAL — Keep internal <think> reasoning extremely brief (1-3 short sentences max), then act immediately.]",
    "low": "[REASONING EFFORT: LOW — Keep internal <think> reasoning concise and direct (under ~150 words), avoiding unnecessary deliberation.]",
    "high": "[REASONING EFFORT: HIGH — Think thoroughly through architecture, edge cases, and verification before responding.]",
    "xhigh": "[REASONING EFFORT: XHIGH — Perform deep, exhaustive step-by-step analysis of all constraints and edge cases before acting.]",
    "max": "[REASONING EFFORT: MAX — Maximum depth reasoning: carefully verify every requirement, edge case, and failure mode before acting.]",
}


def _resolve_model_flags(model: str, body: Dict[str, Any]) -> Tuple[bool, bool, str]:
    m = (model or "deepseek-chat").lower()
    search = "search" in m
    # Support reasoner, r1, pro, thought keywords as thinking models
    if any(k in m for k in ("reasoner", "r1", "pro", "thought")):
        thinking = True
    else:
        thinking = False

    effort_level = "medium"
    effort = body.get("reasoning_effort")
    if isinstance(effort, str):
        effort_level = effort.lower().strip()
        if effort_level in ("off", "none", "disabled"):
            thinking = False
        elif any(k in m for k in ("reasoner", "r1", "pro", "thought")):
            thinking = True
    thinking_obj = body.get("thinking")
    if isinstance(thinking_obj, dict):
        if thinking_obj.get("type") == "disabled":
            thinking = False
            effort_level = "off"
        elif thinking_obj.get("type") == "enabled":
            thinking = True
    return thinking, search, (effort_level if thinking else "off")


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request):
    if hub.ws is None:
        raise HTTPException(
            status_code=503,
            detail="Chưa kết nối với tab Chrome chat.deepseek.com. Hãy mở https://chat.deepseek.com và bật extension/userscript.",
        )

    body = await request.json()
    messages = body.get("messages") or []
    if not messages:
        raise HTTPException(status_code=400, detail="messages is required")

    model = body.get("model") or "deepseek-chat"
    stream = bool(body.get("stream", False))
    tools = body.get("tools")
    thinking_enabled, search_enabled, effort_level = _resolve_model_flags(model, body)

    if not stream:
        return await _run_buffered_completion(body, model, messages, tools, thinking_enabled, search_enabled, effort_level)

    return StreamingResponse(
        _run_streaming_completion(model, messages, tools, thinking_enabled, search_enabled, effort_level),
        media_type="text/event-stream",
    )


@app.post("/v1/messages")
@app.post("/messages")
@app.post("/anthropic/v1/messages")
async def anthropic_messages(request: Request):
    """
    Anthropic-compatible Messages endpoint.
    DeepSeek Harness uses this for native web_search (@deepseek-ai/dsh-web-search-deepseek).
    Always enables web search on DeepSeek Web and returns structured web_search_tool_result content blocks.
    """
    if hub.ws is None:
        raise HTTPException(
            status_code=503,
            detail="Chưa kết nối với tab Chrome chat.deepseek.com. Hãy mở https://chat.deepseek.com và bật extension/userscript.",
        )

    body = await request.json()
    raw_messages = body.get("messages") or []
    if not raw_messages:
        raise HTTPException(status_code=400, detail="messages is required")

    messages = []
    query = ""
    for m in raw_messages:
        role = m.get("role") or "user"
        content = m.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for it in content:
                if isinstance(it, dict) and it.get("type") == "text":
                    parts.append(str(it.get("text", "")))
                elif isinstance(it, str):
                    parts.append(it)
            text = "\n".join(parts)
        if "Perform a web search for the query:" in text:
            query = text.split("Perform a web search for the query:", 1)[1].strip()
        messages.append({"role": role, "content": text})

    model = body.get("model") or "deepseek-chat"
    # Search is always enabled for /messages requests from dsh-web-search-deepseek
    thinking_enabled = False
    search_enabled = True

    thinking_buf: List[str] = []
    content_buf: List[str] = []

    try:
        async for kind, text in _execute_bridge_turn(messages, None, thinking_enabled, search_enabled, "off"):
            if kind == "thinking":
                thinking_buf.append(text)
            elif kind == "content":
                content_buf.append(text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    raw_content = "".join(content_buf)
    cid = f"msg_{uuid.uuid4().hex[:12]}"

    # Extract any markdown links [title](url) as cited web_search_result sources
    url_re = re.compile(r"\[([^\]]+)\]\((https?://[^\s\)]+)\)")
    sources = []
    citations = []
    seen_urls = set()
    for m in url_re.finditer(raw_content):
        title = m.group(1).strip()
        url = m.group(2).strip()
        if url not in seen_urls:
            seen_urls.add(url)
            sources.append({
                "type": "web_search_result",
                "url": url,
                "title": title,
                "page_age": "recent",
            })
            citations.append({
                "type": "web_search_result_citation",
                "url": url,
                "cited_text": title,
            })

    if not sources and query:
        # Fallback dummy search result if DeepSeek returned prose without explicit markdown links
        sources.append({
            "type": "web_search_result",
            "url": f"https://www.google.com/search?q={urllib.parse.quote(query)}",
            "title": query,
            "page_age": "recent",
        })

    response_content = [
        {
            "type": "web_search_tool_result",
            "content": sources,
        },
        {
            "type": "text",
            "text": raw_content,
            "citations": citations,
        },
    ]

    return JSONResponse({
        "id": cid,
        "type": "message",
        "role": "assistant",
        "content": response_content,
        "model": model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 100,
            "output_tokens": max(1, len(raw_content) // 4),
        },
    })


async def _execute_bridge_turn(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    thinking_enabled: bool,
    search_enabled: bool,
    effort_level: str = "medium",
) -> AsyncGenerator[Tuple[str, str], None]:
    hub.waiting_count += 1
    if hub.waiting_count > 1:
        print(
            f"[Local-AI] 📥 Đưa request vào hàng đợi (vị trí #{hub.waiting_count}, đang xếp hàng chờ gửi dần)...",
            flush=True,
        )

    lock_acquired = False
    try:
        async with hub.lock:
            lock_acquired = True
            hub.waiting_count = max(0, hub.waiting_count - 1)
            await hub.pace_before_turn()
            session_id, parent_id, prompt, norm_msgs = hub.chain.prepare_turn(messages, tools)
            if thinking_enabled and effort_level in REASONING_EFFORT_HINTS:
                prompt = f"{prompt}\n\n{REASONING_EFFORT_HINTS[effort_level]}"
            mode_tag = f"CHAIN(parent={parent_id})" if parent_id else "ROOT"
            print(
                f"[Local-AI] -> {mode_tag} | think={thinking_enabled}({effort_level}) | chars={len(prompt)}",
                flush=True,
            )

        backoff_schedule = [6.0, 12.0, 20.0, 30.0, 45.0, 60.0]
        max_attempts = len(backoff_schedule) + 1

        for attempt in range(max_attempts):
            req_id = uuid.uuid4().hex
            q: asyncio.Queue = asyncio.Queue()
            hub.queues[req_id] = q
            parser = DeepSeekSseParser()
            full_reply_parts: List[str] = []

            try:
                await hub.ws.send_text(
                    json.dumps({
                        "type": "completion",
                        "req_id": req_id,
                        "prompt": prompt,
                        "session_id": session_id,
                        "parent_message_id": parent_id,
                        "thinking_enabled": thinking_enabled,
                        "search_enabled": search_enabled,
                        "new_chat": parent_id is None and session_id is None,
                    })
                )

                while True:
                    msg = await asyncio.wait_for(q.get(), timeout=180.0)
                    mtype = msg.get("type")
                    if msg.get("session_id"):
                        parser.session_id = msg["session_id"]
                        if session_id is None:
                            session_id = msg["session_id"]
                    if mtype == "sse_lines":
                        events = parser.feed_lines(msg.get("lines") or [])
                        for kind, text in events:
                            if kind == "error":
                                raise RuntimeError(text)
                            if kind == "content":
                                full_reply_parts.append(text)
                            yield kind, text
                    elif mtype == "error":
                        raise RuntimeError(msg.get("message") or "Unknown browser bridge error")
                    elif mtype == "done":
                        for kind, text in parser._flush_event():
                            if kind == "error":
                                raise RuntimeError(text)
                            if kind == "content":
                                full_reply_parts.append(text)
                            yield kind, text
                        break

                hub.record_turn_end()
                hub.chain.commit_turn(
                    parser.session_id or session_id,
                    parser.response_message_id,
                    norm_msgs,
                    "".join(full_reply_parts),
                )
                return
            except Exception as err:
                hub.record_turn_end()
                err_str = str(err)
                if attempt < len(backoff_schedule) and not full_reply_parts and RATE_LIMIT_RE.search(err_str):
                    wait_s = backoff_schedule[attempt]
                    # Also notify global hub cooldown so queued subagents waiting for hub.lock don't burst immediately
                    hub.trigger_cooldown(wait_s)
                    print(
                        f"[Local-AI] ⏳ DeepSeek rate-limit ({err_str!r}), tự động chờ {wait_s:.0f}s rồi gửi lại "
                        f"({attempt + 1}/{len(backoff_schedule)})...",
                        flush=True,
                    )
                    await asyncio.sleep(wait_s)

                    # If repeatedly hitting rate-limit in an existing session, reset session to avoid stuck/poisoned chats
                    if attempt >= 1 and (session_id is not None or parent_id is not None):
                        print("[Local-AI] 🔄 Resetting chained session to root after rate-limit backoff...", flush=True)
                        session_id = None
                        parent_id = None
                        tools_block = format_tools_prompt(tools)
                        prompt = hub.chain._format_entries(norm_msgs, tools_block=tools_block, has_tools=bool(tools))
                        if thinking_enabled and effort_level in REASONING_EFFORT_HINTS:
                            prompt = f"{prompt}\n\n{REASONING_EFFORT_HINTS[effort_level]}"
                    continue
                raise
            finally:
                hub.queues.pop(req_id, None)
    finally:
        if not lock_acquired:
            hub.waiting_count = max(0, hub.waiting_count - 1)


def _sse_chunk(
    cid: str,
    created: int,
    model: str,
    delta: Dict[str, Any],
    finish_reason: Optional[str] = None,
    usage: Optional[Dict[str, Any]] = None,
) -> str:
    payload: Dict[str, Any] = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "system_fingerprint": "fp_deepseek_web",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _run_streaming_completion(
    model: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    thinking_enabled: bool,
    search_enabled: bool,
    effort_level: str = "medium",
) -> AsyncGenerator[str, None]:
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    has_tools = bool(tools)
    content_buf: List[str] = []
    thinking_chars = 0
    content_chars = 0

    yield _sse_chunk(cid, created, model, {"role": "assistant"})
    try:
        async for kind, text in _execute_bridge_turn(messages, tools, thinking_enabled, search_enabled, effort_level):
            if kind == "thinking":
                thinking_chars += len(text)
                # Always stream reasoning_content live so Pi shows live thinking + tok/s
                yield _sse_chunk(cid, created, model, {"reasoning_content": text})
            elif kind == "content":
                content_chars += len(text)
                if not has_tools:
                    yield _sse_chunk(cid, created, model, {"content": text})
                else:
                    content_buf.append(text)

        if has_tools and content_buf:
            raw_content = "".join(content_buf)
            cleaned_content, tool_calls = extract_tool_calls(raw_content, has_tools=True, tools=tools)
            if cleaned_content:
                yield _sse_chunk(cid, created, model, {"content": cleaned_content})
            if tool_calls:
                stream_tcs = [{**tc, "index": idx} for idx, tc in enumerate(tool_calls)]
                yield _sse_chunk(cid, created, model, {"tool_calls": stream_tcs})
            finish_reason = "tool_calls" if tool_calls else "stop"
        else:
            finish_reason = "stop"

        out_tokens = max(1, (thinking_chars + content_chars) // 4)
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": out_tokens,
            "total_tokens": 100 + out_tokens,
            "completion_tokens_details": {"reasoning_tokens": thinking_chars // 4},
        }
        yield _sse_chunk(cid, created, model, {}, finish_reason=finish_reason, usage=usage)
        yield "data: [DONE]\n\n"
    except Exception as e:
        yield _sse_chunk(cid, created, model, {"content": f"\n\n[Local-AI Error: {e}]"}, finish_reason="stop")
        yield "data: [DONE]\n\n"


async def _run_buffered_completion(
    body: Dict[str, Any],
    model: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    thinking_enabled: bool,
    search_enabled: bool,
    effort_level: str = "medium",
):
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    thinking_buf: List[str] = []
    content_buf: List[str] = []

    try:
        async for kind, text in _execute_bridge_turn(messages, tools, thinking_enabled, search_enabled, effort_level):
            if kind == "thinking":
                thinking_buf.append(text)
            elif kind == "content":
                content_buf.append(text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    raw_content = "".join(content_buf)
    reasoning_content = "".join(thinking_buf)
    cleaned_content, tool_calls = extract_tool_calls(raw_content, has_tools=bool(tools), tools=tools)
    finish_reason = "tool_calls" if tool_calls else "stop"

    msg_obj: Dict[str, Any] = {"role": "assistant", "content": cleaned_content or None}
    if reasoning_content:
        msg_obj["reasoning_content"] = reasoning_content
    if tool_calls:
        msg_obj["tool_calls"] = tool_calls

    out_tokens = max(1, (len(reasoning_content) + len(raw_content)) // 4)
    return JSONResponse({
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "system_fingerprint": "fp_deepseek_web",
        "choices": [{"index": 0, "message": msg_obj, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 100, "completion_tokens": out_tokens, "total_tokens": 100 + out_tokens},
    })


# ── 4. Self-Test & Quick CLI ─────────────────────────────────────────────────

def _self_test() -> None:
    # 1. Test DeepSeek 2026 SSE patch stream parser
    p = DeepSeekSseParser()
    lines = [
        "event: ready",
        'data: {"request_message_id":1,"response_message_id":42}',
        "",
        'data: {"v":{"response":{"message_id":42,"fragments":[{"id":1,"type":"THINK","content":"Let me "}]} }}',
        "",
        'data: {"p":"response/fragments/-1/content","v":"think..."}',
        "",
        'data: {"p":"response/fragments","o":"APPEND","v":[{"id":2,"type":"RESPONSE","content":"Hello "}]}',
        "",
        'data: {"v":"world!"}',
        "",
        "data: [DONE]",
        "",
    ]
    events = p.feed_lines(lines)
    assert p.response_message_id == 42, f"Expected 42, got {p.response_message_id}"
    assert events == [
        ("thinking", "Let me "),
        ("thinking", "think..."),
        ("content", "Hello "),
        ("content", "world!"),
    ], f"Unexpected events: {events}"

    # 2. Test Stateful Session Chaining (Root vs Delta Chained)
    cm = SessionChainManager()
    msgs1 = [{"role": "system", "content": "You are a coder."}, {"role": "user", "content": "Write add(a,b)"}]
    sid, pid, prompt1, norm1 = cm.prepare_turn(msgs1)
    assert sid is None and pid is None
    assert "You are a coder." in prompt1 and "Write add(a,b)" in prompt1
    cm.commit_turn("sess_abc", 101, norm1, "def add(a, b): return a + b")

    msgs2 = msgs1 + [
        {"role": "assistant", "content": "def add(a, b): return a + b"},
        {"role": "user", "content": "Now write sub(a,b)"},
    ]
    sid2, pid2, prompt2, _ = cm.prepare_turn(msgs2)
    assert sid2 == "sess_abc" and pid2 == 101
    assert prompt2 == "Now write sub(a,b)", f"Expected only delta prompt, got: {prompt2!r}"

    # 3. Test Tool Calling extraction (standard, flat JSON + DSML stop tags, backticks in content, native DSML)
    sample_tools = [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "edits": {"type": "array"}}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
            },
        },
    ]
    reply = 'Sure!\n```tool_call\n[{"name": "read_file", "arguments": {"path": "main.py"}}]\n```'
    cleaned, tcs = extract_tool_calls(reply, has_tools=True, tools=sample_tools)
    assert cleaned == "Sure!"
    assert len(tcs) == 1 and tcs[0]["function"]["name"] == "read_file"

    # Real Pi session failure case: flat arguments + unclosed fence terminated by DSML stop tokens
    real_pi_reply = (
        "I'll check the workspace and environment first.\n\n"
        "```bash\nls -la\n```\n\n"
        "```tool_call\n"
        '[{"name": "bash", "command": "ls -la && node -v"}]</｜｜DSML｜｜ parameter>\n'
        "</｜｜DSML｜｜ invoke>\n"
        "</｜｜DSML｜｜ calls>"
    )
    cleaned2, tcs2 = extract_tool_calls(real_pi_reply, has_tools=True, tools=sample_tools)
    assert "```tool_call" not in cleaned2 and "DSML" not in cleaned2, f"Leftover tags in cleaned2: {cleaned2!r}"
    assert len(tcs2) == 1 and tcs2[0]["function"]["name"] == "bash"
    assert json.loads(tcs2[0]["function"]["arguments"]) == {"command": "ls -la && node -v"}

    # Nested markdown backticks inside write tool content
    nested_bt_reply = (
        '```tool_call\n[{"name": "write", "arguments": {"path": "README.md", "content": "# Doc\\n```js\\nx()\\n```"}}]\n```'
    )
    cleaned3, tcs3 = extract_tool_calls(nested_bt_reply, has_tools=True, tools=sample_tools)
    assert cleaned3 == ""
    assert len(tcs3) == 1 and json.loads(tcs3[0]["function"]["arguments"])["path"] == "README.md"

    # Native DSML invoke with array coercion for edit tool
    dsml_reply = (
        '<｜｜DSML｜｜ calls>\n'
        '<｜｜DSML｜｜ invoke name="edit">\n'
        '<｜｜DSML｜｜ parameter name="path" string="true">app.js</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="edits" string="false">[{"oldText": "a", "newText": "b"}]</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n'
        '</｜｜DSML｜｜ calls>'
    )
    cleaned4, tcs4 = extract_tool_calls(dsml_reply, has_tools=True, tools=sample_tools)
    assert cleaned4 == ""
    assert len(tcs4) == 1 and tcs4[0]["function"]["name"] == "edit"
    assert isinstance(json.loads(tcs4[0]["function"]["arguments"])["edits"], list)

    # Hybrid truncated JSON + DSML parameter tag
    hybrid_reply = (
        '```tool_call\n[{"name": "write", "path": "index.html"</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="content" string="true"><h1>Hi</h1></｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>'
    )
    cleaned5, tcs5 = extract_tool_calls(hybrid_reply, has_tools=True, tools=sample_tools)
    assert cleaned5 == ""
    assert len(tcs5) == 1 and json.loads(tcs5[0]["function"]["arguments"]) == {"path": "index.html", "content": "<h1>Hi</h1>"}

    # Wrapped <parameter name="arguments">{"command": "..."}]</parameter>
    wrapped_args_reply = (
        '<｜｜DSML｜｜ invoke name="bash">\n'
        '<｜｜DSML｜｜ parameter name="arguments">{"command": "echo ok"}}]</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>'
    )
    _, tcs6 = extract_tool_calls(wrapped_args_reply, has_tools=True, tools=sample_tools)
    assert len(tcs6) == 1 and json.loads(tcs6[0]["function"]["arguments"]) == {"command": "echo ok"}
    assert RATE_LIMIT_RE.search("Tin nhắn quá thường xuyên. Vui lòng thử lại sau.")

    # Anthropic-style tool blocks parsing (used by DeepSeek Harness / dsh / Cordis sub-agents)
    anthropic_msg = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "I will inspect files."},
            {"type": "tool_use", "name": "read", "input": {"path": "package.json"}},
        ],
    }
    parsed_anthropic_text = _msg_text(anthropic_msg)
    assert '{"name": "read", "arguments": {"path": "package.json"}}' in parsed_anthropic_text

    # Bare command with stray DSML invoke tag (DeepSeek V4 Pro behavior reported by user)
    user_sample_reply = (
        'Structs are not in rl (the C lib namespace). They\'re likely in the ffi cdef namespace, or exposed elsewhere.\n\n'
        'head -50 /Users/nguyenhaianh/Library/Python/3.9/lib/python/site-packages/raylib/init.pyi; echo "=== grep Vector2 in pyi ==="; grep -n "Vector2|class Rectangle|Rectangle =|Color =|def Color|Font =" /Users/nguyenhaianh/Library/Python/3.9/lib/python/site-packages/raylib/init.pyi | head -30 Inspect raylib type stubs for struct constructors\n\n'
        '______________________｜｜DSML｜｜> </invoke>'
    )
    cleaned_user, tcs_user = extract_tool_calls(user_sample_reply, has_tools=True, tools=sample_tools)
    assert len(tcs_user) == 1 and tcs_user[0]["function"]["name"] == "bash"
    assert "head -50" in json.loads(tcs_user[0]["function"]["arguments"])["command"]
    assert "DSML" not in cleaned_user and "</invoke>" not in cleaned_user

    print("✅ All self-tests passed (SSE parser, Session Chaining, Tool Calling, Rate-Limit Retry, DeepSeek Harness)!")


def _cli_ask(prompt: str, model: str = "deepseek-chat") -> None:
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/v1/chat/completions",
        data=json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="ignore").strip()
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[5:].strip())
            delta = chunk["choices"][0].get("delta") or {}
            if delta.get("reasoning_content"):
                sys.stderr.write(f"\033[2m{delta['reasoning_content']}\033[0m")
                sys.stderr.flush()
            if delta.get("content"):
                sys.stdout.write(delta["content"])
                sys.stdout.flush()
    print()


if __name__ == "__main__":
    if "--test" in sys.argv:
        _self_test()
        sys.exit(0)
    if "--ask" in sys.argv:
        idx = sys.argv.index("--ask")
        q = " ".join(sys.argv[idx + 1 :]) or "Hello!"
        _cli_ask(q)
        sys.exit(0)
    print(f"🚀 DeepSeek Web Local AI Server running at http://127.0.0.1:{PORT}/v1")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
