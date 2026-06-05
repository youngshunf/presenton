#!/usr/bin/env python3
"""唤星 new-api 结构化输出兼容 shim（Presenton sidecar 专用，D1 兼容层）.

为什么需要它（活体实测结论，2026-06-05）：
  唤星 new-api 的 gpt-5.x 渠道桥接到 OpenAI Responses API（响应 id 形如 `resp_…`），
  **不支持 `response_format: {type: "json_schema"}`（strict 结构化输出）——schema 被静默丢弃，
  模型返回自由 Markdown 散文**；而 Presenton 的大纲/分页/配图编排硬依赖结构化 JSON
  （`llmai` 的 `_final_content` 直接对流式累积内容 `json.loads`，拿到 Markdown 即
  `JSONDecodeError: Expecting value: line 1 column 1 (char 0)` → 500）。

  实测同一渠道**支持 `response_format: {type: "json_object"}`**（返回合法 JSON）。
  因此本 shim 在 sidecar↔new-api 之间做最小改写：

    请求路径 `*/chat/completions` 且 `response_format.type == "json_schema"`：
      1. 把 `response_format` 改写为 `{"type": "json_object"}`；
      2. 把原 JSON Schema 注入到 system 消息（"只输出符合此 schema 的 JSON，无 markdown"）。
    其余请求（含 `/v1/images/generations`、`/v1/models`、非结构化 chat）**原样透传**。

零 Mock 零 Fake：
  - 真实模型、真实 token（仅转发 Authorization，不持有密钥）、真实 JSON 输出；
  - Presenton 自带 `generate_structured_with_schema_retries` 的 `validate_schema` 重试环
    仍会校验 JSON 是否匹配 schema 并自纠，正确性不靠 shim 兜底；
  - 上游报错/不可达 → 如实把状态码与响应体透传回 sidecar，不伪造成功。

退场条件：一旦 new-api 提供原生支持 `json_schema` 的渠道（如真实 gpt-4o-2024-08-06+），
  把 sidecar 的 `CUSTOM_LLM_URL` 直接指回 new-api 即可摘除本 shim（`--no-llm-compat`）。
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 转发时丢弃的 hop-by-hop / 会被重算的头
_DROP_REQ_HEADERS = {"host", "content-length", "accept-encoding", "connection"}
_DROP_RESP_HEADERS = {"transfer-encoding", "content-length", "connection", "content-encoding"}

_SCHEMA_INSTRUCTION = (
    "You must respond with ONLY a single valid JSON object that strictly conforms to "
    "the following JSON Schema. Do not include any markdown, code fences, comments, or "
    "prose — output the raw JSON object and nothing else.\nJSON Schema:\n{schema}"
)


def _extract_schema(response_format: dict) -> dict | None:
    """从 OpenAI `response_format.json_schema` 里取出真正的 schema 字典（容错多种形状）。"""
    js = response_format.get("json_schema")
    if isinstance(js, dict):
        if isinstance(js.get("schema"), dict):
            return js["schema"]
        # 少数客户端把 schema 直接放在 json_schema 下（无 schema 包裹）
        if "type" in js or "properties" in js:
            return {k: v for k, v in js.items() if k not in ("name", "strict")}
    if isinstance(response_format.get("schema"), dict):
        return response_format["schema"]
    return None


def _rewrite_chat_body(raw: bytes) -> bytes:
    """把 json_schema 结构化请求改写为 json_object + schema 注入 system；其余原样返回。"""
    try:
        body = json.loads(raw)
    except Exception:
        return raw  # 非 JSON 不碰
    if not isinstance(body, dict):
        return raw
    rf = body.get("response_format")
    if not isinstance(rf, dict) or rf.get("type") != "json_schema":
        return raw  # 只改 json_schema，json_object/无格式一律透传
    schema = _extract_schema(rf)
    if schema is None:
        return raw  # 取不到 schema 就不动（宁可透传也不破坏请求）

    instruction = _SCHEMA_INSTRUCTION.format(
        schema=json.dumps(schema, ensure_ascii=False)
    )
    body["response_format"] = {"type": "json_object"}

    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = []
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        existing = messages[0].get("content")
        if isinstance(existing, str):
            messages[0] = {**messages[0], "content": existing + "\n\n" + instruction}
        else:  # content 为多模态 list 时，追加一段文本块
            messages[0] = {
                **messages[0],
                "content": [*existing, {"type": "text", "text": instruction}],
            }
    else:
        messages = [{"role": "system", "content": instruction}, *messages]
    body["messages"] = messages
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.0：响应以连接关闭定界，天然适配 SSE 流式（客户端读到 EOF 即止）。
    protocol_version = "HTTP/1.0"
    upstream_host = ""
    upstream_port = 0
    upstream_is_https = False
    upstream_base_path = ""  # 形如 ""（root）；incoming path 原样拼接
    _seq = 0  # 请求计数（排障可见性；按 path 标注是否改写）

    def log_message(self, fmt: str, *args) -> None:  # 静音默认 access log，避免污染 sidecar 日志
        return

    def _relay(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        # 仅对 chat/completions 的 json_schema 请求改写
        if method == "POST" and self.path.rstrip("/").endswith("/chat/completions"):
            before = len(raw)
            raw = _rewrite_chat_body(raw)
            _Handler._seq += 1
            rewritten = "rewritten(json_schema→json_object)" if len(raw) != before else "passthrough"
            print(f"[shim] #{_Handler._seq} POST {self.path} {rewritten}", flush=True)
        elif method == "POST" and "/images/generations" in self.path:
            _Handler._seq += 1
            print(f"[shim] #{_Handler._seq} POST {self.path} image-gen(passthrough)", flush=True)

        # 本地健康检查（不出网）
        if method == "GET" and self.path.rstrip("/").endswith("/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
            return

        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in _DROP_REQ_HEADERS
        }
        if raw:
            headers["Content-Length"] = str(len(raw))

        conn_cls = (
            http.client.HTTPSConnection if self.upstream_is_https
            else http.client.HTTPConnection
        )
        conn = conn_cls(self.upstream_host, self.upstream_port, timeout=300)
        try:
            conn.request(method, self.upstream_base_path + self.path, body=raw or None,
                         headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in _DROP_RESP_HEADERS:
                    self.send_header(k, v)
            self.end_headers()
            # 增量转发：流式（SSE）逐块写出并 flush；非流式同样按块拷贝。
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception as e:  # 上游故障如实回传（零 fake）
            with __import__("contextlib").suppress(Exception):
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"error": {"message": f"compat-shim upstream error: {e}",
                                          "type": "upstream_unreachable"}}).encode()
                )
        finally:
            conn.close()

    def do_GET(self) -> None:
        self._relay("GET")

    def do_POST(self) -> None:
        self._relay("POST")


def run(port: int, upstream: str) -> int:
    parsed = urllib.parse.urlparse(upstream)
    is_https = parsed.scheme == "https"
    host = parsed.hostname or "127.0.0.1"
    up_port = parsed.port or (443 if is_https else 80)
    base_path = parsed.path.rstrip("/")  # 通常为空（upstream 给 root）

    _Handler.upstream_host = host
    _Handler.upstream_port = up_port
    _Handler.upstream_is_https = is_https
    _Handler.upstream_base_path = base_path

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    print(f"[shim] new-api 兼容 shim 监听 127.0.0.1:{port} -> {host}:{up_port}{base_path} "
          f"(json_schema→json_object+inject)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="唤星 new-api 结构化输出兼容 shim")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--upstream", required=True,
                    help="new-api 根地址（如 http://127.0.0.1:3180），incoming path 原样拼接")
    args = ap.parse_args()
    return run(args.port, args.upstream)


if __name__ == "__main__":
    sys.exit(main())
