#!/usr/bin/env python3
"""LLM-assisted SkillsBench skill-library repair pipeline.

The pipeline turns task + skills + completed jobs into a new repaired skills
library variant. It intentionally writes only under ``skill-libraries/`` and a
separate repair report directory; source task files are treated as read-only
evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from summarize_job_evidence import task_artifacts as raw_task_artifacts
from trajectory_skill_analyzer import analyze_rules, call_llm_json


ROOT = Path(__file__).resolve().parents[1]
MAX_TASK_CHARS = 16_000
MAX_SKILL_CHARS = 14_000
MAX_PROMPT_CHARS = 170_000
DEFAULT_STRONG_MODEL = "gpt-5.5"
DEFAULT_STRONG_BASE_URL = "https://api.camel-hub.com"
DEFAULT_WEAK_MODEL = "deepseek-v4-flash"
STAGE_PROMPT_CHARS = 120_000
STAGE_TASK_CHARS = 10_000
MAX_PUBLIC_CONTEXT_CHARS = 28_000
MAX_PUBLIC_FILE_CHARS = 12_000

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


@dataclass
class RolloutInput:
    artifact_dir: Path
    rollout_dir: Path
    trajectory_path: Path
    result_path: Path | None
    summary_path: Path | None


def now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def sanitize_segment(value: str, fallback: str = "repair") -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    text = re.sub(r"-+", "-", text).strip("-._")
    return (text or fallback)[:96]


def safe_rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except Exception:
        return str(path)


def resolve_root_path(value: str | Path | None, *, must_exist: bool = False) -> Path | None:
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
    if must_exist and not path.exists():
        raise SystemExit(f"Path does not exist: {safe_rel(path)}")
    return path


def read_text(path: Path, limit: int | None = None) -> str:
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


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def is_summary_file(path: Path) -> bool:
    return path.is_file() and path.name == "summary.json"


def looks_like_artifact_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    return (path / "summary.json").exists() or any(
        child.is_dir() and "__" in child.name for child in path.iterdir()
    )


def find_rollout_dirs(artifact_dir: Path) -> list[Path]:
    if not artifact_dir.exists() or not artifact_dir.is_dir():
        return []
    out = []
    seen: set[str] = set()
    for trajectory in sorted(artifact_dir.rglob("trajectory/acp_trajectory.jsonl")):
        rollout = trajectory.parent.parent
        key = str(rollout.resolve())
        if key in seen:
            continue
        seen.add(key)
        out.append(rollout)
    return out


def rollout_from_dir(path: Path) -> RolloutInput | None:
    trajectory = path / "trajectory" / "acp_trajectory.jsonl"
    result = path / "result.json"
    if not trajectory.exists():
        return None
    artifact = path.parent
    return RolloutInput(
        artifact_dir=artifact,
        rollout_dir=path,
        trajectory_path=trajectory,
        result_path=result if result.exists() else None,
        summary_path=(artifact / "summary.json") if (artifact / "summary.json").exists() else None,
    )


def discover_rollouts(paths: list[Path], max_rollouts: int) -> list[RolloutInput]:
    found: list[RolloutInput] = []
    seen: set[str] = set()
    candidate_cap = max(max_rollouts * 8, max_rollouts)

    def add(rollout: RolloutInput | None) -> None:
        if not rollout:
            return
        key = safe_rel(rollout.rollout_dir)
        if key in seen:
            return
        seen.add(key)
        found.append(rollout)

    for raw in paths:
        path = raw.resolve()
        if is_summary_file(path):
            parent = path.parent
            for rollout_dir in find_rollout_dirs(parent):
                add(rollout_from_dir(rollout_dir))
        elif rollout_from_dir(path):
            add(rollout_from_dir(path))
        elif looks_like_artifact_dir(path):
            for rollout_dir in find_rollout_dirs(path):
                add(rollout_from_dir(rollout_dir))
        elif path.is_dir():
            for trajectory in sorted(path.rglob("trajectory/acp_trajectory.jsonl")):
                add(rollout_from_dir(trajectory.parent.parent))
                if len(found) >= candidate_cap:
                    break
        else:
            raise SystemExit(f"Unsupported job path: {safe_rel(path)}")

        if len(found) >= candidate_cap:
            break

    return select_rollouts(found, max_rollouts)


def rollout_passed(rollout: RolloutInput) -> bool:
    result = read_json(rollout.result_path)
    if not isinstance(result, dict):
        return False
    rewards = result.get("rewards") or {}
    reward = rewards.get("reward")
    if isinstance(reward, (int, float)):
        return reward >= 1.0
    score = result.get("score") or result.get("score_excl_errors")
    return str(score).strip() in {"1", "1.0", "100.0%", "100%"}


def select_rollouts(found: list[RolloutInput], max_rollouts: int) -> list[RolloutInput]:
    if len(found) <= max_rollouts:
        return found
    passed = [rollout for rollout in found if rollout_passed(rollout)]
    failed = [rollout for rollout in found if not rollout_passed(rollout)]
    selected = failed[:max_rollouts]
    if passed and not any(rollout_passed(rollout) for rollout in selected):
        if len(selected) >= max_rollouts:
            selected[-1] = passed[0]
        else:
            selected.append(passed[0])
    for rollout in passed:
        if len(selected) >= max_rollouts:
            break
        if rollout not in selected:
            selected.append(rollout)
    return selected[:max_rollouts]


def find_default_source_skills(task_dir: Path) -> Path:
    task_name = task_dir.name
    library_initial = ROOT / "skill-libraries" / task_name / "initial"
    if library_initial.exists():
        return library_initial
    bundled = task_dir / "environment" / "skills"
    if bundled.exists():
        return bundled
    raise SystemExit(f"No source skills found for {safe_rel(task_dir)}")


def list_skills(skills_dir: Path) -> list[dict[str, Any]]:
    skills = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        text = read_text(skill_md, MAX_SKILL_CHARS)
        name_match = re.search(r"^name:\s*(.+)$", text, flags=re.M)
        desc_match = re.search(r"^description:\s*(.+)$", text, flags=re.M)
        skills.append({
            "dir": skill_md.parent.name,
            "name": (name_match.group(1).strip().strip("\"'") if name_match else skill_md.parent.name),
            "description": (desc_match.group(1).strip().strip("\"'") if desc_match else ""),
            "path": safe_rel(skill_md),
            "content": text,
        })
    return skills


def summarize_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    skills = []
    for row in analysis.get("skills") or []:
        if row.get("expected") or row.get("invoked") or row.get("contribution", 0) < 0:
            skills.append({
                "name": row.get("name"),
                "expected": row.get("expected"),
                "invoked": row.get("invoked"),
                "correctness": row.get("correctness"),
                "contribution": row.get("contribution"),
                "matchedKeywords": row.get("matchedKeywords"),
                "assessment": row.get("assessment"),
            })
    judge = analysis.get("judge") or {}
    return {
        "inputs": analysis.get("inputs"),
        "outcome": analysis.get("outcome"),
        "stats": analysis.get("stats"),
        "reasons": analysis.get("reasons"),
        "skills": skills[:20],
        "timeline": (analysis.get("timeline") or [])[:30],
        "judge": {
            "enabled": judge.get("enabled", False),
            "ok": judge.get("ok"),
            "parsed": judge.get("parsed"),
        } if judge.get("enabled") else {"enabled": False},
    }


def run_rollout_analyses(
    task_dir: Path,
    source_skills_dir: Path,
    rollouts: list[RolloutInput],
    keyword_config: dict[str, Any],
    judge_config: dict[str, Any],
) -> list[dict[str, Any]]:
    analyses = []
    for rollout in rollouts:
        analysis = analyze_rules(
            task_dir,
            rollout.trajectory_path,
            rollout.result_path,
            source_skills_dir,
            keyword_config,
        )
        if judge_config.get("enabled"):
            from trajectory_skill_analyzer import run_judge

            analysis["judge"] = run_judge(analysis, judge_config)
        else:
            analysis["judge"] = {"enabled": False}
        analyses.append(analysis)
    return analyses


def compact_json(data: Any, limit: int = STAGE_PROMPT_CHARS) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if len(text) > limit:
        return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
    return text


def stage_id(label: str) -> str:
    return sanitize_segment(label, "stage").lower()


def stage_type(label: str) -> str:
    if label == "00-global-diagnosis":
        return "00-global-diagnosis"
    if label.startswith("10-skill-analysis-"):
        return "10-skill-analysis"
    if label.startswith("20-skill-repair-"):
        return "20-skill-repair"
    if label.startswith("30-new-skill-"):
        return "30-new-skill"
    if label == "90-integration-audit":
        return "90-integration-audit"
    return "unknown"


def stage_skill_dir(label: str) -> str:
    for prefix in ["10-skill-analysis-", "20-skill-repair-", "30-new-skill-"]:
        if label.startswith(prefix):
            return label[len(prefix) :]
    return ""


def stage_depends_on(task_name: str, label: str, skills: list[dict[str, Any]]) -> list[str]:
    kind = stage_type(label)
    skill_dir = stage_skill_dir(label)
    if kind == "10-skill-analysis":
        return [f"{task_name}:00-global-diagnosis"]
    if kind == "20-skill-repair":
        return [f"{task_name}:{stage_id(f'10-skill-analysis-{skill_dir}')}"]
    if kind == "30-new-skill":
        return [f"{task_name}:00-global-diagnosis"] + [
            f"{task_name}:{stage_id(f'10-skill-analysis-{str(skill.get('dir') or '')}')}"
            for skill in skills
        ]
    if kind == "90-integration-audit":
        return [
            f"{task_name}:{stage_id(f'20-skill-repair-{str(skill.get('dir') or '')}')}"
            for skill in skills
        ]
    return []


def stage_trace_record(
    *,
    task_name: str,
    label: str,
    skills: list[dict[str, Any]],
    prompt_path: Path | None = None,
    response_path: Path | None = None,
    parsed_path: Path | None = None,
    prompt_chars: int = 0,
    result: dict[str, Any] | None = None,
    parsed: dict[str, Any] | None = None,
    mode: str = "runtime",
    note: str = "",
) -> dict[str, Any]:
    kind = stage_type(label)
    skill_dir = stage_skill_dir(label)
    row = {
        "conversationId": f"{task_name}:{stage_id(label)}",
        "stage": kind,
        "label": label,
        "skillDir": skill_dir,
        "dependsOn": stage_depends_on(task_name, label, skills),
        "mode": mode,
        "llmCalled": bool(result),
        "ok": ((result or {}).get("ok") if result else None),
        "status": ((result or {}).get("status") if result else "not-called"),
        "elapsedSec": ((result or {}).get("elapsedSec") if result else None),
        "parsed": bool(parsed),
        "promptPath": safe_rel(prompt_path) if prompt_path else "",
        "responsePath": safe_rel(response_path) if response_path else "",
        "parsedPath": safe_rel(parsed_path) if parsed_path else "",
        "promptChars": prompt_chars,
        "note": note,
    }
    return row


def write_stage_trace(report_dir: Path, task_name: str, entries: list[dict[str, Any]], mode: str) -> Path:
    path = report_dir / "stage_trace.json"
    write_json(
        path,
        {
            "schemaVersion": 1,
            "task": task_name,
            "mode": mode,
            "generatedAt": datetime.now(UTC).isoformat(),
            "stageCount": len(entries),
            "entries": entries,
        },
    )
    return path


def stage_llm_call(
    report_dir: Path,
    label: str,
    prompt: str,
    llm_config: dict[str, Any],
    max_tokens: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Path | None]]:
    stages_dir = report_dir / "stages"
    stages_dir.mkdir(parents=True, exist_ok=True)
    sid = stage_id(label)
    prompt_path = stages_dir / f"{sid}.prompt.txt"
    response_path = stages_dir / f"{sid}.response.json"
    parsed_path = stages_dir / f"{sid}.parsed.json"
    cached_result = read_json(response_path)
    cached_parsed = read_json(parsed_path)
    if (
        isinstance(cached_result, dict)
        and cached_result.get("ok") is True
        and isinstance(cached_parsed, dict)
        and prompt_path.exists()
        and prompt_path.read_text(encoding="utf-8", errors="replace") == prompt
    ):
        cached_result = dict(cached_result)
        cached_result["cached"] = True
        return cached_result, cached_parsed, {
            "promptPath": prompt_path,
            "responsePath": response_path,
            "parsedPath": parsed_path,
        }

    prompt_path.write_text(prompt, encoding="utf-8")
    result = call_llm_json(prompt, llm_config, max_tokens, "strong repair model")
    write_json(response_path, result)
    parsed = result.get("parsed")
    if isinstance(parsed, dict):
        write_json(parsed_path, parsed)
    else:
        parsed_path = None
    return result, parsed if isinstance(parsed, dict) else {}, {
        "promptPath": prompt_path,
        "responsePath": response_path,
        "parsedPath": parsed_path,
    }


def prompt_trace_entry(
    *,
    task_name: str,
    label: str,
    skills: list[dict[str, Any]],
    path: Path,
    prompt: str,
    note: str,
) -> dict[str, Any]:
    return stage_trace_record(
        task_name=task_name,
        label=label,
        skills=skills,
        prompt_path=path,
        prompt_chars=len(prompt),
        mode="prepare-prompts-only",
        note=note,
    )


def prepare_prompt_bundle(
    task_dir: Path,
    source_skills_dir: Path,
    output_variant: str,
    report_dir: Path,
    rollouts: list[RolloutInput],
    analyses: list[dict[str, Any]],
    weak_model: str,
) -> dict[str, Any]:
    stages_dir = report_dir / "stages"
    stages_dir.mkdir(parents=True, exist_ok=True)
    skills = list_skills(source_skills_dir)
    prompts = []
    trace_entries: list[dict[str, Any]] = []
    task_name = task_dir.name

    def write_prompt(label: str, prompt: str, note: str) -> None:
        path = stages_dir / f"{stage_id(label)}.prompt.txt"
        path.write_text(prompt, encoding="utf-8")
        trace = prompt_trace_entry(
            task_name=task_name,
            label=label,
            skills=skills,
            path=path,
            prompt=prompt,
            note=note,
        )
        prompts.append({
            "label": label,
            "conversationId": trace["conversationId"],
            "stage": trace["stage"],
            "skillDir": trace["skillDir"],
            "dependsOn": trace["dependsOn"],
            "path": safe_rel(path),
            "chars": len(prompt),
            "note": note,
        })
        trace_entries.append(trace)

    global_prompt = build_global_diagnosis_prompt(
        task_dir,
        source_skills_dir,
        output_variant,
        rollouts,
        analyses,
        weak_model,
    )
    write_prompt("00-global-diagnosis", global_prompt, "Ready-to-run stage 0 prompt.")

    synthetic_global = {
        "prepareOnly": True,
        "taskDiagnosis": {
            "summary": "Prompt preparation only: stage 0 was materialized but not sent to an LLM.",
            "baseline": "Use the stage 0 prompt and rule analysis evidence as the authoritative preflight context.",
            "successPatterns": [],
            "failurePatterns": [],
            "nonRepairableIssues": [],
            "riskControls": ["Do not copy fixed-answer values from trajectories or verifier assertions into skills."],
        },
        "skillPlan": [
            {
                "skillDir": skill.get("dir"),
                "skillName": skill.get("name"),
                "role": "prepare-only",
                "needsDedicatedRepair": True,
                "problems": [],
                "repairIntent": "Materialized for prompt/context inspection; actual role is decided by stage 0 LLM output.",
            }
            for skill in skills
        ],
        "newSkillRequests": [],
    }
    for skill in skills:
        label_base = str(skill.get("dir") or "skill")
        analysis_prompt = build_skill_analysis_prompt(task_dir, skill, synthetic_global, rollouts, analyses)
        write_prompt(
            f"10-skill-analysis-{label_base}",
            analysis_prompt,
            "Prepare-only stage 1 prompt with synthetic global diagnosis placeholder.",
        )
        skill_analysis_stub = {
            "skillDir": skill.get("dir"),
            "skillName": skill.get("name"),
            "decision": "prepare-only",
            "issues": [],
            "repairSpec": {
                "frontmatterChanges": [],
                "bodyChanges": ["Actual repair prompt should use the parsed stage 1 result generated at runtime."],
                "validationGates": [],
                "handoffs": [],
            },
            "oracleLeakRisks": ["Prepare-only stub; inspect prompt context for accidental answer leakage before running LLM repair."],
        }
        repair_prompt = build_skill_repair_prompt(task_dir, skill, synthetic_global, skill_analysis_stub)
        write_prompt(
            f"20-skill-repair-{label_base}",
            repair_prompt,
            "Prepare-only stage 2 template; runtime stage 2 will use the real stage 1 LLM analysis.",
        )

    bundle = {
        "mode": "prepare-prompts-only",
        "task": safe_rel(task_dir),
        "sourceSkillsDir": safe_rel(source_skills_dir),
        "variant": output_variant,
        "reportDir": safe_rel(report_dir),
        "rollouts": [safe_rel(item.rollout_dir) for item in rollouts],
        "promptCount": len(prompts),
        "stageTrace": safe_rel(write_stage_trace(report_dir, task_name, trace_entries, "prepare-prompts-only")),
        "prompts": prompts,
    }
    write_json(report_dir / "prompt_bundle.json", bundle)
    return bundle


def require_stage(label: str, result: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or result.get("rawPreview") or f"{label} LLM call failed")
    if not isinstance(parsed, dict) or not parsed:
        raise RuntimeError(result.get("rawPreview") or f"{label} did not return a JSON object")
    return parsed


def skill_catalog(skills: list[dict[str, Any]], *, include_content: bool = False) -> list[dict[str, Any]]:
    rows = []
    for skill in skills:
        row = {
            "dir": skill.get("dir"),
            "name": skill.get("name"),
            "description": skill.get("description"),
            "path": skill.get("path"),
        }
        if include_content:
            row["content"] = skill.get("content")
        rows.append(row)
    return rows


def normalize_repair_relative_path(path_text: str) -> str:
    """Normalize model-emitted repair paths to output skills-root paths."""
    rel = str(path_text or "").strip().replace("\\", "/")
    rel = re.sub(r"^/+", "", rel)
    rel = re.sub(r"^\./+", "", rel)
    if "/environment/skills/" in rel:
        rel = rel.split("/environment/skills/", 1)[1]
    elif "/skills/" in rel and rel.startswith("tasks/"):
        rel = rel.split("/skills/", 1)[1]
    if rel.startswith("skills/"):
        rel = rel[len("skills/") :]
    return rel


def public_environment_context(task_dir: Path) -> list[dict[str, str]]:
    """Return bounded public task-environment files, excluding the skills library.

    This gives repair stages the public simulator/config semantics without
    reading oracle/verifier files or mixing in a repaired skills variant.
    """
    environment_dir = task_dir / "environment"
    if not environment_dir.exists():
        return []

    allowed_suffixes = {".py", ".json", ".md", ".txt", ".toml", ".yaml", ".yml"}
    rows: list[dict[str, str]] = []
    used = 0
    for path in sorted(environment_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in allowed_suffixes:
            continue
        try:
            path.relative_to(environment_dir / "skills")
            continue
        except ValueError:
            pass
        remaining = MAX_PUBLIC_CONTEXT_CHARS - used
        if remaining <= 0:
            break
        limit = min(MAX_PUBLIC_FILE_CHARS, remaining)
        content = read_text(path, limit)
        used += len(content)
        rows.append({
            "path": safe_rel(path),
            "content": content,
            "truncated": len(content) >= limit,
        })
    return rows


def repair_safe_task_artifacts(task_name: str, rollout: RolloutInput) -> dict[str, Any]:
    """Return verifier/job artifacts safe to include in repair prompts.

    The full evidence digests may contain verifier expected values or successful
    final outputs. Repair prompts should expose failure shape and public-output
    consistency issues, not benchmark answers.
    """
    if not rollout.result_path:
        return {}
    raw = raw_task_artifacts(task_name, rollout.result_path)
    if task_name == "exoplanet-detection-period":
        mismatches = []
        for item in raw.get("assertionFindings") or []:
            if item.get("kind") == "period_mismatch":
                mismatches.append(
                    {
                        "kind": "period_mismatch",
                        "actualOutputWasNumeric": isinstance(item.get("actual"), (int, float)),
                        "verifierRejectedValue": True,
                        "fixedExpectedValueOmitted": True,
                    }
                )
        return {
            "periodFileCaptured": bool(raw.get("periodFiles")),
            "periodMentionCount": len(raw.get("periodMentions") or []),
            "verifierFindings": mismatches,
            "policy": "Numeric expected periods and successful final period values are omitted from repair prompts.",
        }
    if task_name == "hvac-control":
        return {
            "files": raw.get("files") or [],
            "metricChecks": raw.get("metricChecks") or {},
            "assertionFindings": raw.get("assertionFindings") or [],
        }
    return raw


def rollout_evidence(task_name: str, rollouts: list[RolloutInput], analyses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for rollout, analysis in zip(rollouts, analyses):
        item = summarize_analysis(analysis)
        rows.append({
            "rolloutPath": safe_rel(rollout.rollout_dir),
            "artifactDir": safe_rel(rollout.artifact_dir),
            "summaryPath": safe_rel(rollout.summary_path) if rollout.summary_path else "",
            "analysis": item,
            "taskArtifacts": repair_safe_task_artifacts(task_name, rollout),
        })
    return rows


def skill_focus_evidence(
    task_name: str,
    skill: dict[str, Any],
    rollouts: list[RolloutInput],
    analyses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    names = {str(skill.get("name") or ""), str(skill.get("dir") or "")}
    focused = []
    for rollout, analysis in zip(rollouts, analyses):
        matched_rows = []
        for row in analysis.get("skills") or []:
            if str(row.get("name") or "") in names:
                matched_rows.append({
                    "name": row.get("name"),
                    "expected": row.get("expected"),
                    "invoked": row.get("invoked"),
                    "correctness": row.get("correctness"),
                    "contribution": row.get("contribution"),
                    "matchedKeywords": row.get("matchedKeywords"),
                    "assessment": row.get("assessment"),
                })
        summary = summarize_analysis(analysis)
        focused.append({
            "rolloutPath": safe_rel(rollout.rollout_dir),
            "outcome": summary.get("outcome"),
            "stats": summary.get("stats"),
            "reasons": summary.get("reasons"),
            "skillRows": matched_rows,
            "taskArtifacts": repair_safe_task_artifacts(task_name, rollout),
            "timeline": (summary.get("timeline") or [])[:12],
        })
    return focused


def build_global_diagnosis_prompt(
    task_dir: Path,
    source_skills_dir: Path,
    output_variant: str,
    rollouts: list[RolloutInput],
    analyses: list[dict[str, Any]],
    weak_model: str,
) -> str:
    task_text = read_text(task_dir / "task.md", STAGE_TASK_CHARS)
    skills = list_skills(source_skills_dir)
    outcomes = [a.get("outcome") or {} for a in analyses]
    passed = sum(1 for outcome in outcomes if outcome.get("passed"))
    compact = {
        "task": {
            "name": task_dir.name,
            "path": safe_rel(task_dir),
            "taskMd": task_text,
            "publicEnvironment": public_environment_context(task_dir),
        },
        "sourceSkills": {
            "path": safe_rel(source_skills_dir),
            "skills": skill_catalog(skills),
        },
        "runs": rollout_evidence(task_dir.name, rollouts, analyses),
        "requestedOutput": {
            "variant": output_variant,
            "weakModel": weak_model,
            "observed": {"passed": passed, "failed": len(outcomes) - passed, "total": len(outcomes)},
        },
    }
    return (
        "阶段 0：全局诊断。你是 SkillsBench 多阶段技能修复流水线的诊断器。\n"
        "只做诊断和分工，不要输出修复后的 SKILL.md 完整内容。\n\n"
        "硬性约束：\n"
        "- 只能建议修复 skills 库文件；不得建议修改 task.md、verifier、Dockerfile、测试或数据。\n"
        "- 不得泄漏 oracle、固定答案、成功 run 的最终输出、固定路线、固定矩阵或隐藏 benchmark 数值。\n"
        "- 目标弱模型是 deepseek-v4-flash；诊断要转化为短、硬、可执行的后续修复任务。\n\n"
        "上下文使用要求：\n"
        "- 必须使用 publicEnvironment 中的公开 simulator/config 语义来诊断流程缺口，例如积分方式、状态顺序、文件格式和安全约束。\n"
        "- 必须使用 runs[].taskArtifacts 中的 repair-safe 失败形态和公开输出一致性证据；这些证据已省略固定答案类数值，不能反推出或写入 benchmark 答案。\n"
        "- 如果公开环境代码定义了一步更新/离散化语义，后续 skill 必须要求提交产物与该语义一致；不要凭默认控制理论习惯改成另一种离散化。\n"
        "- 如果端到端成功依赖多个 skill 的固定顺序、交接或验证门，请在 newSkillRequests 中提出 planner/runner/validator 类 skill，不要把编排隐含在子 skill 里。\n"
        "- 对需要多个输出文件和仿真闭环的任务，除非现有 skill 已经是明确入口，否则应请求一个 planner/entrypoint skill；helper/validator skill 不能替代入口编排。\n\n"
        "请返回 JSON 对象：\n"
        "{\n"
        "  \"taskDiagnosis\": {\n"
        "    \"summary\": \"...\",\n"
        "    \"baseline\": \"...\",\n"
        "    \"successPatterns\": [\"...\"],\n"
        "    \"failurePatterns\": [\"...\"],\n"
        "    \"nonRepairableIssues\": [\"infra/timeout/etc if any\"],\n"
        "    \"riskControls\": [\"...\"]\n"
        "  },\n"
        "  \"skillPlan\": [\n"
        "    {\"skillDir\":\"existing-skill-dir\", \"skillName\":\"...\", \"role\":\"repair|keep|route-to-planner|deprioritize\", \"needsDedicatedRepair\": true, \"problems\":[\"...\"], \"repairIntent\":\"...\"}\n"
        "  ],\n"
        "  \"newSkillRequests\": [\n"
        "    {\"skillDir\":\"new-skill-dir\", \"skillName\":\"...\", \"purpose\":\"...\", \"repairIntent\":\"...\", \"coordinatesWith\":[\"skill-dir\"]}\n"
        "  ]\n"
        "}\n\n"
        "证据 JSON：\n"
        f"{compact_json(compact)}"
    )


def build_skill_analysis_prompt(
    task_dir: Path,
    skill: dict[str, Any],
    global_diagnosis: dict[str, Any],
    rollouts: list[RolloutInput],
    analyses: list[dict[str, Any]],
) -> str:
    compact = {
        "task": {
            "name": task_dir.name,
            "taskMd": read_text(task_dir / "task.md", 8000),
            "publicEnvironment": public_environment_context(task_dir),
        },
        "globalDiagnosis": global_diagnosis,
        "skill": skill,
        "focusedEvidence": skill_focus_evidence(task_dir.name, skill, rollouts, analyses),
    }
    return (
        f"阶段 1：单 skill 分析。当前 skill 是 `{skill.get('dir')}` / `{skill.get('name')}`。\n"
        "请只分析这一个 skill，不要修其它 skill，不要输出完整文件内容。\n\n"
        "你要判断：它是否应当修改、如何改、它与 planner/runner/其它子 skill 的边界是什么。\n"
        "必须基于证据，避免把成功 run 的具体答案写进后续修复建议。\n\n"
        "必须显式检查 publicEnvironment 中与当前 skill 相关的公开语义；例如 simulator 的积分方式、状态/输入顺序、参考轨迹、日志格式。\n"
        "必须显式使用 focusedEvidence[].taskArtifacts 中的 repair-safe 失败形态；如果该 evidence 只说明 verifier 拒绝了固定答案类输出，不得把隐藏期望值或成功输出写进 repairSpec。\n"
        "如果当前 skill 生成的中间产物会被后续文件或 simulator 使用，repairSpec 必须写出交接字段和语义一致性 gate。\n\n"
        "请返回 JSON 对象：\n"
        "{\n"
        "  \"skillDir\":\"...\",\n"
        "  \"skillName\":\"...\",\n"
        "  \"decision\":\"repair|keep|route-only|deprioritize\",\n"
        "  \"issues\":[{\"label\":\"...\", \"evidence\":\"...\", \"impact\":\"...\"}],\n"
        "  \"repairSpec\": {\n"
        "    \"frontmatterChanges\":[\"...\"],\n"
        "    \"bodyChanges\":[\"...\"],\n"
        "    \"validationGates\":[\"...\"],\n"
        "    \"handoffs\":[\"...\"]\n"
        "  },\n"
        "  \"oracleLeakRisks\":[\"...\"]\n"
        "}\n\n"
        "上下文 JSON：\n"
        f"{compact_json(compact)}"
    )


def build_skill_repair_prompt(
    task_dir: Path,
    skill: dict[str, Any],
    global_diagnosis: dict[str, Any],
    skill_analysis: dict[str, Any],
) -> str:
    compact = {
        "task": {
            "name": task_dir.name,
            "taskMd": read_text(task_dir / "task.md", 7000),
            "publicEnvironment": public_environment_context(task_dir),
        },
        "globalDiagnosis": global_diagnosis,
        "skillAnalysis": skill_analysis,
        "currentSkill": skill,
    }
    return (
        f"阶段 2：单 skill 修复。当前 skill 是 `{skill.get('dir')}` / `{skill.get('name')}`。\n"
        "这是一次独立修复对话：只能输出这个 skill 目录下的文件，通常是完整 `SKILL.md`。\n\n"
        "硬性约束：\n"
        "- 不得修 task/verifier/Dockerfile/tests/data。\n"
        "- 不得写入固定答案、固定路线、固定矩阵、成功 run 的最终输出或隐藏 benchmark 值。\n"
        "- 如果 decision 是 keep，也要明确说明 unchanged，files 可为空。\n"
        "- 如果这是子 skill 且端到端任务应先进 planner/runner，要在 description 和开头段落写清楚。\n\n"
        "路径要求：`relativePath` 是相对于输出 skills 库根目录的路径，例如 `state-space-linearization/SKILL.md`；不要输出 `skills/...`、`tasks/...` 或绝对路径。\n\n"
        "公开环境一致性要求：\n"
        "- 修复内容必须引用 publicEnvironment 中的公开 simulator/config 语义，而不是默认假设。\n"
        "- 对离散时间模型、仿真循环、指标重算、文件格式等内容，必须要求与公开代码的一步更新和数据结构一致。\n"
        "- 不得建议 ZOH、expm 或其它离散化方法来替代公开 simulator 的积分语义，除非公开环境代码本身这样做或 task 明确要求。\n\n"
        "请返回 JSON 对象：\n"
        "{\n"
        "  \"decision\":\"changed|unchanged\",\n"
        "  \"files\":[{\"relativePath\":\"skill-dir/SKILL.md\", \"content\":\"complete file content\", \"why\":\"...\"}],\n"
        "  \"audit\":{\"oracleLeakRisk\":\"low|medium|high\", \"notes\":[\"...\"]}\n"
        "}\n\n"
        "上下文 JSON：\n"
        f"{compact_json(compact)}"
    )


def build_new_skill_prompt(
    task_dir: Path,
    request: dict[str, Any],
    global_diagnosis: dict[str, Any],
    skill_analyses: list[dict[str, Any]],
) -> str:
    compact = {
        "task": {
            "name": task_dir.name,
            "taskMd": read_text(task_dir / "task.md", 7000),
            "publicEnvironment": public_environment_context(task_dir),
        },
        "newSkillRequest": request,
        "globalDiagnosis": global_diagnosis,
        "skillAnalyses": skill_analyses,
    }
    return (
        f"阶段 3：新增 skill/resource 生成。新 skill 请求是 `{request.get('skillDir')}`。\n"
        "这是一次独立生成对话：只输出这个新增 skill 目录下的文件。\n\n"
        "要求：新增 skill 必须泛化为流程/验证/runner/helper，不得包含当前 benchmark 的固定最终答案。\n"
        "如果需要 helper，可以输出同目录下的脚本资源；脚本必须从当前公开输入文件计算，不得硬编码 verifier 期望值。\n\n"
        "路径要求：`relativePath` 是相对于输出 skills 库根目录的路径，例如 `new-skill-dir/SKILL.md` 或 `new-skill-dir/helper.py`；不要输出 `skills/...`、`tasks/...` 或绝对路径。\n\n"
        "新增 skill 必须把 publicEnvironment 中的公开 simulator/config 作为语义来源，尤其是一步更新、参考轨迹、状态/输入顺序和输出文件格式。\n\n"
        "请返回 JSON 对象：\n"
        "{\n"
        "  \"files\":[{\"relativePath\":\"skill-dir/SKILL.md\", \"content\":\"complete file content\", \"why\":\"...\"}],\n"
        "  \"audit\":{\"oracleLeakRisk\":\"low|medium|high\", \"notes\":[\"...\"]}\n"
        "}\n\n"
        "上下文 JSON：\n"
        f"{compact_json(compact)}"
    )


def build_integration_audit_prompt(
    task_dir: Path,
    global_diagnosis: dict[str, Any],
    skill_analyses: list[dict[str, Any]],
    proposed_files: list[dict[str, str]],
) -> str:
    file_index = []
    compact_files = []
    for item in proposed_files:
        content = str(item.get("content") or "")
        file_index.append(
            {
                "relativePath": item.get("relativePath"),
                "why": item.get("why"),
                "contentChars": len(content),
            }
        )
        compact_files.append({
            "relativePath": item.get("relativePath"),
            "why": item.get("why"),
            "contentPreview": content[:6000],
            "truncated": len(content) > 6000,
        })
    compact = {
        "task": {
            "name": task_dir.name,
            "taskMd": read_text(task_dir / "task.md", 7000),
            "publicEnvironment": public_environment_context(task_dir),
        },
        "proposedFileIndex": file_index,
        "globalDiagnosis": global_diagnosis,
        "skillAnalyses": skill_analyses,
        "proposedFiles": compact_files,
    }
    return (
        "阶段 4：集成审计。你是最终 reviewer，只审查多阶段修复结果是否一致、安全、可应用。\n"
        "不要重写文件内容；只批准或拒绝，并给出最终 summary/root causes/repair plan。\n\n"
        "审计重点：\n"
        "- 是否所有修改都在 skills 库内。\n"
        "- `relativePath` 是否相对于输出 skills 库根目录；合法例子是 `skill-dir/SKILL.md`，不要因为缺少 `skills/` 前缀而拒绝。\n"
        "- 是否错误输出了 `tasks/...`、绝对路径或其它会在 output skills 根目录下创建无关嵌套目录的路径。\n"
        "- 是否存在 oracle 泄漏、固定答案、成功 run 的最终输出、隐藏 benchmark 值。\n"
        "- planner/runner 与子 skill 触发是否一致。\n"
        "- helper/validator 是否只使用公开输入并保持泛化。\n\n"
        "额外审计：\n"
        "- 修复是否尊重 publicEnvironment 中公开 simulator/config 的一步更新、离散化、状态顺序、输入顺序和文件格式语义。\n"
        "- 如果 proposedFiles 推荐的算法语义与公开环境代码不一致（例如用另一种离散化替代 simulator 的积分方式），必须拒绝。\n"
        "- 如果端到端任务需要多 skill 编排但没有 planner/runner/validator 或明确入口说明，必须拒绝或标记高风险。\n\n"
        "请返回 JSON 对象：\n"
        "{\n"
        "  \"approved\": true,\n"
        "  \"analysis\":{\"summary\":\"...\", \"baseline\":\"...\", \"rootCauses\":[{\"label\":\"...\", \"evidence\":\"...\", \"skillImpact\":\"...\"}]},\n"
        "  \"repairPlan\":{\"strategy\":\"...\", \"expectedEffect\":\"...\", \"riskControls\":[\"...\"]},\n"
        "  \"audit\":{\"oracleLeakRisk\":\"low|medium|high\", \"taskFilesModified\": false, \"notes\":[\"...\"]}\n"
        "}\n\n"
        "上下文 JSON：\n"
        f"{compact_json(compact)}"
    )


def parse_files_from_stage(label: str, parsed: dict[str, Any]) -> list[dict[str, str]]:
    files = parsed.get("files") or []
    if not isinstance(files, list):
        raise RuntimeError(f"{label} files must be a list")
    out = []
    for item in files:
        if not isinstance(item, dict):
            continue
        relative = normalize_repair_relative_path(str(item.get("relativePath") or ""))
        content = item.get("content")
        if not relative or not isinstance(content, str):
            continue
        out.append({"relativePath": relative, "content": content, "why": str(item.get("why") or label)})
    return out


def run_multi_stage_repair(
    task_dir: Path,
    source_skills_dir: Path,
    output_variant: str,
    report_dir: Path,
    rollouts: list[RolloutInput],
    analyses: list[dict[str, Any]],
    weak_model: str,
    llm_config: dict[str, Any],
    max_tokens: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    skills = list_skills(source_skills_dir)
    stage_summaries: list[dict[str, Any]] = []
    task_name = task_dir.name

    def call(label: str, prompt: str, tokens: int) -> dict[str, Any]:
        result, parsed, paths = stage_llm_call(report_dir, label, prompt, llm_config, min(tokens, max_tokens))
        stage_summaries.append(
            stage_trace_record(
                task_name=task_name,
                label=label,
                skills=skills,
                prompt_path=paths.get("promptPath"),
                response_path=paths.get("responsePath"),
                parsed_path=paths.get("parsedPath"),
                prompt_chars=len(prompt),
                result=result,
                parsed=parsed,
                mode="runtime",
            )
        )
        write_stage_trace(report_dir, task_name, stage_summaries, "runtime")
        return require_stage(label, result, parsed)

    global_prompt = build_global_diagnosis_prompt(task_dir, source_skills_dir, output_variant, rollouts, analyses, weak_model)
    global_diagnosis = call("00-global-diagnosis", global_prompt, 5000)

    skill_analyses: list[dict[str, Any]] = []
    proposed_files: list[dict[str, str]] = []
    for skill in skills:
        label_base = f"{skill.get('dir')}"
        analysis_prompt = build_skill_analysis_prompt(task_dir, skill, global_diagnosis, rollouts, analyses)
        skill_analysis = call(f"10-skill-analysis-{label_base}", analysis_prompt, 3500)
        skill_analyses.append(skill_analysis)

        repair_prompt = build_skill_repair_prompt(task_dir, skill, global_diagnosis, skill_analysis)
        repair_stage = call(f"20-skill-repair-{label_base}", repair_prompt, 8000)
        proposed_files.extend(parse_files_from_stage(f"repair {label_base}", repair_stage))

    for request in global_diagnosis.get("newSkillRequests") or []:
        if not isinstance(request, dict) or not request.get("skillDir"):
            continue
        prompt = build_new_skill_prompt(task_dir, request, global_diagnosis, skill_analyses)
        new_stage = call(f"30-new-skill-{request.get('skillDir')}", prompt, 8000)
        proposed_files.extend(parse_files_from_stage(f"new skill {request.get('skillDir')}", new_stage))

    deduped: list[dict[str, str]] = []
    seen_files: set[str] = set()
    for item in proposed_files:
        rel = str(item.get("relativePath") or "").replace("\\", "/")
        if not rel or rel in seen_files:
            continue
        seen_files.add(rel)
        deduped.append(item)

    if not deduped:
        raise RuntimeError("multi-stage repair produced no files")

    audit_prompt = build_integration_audit_prompt(task_dir, global_diagnosis, skill_analyses, deduped)
    integration = call("90-integration-audit", audit_prompt, 5000)
    if integration.get("approved") is not True:
        raise RuntimeError(f"integration audit rejected repair: {integration}")

    repair = {
        "analysis": integration.get("analysis") or {
            "summary": (global_diagnosis.get("taskDiagnosis") or {}).get("summary", ""),
            "baseline": (global_diagnosis.get("taskDiagnosis") or {}).get("baseline", ""),
            "rootCauses": [],
        },
        "repairPlan": integration.get("repairPlan") or {},
        "files": deduped,
        "audit": integration.get("audit") or {"oracleLeakRisk": "unknown", "taskFilesModified": False, "notes": []},
        "stages": {
            "mode": "multi-stage",
            "globalDiagnosis": global_diagnosis,
            "skillAnalyses": skill_analyses,
            "stageSummaries": stage_summaries,
            "stageTrace": safe_rel(report_dir / "stage_trace.json"),
        },
    }
    llm_result = {
        "ok": True,
        "provider": llm_config.get("provider"),
        "model": llm_config.get("model"),
        "status": "multi-stage",
        "elapsedSec": round(sum(float(item.get("elapsedSec") or 0) for item in stage_summaries), 2),
        "parsed": {"mode": "multi-stage", "stageCount": len(stage_summaries)},
        "stages": stage_summaries,
    }
    return repair, llm_result

def safe_output_file(output_dir: Path, relative_path: str) -> Path:
    rel = Path(str(relative_path).replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts:
        raise SystemExit(f"Unsafe repair file path: {relative_path}")
    if len(rel.parts) < 2 or rel.parts[0].startswith("."):
        raise SystemExit(f"Repair file must be inside a skill directory: {relative_path}")
    target = (output_dir / rel).resolve()
    try:
        target.relative_to(output_dir.resolve())
    except ValueError as exc:
        raise SystemExit(f"Repair file escapes output skills dir: {relative_path}") from exc
    return target


def copy_source_skills(source_dir: Path, output_dir: Path, force: bool) -> None:
    if output_dir.exists():
        if not force:
            raise SystemExit(f"Output skills dir already exists: {safe_rel(output_dir)} (use --force to replace)")
        shutil.rmtree(output_dir)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, output_dir)


def apply_repair_files(output_dir: Path, repair: dict[str, Any]) -> list[dict[str, str]]:
    applied = []
    for item in repair.get("files") or []:
        if not isinstance(item, dict):
            continue
        relative = str(item.get("relativePath") or "").strip()
        content = item.get("content")
        if not relative or not isinstance(content, str):
            raise SystemExit(f"Invalid repair file entry: {item}")
        target = safe_output_file(output_dir, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content.rstrip() + "\n", encoding="utf-8")
        applied.append({"relativePath": relative, "path": safe_rel(target), "why": str(item.get("why") or "")})
    if not applied:
        raise SystemExit("No repair files were applied")
    return applied


def output_skill_files(output_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in output_dir.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )


def sync_repair_metadata_with_output(
    output_dir: Path,
    repair: dict[str, Any],
    applied: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Make repair.json and manifest describe the final deployed skills tree."""
    original_files = {
        str(item.get("relativePath") or "").replace("\\", "/"): item
        for item in repair.get("files") or []
        if isinstance(item, dict) and item.get("relativePath")
    }
    applied_by_rel = {
        str(item.get("relativePath") or "").replace("\\", "/"): item
        for item in applied
        if isinstance(item, dict) and item.get("relativePath")
    }

    synced_files: list[dict[str, Any]] = []
    synced_applied: list[dict[str, str]] = []
    for path in output_skill_files(output_dir):
        relative = path.relative_to(output_dir).as_posix()
        original = dict(original_files.get(relative) or {})
        why = str(
            original.get("why")
            or (applied_by_rel.get(relative) or {}).get("why")
            or "Retained from the source skills library without LLM changes."
        )
        content = path.read_text(encoding="utf-8")
        original["relativePath"] = relative
        original["content"] = content
        original["why"] = why
        if relative not in applied_by_rel:
            original["retainedFromSource"] = True
        synced_files.append(original)
        synced_applied.append({"relativePath": relative, "path": safe_rel(path), "why": why})

    if not synced_files:
        raise SystemExit(f"No files found in output skills dir: {safe_rel(output_dir)}")
    repair["files"] = synced_files
    repair["postAudit"] = True
    return synced_applied


def build_report(
    task_dir: Path,
    source_skills_dir: Path,
    output_skills_dir: Path,
    rollouts: list[RolloutInput],
    analyses: list[dict[str, Any]],
    repair: dict[str, Any],
    applied: list[dict[str, str]],
) -> str:
    outcomes = [a.get("outcome") or {} for a in analyses]
    passed = sum(1 for outcome in outcomes if outcome.get("passed"))
    root_causes = repair.get("analysis", {}).get("rootCauses") or []
    plan = repair.get("repairPlan") or {}
    lines = [
        f"# Skill Repair Report: {task_dir.name}",
        "",
        "## Scope",
        "",
        f"- Task: `{safe_rel(task_dir)}`",
        f"- Source skills: `{safe_rel(source_skills_dir)}`",
        f"- Output skills: `{safe_rel(output_skills_dir)}`",
        f"- Analyzed rollouts: `{len(rollouts)}`",
        f"- Observed pass rate in evidence: `{passed}/{len(outcomes)}`",
        "",
        "## Analysis",
        "",
        str(repair.get("analysis", {}).get("summary") or ""),
        "",
        "## Root Causes",
        "",
    ]
    if root_causes:
        for cause in root_causes:
            lines.append(f"- **{cause.get('label', 'cause')}**: {cause.get('evidence', '')} {cause.get('skillImpact', '')}".strip())
    else:
        lines.append("- No structured root causes returned.")
    lines.extend([
        "",
        "## Repair Plan",
        "",
        str(plan.get("strategy") or ""),
        "",
        f"Expected effect: {plan.get('expectedEffect', '')}",
        "",
    ])
    stages = (repair.get("stages") or {}).get("stageSummaries") or []
    if stages:
        trace_path = (repair.get("stages") or {}).get("stageTrace") or ""
        lines.extend([
            "## Multi-Stage LLM Calls",
            "",
            f"Stage trace: `{trace_path}`",
            "",
        ])
        for stage in stages:
            lines.append(
                f"- `{stage.get('label')}` (`{stage.get('conversationId')}`): ok={stage.get('ok')} status={stage.get('status')} elapsed={stage.get('elapsedSec')}s parsed={stage.get('parsed')}"
            )
        lines.append("")
    lines.extend([
        "## Applied Files",
        "",
    ])
    for item in applied:
        lines.append(f"- `{item['relativePath']}`: {item.get('why', '')}")
    lines.extend([
        "",
        "## Evidence Rollouts",
        "",
    ])
    for rollout, analysis in zip(rollouts, analyses):
        outcome = analysis.get("outcome") or {}
        lines.append(f"- `{safe_rel(rollout.rollout_dir)}`: passed={outcome.get('passed')} reward={outcome.get('reward')} error={outcome.get('errorCategory') or '-'}")
    lines.extend([
        "",
        "## Safety Notes",
        "",
        "- This pipeline copied source skills and only wrote the output skills variant plus report artifacts.",
        "- Task files, verifier files, Dockerfiles, and datasets were not modified by this script.",
        "- Review new skill text for accidental oracle leakage before using it in a benchmark claim.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze, attribute, and repair a SkillsBench skills library with an LLM.")
    parser.add_argument("--task", required=True, help="Task directory, e.g. tasks/bike-rebalance")
    parser.add_argument("--source-skills-dir", help="Source skills dir. Defaults to skill-libraries/<task>/initial or task environment skills.")
    parser.add_argument("--job-path", action="append", default=[], help="Completed job/artifact/rollout directory or summary.json. Repeatable.")
    parser.add_argument("--max-rollouts", type=int, default=8)
    parser.add_argument("--variant", help="Output skill library variant name. Defaults to auto-repair-<timestamp>.")
    parser.add_argument("--output-skills-dir", help="Explicit output skills dir. Defaults to skill-libraries/<task>/<variant>.")
    parser.add_argument("--report-root", default="repair-runs", help="Directory for repair reports and manifests.")
    parser.add_argument("--force", action="store_true", help="Replace output skills dir if it already exists.")
    parser.add_argument("--dry-run", action="store_true", help="Run analysis and LLM planning without writing repaired skills.")
    parser.add_argument("--prepare-prompts-only", action="store_true", help="Materialize multi-stage prompts without calling an LLM or writing repaired skills.")
    parser.add_argument("--strong-provider", choices=["anthropic", "openai"], default="anthropic")
    parser.add_argument("--strong-base-url", default=os.getenv("STRONG_LLM_BASE_URL") or os.getenv("ANTHROPIC_BASE_URL") or DEFAULT_STRONG_BASE_URL)
    parser.add_argument("--strong-model", default=os.getenv("STRONG_LLM_MODEL") or DEFAULT_STRONG_MODEL)
    parser.add_argument(
        "--strong-reasoning-effort",
        choices=["off", "minimal", "low", "medium", "high", "max", "xhigh"],
        default=os.getenv("STRONG_LLM_REASONING_EFFORT") or "off",
        help="Reasoning/thinking intensity sent to the selected repair provider.",
    )
    parser.add_argument("--strong-api-key", default=os.getenv("STRONG_LLM_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY"))
    parser.add_argument("--strong-max-tokens", type=int, default=8000)
    parser.add_argument("--strong-timeout", type=float, default=240)
    parser.add_argument("--weak-model", default=os.getenv("WEAK_LLM_MODEL") or DEFAULT_WEAK_MODEL)
    parser.add_argument("--judge", action="store_true", help="Run per-trajectory judge with the strong model before repair.")
    parser.add_argument("--keyword-mode", choices=["rules", "llm-task", "llm-skills", "llm-both"], default="rules")
    parser.add_argument("--json-output", action="store_true", help="Print machine-readable result JSON.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    task_dir = resolve_root_path(args.task, must_exist=True)
    if not task_dir or not (task_dir / "task.md").exists():
        raise SystemExit("--task must point to a SkillsBench task directory with task.md")

    source_skills_dir = resolve_root_path(args.source_skills_dir, must_exist=True) if args.source_skills_dir else find_default_source_skills(task_dir)
    if not source_skills_dir or not source_skills_dir.exists():
        raise SystemExit("Missing source skills dir")
    if not list(source_skills_dir.glob("*/SKILL.md")):
        raise SystemExit(f"Source skills dir contains no */SKILL.md files: {safe_rel(source_skills_dir)}")

    job_paths = [resolve_root_path(item, must_exist=True) for item in args.job_path]
    if not job_paths:
        raise SystemExit("Provide at least one --job-path pointing at completed job evidence")
    rollouts = discover_rollouts([p for p in job_paths if p], max(1, args.max_rollouts))
    if not rollouts:
        raise SystemExit("No rollout trajectories found under --job-path")

    variant = sanitize_segment(args.variant or f"auto-repair-{now_stamp()}")
    output_skills_dir = resolve_root_path(args.output_skills_dir) if args.output_skills_dir else ROOT / "skill-libraries" / task_dir.name / variant
    report_dir = resolve_root_path(args.report_root) / task_dir.name / variant

    keyword_config = {
        "enabled": args.keyword_mode != "rules" and not args.prepare_prompts_only,
        "mode": args.keyword_mode,
        "provider": args.strong_provider,
        "baseUrl": args.strong_base_url,
        "model": args.strong_model,
        "reasoningEffort": args.strong_reasoning_effort,
        "apiKey": args.strong_api_key,
        "maxTokens": min(args.strong_max_tokens, 3000),
        "timeout": args.strong_timeout,
    }
    judge_config = {
        "enabled": args.judge and not args.prepare_prompts_only,
        "provider": args.strong_provider,
        "baseUrl": args.strong_base_url,
        "model": args.strong_model,
        "reasoningEffort": args.strong_reasoning_effort,
        "apiKey": args.strong_api_key,
        "maxTokens": min(args.strong_max_tokens, 2400),
        "timeout": args.strong_timeout,
    }

    print(f"[repair] task: {safe_rel(task_dir)}", file=sys.stderr)
    print(f"[repair] source skills: {safe_rel(source_skills_dir)}", file=sys.stderr)
    print(f"[repair] rollouts: {len(rollouts)}", file=sys.stderr)
    print("[repair] pipeline mode: multi-stage", file=sys.stderr)
    if args.prepare_prompts_only:
        print("[repair] prepare-prompts-only: LLM-dependent keyword extraction and judging are disabled", file=sys.stderr)
    analyses = run_rollout_analyses(task_dir, source_skills_dir, rollouts, keyword_config, judge_config)

    if args.prepare_prompts_only:
        bundle = prepare_prompt_bundle(
            task_dir,
            source_skills_dir,
            variant,
            report_dir,
            rollouts,
            analyses,
            args.weak_model,
        )
        manifest = {
            "schemaVersion": 1,
            "createdAt": datetime.now(UTC).isoformat(),
            "task": safe_rel(task_dir),
            "sourceSkillsDir": safe_rel(source_skills_dir),
            "outputSkillsDir": safe_rel(output_skills_dir),
            "variant": variant,
            "reportDir": safe_rel(report_dir),
            "dryRun": True,
            "preparePromptsOnly": True,
            "pipelineMode": "multi-stage",
            "usedFallback": False,
            "strongModel": {
                "provider": args.strong_provider,
                "baseUrl": args.strong_base_url,
                "model": args.strong_model,
                "ok": None,
                "status": "not-called",
            },
            "weakModel": args.weak_model,
            "rollouts": [safe_rel(item.rollout_dir) for item in rollouts],
            "appliedFiles": [],
            "stageTrace": bundle.get("stageTrace"),
            "stageCount": bundle.get("promptCount"),
            "audit": {"oracleLeakRisk": "not-run", "taskFilesModified": False, "notes": ["Prompt preparation only; no LLM repair was executed."]},
        }
        write_json(report_dir / "manifest.json", manifest)
        write_json(report_dir / "analysis.json", [summarize_analysis(item) for item in analyses])
        readme = "\n".join(
            [
                f"# Prompt Preparation: {task_dir.name}",
                "",
                f"- Source skills: `{safe_rel(source_skills_dir)}`",
                f"- Variant: `{variant}`",
                f"- Rollouts: `{len(rollouts)}`",
                f"- Prompt count: `{bundle['promptCount']}`",
                f"- Stage trace: `{bundle.get('stageTrace')}`",
                "",
                "This run materialized multi-stage prompts without calling an LLM or writing repaired skills.",
                "Stage 2 prompts are prepare-only templates; runtime repair uses the actual parsed stage 1 result.",
                "",
                "## Prompts",
                "",
                *[
                    f"- `{item['label']}` (`{item.get('conversationId')}`): `{item['path']}` ({item['chars']} chars)"
                    for item in bundle["prompts"]
                ],
                "",
            ]
        )
        (report_dir / "README.md").write_text(readme, encoding="utf-8")
        result = {
            "ok": True,
            "mode": "prepare-prompts-only",
            "manifest": manifest,
            "promptBundle": safe_rel(report_dir / "prompt_bundle.json"),
            "report": safe_rel(report_dir / "README.md"),
        }
        if args.json_output:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"Prompt bundle: {safe_rel(report_dir / 'prompt_bundle.json')}")
            print(f"Prompt report: {safe_rel(report_dir / 'README.md')}")
        return 0

    llm_config = {
        "provider": args.strong_provider,
        "baseUrl": args.strong_base_url,
        "model": args.strong_model,
        "reasoningEffort": args.strong_reasoning_effort,
        "apiKey": args.strong_api_key,
        "maxTokens": args.strong_max_tokens,
        "timeout": args.strong_timeout,
    }
    try:
        repair, llm_result = run_multi_stage_repair(
            task_dir,
            source_skills_dir,
            variant,
            report_dir,
            rollouts,
            analyses,
            args.weak_model,
            llm_config,
            args.strong_max_tokens,
        )
    except Exception as exc:
        llm_result = {
            "ok": False,
            "provider": args.strong_provider,
            "model": args.strong_model,
            "status": "multi-stage-error",
            "error": f"{type(exc).__name__}: {exc}",
        }
        write_json(report_dir / "analysis.json", [summarize_analysis(item) for item in analyses])
        write_json(report_dir / "llm_response.json", llm_result)
        raise SystemExit(f"Strong LLM multi-stage repair failed: {llm_result['error']}. Report partials: {safe_rel(report_dir)}") from exc

    applied: list[dict[str, str]] = []
    if not args.dry_run:
        copy_source_skills(source_skills_dir, output_skills_dir, args.force)
        applied = apply_repair_files(output_skills_dir, repair)
        applied = sync_repair_metadata_with_output(output_skills_dir, repair, applied)

    report = build_report(task_dir, source_skills_dir, output_skills_dir, rollouts, analyses, repair, applied)
    manifest = {
        "schemaVersion": 1,
        "createdAt": datetime.now(UTC).isoformat(),
        "task": safe_rel(task_dir),
        "sourceSkillsDir": safe_rel(source_skills_dir),
        "outputSkillsDir": safe_rel(output_skills_dir),
        "variant": variant,
        "reportDir": safe_rel(report_dir),
        "dryRun": args.dry_run,
        "pipelineMode": "multi-stage",
        "usedFallback": False,
        "strongModel": {
            "provider": args.strong_provider,
            "baseUrl": args.strong_base_url,
            "model": args.strong_model,
            "ok": llm_result.get("ok"),
            "status": llm_result.get("status"),
            "elapsedSec": llm_result.get("elapsedSec"),
        },
        "weakModel": args.weak_model,
        "rollouts": [safe_rel(item.rollout_dir) for item in rollouts],
        "appliedFiles": applied,
        "stageTrace": ((repair.get("stages") or {}).get("stageTrace")),
        "stageCount": len(((repair.get("stages") or {}).get("stageSummaries")) or []),
        "audit": repair.get("audit"),
    }
    write_json(report_dir / "manifest.json", manifest)
    write_json(report_dir / "analysis.json", [summarize_analysis(item) for item in analyses])
    write_json(report_dir / "llm_response.json", llm_result)
    write_json(report_dir / "repair.json", repair)
    (report_dir / "README.md").write_text(report, encoding="utf-8")

    result = {
        "ok": True,
        "manifest": manifest,
        "report": safe_rel(report_dir / "README.md"),
        "outputSkillsDir": safe_rel(output_skills_dir),
    }
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Repair report: {safe_rel(report_dir / 'README.md')}")
        print(f"Output skills: {safe_rel(output_skills_dir)}")
        if args.dry_run:
            print("Dry run: no skills files were written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
