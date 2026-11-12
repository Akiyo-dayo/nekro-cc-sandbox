"""Stub Anthropic Messages API for the release smoke test (stdlib only).

Drives a real Claude Code CLI into the state a force-cancel leaves behind in real use, without any
model or credentials: a background Bash task is running and a model request is in flight when the
whole process group is killed. On the next resume the CLI first handles the "background task
stopped" notification with a synthetic, empty turn (printed as a 0-token "success" result), and
only then answers the new prompt in the same process.

- a request that offers the Bash tool and whose last user turn contains SMOKE-INTERRUPT gets a
  tool_use asking Bash to run `sleep 300` with run_in_background (Claude Code refuses a long
  foreground sleep, a background one is fine and returns at once);
- the CLI then comes back with that tool_result ("running in background"): this request is held
  open (no reply for HOLD_SECONDS), and the smoke test cancels the task while it hangs;
- a request whose last user turn contains SMOKE-AFTER-CANCEL gets the text "after-cancel-ok";
- anything else (warm-up, side requests the CLI makes on its own) gets the text "ok".

Both streaming (SSE) and non-streaming replies are supported. Every request is logged to stdout,
the held one as "-> hold" before it starts waiting. Run it only on a private docker network: it
answers anyone.
"""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

INTERRUPT_MARK = "SMOKE-INTERRUPT"
AFTER_MARK = "SMOKE-AFTER-CANCEL"
HOLD_SECONDS = 600
_counter = 0


def _next_id(prefix: str) -> str:
    global _counter
    _counter += 1
    return f"{prefix}_smoke{_counter:04d}"


def _last_user_turn(messages: list) -> tuple[str, list[str]]:
    """Text of the last user turn, plus a short preview of each tool_result in it."""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content, []
        texts: list[str] = []
        tool_results: list[str] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_result":
                tool_results.append(json.dumps(block.get("content"), ensure_ascii=False)[:200])
        return "\n".join(texts), tool_results
    return "", []


def _offers_bash(body: dict) -> bool:
    tools = body.get("tools")
    return isinstance(tools, list) and any(isinstance(t, dict) and t.get("name") == "Bash" for t in tools)


def _decide(body: dict) -> tuple[str, dict]:
    text, tool_results = _last_user_turn(body.get("messages") or [])
    for preview in tool_results:
        print(f"[fakeapi]   tool_result: {preview}", flush=True)
    if AFTER_MARK in text:
        return "after", {"type": "text", "text": "after-cancel-ok"}
    if INTERRUPT_MARK in text and not tool_results and _offers_bash(body):
        return "start", {
            "type": "tool_use",
            "id": _next_id("toolu"),
            "name": "Bash",
            "input": {"command": "sleep 300", "description": "smoke test", "run_in_background": True},
        }
    if tool_results:
        # the only tool call in this test is ours: hold the request that carries its result
        return "hold", {"type": "text", "text": "late"}
    return "other", {"type": "text", "text": "ok"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # quieter default access log
        return

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._send_json(200, {})

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        try:
            self._handle_post()
        except (BrokenPipeError, ConnectionResetError):
            print(f"[fakeapi] {self.path} client went away", flush=True)

    def _handle_post(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        path = self.path.split("?", 1)[0]
        if path.endswith("/count_tokens"):
            print(f"[fakeapi] {path} count_tokens", flush=True)
            self._send_json(200, {"input_tokens": 10})
            return
        if not path.endswith("/v1/messages"):
            print(f"[fakeapi] {path} -> 404", flush=True)
            self._send_json(404, {"type": "error", "error": {"type": "not_found_error", "message": path}})
            return

        kind, block = _decide(body)
        model = str(body.get("model") or "claude-smoke")
        stream = bool(body.get("stream"))
        print(f"[fakeapi] {path} model={model} stream={stream} tools={_offers_bash(body)} -> {kind}", flush=True)
        if kind == "hold":
            time.sleep(HOLD_SECONDS)
        stop_reason = "tool_use" if block["type"] == "tool_use" else "end_turn"
        message_id = _next_id("msg")
        if not stream:
            self._send_json(200, {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [block],
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            })
            return

        events: list[tuple[str, dict]] = [
            ("message_start", {"type": "message_start", "message": {
                "id": message_id, "type": "message", "role": "assistant", "model": model, "content": [],
                "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1},
            }}),
        ]
        if block["type"] == "text":
            events += [
                ("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "text_delta", "text": block["text"]}}),
            ]
        else:
            events += [
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {
                    "type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {
                    "type": "input_json_delta", "partial_json": json.dumps(block["input"])}}),
            ]
        events += [
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                               "usage": {"output_tokens": 5}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for name, payload in events:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()
        self.close_connection = True


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"[fakeapi] listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
