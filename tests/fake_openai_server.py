"""A tiny local server that speaks the OpenAI chat-completions API, for tests only.

It lets the tests drive CrewAI's REAL OpenAI-compatible HTTP client (the same path used
for Ollama, Groq or any LLM_BASE_URL) without internet. Answers come from ScriptedLLM, so
the server behaves like a model that follows the task steps literally.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripted_llm import ScriptedLLM


def groq_schema_error(tools: list) -> str | None:
    """Groq's strict tool-schema check: a schema with 'required' must have a non-empty
    'properties' map (an empty one counts as missing). Returns Groq's error text or None."""
    def walk(node):
        if isinstance(node, dict):
            if "required" in node and not node.get("properties"):
                return True
            return any(walk(v) for v in node.values())
        if isinstance(node, list):
            return any(walk(v) for v in node)
        return False
    for t in tools:
        fn = t.get("function", {})
        if fn.get("strict") and walk(fn.get("parameters")):
            return (f"invalid JSON schema for tool {fn.get('name')}, tools[0].function.parameters: "
                    "'required' present but 'properties' is missing")
    return None


class FakeOpenAIServer:
    def __init__(self, text_only: bool = False, groq_strict: bool = False, fail_status: int | None = None):
        self.brain = ScriptedLLM(model="fake-openai")
        self.text_only = text_only          # True = a model that never calls tools
        self.groq_strict = groq_strict      # True = reject tool schemas the way Groq does
        self.fail_status = fail_status      # e.g. 503 = a provider outage on every request
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self._send({"object": "list", "data": [{"id": "fake-model", "object": "model"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                outer.requests.append(body)
                tools = body.get("tools") or []
                if outer.fail_status:
                    return self._send({"error": {"message": "simulated outage", "type": "server_error"}},
                                      outer.fail_status)
                problem = groq_schema_error(tools) if outer.groq_strict else None
                if problem:
                    return self._send({"error": {"message": problem, "type": "invalid_request_error",
                                                 "param": "tools", "code": None}}, 400)
                out = "I think the answer is 3." if outer.text_only else outer.brain.call(body.get("messages", []), tools=tools)
                msg = {"role": "assistant", "content": None}
                finish = "stop"
                if isinstance(out, list):
                    msg["tool_calls"] = [{"id": c["id"], "type": "function", "function": c["function"]} for c in out]
                    finish = "tool_calls"
                else:
                    msg["content"] = str(out)
                self._send({"id": f"chatcmpl-{len(outer.requests)}", "object": "chat.completion",
                            "created": int(time.time()), "model": body.get("model", "fake"),
                            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

            def _send(self, obj, status=200):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
