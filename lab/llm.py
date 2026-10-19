"""Two chat adapters with one interface: OpenAI (hosted) and Ollama (local).

Messages are kept in OpenAI chat format; the Ollama adapter converts on the way in and out.
Both use the provider's default sampling settings (no temperature override), so the
10 repetitions sample real run-to-run variation.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx


def load_env() -> None:
    p = Path(__file__).resolve().parents[1] / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


@dataclass
class Reply:
    content: str
    tool_calls: list[dict]              # [{"id", "name", "arguments": dict}]
    latency_s: float
    usage: dict = field(default_factory=dict)


class OpenAIChat:
    def __init__(self, model: str):
        load_env()
        self.model, self.name = model, f"openai:{model}"
        self.http = httpx.Client(base_url="https://api.openai.com/v1", timeout=120,
                                 headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})

    def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        t0 = time.perf_counter()
        for attempt in range(5):
            r = self.http.post("/chat/completions", json={"model": self.model, "messages": messages, "tools": tools})
            if r.status_code in (429, 500, 502, 503):
                time.sleep(2 ** attempt); continue
            r.raise_for_status(); break
        d = r.json()
        m = d["choices"][0]["message"]
        calls = []
        for c in m.get("tool_calls") or []:
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable": c["function"]["arguments"]}
            calls.append({"id": c["id"], "name": c["function"]["name"], "arguments": args})
        return Reply(m.get("content") or "", calls, time.perf_counter() - t0, d.get("usage", {}))


class OllamaChat:
    def __init__(self, model: str, host: str = "http://localhost:11434"):
        self.model, self.name = model, f"ollama:{model}"
        self.http = httpx.Client(base_url=host, timeout=600)

    @staticmethod
    def _to_native(messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                out.append({"role": "assistant", "content": m.get("content") or "",
                            "tool_calls": [{"function": {"name": c["function"]["name"],
                                                         "arguments": json.loads(c["function"]["arguments"])}}
                                           for c in m["tool_calls"]]})
            elif m["role"] == "tool":
                out.append({"role": "tool", "content": m["content"], "tool_name": m.get("name", "")})
            else:
                out.append({"role": m["role"], "content": m.get("content") or ""})
        return out

    def chat(self, messages: list[dict], tools: list[dict]) -> Reply:
        t0 = time.perf_counter()
        r = self.http.post("/api/chat", json={"model": self.model, "messages": self._to_native(messages),
                                              "tools": tools, "stream": False, "think": False})
        r.raise_for_status()
        d = r.json()
        m = d["message"]
        calls = [{"id": f"call_{uuid.uuid4().hex[:8]}", "name": c["function"]["name"],
                  "arguments": c["function"].get("arguments") or {}} for c in m.get("tool_calls") or []]
        usage = {"prompt_tokens": d.get("prompt_eval_count"), "completion_tokens": d.get("eval_count")}
        return Reply(m.get("content") or "", calls, time.perf_counter() - t0, usage)
