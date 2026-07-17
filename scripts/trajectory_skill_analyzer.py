#!/usr/bin/env python3
"""Analyze SkillsBench task trajectories with explicit skill-use reasoning.

The analyzer is intentionally two-layered:
1. A deterministic rules pass extracts task skills, skill invocations, tool use,
   outcome, and common failure patterns.
2. An optional Judge LLM pass reviews the compact evidence and adds qualitative
   judgments about appropriateness, correctness, and skill contribution.

CLI:
  python scripts/trajectory_skill_analyzer.py --task tasks/offer-letter-generator \
    --trajectory jobs/.../trajectory/acp_trajectory.jsonl --result jobs/.../result.json

Server mode:
  echo '{"taskPath":"tasks/...", "trajectoryPath":"jobs/..."}' | \
    python scripts/trajectory_skill_analyzer.py --json-input
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MAX_TEXT = 12_000
MAX_JUDGE_EVENTS = 80
MAX_KEYWORD_SKILL_EXCERPT = 1600
KEYWORD_MODES = {"rules", "llm-task", "llm-skills", "llm-both"}
STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "when", "using",
    "use", "into", "your", "task", "skill", "skills", "file", "data",
    "output", "input", "create", "read", "write", "need", "needs",
}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


@dataclass
class Skill:
    name: str
    description: str
    path: str
    keywords: list[str]
    keyword_source: str = "rules"
    body_excerpt: str = ""


def safe_rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except Exception:
        return str(path)


def resolve_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    try:
        path.relative_to(ROOT)
    except ValueError as exc:
        raise SystemExit(f"Path escapes workspace root: {value}") from exc
    return path


def read_text(path: Path | None, limit: int | None = None) -> str:
    if not path or not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if limit and len(text) > limit:
        return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
    return text


def read_json(path: Path | None) -> Any:
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, flags=re.S)
    if not match:
        return {}, text
    meta: dict[str, Any] = {}
    for raw in match.group(1).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("- "):
            continue
        pair = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if pair:
            meta[pair.group(1)] = pair.group(2).strip().strip("\"'")
    return meta, text[match.end():]


def tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_+-]{2,}", text.lower())
    out = []
    for token in tokens:
        for part in re.split(r"[-_+/]", token):
            if len(part) >= 3 and part not in STOPWORDS:
                out.append(part)
    return out


def clean_keyword(value: Any) -> str:
    keyword = str(value or "").strip().strip("`\"'")
    keyword = re.sub(r"\s+", " ", keyword)
    keyword = keyword[:80].strip().lower()
    if len(keyword) < 2 or keyword in STOPWORDS:
        return ""
    return keyword


def unique_keywords(values: list[Any], limit: int = 80) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        parts = re.split(r"[,;，；\n]+", value) if isinstance(value, str) else [value]
        for part in parts:
            keyword = clean_keyword(part)
            if keyword and keyword not in seen:
                seen.add(keyword)
                out.append(keyword)
                if len(out) >= limit:
                    return out
    return out


def load_skills(task_dir: Path | None, explicit_skills_dir: Path | None = None) -> list[Skill]:
    candidates: list[Path] = []
    if explicit_skills_dir:
        candidates.append(explicit_skills_dir)
    if task_dir:
        candidates.append(task_dir / "environment" / "skills")

    skills: list[Skill] = []
    seen: set[str] = set()
    for skills_dir in candidates:
        if not skills_dir.exists():
            continue
        for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
            text = read_text(skill_md)
            meta, body = parse_frontmatter(text)
            name = str(meta.get("name") or skill_md.parent.name)
            if name in seen:
                continue
            seen.add(name)
            description = str(meta.get("description") or "").strip()
            keywords = unique_keywords(sorted(set(
                tokenize(name)
                + tokenize(description)
                + tokenize(body[:4000])
                + [name.lower(), skill_md.parent.name.lower()]
            )), limit=80)
            skills.append(Skill(
                name=name,
                description=description,
                path=safe_rel(skill_md),
                keywords=keywords,
                body_excerpt=body[:MAX_KEYWORD_SKILL_EXCERPT],
            ))
    return skills


def load_trajectory(path: Path) -> tuple[list[dict[str, Any]], int]:
    events = []
    parse_errors = 0
    for line_no, line in enumerate(read_text(path).splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                event["_line"] = line_no
                event["_index"] = len(events) + 1
                events.append(event)
        except Exception:
            parse_errors += 1
    return events, parse_errors


def event_text(event: dict[str, Any]) -> str:
    chunks: list[str] = []
    for key in ("text", "title", "kind", "status", "tool_call_id"):
        value = event.get(key)
        if isinstance(value, str):
            chunks.append(value)
    content = event.get("content")
    if isinstance(content, list):
        for item in content:
            chunks.append(json.dumps(item, ensure_ascii=False)[:2000])
    elif content is not None:
        chunks.append(json.dumps(content, ensure_ascii=False)[:2000])
    return "\n".join(chunks)


def extract_skill_invocation(event: dict[str, Any], skills: list[Skill]) -> str | None:
    if event.get("type") != "tool_call":
        return None
    text = event_text(event)
    title = str(event.get("title") or "")
    if title.lower() != "skill" and "launching skill:" not in text.lower():
        return None
    match = re.search(r"launching skill:\s*([A-Za-z0-9_.-]+)", text, flags=re.I)
    if match:
        return match.group(1)
    lower = text.lower()
    for skill in skills:
        if skill.name.lower() in lower:
            return skill.name
    return None


def compact_event(event: dict[str, Any]) -> dict[str, Any]:
    text = event_text(event).replace("\r", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > 500:
        text = text[:500] + "..."
    return {
        "index": event.get("_index"),
        "type": event.get("type"),
        "kind": event.get("kind"),
        "title": event.get("title"),
        "status": event.get("status"),
        "preview": text,
    }


def score_skill_relevance(
    skill: Skill,
    task_text: str,
    user_prompt: str,
    task_keywords: list[str] | None = None,
) -> tuple[float, list[str]]:
    task_keyword_lowers = unique_keywords(task_keywords or [], limit=80)
    haystack = f"{task_text}\n{user_prompt}\n{' '.join(task_keyword_lowers)}".lower()
    hits = []
    for keyword in skill.keywords[:80]:
        lower = keyword.lower()
        overlaps_task_keyword = any(
            (len(lower) >= 3 and lower in task_keyword)
            or (len(task_keyword) >= 3 and task_keyword in lower)
            for task_keyword in task_keyword_lowers
        )
        if len(lower) >= 3 and (lower in haystack or overlaps_task_keyword):
            hits.append(keyword)
    direct = 1.0 if skill.name.lower() in haystack else 0.0
    score = min(1.0, direct * 0.45 + len(set(hits)) / 16)
    return round(score, 3), sorted(set(hits))[:14]


def infer_outcome(result: Any, events: list[dict[str, Any]]) -> dict[str, Any]:
    reward = None
    passed = False
    error = None
    category = None
    if isinstance(result, dict):
        rewards = result.get("rewards")
        if isinstance(rewards, dict) and isinstance(rewards.get("reward"), (int, float)):
            reward = float(rewards["reward"])
            passed = reward >= 1.0
        error = result.get("error")
        category = result.get("error_category")
        if error is None and reward is None:
            passed = bool(result.get("passed"))
    timeout_events = [e for e in events if e.get("type") == "agent_timeout"]
    if timeout_events and not error:
        error = "Agent timeout recorded in trajectory"
        category = "timeout"
    return {
        "passed": passed,
        "reward": reward,
        "error": error,
        "errorCategory": category,
        "timeout": bool(timeout_events),
    }


def infer_failure_success_reasons(
    outcome: dict[str, Any],
    result: Any,
    events: list[dict[str, Any]],
    skill_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    failures: list[dict[str, Any]] = []
    successes: list[dict[str, Any]] = []

    tool_calls = [e for e in events if e.get("type") == "tool_call"]
    failed_tools = [e for e in tool_calls if str(e.get("status")).lower() not in ("completed", "")]
    if outcome["passed"]:
        successes.append({"label": "验证通过", "detail": "result.json 显示 reward/pass 达成，最终产物满足 verifier。", "severity": "good"})
        if tool_calls:
            successes.append({"label": "有执行证据", "detail": f"轨迹包含 {len(tool_calls)} 次工具调用，其中 {len(tool_calls) - len(failed_tools)} 次完成。", "severity": "good"})
    else:
        if outcome.get("error"):
            failures.append({"label": "运行错误", "detail": str(outcome["error"])[:900], "severity": "bad"})
        if outcome.get("timeout"):
            failures.append({"label": "Agent 超时", "detail": "轨迹中出现 agent_timeout，模型没有在预算内完成有效行动。", "severity": "bad"})
        if isinstance(result, dict) and result.get("sandbox_startup_info"):
            failures.append({"label": "环境启动失败", "detail": "失败发生在 sandbox 或环境启动阶段，skill 使用分析只能作为缺失证据。", "severity": "bad"})
        if not tool_calls and events:
            failures.append({"label": "没有工具行动", "detail": "轨迹中没有 tool_call，说明模型没有进入实现/验证动作。", "severity": "bad"})
        if failed_tools:
            failures.append({"label": "工具调用失败", "detail": f"{len(failed_tools)} 个工具调用未完成，可能阻断了实现或验证。", "severity": "warn"})
        missed = [s for s in skill_rows if s["expected"] and not s["invoked"]]
        if missed:
            failures.append({"label": "可能漏用关键 skill", "detail": "、".join(s["name"] for s in missed[:5]), "severity": "warn"})

    for row in skill_rows:
        if row["invoked"] and row["expected"] and outcome["passed"]:
            successes.append({"label": f"{row['name']} 使用有效", "detail": row["assessment"], "severity": "good"})
    return failures, successes


def analyze_rules(
    task_dir: Path | None,
    trajectory_path: Path,
    result_path: Path | None,
    skills_dir: Path | None = None,
    keyword_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_md = task_dir / "task.md" if task_dir else None
    task_text = read_text(task_md, MAX_TEXT)
    skills = load_skills(task_dir, skills_dir)
    keyword_extraction = apply_llm_keyword_extraction(task_text, skills, keyword_config or {})
    task_keywords = keyword_extraction.get("taskKeywords") or []
    events, parse_errors = load_trajectory(trajectory_path)
    result = read_json(result_path)
    outcome = infer_outcome(result, events)

    user_prompt = "\n".join(str(e.get("text") or "") for e in events if e.get("type") == "user_message")
    tool_calls = [e for e in events if e.get("type") == "tool_call"]
    type_counts = Counter(str(e.get("type") or "unknown") for e in events)
    tool_kind_counts = Counter(str(e.get("kind") or "unknown") for e in tool_calls)
    tool_status_counts = Counter(str(e.get("status") or "unknown") for e in tool_calls)

    skill_rows: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    all_text_by_event = [(e, event_text(e).lower()) for e in events]

    for skill in skills:
        relevance, matches = score_skill_relevance(skill, task_text, user_prompt, task_keywords)
        expected = relevance >= 0.18
        mentioned_events = [
            e.get("_index") for e, text in all_text_by_event
            if skill.name.lower() in text or any(k in text for k in skill.keywords[:18])
        ][:12]
        invocations = [
            e for e in tool_calls
            if (extract_skill_invocation(e, skills) or "").lower() == skill.name.lower()
        ]
        invocation_index = invocations[0].get("_index") if invocations else None
        related_tools = [
            e for e, text in all_text_by_event
            if e.get("type") == "tool_call"
            and (skill.name.lower() in text or any(k in text for k in skill.keywords[:20]))
        ]
        post_invocation_tools = [
            e for e in tool_calls
            if invocation_index and e.get("_index", 0) > invocation_index
        ][:8]
        failed_related = [e for e in related_tools if str(e.get("status")).lower() not in ("completed", "")]

        if expected and invocations and not failed_related:
            correctness = "good"
            assessment = "任务与该 skill 高相关，且轨迹显示模型显式启动/引用该 skill，后续工具调用未出现明显相关失败。"
        elif expected and invocations and failed_related:
            correctness = "mixed"
            assessment = "模型意识到了该 skill，但相关工具调用存在失败，需要检查参数、文件路径或使用顺序。"
        elif expected and not invocations and related_tools:
            correctness = "partial"
            assessment = "任务与该 skill 相关，模型没有显式启动 skill，但工具调用里出现了相近方法或概念。"
        elif expected:
            correctness = "missed"
            assessment = "任务与该 skill 相关，但轨迹中没有发现明确使用或相关操作。"
        elif invocations:
            correctness = "questionable"
            assessment = "轨迹显示使用了该 skill，但任务相关性较弱，可能是过度调用。"
        else:
            correctness = "not-needed"
            assessment = "没有证据表明该 skill 是当前任务的关键路径。"

        if outcome["passed"]:
            contribution = 0.85 if expected and invocations else 0.55 if expected and related_tools else 0.15 if invocations else 0.05
        else:
            contribution = -0.7 if correctness == "missed" else -0.35 if correctness in ("mixed", "questionable") else 0.0

        row = {
            "name": skill.name,
            "description": skill.description,
            "path": skill.path,
            "keywordSource": skill.keyword_source,
            "relevance": relevance,
            "expected": expected,
            "invoked": bool(invocations),
            "invocationEvent": invocation_index,
            "mentionedEvents": mentioned_events,
            "relatedToolEvents": [e.get("_index") for e in related_tools[:12]],
            "postInvocationToolEvents": [e.get("_index") for e in post_invocation_tools],
            "correctness": correctness,
            "contribution": round(contribution, 2),
            "matchedKeywords": matches,
            "assessment": assessment,
        }
        skill_rows.append(row)

        if invocations:
            for event in invocations:
                timeline.append({
                    "event": event.get("_index"),
                    "skill": skill.name,
                    "kind": "invocation",
                    "label": f"启动 skill: {skill.name}",
                })
        elif expected and mentioned_events:
            timeline.append({
                "event": mentioned_events[0],
                "skill": skill.name,
                "kind": "mention",
                "label": f"提及/接近 skill: {skill.name}",
            })

    skill_rows.sort(key=lambda s: (not s["expected"], -abs(float(s["contribution"])), -float(s["relevance"]), s["name"]))
    timeline.sort(key=lambda item: item.get("event") or 10**9)
    failures, successes = infer_failure_success_reasons(outcome, result, events, skill_rows)

    return {
        "schemaVersion": 1,
        "inputs": {
            "taskPath": safe_rel(task_dir) if task_dir else None,
            "trajectoryPath": safe_rel(trajectory_path),
            "resultPath": safe_rel(result_path) if result_path else None,
            "skillsDir": safe_rel(skills_dir) if skills_dir else None,
        },
        "task": {
            "name": task_dir.name if task_dir else None,
            "promptExcerpt": task_text[:1200],
            "keywords": task_keywords[:30],
            "skillCount": len(skills),
        },
        "keywordExtraction": keyword_extraction,
        "outcome": outcome,
        "stats": {
            "events": len(events),
            "parseErrors": parse_errors,
            "toolCalls": len(tool_calls),
            "eventTypeCounts": dict(type_counts),
            "toolKindCounts": dict(tool_kind_counts),
            "toolStatusCounts": dict(tool_status_counts),
        },
        "reasons": {
            "failure": failures,
            "success": successes,
        },
        "skills": skill_rows,
        "timeline": timeline,
        "events": [compact_event(e) for e in events[:250]],
        "method": {
            "summary": "规则层先根据 task.md、SKILL.md、轨迹事件、result.json 建立证据；可选 LLM 关键词层会总结 task/skill 关键词并合并进相关性判断；随后判断 skill 是否预期、是否显式调用、调用后是否产生相关完成工具调用，并按成功/失败结果估计贡献。",
            "contributionScale": "-1 表示对失败有负贡献，0 表示无明显贡献，1 表示对成功强贡献。",
        },
    }


def normalize_base_url(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}{suffix}"
    return f"{base}/v1{suffix}"


def post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> tuple[int | None, str, float]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace"), time.perf_counter() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace"), time.perf_counter() - started
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}", time.perf_counter() - started


def is_retryable_llm_response(status: int | None, raw: str) -> bool:
    if status in {408, 409, 425, 429, 500, 502, 503, 504}:
        return True
    if status is not None:
        return False
    lowered = raw.lower()
    retryable_markers = [
        "timeout",
        "timed out",
        "remotedisconnected",
        "remote end closed",
        "connectionreseterror",
        "connection reset",
        "connectionabortederror",
        "temporarily unavailable",
        "service unavailable",
    ]
    return any(marker in lowered for marker in retryable_markers)


def post_json_with_retries(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    attempts: int,
) -> tuple[int | None, str, float, list[dict[str, Any]]]:
    attempts = max(1, attempts)
    total_elapsed = 0.0
    records: list[dict[str, Any]] = []
    last_status: int | None = None
    last_raw = ""
    for attempt in range(1, attempts + 1):
        status, raw, elapsed = post_json(url, headers, payload, timeout)
        total_elapsed += elapsed
        last_status = status
        last_raw = raw
        retryable = is_retryable_llm_response(status, raw)
        records.append(
            {
                "attempt": attempt,
                "status": status,
                "elapsedSec": round(elapsed, 2),
                "retryable": retryable,
                "rawPreview": raw[:400],
            }
        )
        if not retryable or attempt >= attempts:
            break
        wait_sec = min(30.0, 2.0 ** (attempt - 1))
        time.sleep(wait_sec)
        total_elapsed += wait_sec
    return last_status, last_raw, total_elapsed, records


def extract_json_object(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def extract_anthropic_sse_text(raw: str) -> str:
    """Extract text deltas from an Anthropic-style SSE response body."""
    chunks: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except Exception:
            continue
        event_type = event.get("type")
        if event_type == "content_block_delta":
            delta = event.get("delta") or {}
            if isinstance(delta, dict) and isinstance(delta.get("text"), str):
                chunks.append(delta["text"])
        elif event_type == "content_block_start":
            block = event.get("content_block") or {}
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                chunks.append(block["text"])
        elif event_type == "message":
            for item in event.get("content") or []:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    chunks.append(item["text"])
    return "".join(chunks)


def call_llm_json(prompt: str, config: dict[str, Any], default_max_tokens: int, missing_label: str) -> dict[str, Any]:
    api_key = (
        config.get("apiKey")
        or os.getenv("ANTHROPIC_AUTH_TOKEN")
        or os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    if not api_key:
        return {"ok": False, "error": f"Missing {missing_label} API key"}

    provider = (config.get("provider") or "anthropic").lower()
    base_url = config.get("baseUrl") or "https://api.camel-hub.com"
    model = config.get("model") or "deepseek-v4-flash"
    timeout = float(config.get("timeout") or 120)
    max_tokens = int(config.get("maxTokens") or default_max_tokens)
    retry_env = os.getenv("STRONG_LLM_RETRIES") or os.getenv("LLM_RETRIES")
    attempts = int(config.get("retries") or retry_env or 3)
    reasoning_effort = str(config.get("reasoningEffort") or os.getenv("STRONG_LLM_REASONING_EFFORT") or "off").strip().lower()
    if reasoning_effort == "minimal" and ("deepseek" in f"{model} {base_url}".lower()):
        reasoning_effort = "low"
    reasoning_budgets = {"minimal": 1024, "low": 2048, "medium": 4096, "high": 8192, "max": 16384, "xhigh": 24576}

    if provider == "openai":
        url = normalize_base_url(base_url, "/chat/completions")
        headers = {"content-type": "application/json", "authorization": f"Bearer {api_key}"}
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
        }
        if reasoning_effort not in {"", "off", "none", "false"}:
            payload["reasoning_effort"] = reasoning_effort
        status, raw, elapsed, attempt_records = post_json_with_retries(url, headers, payload, timeout, attempts)
        data = extract_json_object(raw)
        content = ""
        if isinstance(data, dict) and data.get("choices"):
            content = data["choices"][0].get("message", {}).get("content", "")
        parsed = extract_json_object(content) or data
    else:
        url = normalize_base_url(base_url, "/messages")
        headers = {
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": config.get("anthropicVersion") or "2023-06-01",
        }
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }
        if reasoning_effort in reasoning_budgets:
            payload["thinking"] = {"type": "enabled", "budget_tokens": reasoning_budgets[reasoning_effort]}
        status, raw, elapsed, attempt_records = post_json_with_retries(url, headers, payload, timeout, attempts)
        data = extract_json_object(raw)
        content = ""
        if isinstance(data, dict):
            for item in data.get("content") or []:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    content += item["text"]
        if not content and "event:" in raw and "data:" in raw:
            content = extract_anthropic_sse_text(raw)
        parsed = extract_json_object(content) or data

    return {
        "ok": bool(status and 200 <= status < 300),
        "provider": provider,
        "model": model,
        "status": status,
        "elapsedSec": round(elapsed, 2),
        "attempts": attempt_records,
        "parsed": parsed,
        "rawPreview": raw[:1200],
    }


def normalize_keyword_mode(config: dict[str, Any]) -> str:
    mode = str(config.get("mode") or "rules").strip().lower()
    aliases = {
        "llm": "llm-both",
        "task": "llm-task",
        "skills": "llm-skills",
        "skill": "llm-skills",
        "both": "llm-both",
    }
    mode = aliases.get(mode, mode)
    return mode if mode in KEYWORD_MODES else "rules"


def build_task_keyword_prompt(task_text: str) -> str:
    return (
        "你是 SkillsBench 轨迹分析器的关键词提取模块。请阅读 task.md，提取能代表任务目标、"
        "领域对象、关键文件类型、工具能力、验证重点的短关键词。关键词可以是中文或英文，"
        "每个关键词保持 2 到 6 个词以内，不要输出泛词如 task、file、create。\n"
        "请只输出 JSON：\n"
        "{\n"
        "  \"taskKeywords\": [\"...\"],\n"
        "  \"taskCapabilities\": [\"...\"]\n"
        "}\n\n"
        f"task.md:\n{task_text[:MAX_TEXT]}"
    )


def build_skill_keyword_prompt(skills: list[Skill]) -> str:
    payload = [
        {
            "name": skill.name,
            "description": skill.description,
            "path": skill.path,
            "excerpt": skill.body_excerpt[:MAX_KEYWORD_SKILL_EXCERPT],
        }
        for skill in skills
    ]
    return (
        "你是 SkillsBench 轨迹分析器的 skill 关键词提取模块。请为每个 skill 总结最能触发它、"
        "最能说明它能力边界的短关键词。关键词应覆盖领域名词、文件类型、工具/API、典型操作和验证点。"
        "不要输出泛词如 skill、task、file、create。\n"
        "请只输出 JSON：\n"
        "{\n"
        "  \"skillKeywords\": [\n"
        "    {\"name\": \"skill-name\", \"keywords\": [\"...\"]}\n"
        "  ]\n"
        "}\n\n"
        f"skills:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def parse_task_keyword_response(parsed: Any) -> list[str]:
    if not isinstance(parsed, dict):
        return []
    values: list[Any] = []
    for key in ("taskKeywords", "keywords", "taskCapabilities", "capabilities"):
        item = parsed.get(key)
        if isinstance(item, list):
            values.extend(item)
        elif item:
            values.append(item)
    return unique_keywords(values, limit=80)


def parse_skill_keyword_response(parsed: Any) -> dict[str, list[str]]:
    if not isinstance(parsed, dict):
        return {}
    raw = parsed.get("skillKeywords") or parsed.get("skills") or parsed.get("keywordsBySkill")
    out: dict[str, list[str]] = {}
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("skill") or "").strip()
            keywords = item.get("keywords") or item.get("skillKeywords") or []
            if name:
                out[name.lower()] = unique_keywords(keywords if isinstance(keywords, list) else [keywords], limit=60)
    elif isinstance(raw, dict):
        for name, keywords in raw.items():
            out[str(name).lower()] = unique_keywords(keywords if isinstance(keywords, list) else [keywords], limit=60)
    return {name: keywords for name, keywords in out.items() if keywords}


def apply_llm_keyword_extraction(
    task_text: str,
    skills: list[Skill],
    config: dict[str, Any],
) -> dict[str, Any]:
    mode = normalize_keyword_mode(config)
    enabled = bool(config.get("enabled", mode != "rules")) and mode != "rules"
    info: dict[str, Any] = {
        "enabled": enabled,
        "mode": mode,
        "source": "rules",
        "ok": True,
        "taskKeywords": [],
        "skillKeywords": [],
        "errors": [],
        "calls": [],
    }
    if not enabled:
        return info

    info["source"] = "rules+llm"
    include_task = mode in ("llm-task", "llm-both")
    include_skills = mode in ("llm-skills", "llm-both")

    if include_task:
        result = call_llm_json(build_task_keyword_prompt(task_text), config, 1000, "keyword extraction")
        info["calls"].append({
            "target": "task",
            "ok": result.get("ok", False),
            "status": result.get("status"),
            "elapsedSec": result.get("elapsedSec"),
            "model": result.get("model"),
        })
        if result.get("ok"):
            task_keywords = parse_task_keyword_response(result.get("parsed"))
            info["taskKeywords"] = task_keywords
        else:
            info["ok"] = False
            info["errors"].append(result.get("error") or result.get("rawPreview") or "Task keyword extraction failed")

    if include_skills and skills:
        result = call_llm_json(build_skill_keyword_prompt(skills), config, 1600, "keyword extraction")
        info["calls"].append({
            "target": "skills",
            "ok": result.get("ok", False),
            "status": result.get("status"),
            "elapsedSec": result.get("elapsedSec"),
            "model": result.get("model"),
        })
        if result.get("ok"):
            by_name = parse_skill_keyword_response(result.get("parsed"))
            for skill in skills:
                llm_keywords = by_name.get(skill.name.lower())
                if not llm_keywords:
                    continue
                skill.keywords = unique_keywords(skill.keywords + llm_keywords, limit=120)
                skill.keyword_source = "rules+llm"
                info["skillKeywords"].append({"name": skill.name, "keywords": llm_keywords})
        else:
            info["ok"] = False
            info["errors"].append(result.get("error") or result.get("rawPreview") or "Skill keyword extraction failed")

    if not info["taskKeywords"] and not info["skillKeywords"] and not info["errors"]:
        info["ok"] = False
        info["errors"].append("LLM keyword extraction returned no usable keywords")
    return info


def build_judge_prompt(analysis: dict[str, Any]) -> str:
    compact = {
        "task": analysis["task"],
        "outcome": analysis["outcome"],
        "stats": analysis["stats"],
        "skills": analysis["skills"],
        "timeline": analysis["timeline"],
        "events": analysis["events"][:MAX_JUDGE_EVENTS],
    }
    return (
        "你是 SkillsBench 轨迹审计 Judge。请基于 task、trajectory、result 和 skill 证据，"
        "判断 LLM 是否在合适时机使用了合适 skill，skill 使用是否正确，以及各 skill 对成功或失败的贡献。\n"
        "请只输出 JSON，格式如下：\n"
        "{\n"
        "  \"overall\": {\"verdict\": \"success|failure|mixed\", \"summary\": \"...\", \"confidence\": 0.0},\n"
        "  \"rootCauses\": [{\"label\":\"...\", \"evidence\":\"...\", \"severity\":\"low|medium|high\"}],\n"
        "  \"skillJudgments\": [{\"name\":\"...\", \"appropriateness\":\"good|late|missed|unnecessary|incorrect|unknown\", \"correctness\":\"...\", \"contribution\": -1.0, \"evidence\":\"...\"}],\n"
        "  \"recommendations\": [\"...\"]\n"
        "}\n\n"
        f"证据 JSON：\n{json.dumps(compact, ensure_ascii=False)}"
    )


def run_judge(analysis: dict[str, Any], judge: dict[str, Any]) -> dict[str, Any]:
    if not judge.get("enabled"):
        return {"enabled": False}
    api_key = judge.get("apiKey") or os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {"enabled": True, "ok": False, "error": "Missing Judge LLM API key"}

    provider = (judge.get("provider") or "anthropic").lower()
    base_url = judge.get("baseUrl") or "https://api.camel-hub.com"
    model = judge.get("model") or "deepseek-v4-flash"
    timeout = float(judge.get("timeout") or 120)
    max_tokens = int(judge.get("maxTokens") or 1800)
    prompt = build_judge_prompt(analysis)

    if provider == "openai":
        url = normalize_base_url(base_url, "/chat/completions")
        headers = {"content-type": "application/json", "authorization": f"Bearer {api_key}"}
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
        }
        status, raw, elapsed = post_json(url, headers, payload, timeout)
        data = extract_json_object(raw)
        if isinstance(data, dict) and data.get("choices"):
            content = data["choices"][0].get("message", {}).get("content", "")
            parsed = extract_json_object(content)
        else:
            parsed = data
    else:
        url = normalize_base_url(base_url, "/messages")
        headers = {
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": judge.get("anthropicVersion") or "2023-06-01",
        }
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }
        status, raw, elapsed = post_json(url, headers, payload, timeout)
        data = extract_json_object(raw)
        content = ""
        if isinstance(data, dict):
            for item in data.get("content") or []:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    content += item["text"]
        parsed = extract_json_object(content) or data

    return {
        "enabled": True,
        "ok": bool(status and 200 <= status < 300),
        "provider": provider,
        "model": model,
        "status": status,
        "elapsedSec": round(elapsed, 2),
        "parsed": parsed,
        "rawPreview": raw[:1200],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze SkillsBench trajectory skill use.")
    parser.add_argument("--task", help="Task directory, e.g. tasks/offer-letter-generator")
    parser.add_argument("--trajectory", help="ACP trajectory JSONL path")
    parser.add_argument("--result", help="Optional result.json path")
    parser.add_argument("--skills-dir", help="Optional skills directory")
    parser.add_argument("--json-input", action="store_true", help="Read request JSON from stdin")
    parser.add_argument("--judge", action="store_true", help="Enable Judge LLM in CLI mode")
    parser.add_argument("--judge-provider", default="anthropic", choices=["anthropic", "openai"])
    parser.add_argument("--judge-base-url", default=os.getenv("ANTHROPIC_BASE_URL") or "https://api.camel-hub.com")
    parser.add_argument("--judge-model", default=os.getenv("ANTHROPIC_MODEL") or "deepseek-v4-flash")
    parser.add_argument("--judge-api-key", default=None)
    parser.add_argument("--keyword-mode", default="rules", choices=sorted(KEYWORD_MODES), help="Keyword extraction mode")
    parser.add_argument("--keyword-provider", choices=["anthropic", "openai"], default=None)
    parser.add_argument("--keyword-base-url", default=None)
    parser.add_argument("--keyword-model", default=None)
    parser.add_argument("--keyword-api-key", default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.json_input:
        request = json.loads(sys.stdin.read() or "{}")
        task_path = request.get("taskPath")
        trajectory_path = request.get("trajectoryPath")
        result_path = request.get("resultPath")
        skills_dir = request.get("skillsDir")
        judge = request.get("judge") or {}
        keyword_config = request.get("keywordExtraction") or {}
        if keyword_config.get("reuseJudgeConfig"):
            keyword_enabled = keyword_config.get("enabled", normalize_keyword_mode(keyword_config) != "rules")
            keyword_config = {**judge, **keyword_config, "enabled": keyword_enabled}
    else:
        task_path = args.task
        trajectory_path = args.trajectory
        result_path = args.result
        skills_dir = args.skills_dir
        judge = {
            "enabled": args.judge,
            "provider": args.judge_provider,
            "baseUrl": args.judge_base_url,
            "model": args.judge_model,
            "apiKey": args.judge_api_key,
        }
        keyword_config = {
            "enabled": args.keyword_mode != "rules",
            "mode": args.keyword_mode,
            "provider": args.keyword_provider or args.judge_provider,
            "baseUrl": args.keyword_base_url or args.judge_base_url,
            "model": args.keyword_model or args.judge_model,
            "apiKey": args.keyword_api_key or args.judge_api_key,
        }

    trajectory = resolve_path(trajectory_path)
    if not trajectory or not trajectory.exists():
        raise SystemExit("Missing trajectory path")
    task = resolve_path(task_path)
    result = resolve_path(result_path)
    skills = resolve_path(skills_dir)

    analysis = analyze_rules(task, trajectory, result, skills, keyword_config)
    analysis["judge"] = run_judge(analysis, judge)
    print(json.dumps(analysis, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
