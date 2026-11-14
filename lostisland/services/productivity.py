"""AI assistant query service for local Ollama/LM Studio and remote APIs."""

from __future__ import annotations

import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import ClassVar

from gi.repository import GLib, GObject

from lostisland.services.ollama import _lmstudio_url, _ollama_url

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think>.*$", re.DOTALL | re.IGNORECASE)


class _ToolUnsupportedError(RuntimeError):
    """Raised when a local model rejects native tool schemas cleanly."""


def _strip_thinking(text: str) -> str:
    """Remove model reasoning `<think>` tags cleanly for compact island display."""
    if not text:
        return ""
    cleaned = _THINK_BLOCK_RE.sub("", text)
    cleaned = _THINK_OPEN_RE.sub("", cleaned)
    return cleaned.strip()


def _http_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read()
        return raw.decode("utf-8", "replace")[:400] if raw else str(exc)
    except (OSError, ValueError):
        return str(exc)
    finally:
        try:
            exc.close()
        except OSError:
            pass


class ProductivityService(GObject.Object):
    __gsignals__: ClassVar[dict] = {
        # reply_text, ok
        "ai-reply": (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),
        # status_text, partial_reply
        "ai-progress": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # label, callback (for destructive/power actions proposed by AI)
        "action-confirm": (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
    }

    def __init__(self, cfg: dict, ollama=None, system_agent=None):
        super().__init__()
        self.cfg = cfg
        self.ollama = ollama
        self.system_agent = system_agent
        self.pending_action: tuple[str, object] | None = None
        self.ai_history: list[dict[str, str]] = []
        self._ai_busy = False
        self._ai_gen = 0
        self._stopped = False
        self._cancel_event = threading.Event()

    def request_action_confirm(self, label: str, callback):
        if self._stopped or self.pending_action is not None:
            return False
        self.pending_action = (label, callback)
        self.emit("action-confirm", label, callback)
        return True

    def resolve_pending_action(self, approved: bool):
        act = self.pending_action
        self.pending_action = None
        if approved and act is not None and callable(act[1]):
            act[1]()

    def start(self):
        self._stopped = False

    def stop(self):
        self._stopped = True
        self._cancel_event.set()
        self._ai_gen += 1
        self._ai_busy = False
        self.pending_action = None

    def cancel_ai(self):
        """Abort an in-flight AI generation cleanly and unlock the prompt bar."""
        if not self._ai_busy:
            return
        self._cancel_event.set()
        self._ai_gen += 1
        self._ai_busy = False
        self.emit("ai-reply", "Cancelled.", False)

    # -- AI Assistant ------------------------------------------------------

    @property
    def last_ai_reply(self) -> str:
        for item in reversed(self.ai_history):
            if item.get("role") == "assistant":
                return item.get("content", "")
        return ""

    def clear_ai_history(self):
        self.ai_history.clear()

    def resolved_ai_model(self, provider: str | None = None) -> str:
        prov = provider or self.cfg.get("ai_provider", "ollama")
        model = str(self.cfg.get("ai_model", "") or "").strip()
        ollama_models = list(getattr(self.ollama, "models", []) or [])
        lmstudio_models = list(getattr(self.ollama, "lmstudio_models", []) or [])
        if model:
            if prov == "ollama":
                if not ollama_models or model in ollama_models:
                    return model
            elif prov == "lmstudio":
                if not lmstudio_models or model in lmstudio_models:
                    return model
            elif model not in ollama_models and model not in lmstudio_models:
                return model
        if prov == "ollama":
            if self.ollama is not None:
                if getattr(self.ollama, "running_models", None):
                    return self.ollama.running_models[0]
                if getattr(self.ollama, "models", None):
                    return self.ollama.models[0]
                if getattr(self.ollama, "model", ""):
                    return self.ollama.model
            return "llama3.2"
        if prov == "lmstudio":
            if self.ollama is not None and getattr(
                self.ollama, "lmstudio_models", None
            ):
                return self.ollama.lmstudio_models[0]
            return "local-model"
        return {
            "claude": "claude-sonnet-4-5",
            "gemini": "gemini-2.5-flash",
            "openrouter": "openai/gpt-4o-mini",
        }.get(prov, "llama3.2")

    def ask_ai(self, prompt: str):
        text = (prompt or "").strip()
        if self._stopped or not text or self._ai_busy:
            return
        self._ai_busy = True
        self._cancel_event = threading.Event()
        gen = self._ai_gen
        provider = self.cfg.get("ai_provider", "ollama")
        model = self.resolved_ai_model(provider)
        key = self.cfg.get("ai_api_key", "")
        messages = [*self.ai_history[-8:], {"role": "user", "content": text}]
        threading.Thread(
            target=self._ai_worker,
            args=(provider, model, key, text, messages, gen, self._cancel_event),
            daemon=True,
        ).start()

    def _emit_progress(
        self,
        status: str,
        partial: str = "",
        gen: int | None = None,
        cancel_event: threading.Event | None = None,
    ):
        if cancel_event is not None and cancel_event.is_set():
            return

        def _deliver():
            if self._stopped or (gen is not None and gen != self._ai_gen):
                return False
            self.emit("ai-progress", status, partial)
            return False

        GLib.idle_add(_deliver)

    def _record_local_metrics(self, model: str, stats: dict | None):
        if self.ollama is None or not stats:
            return
        tok_s = float(stats.get("tok_s") or 0.0)
        eval_cnt = int(stats.get("eval_count") or 0)
        if tok_s > 0 and hasattr(self.ollama, "record_metrics"):
            self.ollama.record_metrics(tok_s, eval_cnt, model)

    def _ai_worker(
        self,
        provider: str,
        model: str,
        api_key: str,
        prompt: str,
        messages: list[dict] | None = None,
        gen: int | None = None,
        cancel_event: threading.Event | None = None,
    ):
        base_msgs = list(messages or [{"role": "user", "content": prompt}])
        agent = self.system_agent
        use_tools = agent is not None and bool(self.cfg.get("ai_system_control", False))
        ollama_svc = self.ollama if provider == "ollama" else None
        native_supported = provider == "lmstudio" or (
            provider == "ollama"
            and (ollama_svc is None or ollama_svc.model_supports_tools(model))
        )

        def _on_progress(status: str, partial: str = ""):
            self._emit_progress(status, partial, gen=gen, cancel_event=cancel_event)

        def _on_metrics(stats: dict):
            self._record_local_metrics(model, stats)

        def _run_xml_loop() -> str:
            xml_msgs = [
                {"role": "system", "content": agent.system_prompt(native_tools=False)},
                *base_msgs,
            ]
            reply = ""
            for _step in range(4):
                if cancel_event and cancel_event.is_set():
                    return ""
                reply = self._dispatch_ai(
                    provider,
                    model,
                    api_key,
                    prompt,
                    xml_msgs,
                    on_token=lambda partial: _on_progress("", partial),
                    on_metrics=_on_metrics,
                    cancel_event=cancel_event,
                )
                reply = _strip_thinking(str(reply or ""))
                parsed = agent.parse_tool_call(reply)
                if parsed is None:
                    return agent.strip_tool_call(reply) or reply
                tool, args = parsed
                _on_progress(f"Running {tool}…")
                tool_res = agent.execute_tool(tool, args)
                xml_msgs.append({"role": "assistant", "content": reply})
                xml_msgs.append(
                    {
                        "role": "user",
                        "content": (
                            f"<tool_result tool={json.dumps(tool)}>"
                            f"{json.dumps(tool_res, ensure_ascii=False)}"
                            "</tool_result>\nSummarize concisely for the user."
                        ),
                    }
                )
            return agent.strip_tool_call(reply) or "Done."

        try:
            if use_tools and provider in ("ollama", "lmstudio") and native_supported:
                tool_schemas = agent.tool_schemas()
                native_msgs = [
                    {
                        "role": "system",
                        "content": agent.system_prompt(native_tools=True),
                    },
                    *base_msgs,
                ]
                try:
                    reply = self._run_native_tool_loop(
                        provider,
                        model,
                        api_key,
                        prompt,
                        native_msgs,
                        agent,
                        tool_schemas,
                        on_progress=_on_progress,
                        on_metrics=_on_metrics,
                        cancel_event=cancel_event,
                    )
                except _ToolUnsupportedError:
                    if ollama_svc is not None:
                        ollama_svc.mark_no_tools(model)
                    reply = _run_xml_loop()
            elif use_tools:
                reply = _run_xml_loop()
            else:
                # Respect ai_system_control opt-in: never send system telemetry when off.
                kw = (
                    {
                        "on_token": lambda partial: _on_progress("", partial),
                        "on_metrics": _on_metrics,
                        "cancel_event": cancel_event,
                    }
                    if provider in ("ollama", "lmstudio")
                    else {}
                )
                reply = str(
                    self._dispatch_ai(provider, model, api_key, prompt, base_msgs, **kw)
                    or ""
                )
                reply = _strip_thinking(reply)
            ok = True
        except urllib.error.HTTPError as exc:
            reply = f"AI error: HTTP {exc.code} — {_http_error_detail(exc)}"
            ok = False
        except (
            http.client.HTTPException,
            OSError,
            TimeoutError,
            TypeError,
            ValueError,
            KeyError,
            urllib.error.URLError,
        ) as exc:
            reply = f"AI error: {exc}"
            ok = False
        if cancel_event is not None and cancel_event.is_set():
            return
        GLib.idle_add(self._ai_done, prompt, reply, ok, gen)

    @classmethod
    def _run_native_tool_loop(
        cls,
        provider: str,
        model: str,
        api_key: str,
        prompt: str,
        messages: list[dict],
        agent,
        tools: list[dict],
        on_progress=None,
        on_metrics=None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """Run local function calls as typed tool messages, bounded to five rounds."""
        msgs = list(messages)
        for step in range(5):
            if cancel_event is not None and cancel_event.is_set():
                return ""
            try:
                response = cls._dispatch_ai(
                    provider,
                    model,
                    api_key,
                    prompt,
                    msgs,
                    tools=tools,
                    on_metrics=on_metrics,
                    cancel_event=cancel_event,
                )
            except urllib.error.HTTPError as exc:
                detail = _http_error_detail(exc).lower()
                if exc.code == 400 and "tool" in detail:
                    raise _ToolUnsupportedError(detail) from exc
                raise
            if not isinstance(response, dict):
                return _strip_thinking(str(response or ""))
            calls = response.get("tool_calls") or []
            if not calls:
                content = _strip_thinking(str(response.get("content") or ""))
                parsed = agent.parse_tool_call(content)
                if parsed is None:
                    return agent.strip_tool_call(content) or content
                tool, args = parsed
                if on_progress is not None:
                    on_progress(f"Running {tool}…", "")
                result = agent.execute_tool(tool, args)
                msgs.extend(
                    [
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": (
                                f"<tool_result tool={json.dumps(tool)}>"
                                f"{json.dumps(result, ensure_ascii=False)}"
                                "</tool_result>\nSummarize concisely for the user."
                            ),
                        },
                    ]
                )
                if step == 4:
                    final = cls._dispatch_ai(
                        provider,
                        model,
                        api_key,
                        prompt,
                        msgs,
                        on_token=(
                            (lambda p: on_progress("", p)) if on_progress else None
                        ),
                        on_metrics=on_metrics,
                        cancel_event=cancel_event,
                    )
                    return _strip_thinking(str(final or "System actions completed."))
                continue

            assistant_message = response.get("assistant_message")
            if isinstance(assistant_message, dict):
                msgs.append(assistant_message)

            for call in calls:
                if cancel_event is not None and cancel_event.is_set():
                    return ""
                tool = str(call.get("name") or "").strip()
                args = call.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = None
                if on_progress is not None and tool:
                    on_progress(f"Running {tool}…", "")
                if not tool or not isinstance(args, dict):
                    tool_result = {
                        "ok": False,
                        "error": "The model returned invalid function arguments",
                    }
                else:
                    tool_result = agent.execute_tool(tool, args)
                serialized = json.dumps(tool_result, ensure_ascii=False)
                if provider == "ollama":
                    tool_message = {
                        "role": "tool",
                        "tool_name": tool,
                        "content": serialized,
                    }
                else:
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "content": serialized,
                    }
                msgs.append(tool_message)

            if step == 4:
                msgs.append(
                    {
                        "role": "user",
                        "content": "Summarize the function results briefly. Do not call any more functions.",
                    }
                )
                final = cls._dispatch_ai(
                    provider,
                    model,
                    api_key,
                    prompt,
                    msgs,
                    on_token=(lambda p: on_progress("", p)) if on_progress else None,
                    on_metrics=on_metrics,
                    cancel_event=cancel_event,
                )
                return _strip_thinking(str(final or "System actions completed."))
        return "System actions completed."

    def _ai_done(self, prompt: str, reply: str, ok: bool, gen: int | None = None):
        if self._stopped or (gen is not None and gen != self._ai_gen):
            return False
        self._ai_busy = False
        if ok and reply:
            self.ai_history.append({"role": "user", "content": prompt})
            self.ai_history.append({"role": "assistant", "content": reply})
            del self.ai_history[:-10]
        self.emit("ai-reply", reply, ok)
        return False

    @staticmethod
    def _dispatch_ai(
        provider: str,
        model: str,
        api_key: str,
        prompt: str,
        messages: list[dict] | None = None,
        tools: list[dict] | None = None,
        on_token=None,
        on_metrics=None,
        cancel_event: threading.Event | None = None,
    ) -> str | dict:
        msgs = messages or [{"role": "user", "content": prompt}]
        if provider == "ollama":
            host = _ollama_url()
            use_stream = (
                on_token is not None and tools is None and _post_json is _ORIG_POST_JSON
            )
            body = {
                "model": model or "llama3.2",
                "messages": msgs,
                "stream": use_stream,
            }
            if tools is not None:
                body["tools"] = tools
            if use_stream:
                text, stats = _stream_ollama_chat(
                    f"{host}/api/chat",
                    body,
                    on_token=on_token,
                    cancel_event=cancel_event,
                    timeout=180,
                )
                if on_metrics is not None and stats:
                    on_metrics(stats)
                return text
            data = _post_json(f"{host}/api/chat", body, timeout=180)
            stats = _extract_ollama_stats(data)
            if on_metrics is not None and stats:
                on_metrics(stats)
            message = data.get("message") or {}
            if tools is not None:
                tool_calls = []
                for call in message.get("tool_calls", []) or []:
                    function = call.get("function") or {}
                    tool_calls.append(
                        {
                            "name": function.get("name", ""),
                            "arguments": function.get("arguments", {}),
                        }
                    )
                return {
                    "content": _strip_thinking(str(message.get("content") or "")),
                    "tool_calls": tool_calls,
                    "assistant_message": message,
                }
            return _strip_thinking(str(message.get("content", "")))

        if provider == "lmstudio":
            use_stream = (
                on_token is not None and tools is None and _post_json is _ORIG_POST_JSON
            )
            body = {
                "model": model or "local-model",
                "messages": msgs,
            }
            if tools is not None:
                body["tools"] = tools
            if use_stream:
                body["stream"] = True
                text, stats = _stream_openai_chat(
                    f"{_lmstudio_url()}/v1/chat/completions",
                    body,
                    on_token=on_token,
                    cancel_event=cancel_event,
                    timeout=180,
                )
                if on_metrics is not None and stats:
                    on_metrics(stats)
                return text
            data = _post_json(
                f"{_lmstudio_url()}/v1/chat/completions", body, timeout=180
            )
            lm_stats = _extract_lmstudio_stats(data)
            if on_metrics is not None and lm_stats:
                on_metrics(lm_stats)
            message = data["choices"][0]["message"]
            if tools is not None:
                assistant_message = dict(message)
                tool_calls = []
                for index, call in enumerate(message.get("tool_calls", []) or []):
                    function = call.get("function") or {}
                    call_id = str(call.get("id") or f"lost-island-{index}")
                    tool_calls.append(
                        {
                            "id": call_id,
                            "name": function.get("name", ""),
                            "arguments": function.get("arguments", {}),
                        }
                    )
                    if index < len(assistant_message.get("tool_calls", [])):
                        assistant_message["tool_calls"][index]["id"] = call_id
                return {
                    "content": _strip_thinking(str(message.get("content") or "")),
                    "tool_calls": tool_calls,
                    "assistant_message": assistant_message,
                }
            return _strip_thinking(str(message.get("content") or ""))

        if provider == "claude":
            key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
            if not key:
                raise ValueError("Missing Anthropic API key")
            sys_parts = [m["content"] for m in msgs if m.get("role") == "system"]
            conv = [m for m in msgs if m.get("role") != "system"]
            body: dict = {
                "model": model or "claude-sonnet-4-5",
                "max_tokens": 512,
                "messages": conv,
            }
            if sys_parts:
                body["system"] = "\n\n".join(sys_parts)
            data = _post_json(
                "https://api.anthropic.com/v1/messages",
                body,
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            )
            return str(data["content"][0]["text"]).strip()

        if provider == "gemini":
            key = api_key or os.environ.get("GEMINI_API_KEY", "")
            if not key:
                raise ValueError("Missing Gemini API key")
            m = model or "gemini-2.5-flash"
            sys_parts = [
                m_it["content"] for m_it in msgs if m_it.get("role") == "system"
            ]
            contents = [
                {
                    "role": "model" if item["role"] == "assistant" else "user",
                    "parts": [{"text": item["content"]}],
                }
                for item in msgs
                if item.get("role") != "system"
            ]
            body = {"contents": contents}
            if sys_parts:
                body["systemInstruction"] = {
                    "parts": [{"text": "\n\n".join(sys_parts)}]
                }
            data = _post_json(
                f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                body,
                headers={"x-goog-api-key": key},
            )
            return str(data["candidates"][0]["content"]["parts"][0]["text"]).strip()

        if provider == "openrouter":
            key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
            if not key:
                raise ValueError("Missing OpenRouter API key")
            body = {
                "model": model or "openai/gpt-4o-mini",
                "messages": msgs,
            }
            data = _post_json(
                "https://openrouter.ai/api/v1/chat/completions",
                body,
                headers={"Authorization": f"Bearer {key}"},
            )
            return str(data["choices"][0]["message"]["content"]).strip()

        raise ValueError(f"Unknown AI provider: {provider}")


def _extract_ollama_stats(data: dict) -> dict | None:
    try:
        ev_cnt = int(data.get("eval_count") or 0)
        ev_ns = int(data.get("eval_duration") or 0)
    except (TypeError, ValueError):
        return None
    if ev_cnt > 0 and ev_ns > 0:
        return {"tok_s": ev_cnt / (ev_ns / 1e9), "eval_count": ev_cnt}
    return None


def _extract_lmstudio_stats(data: dict) -> dict | None:
    stats = data.get("stats") if isinstance(data, dict) else None
    if isinstance(stats, dict) and stats.get("tokens_per_second"):
        try:
            usage = data.get("usage") or {}
            return {
                "tok_s": float(stats["tokens_per_second"]),
                "eval_count": int(usage.get("completion_tokens") or 0),
            }
        except (TypeError, ValueError):
            pass
    return None


def _stream_ollama_chat(
    url: str,
    payload: dict,
    on_token=None,
    cancel_event: threading.Event | None = None,
    timeout: float = 180,
) -> tuple[str, dict | None]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "lost-island"},
        method="POST",
    )
    parts: list[str] = []
    stats: dict | None = None
    last_emit = 0.0
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw_line in resp:
            if cancel_event is not None and cancel_event.is_set():
                break
            line = raw_line.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                pkt = json.loads(line)
            except ValueError:
                continue
            msg = pkt.get("message") or {}
            chunk = str(msg.get("content") or "")
            if chunk:
                parts.append(chunk)
                now = time.monotonic()
                if on_token is not None and (now - last_emit) >= 0.075:
                    last_emit = now
                    shown = _strip_thinking("".join(parts))
                    if shown:
                        on_token(shown)
            if pkt.get("done"):
                stats = _extract_ollama_stats(pkt)
                break
    return _strip_thinking("".join(parts)), stats


def _stream_openai_chat(
    url: str,
    payload: dict,
    on_token=None,
    cancel_event: threading.Event | None = None,
    timeout: float = 180,
) -> tuple[str, dict | None]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "lost-island"},
        method="POST",
    )
    parts: list[str] = []
    stats: dict | None = None
    last_emit = 0.0
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw_line in resp:
            if cancel_event is not None and cancel_event.is_set():
                break
            line = raw_line.decode("utf-8", "replace").strip()
            if not line or not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                pkt = json.loads(data_str)
            except ValueError:
                continue
            choices = pkt.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                chunk = str(delta.get("content") or "")
                if chunk:
                    parts.append(chunk)
                    now = time.monotonic()
                    if on_token is not None and (now - last_emit) >= 0.075:
                        last_emit = now
                        shown = _strip_thinking("".join(parts))
                        if shown:
                            on_token(shown)
            extracted = _extract_lmstudio_stats(pkt)
            if extracted:
                stats = extracted
    elapsed = max(0.05, time.monotonic() - t0)
    full_text = _strip_thinking("".join(parts))
    if stats is None and parts:
        # Rough fallback if server did not send explicit stats chunk
        stats = {"tok_s": max(0.1, len(parts) / elapsed), "eval_count": len(parts)}
    return full_text, stats


def _post_json(
    url: str,
    payload: dict,
    headers: dict | None = None,
    timeout: float = 20,
) -> dict:
    hdrs = {"Content-Type": "application/json", "User-Agent": "lost-island"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=hdrs, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    if not isinstance(data, dict):
        raise TypeError("unexpected JSON response")
    return data


_ORIG_POST_JSON = _post_json
