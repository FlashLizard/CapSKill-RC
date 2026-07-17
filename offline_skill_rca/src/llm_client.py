"""OpenAI-compatible repair LLM 客户端。

该客户端只做三件事：发送 chat/completions 请求、把 request/response/parsed JSON
完整落盘、把模型输出解析为 JSON。它不参与 repair 决策，也不修改 prompt 内容。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


_REASONING_DISABLED_VALUES = {"", "0", "false", "no", "none", "off", "disable", "disabled"}
_DEEPSEEK_REASONING_VALUES = {"low", "medium", "high", "max", "xhigh"}


@dataclass
class LLMClient:
    """一个可记录交互轨迹的 LLM 调用封装。"""
    base_url: str
    api_key: str
    model: str
    timeout_sec: float = 1800.0
    max_retries: int = 3
    timeout_env_key: str | None = None
    max_retries_env_key: str | None = None
    transcript_dir: Path | None = None
    transcript_name: str = "stage-llm"
    reasoning_effort: str = ""

    def chat_json(self, system: str, user: str, max_tokens: int = 24_000) -> dict[str, Any]:
        """发送一次对话请求，并强制解析 JSON 返回。

        每次调用都会写三类 transcript：
        request.json 记录发出的消息，response.json 记录原始响应，parsed.json 记录
        JSON 解析后的结构。这些文件是可视化 repair 交互轨迹的主要数据源。
        """
        endpoint = self.endpoint()
        request_started_at = time.monotonic()
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        raw_reasoning_effort = (
            self.reasoning_effort
            or os.getenv("OFFLINE_SKILL_RCA_REASONING_EFFORT", "minimal")
        ).strip()
        reasoning_effort = normalize_reasoning_effort(
            raw_reasoning_effort,
            base_url=self.base_url,
            model=self.model,
        )
        if reasoning_effort:
            # gpt-5.5 这类 reasoning 模型在部分 OpenAI-compatible 网关上，如果不显式
            # 降低 reasoning effort，可能只返回隐藏 reasoning token 而没有 message.content。
            payload["reasoning_effort"] = reasoning_effort
            if should_send_nested_reasoning(self.base_url, self.model):
                payload["reasoning"] = {"effort": reasoning_effort}
        response_format = os.getenv("OFFLINE_SKILL_RCA_RESPONSE_FORMAT", "").strip()
        if response_format:
            # 仅在调用方显式启用时要求网关返回 JSON object；部分兼容网关不支持该字段，
            # 所以默认不打开，避免把可用模型误判为参数错误。
            payload["response_format"] = {"type": response_format}
        self.write_transcript_json(
            f"{self.transcript_name}.request.json",
            {
                "endpoint": endpoint,
                "model": self.model,
                "temperature": payload["temperature"],
                "requested_reasoning_effort": raw_reasoning_effort,
                "reasoning_effort": payload.get("reasoning_effort"),
                "reasoning": payload.get("reasoning"),
                "max_tokens": max_tokens,
                "messages": payload["messages"],
            },
        )
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        # 长 repair prompt 可能导致模型响应很慢；默认 read timeout 给到 30 分钟，
        # 也允许用环境变量临时覆盖。
        timeout_sec = resolve_runtime_number(
            specific_env_key=self.timeout_env_key,
            global_env_key="OFFLINE_SKILL_RCA_TIMEOUT_SEC",
            default=self.timeout_sec,
            cast=float,
        )
        timeout = httpx.Timeout(connect=60.0, read=timeout_sec, write=60.0, pool=60.0)
        max_retries = resolve_runtime_number(
            specific_env_key=self.max_retries_env_key,
            global_env_key="OFFLINE_SKILL_RCA_MAX_RETRIES",
            default=self.max_retries,
            cast=int,
        )
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            log_progress(
                f"LLM start {self.transcript_name} attempt {attempt + 1}/{max_retries + 1} "
                f"timeout={timeout_sec:.0f}s prompt_chars={len(user)}"
            )
            try:
                with httpx.Client(timeout=timeout, trust_env=False) as client:
                    response = client.post(endpoint, headers=headers, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # 只对网络/超时类异常重试；HTTP 4xx/5xx 会在下面记录响应体后直接报错。
                last_exc = exc
                log_progress(f"LLM transport error {self.transcript_name} attempt {attempt + 1}: {exc}")
                if attempt >= max_retries:
                    raise
                time.sleep(5)
                continue
            if response.status_code >= 400:
                # 错误响应也落盘，方便判断是额度、鉴权、模型名还是服务端问题。
                self.write_transcript_json(
                    f"{self.transcript_name}.error-attempt-{attempt + 1}.json",
                    {
                        "status_code": response.status_code,
                        "response_text": response.text,
                    },
                )
                if can_retry_with_low_reasoning(response.text, payload) and attempt < max_retries:
                    payload["reasoning_effort"] = "low"
                    if isinstance(payload.get("reasoning"), dict):
                        payload["reasoning"]["effort"] = "low"
                    self.write_transcript_json(
                        f"{self.transcript_name}.reasoning-effort-retry-{attempt + 1}.json",
                        {
                            "reason": "provider_rejected_reasoning_effort",
                            "next_reasoning_effort": "low",
                        },
                    )
                    time.sleep(1)
                    continue
                if response.status_code >= 500 and attempt < max_retries:
                    log_progress(
                        f"LLM HTTP {response.status_code} {self.transcript_name} attempt {attempt + 1}; retrying"
                    )
                    time.sleep(5)
                    continue
                raise RuntimeError(f"LLM request failed: HTTP {response.status_code}: {response.text[:1200]}")
            json_error: Exception | None = None
            try:
                data = response.json()
            except ValueError as exc:
                json_error = exc
                try:
                    data = parse_sse_chat_response(response.text)
                except RuntimeError:
                    data = None
            if data is None:
                # 少数网关会返回 200 但 body 为空或不是 JSON；这和 reasoning-only
                # 响应一样属于上游瞬时异常，需要记录原文并按 retry 策略处理。
                last_exc = json_error or RuntimeError("LLM response body was JSON null")
                self.write_transcript_json(
                    f"{self.transcript_name}.non-json-attempt-{attempt + 1}.json",
                    {
                        "status_code": response.status_code,
                        "response_text": response.text[:4000],
                        "error": str(last_exc),
                    },
                )
                if attempt >= max_retries:
                    raise RuntimeError(
                        f"LLM returned non-JSON HTTP response after {attempt + 1} attempt(s): {response.text[:1200]}"
                    ) from last_exc
                time.sleep(5)
                continue
            try:
                content = extract_content(data)
                self.write_transcript_json(
                    f"{self.transcript_name}.response.json",
                    {
                        "status_code": response.status_code,
                        "attempt": attempt + 1,
                        "raw_response": data,
                        "extracted_content": content,
                    },
                )
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    # 有些模型会把 JSON 包在 ```json fenced block 中；失败时再做一次宽容提取。
                    parsed = json.loads(extract_json_object(content))
                self.write_transcript_json(f"{self.transcript_name}.parsed.json", parsed)
                elapsed = time.monotonic() - request_started_at
                log_progress(f"LLM done {self.transcript_name} elapsed={elapsed:.1f}s")
                return parsed
            except (RuntimeError, json.JSONDecodeError) as exc:
                last_exc = exc
                self.write_transcript_json(
                    f"{self.transcript_name}.parse-error-attempt-{attempt + 1}.json",
                    {
                        "status_code": response.status_code,
                        "raw_response": data,
                        "error": str(exc),
                    },
                )
                if attempt >= max_retries:
                    raise
                log_progress(f"LLM parse error {self.transcript_name} attempt {attempt + 1}; retrying: {exc}")
                time.sleep(5)
        raise RuntimeError(f"LLM request failed after retries: {last_exc}")

    def endpoint(self) -> str:
        """把用户传入的 base_url 规范化成 chat/completions endpoint。"""
        base = self.base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/v1/chat/completions"

    def write_transcript_json(self, filename: str, data: Any) -> None:
        """写入 transcript JSON；未配置目录时静默跳过。"""
        if not self.transcript_dir:
            return
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        (self.transcript_dir / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def log_progress(message: str) -> None:
    """向终端输出带时间戳的 repair 进度。

    长 repair 经常卡在上游 LLM 响应上；这类日志不改变任何 prompt 或结果，只让
    调用方能看到当前正在等待哪一个 stage/request。
    """
    if str(os.getenv("OFFLINE_SKILL_RCA_QUIET") or "").strip().lower() in {"1", "true", "yes"}:
        return
    stamp = time.strftime("%m/%d %H:%M:%S")
    print(f"[{stamp}] {message}", file=sys.stderr, flush=True)


def resolve_runtime_number(
    *,
    specific_env_key: str | None,
    global_env_key: str,
    default: float | int,
    cast: Any,
) -> Any:
    """解析运行时数值配置。

    如果某个 stage 绑定了专属环境变量，即使该变量没有设置，也优先使用调用方
    传入的 stage 默认值。这样 Stage 2 可以有较短超时，不会被全局 30 分钟超时
    意外拉长。
    """
    if specific_env_key:
        raw = os.getenv(specific_env_key)
        return cast(raw) if raw not in (None, "") else cast(default)
    raw = os.getenv(global_env_key)
    return cast(raw) if raw not in (None, "") else cast(default)


def is_deepseek_provider(base_url: str, model: str) -> bool:
    """判断当前 OpenAI-compatible 调用是否面向 DeepSeek。

    DeepSeek 的 ``reasoning_effort`` 枚举不包含 OpenAI/Camel 网关常见的
    ``minimal``，如果照原样发送会在请求反序列化阶段被 HTTP 400 拒绝。
    """
    target = f"{base_url} {model}".lower()
    return "deepseek" in target


def normalize_reasoning_effort(raw_effort: str, *, base_url: str, model: str) -> str:
    """把用户/默认 reasoning effort 转成目标 provider 接受的取值。

    默认仍使用 ``minimal``，以兼容原先的 gpt-5.5/Camel 使用方式；但 DeepSeek
    只接受 ``low/medium/high/max/xhigh``，所以把 ``minimal`` 映射为最接近的
    ``low``。用户也可以通过 ``OFFLINE_SKILL_RCA_REASONING_EFFORT=off`` 完全
    关闭该参数。
    """
    effort = (raw_effort or "").strip().lower()
    if effort in _REASONING_DISABLED_VALUES:
        return ""
    if is_deepseek_provider(base_url, model):
        if effort == "minimal":
            return "low"
        if effort not in _DEEPSEEK_REASONING_VALUES:
            return "low"
    return effort


def should_send_nested_reasoning(base_url: str, model: str) -> bool:
    """是否同时发送 ``reasoning: {effort: ...}`` 兼容字段。

    DeepSeek 报错来自严格的请求 schema；对它只发送明确支持的
    ``reasoning_effort``，减少额外字段带来的兼容风险。
    """
    return not is_deepseek_provider(base_url, model)


def can_retry_with_low_reasoning(response_text: str, payload: dict[str, Any]) -> bool:
    """检测 provider 是否因为 ``minimal`` reasoning effort 拒绝了请求。"""
    if str(payload.get("reasoning_effort") or "").lower() != "minimal":
        return False
    text = response_text.lower()
    return "reasoning_effort" in text and "minimal" in text and "unknown variant" in text


def extract_content(data: dict[str, Any]) -> str:
    """从 OpenAI-compatible 响应结构中提取 assistant content。"""
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception as exc:
        raise RuntimeError(f"Unexpected LLM response shape: {str(data)[:1200]}") from exc
    if isinstance(content, list):
        # 兼容部分多模态/分段 content 返回格式，把文本片段拼接成一个字符串。
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def parse_sse_chat_response(text: str) -> dict[str, Any]:
    """把 OpenAI-compatible SSE chunk 响应聚合成普通 chat completion 结构。

    某些网关即使请求中没有 ``stream=true``，也可能返回 ``data: {...}`` 的
    ``chat.completion.chunk`` 文本流。这里只做格式转换，不改写模型内容。
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    parsed_any = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith("data:"):
            raise RuntimeError("Not an SSE chat response")
        payload = line[len("data:") :].strip()
        if payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Malformed SSE JSON chunk") from exc
        parsed_any = True
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        message = choices[0].get("message") or {}
        if delta.get("content") is not None:
            content_parts.append(str(delta.get("content") or ""))
        if message.get("content") is not None:
            content_parts.append(str(message.get("content") or ""))
        if delta.get("reasoning_content") is not None:
            reasoning_parts.append(str(delta.get("reasoning_content") or ""))
        if message.get("reasoning_content") is not None:
            reasoning_parts.append(str(message.get("reasoning_content") or ""))
    if not parsed_any:
        raise RuntimeError("No SSE chunks parsed")
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    return {
        "object": "chat.completion",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "raw_stream": True,
    }


def extract_json_object(text: str) -> str:
    """提取第一个括号平衡的 JSON object。

    网关偶尔会在一个完整 JSON 后多返回一个 ``}``，或者把嵌套 JSON 包进代码
    围栏。用 ``rfind`` 会把尾部多余括号也纳入候选，非贪婪正则又会在第一个
    内层对象处过早结束。这里按字符扫描并处理字符串转义，确定性返回第一个完整
    的最外层对象；对象之后的说明文字或多余括号不会触发一次昂贵的 LLM 重试。
    """
    start = text.find("{")
    if start == -1:
        raise json.JSONDecodeError("No JSON object found", text, 0)
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
            if depth < 0:
                break
    raise json.JSONDecodeError("No balanced JSON object found", text, start)
