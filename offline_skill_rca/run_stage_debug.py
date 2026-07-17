#!/usr/bin/env python3
"""Offline SkillRCA stage-by-stage debugging entrypoint.

这个脚本服务于 Web 调试页：它把完整 repair pipeline 拆成可单独执行的
Stage 1-8，并把每个 Stage 的 prompt、request、response、parsed JSON 和最终
应用结果都写在同一个 ``repair-runs`` 目录里。Stage 8 会逐建议生成候选文件、
调用 repair LLM 审查，并只把审查通过的候选提交到技能库副本。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.io_utils import (
    AGENT_ONLY_EVIDENCE_POLICY_VERSION,
    discover_rollout_selection,
    list_skill_files,
    load_trajectory,
    read_json,
    read_text,
    safe_rel,
    sanitize_agent_only_visible_result,
    write_json,
)
from src.pipeline import (
    OfflineSkillRCAConfig,
    build_evidence_bundle,
    build_prompt_index,
    enrich_analysis_from_stage_outputs,
    normalize_analysis,
    prepare_output_dir,
    sanitize_trajectory_for_llm,
    validate_failed_trajectories,
    validate_config,
    write_outputs,
)
from src.calculations import postprocess_skill_coverage, summarize_node_coverage
from src.stages.common import json_block
from src.stages import (
    stage_01_input_standardization,
    stage_02_capability_graph,
    stage_03_failure_event_extraction,
    stage_04_failure_event_alignment,
    stage_05_node_execution_assessment,
    stage_06_skill_repair_suggestions,
    stage_07_repair_action_merge,
    stage_08_transactional_skill_repair,
)
from src.stages.common import PROMPT_VARIABLE_DIR, write_prompt_file

ROOT = Path(__file__).resolve().parents[1]

STAGES: list[dict[str, Any]] = [
    {
        "id": "stage-01-input-standardization",
        "key": "stage_01_input_standardization",
        "label": "Stage 1 输入标准化",
        "template": "stage-01-input-standardization.txt",
        "deps": [],
    },
    {
        "id": "stage-02-capability-graph",
        "key": "stage_02_capability_graph",
        "label": "Stage 2 能力图与技能覆盖",
        "template": "stage-02-capability-graph.txt",
        "deps": ["stage_01a_task_description_standardization", "stage_01b_skill_standardizations"],
    },
    {
        "id": "stage-03-failure-event-extraction",
        "key": "stage_03_failure_events_by_trace",
        "label": "Stage 3 逐轨迹失败因果抽取",
        "template": "stage-03-failure-event-extraction.txt",
        "deps": [
            "stage_01a_task_description_standardization",
        ],
        "parallel": True,
    },
    {
        "id": "stage-04-failure-event-alignment",
        "key": "stage_04_failure_event_alignment",
        "label": "Stage 4 失败/原因事件到能力节点对齐",
        "template": "stage-04-failure-event-alignment.txt",
        "deps": ["stage_02_capability_graph", "stage_03_failure_events_by_trace"],
    },
    {
        "id": "stage-05-node-execution-assessment",
        "key": "stage_05_node_execution_assessments",
        "label": "Stage 5 逐轨迹能力节点执行判断",
        "template": "stage-05-node-execution-assessment.txt",
        "deps": ["stage_02_capability_graph", "stage_03_failure_events_by_trace", "stage_04_failure_event_alignment"],
        "parallel": True,
        "calculable": True,
    },
    {
        "id": "stage-06-skill-repair-suggestions",
        "key": "stage_06_skill_repair_suggestions",
        "label": "Stage 6 逐能力节点技能修复建议",
        "template": "stage-06-skill-repair-suggestions.txt",
        "deps": [
            "stage_01b_skill_standardizations",
            "stage_02_capability_graph",
            "stage_03_failure_events_by_trace",
            "stage_04_failure_event_alignment",
            "stage_05_node_execution_assessments",
        ],
        "parallel": True,
    },
    {
        "id": "stage-07-repair-action-merge",
        "key": "stage_07_repair_action_merge",
        "label": "Stage 7 修复操作合并",
        "template": "stage-07-repair-action-merge.txt",
        "deps": ["stage_06_skill_repair_suggestions"],
    },
    {
        "id": "stage-08-transactional-skill-repair",
        "key": "stage_08_transactional_skill_repair",
        "label": "Stage 8 事务式技能修复",
        "template": "stage-08-skill-repair.txt",
        "deps": [
            "stage_01b_skill_standardizations",
            "stage_02_capability_graph",
            "stage_03_failure_events_by_trace",
            "stage_04_failure_event_alignment",
            "stage_05_node_execution_assessments",
            "stage_06_skill_repair_suggestions",
            "stage_07_repair_action_merge",
        ],
    },
]

STAGE_BY_ID = {stage["id"]: stage for stage in STAGES}
STAGE_BY_KEY = {stage["key"]: stage for stage in STAGES}


def root_path(value: str | None) -> Path | None:
    """把命令行路径解析到仓库根目录下。"""
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def read_json(path: Path) -> Any:
    """读取 JSON；缺失时返回 None，便于 status 在半成品目录上运行。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_stdout_json(data: Any) -> None:
    """CLI 与 Web 后端之间用 JSON 交换结果。"""
    text = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    except Exception:
        sys.stdout.buffer.write((text + "\n").encode("utf-8"))


def rel(path: Path | None) -> str:
    """输出相对仓库根目录的 POSIX 路径。"""
    return safe_rel(ROOT, path) if path else ""


def configured_bool(args: argparse.Namespace, arg_name: str, env_name: str, manifest: dict[str, Any] | None, manifest_key: str) -> bool:
    """读取可显式开启或关闭、同时支持环境变量和 manifest 的布尔选项。"""
    explicit = getattr(args, arg_name, None)
    if explicit is not None:
        return bool(explicit)
    env_value = os.getenv(env_name)
    if env_value is not None:
        return env_value.strip().lower() in {"1", "true", "yes", "on"}
    return bool((manifest or {}).get(manifest_key))


def build_config(args: argparse.Namespace, manifest: dict[str, Any] | None = None) -> OfflineSkillRCAConfig:
    """从命令行参数和 stage_debug_manifest 组装统一配置。"""
    output_dir = root_path(args.output_dir)
    if output_dir is None:
        raise SystemExit("--output-dir is required")
    # 完整 pipeline 产物没有 stage_debug_manifest.json；它仍会写 input_bundle.json。
    # 这里在 manifest 缺失时从 bundle 中恢复 task/skills 路径，使 debug CLI 可以
    # 接管半途中断的运行目录并继续 Stage 7。
    bundle_hint = read_json(output_dir / "input_bundle.json") or {}
    task_dir = root_path(getattr(args, "task_dir", None) or (manifest or {}).get("taskDir") or bundle_hint.get("task_dir"))
    skills_dir = root_path(getattr(args, "skills_dir", None) or (manifest or {}).get("skillsDir") or bundle_hint.get("skills_dir"))
    output_skills_dir = root_path(getattr(args, "output_skills_dir", None) or (manifest or {}).get("outputSkillsDir"))
    if output_skills_dir is None:
        output_skills_dir = output_dir / "repaired-skills"
    traces_raw = getattr(args, "traces", None) or (manifest or {}).get("tracePaths") or [
        item.get("rollout_dir")
        for item in bundle_hint.get("failed_trajectories") or []
        if isinstance(item, dict) and item.get("rollout_dir")
    ]
    trace_paths = [root_path(item) for item in traces_raw]
    return OfflineSkillRCAConfig(
        root=ROOT,
        task_dir=task_dir,
        skills_dir=skills_dir,
        trace_paths=trace_paths,
        output_dir=output_dir,
        output_skills_dir=output_skills_dir,
        strong_base_url=getattr(args, "strong_base_url", None)
        or os.getenv("OFFLINE_SKILL_RCA_BASE_URL")
        or (manifest or {}).get("strongBaseUrl")
        or "https://api.camel-hub.com",
        strong_api_key=getattr(args, "strong_api_key", None)
        or os.getenv("OFFLINE_SKILL_RCA_API_KEY")
        or os.getenv("LLM_API_KEY")
        or "",
        strong_model=getattr(args, "strong_model", None)
        or os.getenv("OFFLINE_SKILL_RCA_MODEL")
        or (manifest or {}).get("strongModel")
        or "gpt-5.5",
        use_separate_review_llm=configured_bool(
            args,
            "use_separate_review_llm",
            "OFFLINE_SKILL_RCA_USE_SEPARATE_REVIEW_LLM",
            manifest,
            "separateReviewLlm",
        ),
        review_base_url=getattr(args, "review_base_url", None)
        or os.getenv("OFFLINE_SKILL_RCA_REVIEW_BASE_URL")
        or (manifest or {}).get("reviewBaseUrl")
        or "",
        review_api_key=getattr(args, "review_api_key", None)
        or os.getenv("OFFLINE_SKILL_RCA_REVIEW_API_KEY")
        or "",
        review_model=getattr(args, "review_model", None)
        or os.getenv("OFFLINE_SKILL_RCA_REVIEW_MODEL")
        or (manifest or {}).get("reviewModel")
        or "",
        strong_reasoning_effort=getattr(args, "strong_reasoning_effort", None)
        or os.getenv("OFFLINE_SKILL_RCA_REASONING_EFFORT")
        or (manifest or {}).get("strongReasoningEffort")
        or "minimal",
        review_reasoning_effort=getattr(args, "review_reasoning_effort", None)
        or os.getenv("OFFLINE_SKILL_RCA_REVIEW_REASONING_EFFORT")
        or (manifest or {}).get("reviewReasoningEffort")
        or "minimal",
        max_traces=int(getattr(args, "max_traces", None) or (manifest or {}).get("maxTraces") or 5),
        max_prompt_chars=int(getattr(args, "max_prompt_chars", None) or (manifest or {}).get("maxPromptChars") or 220_000),
        trace_analysis_workers=int(
            getattr(args, "trace_analysis_workers", None)
            or os.getenv("OFFLINE_SKILL_RCA_TRACE_WORKERS")
            or (manifest or {}).get("traceAnalysisWorkers")
            or 5
        ),
        add_skill_merge_threshold=max(
            0,
            min(
                1000,
                int(
                    getattr(args, "add_skill_merge_threshold", None)
                    or os.getenv("OFFLINE_SKILL_RCA_ADD_SKILL_MERGE_THRESHOLD")
                    or (manifest or {}).get("addSkillMergeThreshold")
                    or 0
                ),
            ),
        ),
        add_skill_target_count=max(
            0,
            min(
                1000,
                int(
                    getattr(args, "add_skill_target_count", None)
                    or os.getenv("OFFLINE_SKILL_RCA_ADD_SKILL_TARGET_COUNT")
                    or (manifest or {}).get("addSkillTargetCount")
                    or 0
                ),
            ),
        ),
        max_new_skill_count=max(
            0,
            min(
                1000,
                int(
                    getattr(args, "max_new_skills", None)
                    or os.getenv("OFFLINE_SKILL_RCA_MAX_NEW_SKILLS")
                    or (manifest or {}).get("maxNewSkillCount")
                    or 2
                ),
            ),
        ),
        skill_word_limit=max(
            100,
            min(
                20_000,
                int(
                    getattr(args, "skill_word_limit", None)
                    or os.getenv("OFFLINE_SKILL_RCA_SKILL_WORD_LIMIT")
                    or (manifest or {}).get("skillWordLimit")
                    or 1200
                ),
            ),
        ),
        stage7_max_operations=max(
            1,
            min(
                1000,
                int(
                    getattr(args, "stage7_max_operations", None)
                    or os.getenv("OFFLINE_SKILL_RCA_STAGE7_MAX_OPERATIONS")
                    or os.getenv("OFFLINE_SKILL_RCA_STAGE6_MAX_OPERATIONS")
                    or (manifest or {}).get("stage7MaxOperations")
                    or (manifest or {}).get("stage6MaxOperations")
                    or 30
                ),
            ),
        ),
        stage7_repair_mode=(
            "skill_package"
            if (
                getattr(args, "stage7_repair_mode", None)
                or os.getenv("OFFLINE_SKILL_RCA_STAGE7_REPAIR_MODE")
                or (manifest or {}).get("stage7RepairMode")
            ) == "skill_package"
            else "per_suggestion"
        ),
        stage7_skill_package_size=max(
            1,
            min(
                100,
                int(
                    getattr(args, "stage7_skill_package_size", None)
                    or os.getenv("OFFLINE_SKILL_RCA_STAGE7_SKILL_PACKAGE_SIZE")
                    or (manifest or {}).get("stage7SkillPackageSize")
                    or 3
                ),
            ),
        ),
        # init 时的 force 也要写入 manifest。Stage 7 往往在之后的独立命令中才
        # 第一次复制技能库；若不保留该选择，就无法安全处理同名输出副本。
        force=bool(getattr(args, "force", False) or (manifest or {}).get("force")),
        prepare_prompts_only=False,
        apply_repaired_skills=not bool(getattr(args, "no_apply", False)),
    )


def manifest_path(output_dir: Path) -> Path:
    """本次 stage 调试运行的本地 manifest 路径。"""
    return output_dir / "stage_debug_manifest.json"


def load_manifest(output_dir: Path) -> dict[str, Any]:
    """读取 stage 调试 manifest。"""
    return read_json(manifest_path(output_dir)) or {}


def persist_runtime_options(config: OfflineSkillRCAConfig) -> None:
    """保存调试时修改的非密钥选项，供后续命令和页面复用。"""
    manifest = load_manifest(config.output_dir)
    if not manifest:
        return
    manifest.update(
        {
            "strongBaseUrl": config.strong_base_url,
            "strongModel": config.strong_model,
            "strongReasoningEffort": config.strong_reasoning_effort,
            "separateReviewLlm": config.use_separate_review_llm,
            "reviewBaseUrl": config.review_base_url if config.use_separate_review_llm else "",
            "reviewModel": config.review_model if config.use_separate_review_llm else "",
            "reviewReasoningEffort": config.review_reasoning_effort if config.use_separate_review_llm else "",
            "stage7MaxOperations": config.stage7_max_operations,
            "stage7RepairMode": config.stage7_repair_mode,
            "stage7SkillPackageSize": config.stage7_skill_package_size,
        }
    )
    write_json(manifest_path(config.output_dir), manifest)


def _manifest_trace_selection(config: OfflineSkillRCAConfig, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把本地筛选记录转成不含绝对路径的运行诊断信息。"""
    output = []
    for record in records:
        item = dict(record)
        rollout_dir = item.pop("rolloutDir", "")
        item["path"] = safe_rel(config.root, Path(rollout_dir)) if rollout_dir else ""
        output.append(item)
    return output


def save_manifest(
    config: OfflineSkillRCAConfig,
    rollout_dirs: list[Path],
    trace_selection: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """保存不含 API key 的运行配置，方便之后逐 stage 续跑。"""
    manifest = {
        "method": "Offline SkillRCA v2 stage-debug",
        "createdAt": datetime.now(UTC).isoformat(),
        "taskDir": rel(config.task_dir),
        "skillsDir": rel(config.skills_dir),
        "outputDir": rel(config.output_dir),
        "outputSkillsDir": rel(config.output_skills_dir),
        "tracePaths": [rel(path) for path in config.trace_paths if path],
        "rolloutDirs": [rel(path) for path in rollout_dirs],
        "traceSelection": trace_selection or [],
        "strongBaseUrl": config.strong_base_url,
        "strongModel": config.strong_model,
        "strongReasoningEffort": config.strong_reasoning_effort,
        "separateReviewLlm": config.use_separate_review_llm,
        "reviewBaseUrl": config.review_base_url if config.use_separate_review_llm else "",
        "reviewModel": config.review_model if config.use_separate_review_llm else "",
        "reviewReasoningEffort": config.review_reasoning_effort if config.use_separate_review_llm else "",
        "maxTraces": config.max_traces,
        "maxPromptChars": config.max_prompt_chars,
        "traceAnalysisWorkers": config.trace_analysis_workers,
        "addSkillMergeThreshold": config.add_skill_merge_threshold,
        "addSkillTargetCount": config.add_skill_target_count,
        "maxNewSkillCount": config.max_new_skill_count,
        "skillWordLimit": config.skill_word_limit,
        "stage7MaxOperations": config.stage7_max_operations,
        "stage7RepairMode": config.stage7_repair_mode,
        "stage7SkillPackageSize": config.stage7_skill_package_size,
        "force": config.force,
        "stageCount": len(STAGES),
        "stages": STAGES,
    }
    write_json(manifest_path(config.output_dir), manifest)
    return manifest


def init_run(args: argparse.Namespace) -> dict[str, Any]:
    """初始化一个可按 stage 调试的输出目录。"""
    config = build_config(args)
    validate_config(config)
    task_text = read_text(config.task_dir / "task.md", 32_000)
    skill_files = list_skill_files(config.root, config.skills_dir)
    rollout_dirs, trace_selection = discover_rollout_selection(
        [path for path in config.trace_paths if path],
        config.max_traces,
    )
    trajectories = [load_trajectory(config.root, rollout) for rollout in rollout_dirs]
    validate_failed_trajectories(trajectories)
    prepare_output_dir(config)
    trace_selection_for_manifest = _manifest_trace_selection(config, trace_selection)
    write_json(config.output_dir / "trace_selection.json", trace_selection_for_manifest)
    bundle = build_evidence_bundle(config, task_text, skill_files, trajectories)
    write_json(config.output_dir / "input_bundle.json", bundle)
    (config.output_dir / "offline_skill_rca_prompt.txt").write_text(build_prompt_index(config, bundle), encoding="utf-8")
    manifest = save_manifest(config, rollout_dirs, trace_selection_for_manifest)
    status = compact_command_status(config.output_dir)
    return {"ok": True, "manifest": manifest, "status": status}


def load_bundle(output_dir: Path) -> dict[str, Any]:
    """读取输入包，并按 agent-only 策略重建或清洗每条轨迹证据。"""
    bundle_path = output_dir / "input_bundle.json"
    bundle = read_json(bundle_path)
    if not isinstance(bundle, dict):
        raise SystemExit(f"Missing input_bundle.json under {output_dir}")
    manifest = load_manifest(output_dir)
    rollout_dirs = list(manifest.get("rolloutDirs") or [])
    trajectories = list(bundle.get("failed_trajectories") or [])
    changed = False
    for index, trajectory in enumerate(trajectories):
        if not isinstance(trajectory, dict):
            continue
        if index >= len(rollout_dirs):
            cleaned = sanitize_agent_only_visible_result(trajectory.get("visible_failure_result"))
            if cleaned != trajectory.get("visible_failure_result"):
                trajectory["visible_failure_result"] = cleaned
                changed = True
            continue
        rollout_dir = root_path(str(rollout_dirs[index]))
        if not rollout_dir or not rollout_dir.exists():
            cleaned = sanitize_agent_only_visible_result(trajectory.get("visible_failure_result"))
            if cleaned != trajectory.get("visible_failure_result"):
                trajectory["visible_failure_result"] = cleaned
                changed = True
            continue
        refreshed = sanitize_trajectory_for_llm(load_trajectory(ROOT, rollout_dir))
        if trajectory.get("visible_failure_result") != refreshed["visible_failure_result"]:
            trajectory["visible_failure_result"] = refreshed["visible_failure_result"]
            changed = True
        if trajectory.get("final_artifacts") != refreshed["final_artifacts"]:
            trajectory["final_artifacts"] = refreshed["final_artifacts"]
            changed = True
        # 旧 bundle 的 steps 仍可保留；Stage 3 执行时会再次从原轨迹重建。
    if changed:
        bundle["failed_trajectories"] = trajectories
        write_json(bundle_path, bundle)
    validate_failed_trajectories(trajectories)
    return bundle


def stage_output_dir(output_dir: Path) -> Path:
    """单阶段输出快照目录。"""
    return output_dir / "stage_outputs_individual"


def stage_output_file(output_dir: Path, key: str) -> Path:
    """某个 stage 的结构化输出快照。"""
    return stage_output_dir(output_dir) / f"{key}.json"


STAGE_OUTPUT_ALIASES = {
    "stage_01a_task_description_standardization": ("stage_00a_task_description_standardization",),
    "stage_01b_skill_standardizations": ("stage_00b_skill_standardizations",),
    "stage_01_input_standardization": ("stage_00_input_standardization",),
    "stage_02_capability_graph": ("stage_01_s0_capability_dag",),
    "stage_03_failure_events_by_trace": ("stage_02_failure_events_by_trace",),
    "stage_04_failure_event_alignment": ("stage_03_event_to_s0_alignment",),
    "stage_06_skill_repair_suggestions": ("stage_05_skill_repair_suggestions", "stage_05_root_cause_hypotheses"),
    "stage_07_repair_action_merge": ("stage_06_repair_action_merge", "stage_05b_repair_action_merge"),
    "stage_08_transactional_skill_repair": ("stage_07_transactional_skill_repair", "stage_06_transactional_skill_repair"),
}


TRANSCRIPT_ALIASES = {
    "stage-01a-task-description-standardization": ("stage-00a-task-description-standardization",),
    "stage-02-capability-graph": ("stage-01-s0-capability-dag",),
    "stage-04-failure-event-alignment": ("stage-03-event-to-s0-alignment",),
    "stage-06-skill-repair-suggestions": ("stage-05-skill-repair-suggestions", "stage-05-root-cause-hypotheses"),
    "stage-07-repair-action-merge": ("stage-06-repair-action-merge", "stage-05b-repair-action-merge"),
}


def read_stage_output(output_dir: Path, key: str) -> Any:
    """读取 stage 输出，兼容删除 Stage 2 前的旧编号文件。"""
    value = read_json(stage_output_file(output_dir, key))
    if value is not None:
        return value
    aggregate = read_json(output_dir / "stage_outputs.json")
    if isinstance(aggregate, dict) and key in aggregate:
        return aggregate.get(key)
    for old_key in STAGE_OUTPUT_ALIASES.get(key, ()):
        value = read_json(stage_output_file(output_dir, old_key))
        if value is not None:
            return value
        if isinstance(aggregate, dict) and old_key in aggregate:
            return aggregate.get(old_key)
    return None


def normalize_stage2_output(output_dir: Path, value: Any) -> Any:
    """把新旧运行的能力图输出统一成合并后的 Stage 2 结构。

    历史运行把能力图和 Skill 覆盖分别保存在旧 Stage 1、旧 Stage 4。读取历史
    目录时在本地合并两者，并统一改用 ``capability_graph``；这只做结构迁移，
    不会新增任何 LLM 判断。
    """
    if not isinstance(value, dict):
        return value
    merged = dict(value)
    legacy_graph = merged.pop("S0_capability_dag", None)
    if "capability_graph" not in merged and isinstance(legacy_graph, dict):
        merged["capability_graph"] = legacy_graph

    if not isinstance(merged.get("skill_coverage_matrix"), list):
        legacy_coverage = read_json(stage_output_file(output_dir, "stage_04_s0_to_skill_coverage"))
        if legacy_coverage is None:
            aggregate = read_json(output_dir / "stage_outputs.json")
            if isinstance(aggregate, dict):
                legacy_coverage = aggregate.get("stage_04_s0_to_skill_coverage")
        if not isinstance(legacy_coverage, dict):
            legacy_coverage = parsed_transcript(output_dir, "stage-04-s0-to-skill-coverage")
        if isinstance(legacy_coverage, dict):
            for field in ("skill_coverage_matrix", "node_coverage_summary", "coverage_notes"):
                if field in legacy_coverage:
                    merged[field] = legacy_coverage[field]

    merged = stage_02_capability_graph.postprocess_skill_coverage(merged)
    merged["node_coverage_summary"] = stage_02_capability_graph.summarize_node_coverage(merged)
    return merged


def stage_output_info(output_dir: Path, key: str) -> dict[str, Any] | None:
    """返回 stage 输出文件信息，兼容旧编号文件。"""
    info = path_info(stage_output_file(output_dir, key))
    if info:
        return info
    aggregate = read_json(output_dir / "stage_outputs.json")
    if isinstance(aggregate, dict) and key in aggregate:
        info = path_info(output_dir / "stage_outputs.json")
        if info:
            return {**info, "jsonKey": key}
    for old_key in STAGE_OUTPUT_ALIASES.get(key, ()):
        info = path_info(stage_output_file(output_dir, old_key))
        if info:
            return info
        if isinstance(aggregate, dict) and old_key in aggregate:
            info = path_info(output_dir / "stage_outputs.json")
            if info:
                return {**info, "jsonKey": old_key}
    return None


def save_stage_output(config: OfflineSkillRCAConfig, key: str, value: Any) -> None:
    """保存单个 stage 输出，并刷新聚合 stage_outputs.json。"""
    write_json(stage_output_file(config.output_dir, key), value)
    write_json(config.output_dir / "stage_outputs.json", collect_stage_outputs(config.output_dir))
    if key == "stage_03_failure_events_by_trace":
        write_json(config.output_dir / "trace_analyses.json", value)


def parsed_transcript(output_dir: Path, name: str) -> Any:
    """优先读取 transcript parsed JSON；不存在时回退到 individual output。"""
    value = read_json(output_dir / "llm_transcript" / f"{name}.parsed.json")
    if value is not None:
        return value
    if name.startswith("stage-03-traj-"):
        old_name = f"stage-02-traj-{name.removeprefix('stage-03-traj-')}"
        return read_json(output_dir / "llm_transcript" / f"{old_name}.parsed.json")
    for old_name in TRANSCRIPT_ALIASES.get(name, ()):
        value = read_json(output_dir / "llm_transcript" / f"{old_name}.parsed.json")
        if value is not None:
            return value
    return None


def merge_stage1_skill_result(
    skills: list[dict[str, Any]],
    existing: list[dict[str, Any]] | None,
    skill_index: int,
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    """把单个 Stage 1b Skill 输出合并进已有列表，并保持原始 Skill 顺序。

    Web 调试页允许只重跑某一个 skill 的 1b 子阶段。这里用 skill_id/path 做
    稳定匹配，避免只靠列表长度导致重跑后顺序漂移。
    """
    merged: list[dict[str, Any] | None] = [None] * len(skills)
    existing_items = existing if isinstance(existing, list) else []
    for item in existing_items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("skill_id")
        item_path = item.get("path")
        for index, skill in enumerate(skills):
            if merged[index] is not None:
                continue
            if item_id and item_id == skill.get("skill_id"):
                merged[index] = item
                break
            if item_path and item_path == skill.get("path"):
                merged[index] = item
                break
    if 0 <= skill_index < len(merged):
        merged[skill_index] = result
    return [item for item in merged if item is not None]


def collect_stage3_outputs(output_dir: Path, bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """按输入轨迹顺序收集 Stage 3 的逐轨迹 parsed 输出。"""
    outputs: list[dict[str, Any]] = []
    for index, traj in enumerate(bundle.get("failed_trajectories") or []):
        name = stage_03_failure_event_extraction.stage_name(index, traj)
        parsed = parsed_transcript(output_dir, name)
        if parsed is None:
            parsed = parsed_transcript(output_dir, f"{name}-fallback")
        if isinstance(parsed, dict):
            outputs.append(parsed)
    saved = read_stage_output(output_dir, "stage_03_failure_events_by_trace")
    if isinstance(saved, list) and len(saved) > len(outputs):
        return saved
    return outputs


def collect_stage5_outputs(
    output_dir: Path,
    bundle: dict[str, Any],
    stage2: dict[str, Any],
) -> list[dict[str, Any]]:
    """收集 Stage 5 逐轨迹响应，并用本地公式补齐节点状态。"""
    outputs: list[dict[str, Any]] = []
    for index, trajectory in enumerate(bundle.get("failed_trajectories") or []):
        name = stage_05_node_execution_assessment.stage_name(index, trajectory)
        parsed = parsed_transcript(output_dir, name)
        if isinstance(parsed, dict):
            outputs.append(stage_05_node_execution_assessment.postprocess_trace_assessment(parsed, trajectory, stage2))
    saved = read_stage_output(output_dir, "stage_05_node_execution_assessments")
    if isinstance(saved, list) and len(saved) > len(outputs):
        return saved
    return outputs


def collect_stage6_node_outputs(output_dir: Path, node_inputs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """按需修复 node 顺序收集 Stage 6 的逐 node parsed 输出。"""
    outputs: list[dict[str, Any]] = []
    done_inputs: list[dict[str, Any]] = []
    for index, node_input in enumerate(node_inputs):
        name = stage_06_skill_repair_suggestions.stage_name(index, node_input)
        parsed = parsed_transcript(output_dir, name)
        if isinstance(parsed, dict):
            outputs.append(stage_06_skill_repair_suggestions._normalize_node_response(parsed, node_input))
            done_inputs.append(node_input)
    if not outputs and node_inputs:
        return None
    return stage_06_skill_repair_suggestions.compose_result(done_inputs, outputs)


def collect_stage1_outputs(output_dir: Path, bundle: dict[str, Any]) -> dict[str, Any]:
    """收集拆分版 Stage 1 的 1a/1b 输出，并组合成本地 Stage 1 快照。"""
    task = parsed_transcript(output_dir, stage_01_input_standardization.TASK_STAGE_NAME)
    saved_task = read_stage_output(output_dir, "stage_01a_task_description_standardization")
    if not isinstance(task, dict):
        task = saved_task

    skills: list[dict[str, Any]] = []
    for index, skill in enumerate(bundle.get("skill_library") or []):
        name = stage_01_input_standardization.skill_stage_name(index, skill)
        parsed = parsed_transcript(output_dir, name)
        if isinstance(parsed, dict):
            skills.append(parsed)
    saved_skills = read_stage_output(output_dir, "stage_01b_skill_standardizations")
    if isinstance(saved_skills, list) and len(saved_skills) > len(skills):
        skills = saved_skills

    final = read_stage_output(output_dir, "stage_01_input_standardization")
    if final is None and isinstance(task, dict):
        final = stage_01_input_standardization.compose_result(task, skills)

    out: dict[str, Any] = {}
    if isinstance(task, dict):
        out["stage_01a_task_description_standardization"] = task
    if skills:
        out["stage_01b_skill_standardizations"] = skills
    if isinstance(final, dict):
        out["stage_01_input_standardization"] = final
    return out


def collect_stage_outputs(output_dir: Path) -> dict[str, Any]:
    """从 transcript 和 individual output 中汇总所有已完成 stage。"""
    bundle = read_json(output_dir / "input_bundle.json") or {}
    out: dict[str, Any] = {}
    out.update(collect_stage1_outputs(output_dir, bundle))
    # Stage 3 是逐轨迹并行输出；后续 Stage 5 的本地重组需要先拿到完整
    # failure-events 列表，因此它必须早于 Stage 5 reconstruction 进入 out。
    stage3 = collect_stage3_outputs(output_dir, bundle)
    if stage3:
        out["stage_03_failure_events_by_trace"] = stage3
    transcript_names = {
        "stage_02_capability_graph": "stage-02-capability-graph",
        "stage_04_failure_event_alignment": "stage-04-failure-event-alignment",
    }
    for key, transcript_name in transcript_names.items():
        saved = read_stage_output(output_dir, key)
        parsed = parsed_transcript(output_dir, transcript_name)
        # Stage 2 会把 LLM parsed response 经过本地公式补齐 coverage 字段；
        # 因此优先使用 saved stage output，避免被原始 parsed response 覆盖。
        if key == "stage_02_capability_graph" and saved is not None:
            value = saved
        else:
            value = parsed if isinstance(parsed, dict) else saved
        if key == "stage_02_capability_graph":
            value = normalize_stage2_output(output_dir, value)
        if value is not None:
            out[key] = value
    if "stage_02_capability_graph" in out:
        stage5 = collect_stage5_outputs(output_dir, bundle, out["stage_02_capability_graph"])
        if stage5:
            out["stage_05_node_execution_assessments"] = stage5
    saved_stage6 = read_stage_output(output_dir, "stage_06_skill_repair_suggestions")
    if saved_stage6 is not None:
        out["stage_06_skill_repair_suggestions"] = saved_stage6
    else:
        # 完整 pipeline 可能在 Stage 7 中途失败，导致 Stage 5 没来得及保存成
        # individual output，但逐 node transcript 已经存在。这里按同一套本地
        # node-input 规则重组 Stage 5，保证 Stage 7 可以从已有事务状态续跑。
        try:
            required_keys = [
                "stage_01b_skill_standardizations",
                "stage_02_capability_graph",
                "stage_03_failure_events_by_trace",
                "stage_04_failure_event_alignment",
                "stage_05_node_execution_assessments",
            ]
            if all(key in out for key in required_keys):
                node_inputs = stage_06_skill_repair_suggestions.prepare_node_inputs(
                    bundle,
                    out["stage_02_capability_graph"],
                    out["stage_01b_skill_standardizations"],
                    out["stage_03_failure_events_by_trace"],
                    out["stage_04_failure_event_alignment"],
                    out["stage_05_node_execution_assessments"],
                )
                rebuilt_stage6 = collect_stage6_node_outputs(output_dir, node_inputs)
                if rebuilt_stage6 is not None:
                    out["stage_06_skill_repair_suggestions"] = rebuilt_stage6
        except Exception:
            # status/prompt 页面不应因为一个历史运行目录的半成品输出而整体崩溃；
            # 具体 run 命令仍会通过 require(...) 给出明确缺失依赖。
            pass
    saved_stage7 = read_stage_output(output_dir, "stage_07_repair_action_merge")
    if saved_stage7 is not None:
        out["stage_07_repair_action_merge"] = saved_stage7
    # Stage 7 包含多个动态命名的 repair/review transcript，不能用单一 parsed
    # 文件聚合；持久化状态文件才是这一阶段唯一可信的结构化输出。
    transactional = read_stage_output(output_dir, "stage_08_transactional_skill_repair")
    if isinstance(transactional, dict):
        out["stage_08_transactional_skill_repair"] = transactional
    return out


def require(outputs: dict[str, Any], key: str) -> Any:
    """读取前序 stage 输出；缺失时给出清晰错误。"""
    if key not in outputs:
        stage = STAGE_BY_KEY.get(key)
        label = stage["label"] if stage else key
        raise SystemExit(f"Missing dependency: {label}. Run it first.")
    return outputs[key]


def require_agent_only_stage3(outputs: dict[str, Any]) -> Any:
    """拒绝复用可能接触过 verifier 数据的旧 Stage 3 输出。"""
    stage3 = require(outputs, "stage_03_failure_events_by_trace")
    items = stage3 if isinstance(stage3, list) else []
    if not items or any(
        not isinstance(item, dict)
        or item.get("evidence_policy_version") != AGENT_ONLY_EVIDENCE_POLICY_VERSION
        for item in items
    ):
        raise SystemExit(
            "Stage 3 output is missing the agent-only evidence policy marker. "
            "Rerun Stage 3 before generating or running any later stage."
        )
    return stage3


def write_stage_prompt(
    config: OfflineSkillRCAConfig,
    stage_id: str,
    bundle: dict[str, Any],
    outputs: dict[str, Any],
    trace_index: int | None = None,
) -> list[str]:
    """只生成某个 stage 的 prompt，不调用 repair LLM。"""
    prompts: list[str] = []
    if stage_id in {stage["id"] for stage in STAGES[3:]}:
        require_agent_only_stage3(outputs)
    if stage_id == "stage-01-input-standardization":
        skills = list(bundle.get("skill_library") or [])
        indexes = [trace_index] if trace_index is not None else list(range(len(skills) + 1))
        for index in indexes:
            if index == 0:
                prompt = stage_01_input_standardization.build_task_prompt(bundle, config.max_prompt_chars)
                write_prompt_file(config, stage_01_input_standardization.TASK_STAGE_NAME, prompt)
                prompts.append(f"prompts/{stage_01_input_standardization.TASK_STAGE_NAME}.prompt.txt")
            elif 1 <= index <= len(skills):
                skill = skills[index - 1]
                name = stage_01_input_standardization.skill_stage_name(index - 1, skill)
                prompt = stage_01_input_standardization.build_skill_prompt(skill, config.max_prompt_chars)
                write_prompt_file(config, name, prompt)
                prompts.append(f"prompts/{name}.prompt.txt")
            else:
                raise SystemExit(f"Stage 1 child index out of range: {index}")
    elif stage_id == "stage-02-capability-graph":
        prompt = stage_02_capability_graph.build_prompt(
            bundle,
            require(outputs, "stage_01a_task_description_standardization"),
            require(outputs, "stage_01b_skill_standardizations"),
            config.max_prompt_chars,
        )
        write_prompt_file(config, stage_02_capability_graph.STAGE_NAME, prompt)
        prompts.append(f"prompts/{stage_02_capability_graph.STAGE_NAME}.prompt.txt")
    elif stage_id == "stage-03-failure-event-extraction":
        trajectories = list(bundle.get("failed_trajectories") or [])
        indexes = [trace_index] if trace_index is not None else list(range(len(trajectories)))
        for index in indexes:
            if index < 0 or index >= len(trajectories):
                raise SystemExit(f"trace index out of range: {index}")
            traj = trajectories[index]
            stage_traj = stage_03_failure_event_extraction.stage3_trajectory_input(config, traj)
            name = stage_03_failure_event_extraction.stage_name(index, stage_traj)
            prompt = stage_03_failure_event_extraction.build_prompt(
                bundle,
                require(outputs, "stage_01a_task_description_standardization"),
                outputs.get("stage_01b_skill_standardizations") or [],
                outputs.get("stage_02_capability_graph") or {},
                stage_traj,
                config.max_prompt_chars,
            )
            write_prompt_file(config, name, prompt)
            prompts.append(f"prompts/{name}.prompt.txt")
    elif stage_id == "stage-04-failure-event-alignment":
        prompt = stage_04_failure_event_alignment.build_prompt(
            bundle,
            require(outputs, "stage_02_capability_graph"),
            require(outputs, "stage_03_failure_events_by_trace"),
            config.max_prompt_chars,
        )
        write_prompt_file(config, stage_04_failure_event_alignment.STAGE_NAME, prompt)
        prompts.append(f"prompts/{stage_04_failure_event_alignment.STAGE_NAME}.prompt.txt")
    elif stage_id == "stage-05-node-execution-assessment":
        trajectories = list(bundle.get("failed_trajectories") or [])
        indexes = [trace_index] if trace_index is not None else list(range(len(trajectories)))
        for index in indexes:
            if index < 0 or index >= len(trajectories):
                raise SystemExit(f"Stage 5 trace index out of range: {index}")
            trajectory = trajectories[index]
            name = stage_05_node_execution_assessment.stage_name(index, trajectory)
            prompt = stage_05_node_execution_assessment.build_prompt(
                require(outputs, "stage_02_capability_graph"),
                trajectory,
                stage_05_node_execution_assessment._trace_analysis(
                    require(outputs, "stage_03_failure_events_by_trace"), trajectory.get("traj_id")
                ),
                stage_05_node_execution_assessment._trace_alignments(
                    require(outputs, "stage_04_failure_event_alignment"), trajectory.get("traj_id")
                ),
                config.max_prompt_chars,
            )
            write_prompt_file(config, name, prompt)
            prompts.append(f"prompts/{name}.prompt.txt")
    elif stage_id == "stage-06-skill-repair-suggestions":
        stage2 = require(outputs, "stage_02_capability_graph")
        skill_stage1 = require(outputs, "stage_01b_skill_standardizations")
        node_inputs = stage_06_skill_repair_suggestions.prepare_node_inputs(
            bundle,
            stage2,
            skill_stage1,
            require(outputs, "stage_03_failure_events_by_trace"),
            require(outputs, "stage_04_failure_event_alignment"),
            require(outputs, "stage_05_node_execution_assessments"),
        )
        indexes = [trace_index] if trace_index is not None else list(range(len(node_inputs)))
        for index in indexes:
            if index < 0 or index >= len(node_inputs):
                raise SystemExit(f"Stage 6 node index out of range: {index}")
            node_input = node_inputs[index]
            name = stage_06_skill_repair_suggestions.stage_name(index, node_input)
            prompt = stage_06_skill_repair_suggestions.build_node_prompt(
                bundle,
                stage2,
                skill_stage1,
                node_input,
                config.max_prompt_chars,
            )
            write_prompt_file(config, name, prompt)
            prompts.append(f"prompts/{name}.prompt.txt")
    elif stage_id == "stage-07-repair-action-merge":
        prompt = stage_07_repair_action_merge.build_prompt(
            config,
            require(outputs, "stage_06_skill_repair_suggestions"),
            config.max_prompt_chars,
        )
        write_prompt_file(config, stage_07_repair_action_merge.STAGE_NAME, prompt)
        prompts.append(f"prompts/{stage_07_repair_action_merge.STAGE_NAME}.prompt.txt")
    elif stage_id == "stage-08-transactional-skill-repair":
        # Prompt 预览只生成状态机的“下一次调用”，不会调用 LLM 或提交候选文件。
        # 首次预览会复制源技能库并初始化事务状态，这是 Stage 7 的必要准备步骤。
        stage7_bundle = {
            **bundle,
            "stage_01b_skill_standardizations": require(outputs, "stage_01b_skill_standardizations"),
        }
        name, prompt = stage_08_transactional_skill_repair.preview_next_prompt(
            config,
            stage7_bundle,
            require(outputs, "stage_07_repair_action_merge"),
        )
        if name and prompt:
            prompts.append(f"prompts/{name}.prompt.txt")
    else:
        raise SystemExit(f"Unknown stage: {stage_id}")
    return prompts


def prompt_only(args: argparse.Namespace) -> dict[str, Any]:
    """Web 调试页的“生成 Prompt”操作。"""
    output_dir = root_path(args.output_dir)
    manifest = load_manifest(output_dir)
    config = build_config(args, manifest)
    persist_runtime_options(config)
    bundle = load_bundle(config.output_dir)
    outputs = collect_stage_outputs(config.output_dir)
    prompts = write_stage_prompt(config, args.stage, bundle, outputs, args.trace_index)
    return {
        "ok": True,
        "prompts": prompts,
        "status": compact_command_status(config.output_dir, args.stage),
    }


def run_stage(args: argparse.Namespace) -> dict[str, Any]:
    """执行一个 stage。

    大多数 stage 会调用 repair LLM；Stage 5 按轨迹并行判断节点执行事实，
    Stage 6 按需修复节点并行生成 Skill 修复建议。
    """
    output_dir = root_path(args.output_dir)
    manifest = load_manifest(output_dir)
    config = build_config(args, manifest)
    persist_runtime_options(config)
    if not config.strong_api_key:
        raise SystemExit("Missing strong-model API key for stage run.")
    bundle = load_bundle(config.output_dir)
    outputs = collect_stage_outputs(config.output_dir)
    stage_id = args.stage
    if stage_id in {stage["id"] for stage in STAGES[3:]}:
        require_agent_only_stage3(outputs)
    if stage_id == "stage-01-input-standardization":
        skills = list(bundle.get("skill_library") or [])
        if args.trace_index is None:
            result = stage_01_input_standardization.run(config, bundle)
            task_result = result.get("stage_01a_task_description_standardization")
            skill_results = result.get("stage_01b_skill_standardizations")
            if isinstance(task_result, dict):
                save_stage_output(config, "stage_01a_task_description_standardization", task_result)
            if isinstance(skill_results, list):
                save_stage_output(config, "stage_01b_skill_standardizations", skill_results)
            save_stage_output(config, "stage_01_input_standardization", result)
        else:
            index = int(args.trace_index)
            if index == 0:
                result = stage_01_input_standardization.run_task(config, bundle)
                save_stage_output(config, "stage_01a_task_description_standardization", result)
                existing = outputs.get("stage_01b_skill_standardizations")
                if isinstance(existing, list) and len(existing) >= len(skills):
                    save_stage_output(config, "stage_01_input_standardization", stage_01_input_standardization.compose_result(result, existing))
            elif 1 <= index <= len(skills):
                skill_index = index - 1
                result = stage_01_input_standardization.run_skill_one(config, skill_index, skills[skill_index])
                existing = outputs.get("stage_01b_skill_standardizations")
                merged = merge_stage1_skill_result(skills, existing if isinstance(existing, list) else [], skill_index, result)
                save_stage_output(config, "stage_01b_skill_standardizations", merged)
                task_result = outputs.get("stage_01a_task_description_standardization")
                if isinstance(task_result, dict) and len(merged) >= len(skills):
                    save_stage_output(config, "stage_01_input_standardization", stage_01_input_standardization.compose_result(task_result, merged))
            else:
                raise SystemExit(f"Stage 1 child index out of range: {index}")
    elif stage_id == "stage-02-capability-graph":
        result = stage_02_capability_graph.run(
            config,
            bundle,
            require(outputs, "stage_01a_task_description_standardization"),
            require(outputs, "stage_01b_skill_standardizations"),
        )
        save_stage_output(config, "stage_02_capability_graph", result)
    elif stage_id == "stage-03-failure-event-extraction":
        trajectories = list(bundle.get("failed_trajectories") or [])
        task_stage1 = require(outputs, "stage_01a_task_description_standardization")
        skill_stage1 = outputs.get("stage_01b_skill_standardizations") or []
        stage2 = outputs.get("stage_02_capability_graph") or {}
        if args.trace_index is None:
            result = stage_03_failure_event_extraction.run(config, bundle, task_stage1, skill_stage1, stage2)
        else:
            index = int(args.trace_index)
            if index < 0 or index >= len(trajectories):
                raise SystemExit(f"trace index out of range: {index}")
            stage_03_failure_event_extraction.run_one(config, bundle, task_stage1, skill_stage1, stage2, index, trajectories[index])
            result = collect_stage3_outputs(config.output_dir, bundle)
        save_stage_output(config, "stage_03_failure_events_by_trace", result)
    elif stage_id == "stage-04-failure-event-alignment":
        result = stage_04_failure_event_alignment.run(
            config,
            bundle,
            require(outputs, "stage_02_capability_graph"),
            require(outputs, "stage_03_failure_events_by_trace"),
        )
        save_stage_output(config, "stage_04_failure_event_alignment", result)
    elif stage_id == "stage-05-node-execution-assessment":
        trajectories = list(bundle.get("failed_trajectories") or [])
        stage2 = require(outputs, "stage_02_capability_graph")
        stage3 = require(outputs, "stage_03_failure_events_by_trace")
        stage4 = require(outputs, "stage_04_failure_event_alignment")
        if args.trace_index is None:
            result = stage_05_node_execution_assessment.run(config, bundle, stage2, stage3, stage4)
        else:
            index = int(args.trace_index)
            if index < 0 or index >= len(trajectories):
                raise SystemExit(f"Stage 5 trace index out of range: {index}")
            stage_05_node_execution_assessment.run_one(config, stage2, stage3, stage4, index, trajectories[index])
            result = collect_stage5_outputs(config.output_dir, bundle, stage2)
        save_stage_output(config, "stage_05_node_execution_assessments", result)
    elif stage_id == "stage-06-skill-repair-suggestions":
        stage2 = require(outputs, "stage_02_capability_graph")
        skill_stage1 = require(outputs, "stage_01b_skill_standardizations")
        if args.trace_index is None:
            result = stage_06_skill_repair_suggestions.run(
                config,
                bundle,
                stage2,
                skill_stage1,
                require(outputs, "stage_03_failure_events_by_trace"),
                require(outputs, "stage_04_failure_event_alignment"),
                require(outputs, "stage_05_node_execution_assessments"),
            )
        else:
            node_inputs = stage_06_skill_repair_suggestions.prepare_node_inputs(
                bundle,
                stage2,
                skill_stage1,
                require(outputs, "stage_03_failure_events_by_trace"),
                require(outputs, "stage_04_failure_event_alignment"),
                require(outputs, "stage_05_node_execution_assessments"),
            )
            index = int(args.trace_index)
            if index < 0 or index >= len(node_inputs):
                raise SystemExit(f"Stage 6 node index out of range: {index}")
            stage_06_skill_repair_suggestions.run_one(config, bundle, stage2, skill_stage1, index, node_inputs[index])
            result = collect_stage6_node_outputs(config.output_dir, node_inputs) or stage_06_skill_repair_suggestions.compose_result(node_inputs, [])
        save_stage_output(config, "stage_06_skill_repair_suggestions", result)
    elif stage_id == "stage-07-repair-action-merge":
        result = stage_07_repair_action_merge.run(
            config,
            require(outputs, "stage_06_skill_repair_suggestions"),
        )
        save_stage_output(config, "stage_07_repair_action_merge", result)
    elif stage_id == "stage-08-transactional-skill-repair":
        # debug 单步模式每次只执行 repair 或 review 中的一次 LLM 调用；连续模式
        # 则在同一后台进程内循环，仍会在每个调用后原子保存状态。
        mode = str(getattr(args, "stage_run_mode", None) or "step")
        stage7_bundle = {
            **bundle,
            "stage_01b_skill_standardizations": require(outputs, "stage_01b_skill_standardizations"),
        }
        result = stage_08_transactional_skill_repair.run(
            config,
            stage7_bundle,
            require(outputs, "stage_07_repair_action_merge"),
            mode=mode,
            max_operations=config.stage7_max_operations,
        )
        save_stage_output(config, "stage_08_transactional_skill_repair", result)
    else:
        raise SystemExit(f"Unknown stage: {stage_id}")
    return {
        "ok": True,
        "stage": stage_id,
        "status": compact_command_status(config.output_dir, stage_id),
    }


def calculate_stage(args: argparse.Namespace) -> dict[str, Any]:
    """对需要公式计算的 stage 执行本地后处理。

    这个命令不调用 repair LLM。它只读取已有 parsed response 或 stage output，
    用 ``src.calculations`` 中的确定性公式补齐计算字段，然后保存为对应 stage
    output，供 debug 页面和后续 stage 使用。
    """
    output_dir = root_path(args.output_dir)
    manifest = load_manifest(output_dir)
    config = build_config(args, manifest)
    bundle = load_bundle(config.output_dir)
    outputs = collect_stage_outputs(config.output_dir)
    stage_id = args.stage

    if stage_id == "stage-02-capability-graph":
        raw = parsed_transcript(config.output_dir, stage_02_capability_graph.STAGE_NAME)
        source = "parsed_response"
        if raw is None:
            raw = outputs.get("stage_02_capability_graph")
            source = "stage_output"
        if not isinstance(raw, dict):
            raise SystemExit("Missing Stage 2 parsed response or output to calculate.")
        result = postprocess_skill_coverage(raw)
        result["node_coverage_summary"] = summarize_node_coverage(result)
        save_stage_output(config, "stage_02_capability_graph", result)
        return {
            "ok": True,
            "stage": stage_id,
            "calculated": True,
            "source": source,
            "output": "stage_outputs_individual/stage_02_capability_graph.json",
            "status": compact_command_status(config.output_dir, stage_id),
        }

    if stage_id == "stage-05-node-execution-assessment":
        stage2 = require(outputs, "stage_02_capability_graph")
        result = collect_stage5_outputs(config.output_dir, bundle, stage2)
        if not result:
            raise SystemExit("Missing Stage 5 parsed responses to calculate.")
        save_stage_output(config, "stage_05_node_execution_assessments", result)
        return {
            "ok": True,
            "stage": stage_id,
            "calculated": True,
            "source": "per-trajectory parsed responses",
            "output": "stage_outputs_individual/stage_05_node_execution_assessments.json",
            "status": compact_command_status(config.output_dir, stage_id),
        }

    raise SystemExit(f"Stage does not have a local calculation step: {stage_id}")


def finalize_run(args: argparse.Namespace) -> dict[str, Any]:
    """导出已完成 Stage 8 的报告，不再二次应用 Skills。"""
    output_dir = root_path(args.output_dir)
    manifest = load_manifest(output_dir)
    config = build_config(args, manifest)
    outputs = collect_stage_outputs(config.output_dir)
    stage8_state = require(outputs, "stage_08_transactional_skill_repair")
    if stage8_state.get("status") != "completed":
        raise SystemExit("Stage 8 is not completed; continue the transactional repair first.")
    analysis = normalize_analysis(stage_08_transactional_skill_repair.final_analysis(stage8_state))
    enrich_analysis_from_stage_outputs(analysis, outputs)
    trace_analyses = outputs.get("stage_03_failure_events_by_trace") or []
    analysis["metadata"] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "method": "Offline SkillRCA v2 explicit-stage web-debug",
        "taskDir": rel(config.task_dir),
        "sourceSkillsDir": rel(config.skills_dir),
        "outputSkillsDir": rel(config.output_skills_dir),
        "strongModel": config.strong_model,
        "traceCount": len(trace_analyses),
        "traceAnalysisWorkers": config.trace_analysis_workers,
        "stageCount": len(STAGES),
        "visibleInputs": [
            "task_description",
            "skill_library",
            "failed_trajectories",
            "success_0_1",
            "visible_failure_results",
            "captured_final_artifacts",
        ],
        "hiddenInputsExcluded": [
            "verifier source and test source",
            "verifier rubric and hidden scoring instructions",
            "verifier metrics",
            "local static skill scoring",
        ],
    }
    analysis["trace_analyses"] = trace_analyses
    analysis["stage_outputs"] = dict(outputs)
    write_outputs(config, analysis)
    # Stage 8 已经逐建议提交到工作副本，这里只写报告和兼容 JSON。
    applied = True
    write_json(config.output_dir / "stage_outputs.json", outputs)
    return {
        "ok": True,
        "applied": applied,
        "outputDir": rel(config.output_dir),
        "outputSkillsDir": rel(config.output_skills_dir),
        "status": compact_command_status(config.output_dir, "stage-08-transactional-skill-repair"),
    }


def path_info(path: Path) -> dict[str, Any] | None:
    """返回文件存在性、大小和更新时间。"""
    try:
        stat = path.stat()
        if not path.is_file():
            return None
        return {"path": rel(path), "size": stat.st_size, "modifiedAt": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()}
    except OSError:
        return None


def transcript_files(output_dir: Path, name: str) -> dict[str, Any]:
    """列出某个 transcript name 关联的所有调试文件。"""
    prompt = output_dir / "prompts" / f"{name}.prompt.txt"
    transcript_dir = output_dir / "llm_transcript"
    error_files = []
    if transcript_dir.exists():
        for item in sorted(transcript_dir.glob(f"{name}*.json")):
            if any(token in item.name for token in ["error", "non-json", "parse-error"]):
                info = path_info(item)
                if info:
                    error_files.append(info)
    return {
        "prompt": path_info(prompt),
        "request": path_info(transcript_dir / f"{name}.request.json"),
        "response": path_info(transcript_dir / f"{name}.response.json"),
        "parsed": path_info(transcript_dir / f"{name}.parsed.json"),
        "errors": error_files,
    }


def stage_status_from_files(files: dict[str, Any], output_exists: bool = False) -> str:
    """根据文件状态推导 Web 上显示的 stage 状态。"""
    if files.get("errors") and not files.get("parsed") and not output_exists:
        return "error"
    if files.get("parsed") or output_exists:
        return "done"
    if files.get("response"):
        return "response"
    if files.get("request"):
        return "running-or-requested"
    if files.get("prompt"):
        return "prompted"
    return "missing"


def aggregate_child_status(children: list[dict[str, Any]], final_output_exists: bool = False) -> str:
    """把子阶段状态折叠成父 stage 状态。"""
    if final_output_exists:
        return "done"
    if not children:
        return "missing"
    done = sum(1 for child in children if child.get("status") == "done")
    if done == len(children):
        return "done"
    if done:
        return "partial"
    if any(child.get("status") == "error" for child in children):
        return "error"
    if any(child.get("status") == "response" for child in children):
        return "response"
    if any(child.get("status") == "running-or-requested" for child in children):
        return "running-or-requested"
    if any(child.get("status") == "prompted" for child in children):
        return "prompted"
    return "missing"


def saved_stage1_skill_exists(saved_skills: Any, skill: dict[str, Any], index: int) -> bool:
    """判断某个 skill 的 Stage 1b 结果是否已保存。"""
    if not isinstance(saved_skills, list):
        return False
    for item_index, item in enumerate(saved_skills):
        if not isinstance(item, dict):
            continue
        if item.get("skill_id") and item.get("skill_id") == skill.get("skill_id"):
            return True
        if item.get("path") and item.get("path") == skill.get("path"):
            return True
        if item_index == index and (item.get("skill_id") or item.get("title") or item.get("path")):
            return True
    return False


def collect_stage1_children(output_dir: Path, bundle: dict[str, Any], outputs: dict[str, Any]) -> list[dict[str, Any]]:
    """为 Web 状态表构造 Stage 1 的 1a/1b 子阶段。"""
    children: list[dict[str, Any]] = []
    task_files = transcript_files(output_dir, stage_01_input_standardization.TASK_STAGE_NAME)
    task_output = stage_output_info(output_dir, "stage_01a_task_description_standardization")
    children.append(
        {
            "index": 0,
            "kind": "task",
            "name": stage_01_input_standardization.TASK_STAGE_NAME,
            "label": "Stage 1a 任务描述标准化",
            "template": "stage-01a-task-description-standardization.txt",
            "status": stage_status_from_files(task_files, output_exists=bool(task_output)),
            "files": task_files,
            "output": task_output,
        }
    )

    saved_skills = outputs.get("stage_01b_skill_standardizations")
    skills = list(bundle.get("skill_library") or [])
    for index, skill in enumerate(skills):
        name = stage_01_input_standardization.skill_stage_name(index, skill)
        files = transcript_files(output_dir, name)
        skill_output_exists = saved_stage1_skill_exists(saved_skills, skill, index)
        children.append(
            {
                "index": index + 1,
                "kind": "skill",
                "skillIndex": index,
                "name": name,
                "label": f"Stage 1b Skill {index + 1:02d}: {skill.get('skill_id') or skill.get('title') or index + 1}",
                "skillId": skill.get("skill_id"),
                "skillPath": skill.get("path"),
                "template": "stage-01b-skill-standardization.txt",
                "status": stage_status_from_files(files, output_exists=skill_output_exists),
                "files": files,
                "output": stage_output_info(output_dir, "stage_01b_skill_standardizations") if skill_output_exists else None,
            }
        )

    return children


def collect_stage7_children(output_dir: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    """把 Stage 7 的每次 repair/review LLM 调用展示成一个可审阅子阶段。"""
    suggestion_by_index = {
        int(item.get("index") or 0): item
        for item in state.get("suggestions") or []
        if isinstance(item, dict)
    }
    children: list[dict[str, Any]] = []
    for interaction in state.get("interactions") or []:
        if not isinstance(interaction, dict):
            continue
        name = str(interaction.get("name") or "")
        files = transcript_files(output_dir, name)
        raw_status = str(interaction.get("status") or "")
        if raw_status == "done":
            status = "done"
        elif raw_status == "error":
            status = "error"
        elif raw_status == "interrupted":
            status = "interrupted"
        else:
            status = stage_status_from_files(files)
        suggestion_index = int(interaction.get("suggestion_index") or 0)
        suggestion = suggestion_by_index.get(suggestion_index) or {}
        operation = str(interaction.get("operation") or "")
        operation_label = "生成候选" if operation == "repair" else "LLM 审查"
        attempt_number = int(interaction.get("attempt_number") or 1)
        suggestion_ids = interaction.get("suggestion_ids") or suggestion.get("suggestion_ids") or [interaction.get("suggestion_id")]
        suggestion_ids = [str(value) for value in suggestion_ids if value]
        unit_label = f"建议包 {len(suggestion_ids)} 条" if len(suggestion_ids) > 1 else "建议"
        children.append(
            {
                "index": int(interaction.get("sequence") or len(children) + 1),
                "kind": operation,
                "name": name,
                "label": (
                    f"{unit_label} {suggestion_index + 1:02d} · 尝试 {attempt_number:02d} · "
                    f"{operation_label}: {interaction.get('suggestion_id') or ''}"
                ),
                "suggestionIndex": suggestion_index,
                "suggestionId": interaction.get("suggestion_id"),
                "suggestionIds": suggestion_ids,
                "repairUnitMode": suggestion.get("repair_unit_mode") or interaction.get("repair_unit_mode") or "single_suggestion",
                "nodeId": interaction.get("node_id"),
                "repairAction": interaction.get("action"),
                "operation": operation,
                "attemptNumber": attempt_number,
                "reviewDecision": interaction.get("review_decision"),
                "error": interaction.get("error"),
                # 历史状态里可能记录旧 Stage 6 模板名。文件区仍保留原始 prompt，
                # 但模板编辑入口统一指向当前 Stage 7 模板，避免页面出现两套编号。
                "template": (
                    stage_08_transactional_skill_repair.REPAIR_TEMPLATE
                    if operation == "repair"
                    else stage_08_transactional_skill_repair.REVIEW_TEMPLATE
                ),
                "status": status,
                "files": files,
                "evidence": path_info(root_path(str(interaction.get("evidence_archive") or "")))
                if interaction.get("evidence_archive")
                else None,
                "output": path_info(root_path(str(interaction.get("candidate_archive") or "")))
                if interaction.get("candidate_archive")
                else None,
                "sourceSuggestion": suggestion.get("source_suggestion"),
            }
        )
    return children


def stage7_template_children(state: dict[str, Any]) -> list[dict[str, Any]]:
    """固定展示 Stage 7 的 repair/review 两套可编辑模板。

    Review 模板不应等到首次候选生成后才出现，否则用户无法在运行 Stage 7 前
    调整审查标准。模板子项与实际 interaction 子项并列，并使用字符串索引避免
    和 interaction sequence 冲突。
    """
    preview_operation = str(state.get("next_preview_operation") or "")
    prompt_info = (
        path_info(root_path(str(state.get("next_prompt_archive") or "")))
        if state.get("next_prompt_archive")
        else None
    )
    evidence_info = (
        path_info(root_path(str(state.get("next_evidence_archive") or "")))
        if state.get("next_evidence_archive")
        else None
    )

    def template_child(operation: str, child_id: str, label: str, template: str) -> dict[str, Any]:
        matches_preview = preview_operation == operation
        return {
            "index": f"template-{operation}",
            "kind": "template",
            "operation": operation,
            "id": child_id,
            "name": f"{child_id}-template",
            "label": label,
            "template": template,
            "status": "prompted" if matches_preview and prompt_info else "template",
            "files": {"prompt": prompt_info} if matches_preview and prompt_info else {},
            "evidence": evidence_info if matches_preview else None,
        }

    return [
        template_child("repair", "stage-08-skill-repair", "Stage 8 模板 · 生成候选文件", stage_08_transactional_skill_repair.REPAIR_TEMPLATE),
        template_child("review", "stage-08-skill-review", "Stage 8 模板 · 审查候选文件", stage_08_transactional_skill_repair.REVIEW_TEMPLATE),
    ]


def collect_status(output_dir: Path) -> dict[str, Any]:
    """生成 Web 调试页需要的运行状态摘要。"""
    manifest = load_manifest(output_dir)
    bundle = read_json(output_dir / "input_bundle.json") or {}
    outputs = collect_stage_outputs(output_dir) if output_dir.exists() else {}
    stage_rows = []
    for stage in STAGES:
        if stage["id"] == "stage-01-input-standardization":
            children = collect_stage1_children(output_dir, bundle, outputs)
            done = sum(1 for child in children if child["status"] == "done")
            final_output = stage_output_info(output_dir, stage["key"])
            stage_rows.append(
                {
                    **stage,
                    "status": aggregate_child_status(children, final_output_exists=bool(final_output)),
                    "doneCount": done,
                    "totalCount": len(children),
                    "depsReady": True,
                    "children": children,
                    "files": {},
                    "output": final_output,
                }
            )
            continue
        if stage["id"] == "stage-03-failure-event-extraction":
            children = []
            for index, traj in enumerate(bundle.get("failed_trajectories") or []):
                name = stage_03_failure_event_extraction.stage_name(index, traj)
                files = transcript_files(output_dir, name)
                fallback_files = transcript_files(output_dir, f"{name}-fallback")
                parsed = files.get("parsed") or fallback_files.get("parsed")
                status = stage_status_from_files({**files, "parsed": parsed, "errors": files.get("errors", []) + fallback_files.get("errors", [])})
                children.append(
                    {
                        "index": index,
                        "kind": "trajectory",
                        "name": name,
                        "label": f"Stage 3 trajectory {index + 1:02d}: {traj.get('traj_id')}",
                        "trajId": traj.get("traj_id"),
                        "stepCount": len(traj.get("steps") or []),
                        "success": traj.get("success"),
                        "template": "stage-03-failure-event-extraction.txt",
                        "status": status,
                        "files": files,
                        "fallbackFiles": fallback_files,
                    }
                )
            done = sum(1 for child in children if child["status"] == "done")
            if children and done == len(children):
                status = "done"
            elif done:
                status = "partial"
            elif any(child["status"] == "error" for child in children):
                status = "error"
            elif any(child["status"] == "prompted" for child in children):
                status = "prompted"
            else:
                status = "missing"
            stage_rows.append(
                {
                    **stage,
                    "status": status,
                    "doneCount": done,
                    "totalCount": len(children),
                    "depsReady": all(dep in outputs for dep in stage["deps"]),
                    "children": children,
                    "output": stage_output_info(output_dir, stage["key"]),
                }
            )
            continue
        if stage["id"] == "stage-05-node-execution-assessment":
            children = []
            for index, trajectory in enumerate(bundle.get("failed_trajectories") or []):
                name = stage_05_node_execution_assessment.stage_name(index, trajectory)
                files = transcript_files(output_dir, name)
                children.append(
                    {
                        "index": index,
                        "kind": "trajectory",
                        "name": name,
                        "label": f"Stage 5 trajectory {index + 1:02d}: {trajectory.get('traj_id')}",
                        "trajId": trajectory.get("traj_id"),
                        "stepCount": len(trajectory.get("steps") or []),
                        "success": trajectory.get("success"),
                        "template": "stage-05-node-execution-assessment.txt",
                        "status": stage_status_from_files(files),
                        "files": files,
                    }
                )
            done = sum(1 for child in children if child["status"] == "done")
            output_info = stage_output_info(output_dir, stage["key"])
            stage_rows.append(
                {
                    **stage,
                    "status": aggregate_child_status(children, final_output_exists=bool(output_info)),
                    "doneCount": done,
                    "totalCount": len(children),
                    "depsReady": all(dep in outputs for dep in stage["deps"]),
                    "children": children,
                    "output": output_info,
                }
            )
            continue
        if stage["id"] == "stage-06-skill-repair-suggestions":
            deps_ready = all(dep in outputs for dep in stage["deps"])
            children = []
            if deps_ready:
                node_inputs = stage_06_skill_repair_suggestions.prepare_node_inputs(
                    bundle,
                    outputs["stage_02_capability_graph"],
                    outputs["stage_01b_skill_standardizations"],
                    outputs["stage_03_failure_events_by_trace"],
                    outputs["stage_04_failure_event_alignment"],
                    outputs["stage_05_node_execution_assessments"],
                )
                for index, node_input in enumerate(node_inputs):
                    name = stage_06_skill_repair_suggestions.stage_name(index, node_input)
                    files = transcript_files(output_dir, name)
                    children.append(
                        {
                            "index": index,
                            "kind": "node",
                            "name": name,
                            "label": f"Stage 6 node {index + 1:02d}: {node_input.get('node_id')}",
                            "nodeId": node_input.get("node_id"),
                            "localRecommendedAction": node_input.get("local_recommended_action"),
                            "nodeRepairAction": node_input.get("local_recommended_action"),
                            "targetSkillIds": node_input.get("target_skill_ids") or [],
                            "classification": node_input.get("classification") or {},
                            "nodeBoundEvidence": node_input.get("node_bound_evidence") or {},
                            "nodeRelatedSkillLibrary": node_input.get("node_related_skill_library") or [],
                            "template": "stage-06-skill-repair-suggestions.txt",
                            "status": stage_status_from_files(files),
                            "files": files,
                        }
                    )
            done = sum(1 for child in children if child["status"] == "done")
            output_info = stage_output_info(output_dir, stage["key"])
            if children and done == len(children):
                status = "done"
            elif done:
                status = "partial"
            elif any(child["status"] == "error" for child in children):
                status = "error"
            elif any(child["status"] == "prompted" for child in children):
                status = "prompted"
            elif deps_ready and not children and output_info:
                status = "done"
            else:
                status = "missing"
            stage_rows.append(
                {
                    **stage,
                    "status": status,
                    "doneCount": done,
                    "totalCount": len(children),
                    "depsReady": deps_ready,
                    "children": children,
                    "output": output_info,
                }
            )
            continue
        if stage["id"] == "stage-08-transactional-skill-repair":
            state = outputs.get("stage_08_transactional_skill_repair") or {}
            interactions = collect_stage7_children(output_dir, state) if isinstance(state, dict) else []
            children = stage7_template_children(state if isinstance(state, dict) else {}) + interactions
            raw_status = str(state.get("status") or "missing") if isinstance(state, dict) else "missing"
            status_map = {
                "completed": "done",
                "running": "running-or-requested",
                "paused": "paused",
                "pending": "prompted",
            }
            stage_rows.append(
                {
                    **stage,
                    "status": status_map.get(raw_status, raw_status),
                    "doneCount": int(state.get("accepted_count") or 0) if isinstance(state, dict) else 0,
                    "totalCount": int(state.get("suggestion_count") or 0) if isinstance(state, dict) else 0,
                    "depsReady": all(dep in outputs for dep in stage["deps"]),
                    "children": children,
                    "files": {
                        "prompt": path_info(root_path(str(state.get("next_prompt_archive") or "")))
                    }
                    if isinstance(state, dict) and state.get("next_prompt_archive")
                    else {},
                    "output": stage_output_info(output_dir, stage["key"]),
                    "nextOperation": state.get("next_operation") if isinstance(state, dict) else None,
                    "currentSuggestionIndex": state.get("current_suggestion_index") if isinstance(state, dict) else None,
                    "repairMode": state.get("repair_mode") if isinstance(state, dict) else None,
                    "skillPackageSize": state.get("skill_package_size") if isinstance(state, dict) else None,
                    "repairUnitCount": state.get("repair_unit_count") if isinstance(state, dict) else None,
                    "separateReviewLlm": bool(manifest.get("separateReviewLlm")),
                    "reviewBaseUrl": manifest.get("reviewBaseUrl") or "",
                    "reviewModel": manifest.get("reviewModel") or "",
                    "workingSkillsDir": state.get("working_skills_dir") if isinstance(state, dict) else None,
                    "pauseReason": state.get("pause_reason") if isinstance(state, dict) else None,
                    "lastRunOperationLimit": (state.get("last_until_complete_run") or {}).get("max_operations")
                    if isinstance(state, dict)
                    else None,
                    "lastRunOperationsExecuted": (state.get("last_until_complete_run") or {}).get("operations_executed")
                    if isinstance(state, dict)
                    else None,
                    "operationLimitReached": (state.get("last_until_complete_run") or {}).get("limit_reached")
                    if isinstance(state, dict)
                    else False,
                    "totalLlmOperations": state.get("interaction_sequence") if isinstance(state, dict) else 0,
                    "nextEvidence": path_info(root_path(str(state.get("next_evidence_archive") or "")))
                    if isinstance(state, dict) and state.get("next_evidence_archive")
                    else None,
                }
            )
            continue
        files = transcript_files(output_dir, stage["id"])
        output_info = stage_output_info(output_dir, stage["key"])
        stage_rows.append(
            {
                **stage,
                "status": stage_status_from_files(files, output_exists=bool(output_info)),
                "depsReady": all(dep in outputs for dep in stage["deps"]),
                "files": files,
                "output": output_info,
            }
        )
    return {
        "runDir": rel(output_dir),
        "exists": output_dir.exists(),
        "manifest": manifest,
        "traceSelection": read_json(output_dir / "trace_selection.json") or manifest.get("traceSelection") or [],
        "inputBundle": path_info(output_dir / "input_bundle.json"),
        "stageOutputs": path_info(output_dir / "stage_outputs.json"),
        "finalReport": path_info(output_dir / "diagnosis_report.md"),
        "appliedManifest": path_info(output_dir / "applied_repair_manifest.json"),
        "stages": stage_rows,
    }


def compact_command_status(output_dir: Path, stage_id: str | None = None) -> dict[str, Any]:
    """为后台命令 stdout 生成小型状态摘要。

    Web Server 会在子进程退出后通过 ``status`` 命令重新读取完整状态，因此
    prompt/run/calculate/finalize 没有必要把包含全部子阶段、文件和证据的对象再次
    打印到实时日志。完整对象在复杂 Stage 7 中可达到数千行；逐行 SSE 传输会让
    浏览器反复重绘大段日志，看起来像页面卡死。
    """
    status = collect_status(output_dir)
    stages = list(status.get("stages") or [])
    selected = next((row for row in stages if row.get("id") == stage_id), None)
    stage_summary = None
    if isinstance(selected, dict):
        stage_summary = {
            key: selected.get(key)
            for key in (
                "id",
                "status",
                "doneCount",
                "totalCount",
                "nextOperation",
                "currentSuggestionIndex",
                "repairMode",
                "skillPackageSize",
                "repairUnitCount",
                "pauseReason",
                "lastRunOperationLimit",
                "lastRunOperationsExecuted",
                "operationLimitReached",
                "totalLlmOperations",
            )
            if key in selected
        }
    return {
        "runDir": status.get("runDir"),
        "stage": stage_summary,
        "completedStageCount": sum(1 for row in stages if row.get("status") == "done"),
        "totalStageCount": len(stages),
    }


def status_cmd(args: argparse.Namespace) -> dict[str, Any]:
    """输出当前 stage 调试运行状态。"""
    output_dir = root_path(args.output_dir)
    if output_dir is None:
        raise SystemExit("--output-dir is required")
    return {"ok": True, "status": collect_status(output_dir)}


def pause_cmd(args: argparse.Namespace) -> dict[str, Any]:
    """持久化暂停 Stage 7，供 Web 在终止连续运行进程后调用。"""
    output_dir = root_path(args.output_dir)
    manifest = load_manifest(output_dir)
    config = build_config(args, manifest)
    state = stage_08_transactional_skill_repair.pause(config, "Paused by the Stage Debug user.")
    save_stage_output(config, "stage_08_transactional_skill_repair", state)
    return {
        "ok": True,
        "status": compact_command_status(config.output_dir, "stage-08-transactional-skill-repair"),
    }


def template_variable_defaults() -> dict[str, str]:
    """返回 Web 可编辑模板变量的默认值。

    这些默认值来自 pipeline/stage 里的 schema 函数。若用户在
    ``prompt_templates/variables`` 下保存了同名 ``.txt`` 文件，实际 prompt
    渲染会使用保存后的文件；这里仍保留默认值，便于 Web 一键恢复或首次编辑。
    """
    from src.pipeline import failure_event_alignment_schema, trace_analysis_schema
    values = {
        "task_standardization_schema": stage_01_input_standardization.task_schema(),
        "skill_standardization_schema": stage_01_input_standardization.skill_schema(),
        "stage2_schema": stage_02_capability_graph.stage_schema(),
        "trace_analysis_schema": trace_analysis_schema(),
        "failure_event_alignment_schema": failure_event_alignment_schema(),
        "node_execution_assessment_schema": stage_05_node_execution_assessment.assessment_schema(),
        "stage6_schema": stage_06_skill_repair_suggestions.stage6_schema(),
        "repair_action_merge_schema": stage_07_repair_action_merge.merge_schema(),
        "stage8_repair_schema": stage_08_transactional_skill_repair.repair_schema(),
        "stage8_review_schema": stage_08_transactional_skill_repair.review_schema(),
        "claude_code_skill_spec": stage_08_transactional_skill_repair.claude_code_skill_spec(),
    }
    return {
        key: value if isinstance(value, str) else json_block(value)
        for key, value in values.items()
    }


def template_variable_cmd(args: argparse.Namespace) -> dict[str, Any]:
    """读取某个模板变量的默认值和当前 override。"""
    name = args.name.strip()
    defaults = template_variable_defaults()
    if name not in defaults:
        raise SystemExit(f"Unknown template variable: {name}")
    path = PROMPT_VARIABLE_DIR / f"{name}.txt"
    saved = path.read_text(encoding="utf-8") if path.exists() else None
    return {
        "ok": True,
        "name": name,
        "path": rel(path),
        "exists": path.exists(),
        "defaultText": defaults[name],
        "text": saved if saved is not None else defaults[name],
    }


def parse_args() -> argparse.Namespace:
    """解析 stage debug CLI 参数。"""
    parser = argparse.ArgumentParser(description="Run Offline SkillRCA one stage at a time.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common_model(p: argparse.ArgumentParser) -> None:
        p.add_argument("--strong-base-url")
        p.add_argument("--strong-api-key")
        p.add_argument("--strong-model")
        p.add_argument(
            "--strong-reasoning-effort",
            choices=["off", "minimal", "low", "medium", "high", "max", "xhigh"],
        )
        p.add_argument("--use-separate-review-llm", dest="use_separate_review_llm", action="store_true")
        p.add_argument("--no-separate-review-llm", dest="use_separate_review_llm", action="store_false")
        p.set_defaults(use_separate_review_llm=None)
        p.add_argument("--review-base-url")
        p.add_argument("--review-api-key")
        p.add_argument("--review-model")
        p.add_argument(
            "--review-reasoning-effort",
            choices=["off", "minimal", "low", "medium", "high", "max", "xhigh"],
        )
        p.add_argument("--max-prompt-chars", type=int)
        p.add_argument("--trace-analysis-workers", type=int)
        p.add_argument("--add-skill-merge-threshold", type=int)
        p.add_argument("--add-skill-target-count", type=int)
        p.add_argument("--max-new-skills", type=int)
        p.add_argument("--skill-word-limit", type=int)
        p.add_argument("--stage7-max-operations", type=int)
        p.add_argument("--stage7-repair-mode", choices=["per_suggestion", "skill_package"])
        p.add_argument("--stage7-skill-package-size", type=int)
        p.add_argument("--output-skills-dir")
        p.add_argument("--no-apply", action="store_true")

    init = sub.add_parser("init", help="Prepare input_bundle and manifest.")
    init.add_argument("--task-dir", required=True)
    init.add_argument("--skills-dir", required=True)
    init.add_argument("--traces", nargs="+", required=True)
    init.add_argument("--output-dir", required=True)
    init.add_argument("--max-traces", type=int, default=5)
    init.add_argument("--force", action="store_true")
    add_common_model(init)

    prompt = sub.add_parser("prompt", help="Render one stage prompt without calling the LLM.")
    prompt.add_argument("--output-dir", required=True)
    prompt.add_argument("--stage", required=True, choices=[stage["id"] for stage in STAGES])
    prompt.add_argument("--trace-index", type=int)
    add_common_model(prompt)

    run = sub.add_parser("run", help="Run one stage; most stages call the repair LLM, deterministic stages use local code.")
    run.add_argument("--output-dir", required=True)
    run.add_argument("--stage", required=True, choices=[stage["id"] for stage in STAGES])
    run.add_argument("--trace-index", type=int)
    run.add_argument(
        "--stage-run-mode",
        choices=["step", "until-complete"],
        default="step",
        help="Stage 8 only: execute one LLM interaction or continue until all suggestions pass review.",
    )
    add_common_model(run)

    calculate = sub.add_parser("calculate", help="Calculate local formula fields for one stage output.")
    calculate.add_argument("--output-dir", required=True)
    calculate.add_argument(
        "--stage",
        required=True,
        choices=["stage-02-capability-graph", "stage-05-node-execution-assessment"],
    )
    add_common_model(calculate)

    final = sub.add_parser("finalize", help="Write final report and apply repaired skills.")
    final.add_argument("--output-dir", required=True)
    add_common_model(final)

    status = sub.add_parser("status", help="Read stage status.")
    status.add_argument("--output-dir", required=True)

    pause = sub.add_parser("pause", help="Persist a paused Stage 8 state after stopping a background process.")
    pause.add_argument("--output-dir", required=True)
    add_common_model(pause)

    variable = sub.add_parser("template-variable", help="Read a prompt template variable default/override.")
    variable.add_argument("--name", required=True)

    return parser.parse_args()


def main() -> int:
    """CLI 入口。"""
    args = parse_args()
    handlers = {
        "init": init_run,
        "prompt": prompt_only,
        "run": run_stage,
        "calculate": calculate_stage,
        "finalize": finalize_run,
        "status": status_cmd,
        "pause": pause_cmd,
        "template-variable": template_variable_cmd,
    }
    result = handlers[args.command](args)
    write_stdout_json(result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        # 保持 stdout 为 JSON，方便 Node 后端把错误原样展示在页面上。
        write_stdout_json({"ok": False, "error": str(exc)})
        raise
