"""各个 repair stage 共享的 prompt 与 LLM 调用工具。

阶段脚本只关心“如何把上一阶段输出拼成当前阶段 prompt”。模板读取、占位符
替换、请求记录和系统提示词等横切逻辑统一放在这里，避免每个 stage 重复实现。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..llm_client import LLMClient

PROMPT_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "prompt_templates"
PROMPT_VARIABLE_DIR = PROMPT_TEMPLATE_DIR / "variables"


def prompt_template_path(name: str) -> Path:
    """返回某个外置 prompt 模板的绝对路径。"""
    return PROMPT_TEMPLATE_DIR / name


def read_prompt_template(name: str) -> str:
    """读取 prompt 模板文本，模板不存在时直接终止运行。

    这里使用 ``SystemExit`` 是为了让 CLI 用户看到明确的缺失文件路径，而不是
    更深层的 FileNotFoundError 栈。
    """
    path = prompt_template_path(name)
    if not path.exists():
        raise SystemExit(f"Missing prompt template: {path}")
    return path.read_text(encoding="utf-8")


def json_block(value: Any) -> str:
    """把 schema 或 payload 片段格式化成稳定、可读的 JSON 字符串。"""
    return json.dumps(value, ensure_ascii=False, indent=2)


def render_prompt_template(name: str, values: dict[str, Any]) -> str:
    """用简单的 ``{{name}}`` 占位符替换渲染 prompt 模板。

    模板本身保持纯文本，方便你直接编辑；Python 只负责注入 schema 等动态内容。
    如果 ``prompt_templates/variables/<name>.txt`` 存在，则优先使用这个可编辑
    override。这样 Web 调试页修改模板变量后，后续 prompt 生成会自动复用。
    """
    text = read_prompt_template(name)
    for key, value in values.items():
        override_path = PROMPT_VARIABLE_DIR / f"{key}.txt"
        replacement = override_path.read_text(encoding="utf-8") if override_path.exists() else str(value)
        text = text.replace(f"{{{{{key}}}}}", replacement)
    return text


def stage_system_prompt(stage: str) -> str:
    """根据可编辑模板生成每个阶段共用的 system prompt。

    ``system-prompt.txt`` 位于 ``offline_skill_rca/prompt_templates``，可由 Web
    调试页直接修改并保存。这样 system prompt 和各阶段 user prompt 一样，都能
    被审阅、调试和复用。
    """
    template = read_prompt_template("system-prompt.txt")
    return template.replace("{{stage_name}}", stage).strip()


def make_llm(config: Any, transcript_name: str, llm_role: str = "repair") -> LLMClient:
    """根据运行配置创建 LLMClient，并绑定当前阶段的 transcript 名称。

    只有 Stage 7 review 会传入 ``llm_role="review"``。若没有启用独立审查
    LLM，则仍回退到 strong_*，保证旧配置和旧运行目录可以直接续跑。
    """
    is_stage3_trace = transcript_name.startswith("stage-03-traj-")
    timeout_sec = 600.0 if is_stage3_trace else 1800.0
    max_retries = 1 if is_stage3_trace else 3
    separate_review = llm_role == "review" and bool(getattr(config, "use_separate_review_llm", False))
    base_url = (
        getattr(config, "review_base_url", "") or config.strong_base_url
        if separate_review
        else config.strong_base_url
    )
    api_key = (
        getattr(config, "review_api_key", "") or config.strong_api_key
        if separate_review
        else config.strong_api_key
    )
    model = (
        getattr(config, "review_model", "") or config.strong_model
        if separate_review
        else config.strong_model
    )
    reasoning_effort = (
        getattr(config, "review_reasoning_effort", "") or getattr(config, "strong_reasoning_effort", "")
        if separate_review
        else getattr(config, "strong_reasoning_effort", "")
    )
    return LLMClient(
        base_url,
        api_key,
        model,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        timeout_env_key="OFFLINE_SKILL_RCA_STAGE3_TIMEOUT_SEC" if is_stage3_trace else None,
        max_retries_env_key="OFFLINE_SKILL_RCA_STAGE3_MAX_RETRIES" if is_stage3_trace else None,
        transcript_dir=config.output_dir / "llm_transcript",
        transcript_name=transcript_name,
        reasoning_effort=reasoning_effort,
    )


def write_prompt_file(config: Any, name: str, prompt: str) -> None:
    """把实际发送给 repair LLM 的 prompt 落盘，便于之后诊断和可视化。"""
    from ..pipeline import sanitize

    prompts_dir = config.output_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / f"{sanitize(name)}.prompt.txt").write_text(prompt, encoding="utf-8")


def run_llm_stage(
    config: Any,
    name: str,
    prompt: str,
    max_tokens: int | None = None,
    llm_role: str = "repair",
) -> dict[str, Any]:
    """执行一个标准的单轮 stage：写 prompt、调用 LLM、返回 JSON。"""
    write_prompt_file(config, name, prompt)
    token_budget = max_tokens
    if token_budget is None:
        token_budget = int(os.getenv("OFFLINE_SKILL_RCA_MAX_TOKENS") or 24_000)
    return make_llm(config, name, llm_role=llm_role).chat_json(
        stage_system_prompt(name), prompt, max_tokens=token_budget
    )


def shorten(value: Any, limit: int) -> str:
    """把任意值转成短文本，供跨阶段 prompt 压缩使用。"""
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


def compact_trace_analyses(stage3: list[dict[str, Any]], text_limit: int = 700) -> list[dict[str, Any]]:
    """压缩 Stage 3 的逐轨迹分析结果。

    Stage 3 每条轨迹的 response 往往很长；Stage 5 只需要可归因的结构化证据，
    所以这里保留 bad_events、capability_node_status、Skill usage 和 candidate gaps。
    """
    compacted = []
    for item in stage3:
        bad_events = []
        for event in events_from_trace_analysis(item):
            if not isinstance(event, dict):
                continue
            bad_events.append(
                {
                    "event_id": event.get("event_id"),
                    "step_id": event.get("step_id"),
                    "intent": shorten(event.get("intent"), text_limit),
                    "observed_behavior": shorten(event.get("observed") or event.get("observed_behavior"), text_limit),
                    "expected_behavior_from_task_and_skills": shorten(
                        event.get("expected") or event.get("expected_behavior_from_task_and_skills"), text_limit
                    ),
                    "downstream_consequence": shorten(event.get("consequence") or event.get("downstream_consequence"), text_limit),
                    "suspected_skill_ids": event.get("suspected_skill_ids") or [],
                    "severity": event.get("severity"),
                    "first_actionable_fault_candidate": event.get("first_actionable") if "first_actionable" in event else event.get("first_actionable_fault_candidate"),
                    "evidence_span": shorten(event.get("evidence") or event.get("evidence_span"), text_limit),
                }
            )
        skill_usage = []
        for usage in item.get("skill_usage_observations") or []:
            if isinstance(usage, dict):
                skill_usage.append(
                    {
                        "skill_id": usage.get("skill_id"),
                        "used_or_ignored_or_misused": usage.get("used_or_ignored_or_misused"),
                        "evidence": shorten(usage.get("evidence"), text_limit),
                    }
                )
        candidate_gaps = []
        for gap in item.get("candidate_skill_gaps") or []:
            if isinstance(gap, dict):
                candidate_gaps.append(
                    {
                        "gap_type": gap.get("gap_type"),
                        "target_skill_ids": gap.get("target_skill_ids") or [],
                        "description": shorten(gap.get("description"), text_limit),
                        "supporting_events": gap.get("supporting_events") or [],
                        "confidence": gap.get("confidence"),
                    }
                )
        non_skill_signals = [
            shorten(signal, text_limit) if not isinstance(signal, dict) else {key: shorten(value, text_limit) for key, value in signal.items()}
            for signal in (item.get("non_skill_signals") or [])
        ]
        compacted.append(
            {
                "traj_id": item.get("traj_id"),
                "success": item.get("success", 0),
                "trajectory_summary": shorten(item.get("trajectory_summary"), text_limit),
                "bad_events": bad_events,
                "capability_node_status": compact_capability_node_status(
                    item.get("node_status") or item.get("capability_node_status") or item.get("DAG_node_status") or {}, text_limit
                ),
                "skill_usage_observations": skill_usage,
                "candidate_skill_gaps": candidate_gaps,
                "non_skill_signals": non_skill_signals,
                "evidence_limits": shorten(item.get("evidence_limits"), text_limit),
            }
        )
    return compacted


def compact_failure_events_only(stage3: list[dict[str, Any]], text_limit: int = 500) -> list[dict[str, Any]]:
    """兼容旧调用：保留最小 bad event 视图。"""
    compacted = []
    for item in stage3:
        events = []
        for event in events_from_trace_analysis(item):
            if not isinstance(event, dict):
                continue
            events.append(
                {
                    "event_id": event.get("event_id"),
                    "traj_id": item.get("traj_id"),
                    "step_id": event.get("step_id"),
                    "intent": shorten(event.get("intent"), text_limit),
                    "observed_behavior": shorten(event.get("observed") or event.get("observed_behavior"), text_limit),
                    "expected_behavior": shorten(event.get("expected") or event.get("expected_behavior_from_task_and_skills"), text_limit),
                    "downstream_consequence": shorten(event.get("consequence") or event.get("downstream_consequence"), text_limit),
                    "suspected_skill_ids": event.get("suspected_skill_ids") or [],
                    "severity": event.get("severity"),
                    "first_actionable_fault_candidate": event.get("first_actionable") if "first_actionable" in event else event.get("first_actionable_fault_candidate"),
                    "evidence_span": shorten(event.get("evidence") or event.get("evidence_span"), text_limit),
                }
            )
        compacted.append(
            {
                "traj_id": item.get("traj_id"),
                "success": item.get("success", 0),
                "trajectory_summary": shorten(item.get("trajectory_summary"), text_limit),
                "bad_events": events,
                "capability_node_status": compact_capability_node_status(
                    item.get("node_status") or item.get("capability_node_status") or item.get("DAG_node_status") or {}, text_limit
                ),
            }
        )
    return compacted


def events_from_trace_analysis(item: dict[str, Any]) -> list[Any]:
    """读取 Stage 3 的 bad_events，并兼容旧输出里的 failure_events。"""
    events = item.get("bad_events")
    if events is None:
        events = item.get("failure_events")
    return events or []


def compact_capability_node_status(value: dict[str, Any], text_limit: int) -> dict[str, Any]:
    """压缩 capability_node_status 中的 reason 字段，保留节点状态。"""
    out: dict[str, Any] = {}
    if not isinstance(value, dict):
        return out
    for node_id, status in value.items():
        if isinstance(status, dict):
            out[str(node_id)] = {
                "status": status.get("status"),
                "reason": shorten(status.get("reason"), text_limit),
            }
        else:
            out[str(node_id)] = status
    return out
