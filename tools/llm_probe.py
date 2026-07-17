#!/usr/bin/env python3
"""测试任意 OpenAI-compatible chat-completions 供应商。

这个小工具不依赖 repair pipeline，因此可以在正式运行前单独验证 endpoint、模型名、
认证和基础 JSON 响应。它只在输出中显示 key 是否存在，不会打印 key 本身。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def endpoint(base_url: str) -> str:
    """兼容 base URL、/v1 和完整 /chat/completions 三种输入。"""
    value = base_url.rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    if value.endswith("/v1"):
        return f"{value}/chat/completions"
    return f"{value}/v1/chat/completions"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe an OpenAI-compatible chat completion API")
    parser.add_argument("--provider", default="openai", help="provider label, used only in the printed summary")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key", default=os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "")
    prompt = parser.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file", type=Path)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--reasoning-effort", default="")
    parser.add_argument("--timeout", type=float, default=60)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.api_key:
        print("Missing API key: pass --api-key or set LLM_API_KEY/OPENAI_API_KEY", file=sys.stderr)
        return 2
    prompt = args.prompt if args.prompt is not None else args.prompt_file.read_text(encoding="utf-8")
    payload: dict[str, object] = {
        "model": args.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": max(1, args.max_tokens),
    }
    if args.reasoning_effort and args.reasoning_effort.lower() not in {"off", "none", "false"}:
        payload["reasoning_effort"] = args.reasoning_effort
    request = urllib.request.Request(
        endpoint(args.base_url),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {args.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "OpenAI/1.0",
            "Accept": "application/json",
        },
        method="POST",
    )
    print(json.dumps({"provider": args.provider, "endpoint": endpoint(args.base_url), "model": args.model, "api_key_present": True}, indent=2))
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:2000]
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(f"HTTP {error.code} after {elapsed_ms:.1f} ms: {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(f"Request failed after {elapsed_ms:.1f} ms: {error}", file=sys.stderr)
        return 1
    elapsed_ms = (time.perf_counter() - started) * 1000
    content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
    print(json.dumps({"ok": True, "latency_ms": round(elapsed_ms, 1), "content": content, "response_keys": sorted(body.keys())}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
