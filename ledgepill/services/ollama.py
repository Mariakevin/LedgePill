"""Local LLM runtime monitoring & control: Ollama and LM Studio.

Queries model inventory, VRAM GPU offload percentages, context sizes, and
capabilities off the UI thread, and exposes load/unload & tok/s tracking.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import urllib.error
import urllib.request
from typing import ClassVar

from gi.repository import GLib, GObject

_GIB = 1024**3
_NET_ERRORS = (
    AttributeError,
    http.client.HTTPException,
    OSError,
    TimeoutError,
    TypeError,
    ValueError,
    urllib.error.URLError,
)


def _lmstudio_url() -> str:
    host = os.environ.get("LMSTUDIO_HOST", "127.0.0.1:1234").strip()
    if not host:
        host = "127.0.0.1:1234"
    if "://" not in host:
        host = "http://" + host
    return host.rstrip("/")


def _ollama_url() -> str:
    host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").strip()
    if not host:
        host = "127.0.0.1:11434"
    if "://" not in host:
        host = "http://" + host
    return host.rstrip("/")


class OllamaService(GObject.Object):
    __gsignals__: ClassVar[dict] = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self):
        super().__init__()
        self.available = False
        self.status = "offline"
        self.model = ""
        self.models = []
        self.running_models = []
        self.running_details: list[dict] = []
        self.model_details: dict[str, dict] = {}
        self.tokens_per_sec = 0.0
        self.last_eval_tokens = 0
        self.last_model_used = ""
        self.lmstudio_available = False
        self.lmstudio_models = []
        self.lmstudio_loaded: list[str] = []
        self.lmstudio_details: dict[str, dict] = {}
        self._model_caps: dict[str, list[str]] = {}
        self._no_tools_models: set[str] = set()
        self._base = _ollama_url()
        self._timer = 0
        self._busy = False
        self._gen = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if not self._timer:
            self.refresh_now()
            self._timer = GLib.timeout_add_seconds(10, self._tick)

    def stop(self):
        self._busy = False
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self._gen += 1

    def _tick(self):
        self.refresh_now()
        return True

    def refresh_now(self):
        if self._busy:
            return
        self._busy = True
        gen = self._gen
        threading.Thread(target=self._fetch, args=(gen,), daemon=True).start()

    # -- fetch & control ---------------------------------------------------

    def _get_json(self, url: str):
        with urllib.request.urlopen(url, timeout=1.5) as r:
            data = json.load(r)
        if not isinstance(data, dict):
            raise TypeError("Unexpected local model service response")
        return data

    def _post_json(self, url: str, payload: dict, timeout: float = 2.0):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        return data if isinstance(data, dict) else {}

    def model_supports_tools(self, model: str) -> bool:
        name = (model or "").strip()
        if not name or name in self._no_tools_models:
            return False
        caps = self._model_caps.get(name)
        if caps is not None:
            return "tools" in caps
        return True

    def mark_no_tools(self, model: str):
        name = (model or "").strip()
        if name:
            self._no_tools_models.add(name)

    def record_metrics(
        self, tokens_per_sec: float, eval_tokens: int = 0, model: str = ""
    ):
        """Record latest inference throughput safely on the GLib main loop."""
        tok_s = max(0.0, float(tokens_per_sec or 0.0))
        cnt = max(0, int(eval_tokens or 0))
        mod = str(model or "")
        GLib.idle_add(self._apply_metrics, tok_s, cnt, mod, self._gen)

    def _apply_metrics(self, tok_s: float, eval_tokens: int, model: str, gen: int):
        if gen != self._gen:
            return False
        if tok_s > 0:
            self.tokens_per_sec = round(tok_s, 1)
        if eval_tokens > 0:
            self.last_eval_tokens = eval_tokens
        if model:
            self.last_model_used = model
        self.emit("changed")
        return False

    def preload_model(self, model: str = "", keep_alive: str = "10m") -> bool:
        """Warm an Ollama model into VRAM or adjust its keep-alive window."""
        target = (model or self.model or "").strip()
        if not target:
            return False
        return self._set_keep_alive(target, keep_alive)

    def unload_model(self, model: str = "") -> bool:
        """Unload a running Ollama model from VRAM immediately (keep_alive=0)."""
        target = (
            model
            or (self.running_models[0] if self.running_models else "")
            or self.model
            or ""
        ).strip()
        if not target:
            return False
        return self._set_keep_alive(target, 0)

    def _set_keep_alive(self, model: str, keep_alive) -> bool:
        try:
            self._post_json(
                f"{self._base}/api/chat",
                {"model": model, "messages": [], "keep_alive": keep_alive},
                timeout=4.0,
            )
            self.refresh_now()
            return True
        except _NET_ERRORS:
            return False

    @staticmethod
    def _parse_ps_details(ps: dict) -> list[dict]:
        rows: list[dict] = []
        for item in ps.get("models", []) or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            size = int(item.get("size") or 0)
            size_vram = int(item.get("size_vram") or 0)
            vram_pct = (
                max(0, min(100, round(100.0 * size_vram / size))) if size > 0 else 0
            )
            det = item.get("details") or {}
            rows.append(
                {
                    "name": name,
                    "size_gb": round(size / _GIB, 2) if size > 0 else 0.0,
                    "vram_gb": round(size_vram / _GIB, 2) if size_vram > 0 else 0.0,
                    "vram_pct": vram_pct,
                    "context_length": int(item.get("context_length") or 0),
                    "quantization": str(det.get("quantization_level") or ""),
                    "parameter_size": str(det.get("parameter_size") or ""),
                }
            )
        return rows

    @staticmethod
    def _parse_tag_details(tags: dict) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for item in tags.get("models", []) or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            size = int(item.get("size") or 0)
            det = item.get("details") or {}
            out[name] = {
                "size_gb": round(size / _GIB, 2) if size > 0 else 0.0,
                "parameter_size": str(det.get("parameter_size") or ""),
                "quantization": str(det.get("quantization_level") or ""),
                "family": str(det.get("family") or ""),
            }
        return out

    def _fetch(self, gen):
        available = False
        models: list[str] = []
        running: list[str] = []
        run_details: list[dict] = []
        tag_details: dict[str, dict] = {}
        model = ""
        status = "offline"
        caps_update: tuple[str, list[str]] | None = None
        try:
            self._get_json(f"{self._base}/api/version")
            tags = self._get_json(f"{self._base}/api/tags")
            ps = self._get_json(f"{self._base}/api/ps")
            tag_details = self._parse_tag_details(tags)
            models = list(tag_details.keys()) or [
                m.get("name", "") for m in tags.get("models", []) if m.get("name")
            ]
            run_details = self._parse_ps_details(ps)
            running = [d["name"] for d in run_details]
            model = running[0] if running else (models[0] if models else "")
            status = "active" if running else "ready"
            available = True
            if model and model not in self._model_caps:
                try:
                    shown = self._post_json(
                        f"{self._base}/api/show", {"model": model}, timeout=1.2
                    )
                    caps = shown.get("capabilities")
                    if isinstance(caps, list):
                        caps_update = (model, [str(c) for c in caps])
                except _NET_ERRORS:
                    pass
        except _NET_ERRORS:
            pass
        lm_models, lm_avail, lm_loaded, lm_details = self._fetch_lmstudio()
        GLib.idle_add(
            self._apply,
            available,
            models,
            running,
            status,
            model,
            lm_avail,
            lm_models,
            gen,
            run_details,
            tag_details,
            lm_loaded,
            lm_details,
            caps_update,
        )

    def _fetch_lmstudio(self):
        base = _lmstudio_url()
        try:
            v0 = self._get_json(f"{base}/api/v0/models")
            loaded: list[str] = []
            unloaded: list[str] = []
            details: dict[str, dict] = {}
            for item in v0.get("data", []) or []:
                if not isinstance(item, dict):
                    continue
                mid = str(item.get("id") or "")
                if not mid:
                    continue
                state = str(item.get("state") or "")
                details[mid] = {
                    "state": state or "unknown",
                    "quantization": str(item.get("quantization") or ""),
                    "arch": str(item.get("arch") or ""),
                    "max_context_length": int(item.get("max_context_length") or 0),
                    "loaded_context_length": int(
                        item.get("loaded_context_length") or 0
                    ),
                }
                if state == "loaded":
                    loaded.append(mid)
                else:
                    unloaded.append(mid)
            if loaded or unloaded:
                return loaded + unloaded, True, loaded, details
        except _NET_ERRORS:
            pass
        try:
            data = self._get_json(f"{base}/v1/models")
            models = [m.get("id", "") for m in data.get("data", []) if m.get("id")]
            return models, True, [], {}
        except _NET_ERRORS:
            return [], False, [], {}

    def _apply(
        self,
        available,
        models,
        running,
        status,
        model,
        lmstudio_available=False,
        lmstudio_models=None,
        gen=None,
        running_details=None,
        model_details=None,
        lmstudio_loaded=None,
        lmstudio_details=None,
        caps_update=None,
    ):
        if gen is not None and gen != self._gen:
            return False
        self.available = bool(available)
        self.models = list(models or [])
        self.running_models = list(running or [])
        self.running_details = list(running_details or [])
        self.model_details = dict(model_details or {})
        self.status = status or ("ready" if available else "offline")
        self.model = model or ""
        self.lmstudio_available = bool(lmstudio_available)
        self.lmstudio_models = list(lmstudio_models or [])
        self.lmstudio_loaded = list(lmstudio_loaded or [])
        self.lmstudio_details = dict(lmstudio_details or {})
        if caps_update is not None:
            self._model_caps[caps_update[0]] = caps_update[1]
        self._busy = False
        self.emit("changed")
        return False
