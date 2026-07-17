"""Offline SkillRCA 主编排逻辑。

这个模块连接 CLI、IO、分阶段 repair LLM 调用和最终产物写入。它刻意不在本地
推断“应该怎么修 skill”，而是只负责：

1. 收集并清洗允许给 repair LLM 看的证据；
2. 按 Stage 1-8 调用 repair LLM；
3. 保存每轮 prompt/transcript/parsed output；
4. Stage 8 逐建议生成、审查并提交到复制出的技能库目录。

这样可以保证 repair 过程符合“全程由指定 repair LLM 操作”的实验约束。
"""
from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from typing import Any

from .io_utils import (
    copy_tree,
    discover_rollout_dirs,
    ensure_inside,
    list_skill_files,
    load_trajectory,
    normalize_patch_path,
    read_text,
    safe_rel,
    write_csv,
    write_json,
)
from .llm_client import LLMClient
from .render import render_report
from .schemas import OfflineSkillRCAConfig, OfflineSkillRCAResult, SkillFile, Trajectory, to_plain


def run_offline_skill_rca(config: OfflineSkillRCAConfig) -> OfflineSkillRCAResult:
    """运行完整 Offline SkillRCA 流程。

    该函数是主入口：验证配置、读取 task/skills/traces、构造可见证据包、执行
    多阶段 repair，并写出最终报告和修复后的 skills。
    """
    validate_config(config)
    # 任务描述、技能库和轨迹是 repair LLM 唯一可见的核心输入。
    task_text = read_text(config.task_dir / "task.md", 32_000)
    skill_files = list_skill_files(config.root, config.skills_dir)
    rollout_dirs = discover_rollout_dirs([path for path in config.trace_paths if path], config.max_traces)
    trajectories = [load_trajectory(config.root, rollout) for rollout in rollout_dirs]
    validate_failed_trajectories(trajectories)
    prepare_output_dir(config)

    evidence_bundle = build_evidence_bundle(config, task_text, skill_files, trajectories)
    # input_bundle.json 是可审计的“LLM 可见输入”快照，后续可视化和诊断都依赖它。
    write_json(config.output_dir / "input_bundle.json", evidence_bundle)
    prompt_index = build_prompt_index(config, evidence_bundle)
    (config.output_dir / "offline_skill_rca_prompt.txt").write_text(prompt_index, encoding="utf-8")

    if config.prepare_prompts_only:
        # 仅准备 prompt 时不调用 LLM，适合检查模板渲染和证据清洗结果。
        write_prepare_only_prompts(config, evidence_bundle)
        return OfflineSkillRCAResult(output_dir=config.output_dir, output_skills_dir=None)

    stage_outputs = run_multistage_repair(config, evidence_bundle)
    trace_analyses = stage_outputs.get("stage_03_failure_events_by_trace") or []
    write_json(config.output_dir / "trace_analyses.json", trace_analyses)
    write_json(config.output_dir / "stage_outputs.json", stage_outputs)
    # Stage 8 已经完成逐建议修复、审查和事务式提交，不再需要额外汇总阶段。
    from .stages import stage_08_transactional_skill_repair

    stage8_state = stage_outputs.get("stage_08_transactional_skill_repair") or {}
    analysis = normalize_analysis(stage_08_transactional_skill_repair.final_analysis(stage8_state))
    enrich_analysis_from_stage_outputs(analysis, stage_outputs)
    analysis["metadata"] = {
        # metadata 明确记录实验边界，便于之后判断某次修复是否满足“不作弊”约束。
        "generated_at": datetime.now(UTC).isoformat(),
        "method": "Offline SkillRCA v2 explicit-stage multi-round",
        "taskDir": safe_rel(config.root, config.task_dir),
        "sourceSkillsDir": safe_rel(config.root, config.skills_dir),
        "outputSkillsDir": safe_rel(config.root, config.output_skills_dir) if config.output_skills_dir else "",
        "strongModel": config.strong_model,
        "separateReviewLlm": config.use_separate_review_llm,
        "reviewBaseUrl": config.review_base_url if config.use_separate_review_llm else "",
        "reviewModel": config.review_model if config.use_separate_review_llm else "",
        "traceCount": len(trajectories),
        "traceAnalysisWorkers": config.trace_analysis_workers,
        "addSkillMergeThreshold": config.add_skill_merge_threshold,
        "addSkillTargetCount": config.add_skill_target_count,
        "maxNewSkillCount": config.max_new_skill_count,
        "skillWordLimit": config.skill_word_limit,
        "stage7MaxOperations": config.stage7_max_operations,
        "stage7RepairMode": config.stage7_repair_mode,
        "stage7SkillPackageSize": config.stage7_skill_package_size,
        "stageCount": 8,
        "stages": [
            "Stage 1a: task description standardization",
            "Stage 1b: per-Skill standardization, one LLM call per Skill",
            "Stage 2: capability graph and node-Skill coverage",
            "Stage 3: per-trajectory failure/cause event extraction, parallel",
            "Stage 4: failure/cause-event-to-capability-node alignment",
            "Stage 5: per-trajectory node execution judgments and deterministic status calculation, parallel",
            "Stage 6: per-node Skill repair suggestions",
            "Stage 7: conditional add_new_skill action clustering and conversion",
            "Stage 8: transactional Skill repair by suggestion or same-Skill package with per-attempt LLM review",
        ],
        "visibleInputs": [
            "task_description",
            "skill_library",
            "failed_trajectories",
            "success_0_1",
            "agent_harness_runtime_errors",
            "agent_produced_final_artifacts",
        ],
        "hiddenInputsExcluded": [
            "all verifier files, fields, outputs, errors, rewards, tests, rubrics, metrics, and reviews",
            "local static skill scoring",
        ],
    }
    analysis["trace_analyses"] = trace_analyses
    # 不把最终 analysis 自身再挂回 stage_outputs，避免 JSON 归档时形成循环引用。
    analysis["stage_outputs"] = dict(stage_outputs)
    write_outputs(config, analysis)
    # Stage 8 已经在 output_skills_dir 的副本上逐次提交通过审查的修改，禁止在
    # 这里再次批量覆盖，否则会破坏逐建议事务边界。
    non_skill_blockers = blocker_descriptions(analysis.get("non_skill_blockers") or [])
    return OfflineSkillRCAResult(
        output_dir=config.output_dir, output_skills_dir=config.output_skills_dir, non_skill_blockers=non_skill_blockers
    )


def validate_config(config: OfflineSkillRCAConfig) -> None:
    """检查输入输出路径是否存在且位于仓库根目录内。"""
    if not config.task_dir or not config.task_dir.exists():
        raise SystemExit(f"Task dir does not exist: {config.task_dir}")
    if not (config.task_dir / "task.md").exists():
        raise SystemExit(f"Task dir has no task.md: {config.task_dir}")
    if not config.skills_dir or not config.skills_dir.exists():
        raise SystemExit(f"Skills dir does not exist: {config.skills_dir}")
    ensure_inside(config.root, config.task_dir)
    ensure_inside(config.root, config.skills_dir)
    ensure_inside(config.root, config.output_dir)
    if config.output_skills_dir:
        ensure_inside(config.root, config.output_skills_dir)
    for path in config.trace_paths:
        if path:
            ensure_inside(config.root, path)


def validate_failed_trajectories(trajectories: list[Trajectory | dict[str, Any]]) -> None:
    """拒绝把成功 rollout 当作失败轨迹送入 repair pipeline。

    Offline SkillRCA 的输入契约要求每条轨迹都是已经确定的失败运行。这里使用
    本地代码读取规范化后的 0/1 outcome；repair LLM 无权重新判断该事实。旧的
    Web debug bundle 可能保存为字典，因此同时支持 dataclass 与字典形式。
    """
    successful_ids: list[str] = []
    for index, trajectory in enumerate(trajectories):
        if isinstance(trajectory, Trajectory):
            traj_id = str(trajectory.traj_id or index + 1)
            success = trajectory.result.get("success", 0)
        else:
            traj_id = str(trajectory.get("traj_id") or index + 1)
            visible = trajectory.get("visible_failure_result")
            success = (
                visible.get("success", trajectory.get("success", 0))
                if isinstance(visible, dict)
                else trajectory.get("success", 0)
            )
        if bool(success):
            successful_ids.append(traj_id)
    if successful_ids:
        joined = ", ".join(successful_ids)
        raise SystemExit(
            "Offline SkillRCA accepts failed trajectories only; "
            f"the following inputs have authoritative success=1: {joined}"
        )


def prepare_output_dir(config: OfflineSkillRCAConfig) -> None:
    """准备本次 repair 输出目录。

    为避免污染已有结果，默认不覆盖；只有传入 ``--force`` 时才删除旧目录。
    """
    if config.output_dir.exists():
        if not config.force:
            raise SystemExit(f"Output dir already exists: {config.output_dir} (use --force)")
        shutil.rmtree(config.output_dir)
    config.output_dir.mkdir(parents=True, exist_ok=True)


def build_evidence_bundle(
    config: OfflineSkillRCAConfig,
    task_text: str,
    skill_files: list[SkillFile],
    trajectories: list[Trajectory],
) -> dict[str, Any]:
    """构造发送给 repair LLM 的可见证据包。

    这是防止“作弊”的关键边界：bundle 包含任务文本、Skill 文件、agent 失败轨迹、
    agent/harness 运行错误和 agent 产物；不包含任何 verifier 文件、字段、输出、
    reward、测试信息、metric、review 或本地静态评分。
    """
    return {
        "task_description": task_text,
        "task_dir": safe_rel(config.root, config.task_dir),
        "skills_dir": safe_rel(config.root, config.skills_dir),
        "skill_library": [
            # skill content 会按长度截断，但保留路径和 metadata，方便 LLM 定位要修的文件。
            {
                "skill_id": skill.skill_id,
                "title": skill.title,
                "path": skill.path,
                "metadata": skill.metadata,
                "content": truncate(skill.content, 24_000),
                "attached_files": truncate_attached_files(skill.attached_files),
            }
            for skill in skill_files
        ],
        "failed_trajectories": [sanitize_trajectory_for_llm(traj) for traj in trajectories],
        "constraints": {
            # constraints 会被每个阶段 prompt 传递，用来持续提醒 repair LLM 实验边界。
            "failure_trace_count": len(trajectories),
            "visible_outcome": "success 0/1 plus agent/harness runtime errors and archived or trajectory-reconstructed agent artifacts",
            "patch_scope": "skills library only",
            "no_task_or_hidden_evaluator_edits": True,
            "no_final_answer_leakage": True,
            "hidden_evaluator_implementation_excluded": True,
            "local_static_skill_scoring_excluded": True,
            "weak_model": "deepseek-v4-flash",
            "harness": "claude code",
        },
    }


def sanitize_trajectory_for_llm(traj: Trajectory) -> dict[str, Any]:
    """把 Trajectory 转成 repair LLM 可见的轨迹输入。

    这里故意不输出 messages、tool_calls、observations、final_output 或
    rollout_dir。repair LLM 看到本地从原始 ACP trajectory 确定性格式化得到的
    steps、agent/harness 运行错误和归档或轨迹重建的 agent 最终产物。
    """
    from .io_utils import sanitize_agent_artifacts, sanitize_agent_only_visible_result

    visible_result = sanitize_agent_only_visible_result(traj.result)
    success = int(visible_result["success"])
    return {
        "traj_id": traj.traj_id,
        "task_id": traj.task_id,
        "success": success,
        "step_formatting_provenance": "generated_by_local_code_from_acp_trajectory_jsonl; no LLM summarization",
        "steps": [sanitize_step(step) for step in traj.steps],
        "visible_failure_result": to_plain(visible_result),
        "final_artifacts": to_plain(sanitize_agent_artifacts(traj.final_artifacts)),
    }


def sanitize_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """清洗工具调用摘要，只保留 step/title/status/summary。"""
    out = []
    for call in tool_calls:
        out.append(
            {
                "step_id": call.get("step_id"),
                "title": truncate(str(call.get("title") or ""), 240),
                "status": truncate(str(call.get("status") or ""), 80),
                "summary": truncate(str(call.get("summary") or ""), 900),
            }
        )
    return out


def sanitize_step(step: Any) -> dict[str, Any]:
    """清洗单个 TraceStep，并去掉重复文本字段。

    Stage 2 的轨迹证据不能出现本地截断标记，因此这里不再调用 ``truncate``。
    ``action_summary``、``raw_visible_text``、``observation_summary`` 和
    ``error_signal`` 均直接来自原始 ACP event 的可见文本；若后三者与
    ``action_summary`` 内容重复，则不保留。
    """
    plain = to_plain(step)
    cleaned = {
        "step_id": plain.get("step_id"),
        "role": plain.get("role"),
        "event_type": plain.get("event_type"),
        "status": str(plain.get("status") or ""),
        "action_type": plain.get("action_type"),
        "action_summary": str(plain.get("action_summary") or ""),
        "raw_visible_text": str(plain.get("raw_visible_text") or "") if plain.get("raw_visible_text") else None,
        "tool_name": str(plain.get("tool_name") or "") if plain.get("tool_name") else None,
        "observation_summary": str(plain.get("observation_summary") or "") if plain.get("observation_summary") else None,
        "mentioned_skills": plain.get("mentioned_skills") or [],
        "produced_artifacts": plain.get("produced_artifacts") or [],
        "error_signal": str(plain.get("error_signal") or "") if plain.get("error_signal") else None,
    }
    return remove_duplicate_step_text(cleaned)


def remove_duplicate_step_text(step: dict[str, Any]) -> dict[str, Any]:
    """删除与 action_summary 重复的 step 文本字段。"""
    if step.get("event_type") == "agent_thought":
        # agent_thought 是模型内部思考，不属于外部可观察工具结果；保留元数据即可。
        # 这样避免把长篇推理链塞给 repair LLM，也不需要对其做截断。
        for key in ("action_summary", "raw_visible_text", "observation_summary", "error_signal"):
            step.pop(key, None)
        return {key: value for key, value in step.items() if value not in (None, "")}
    action_text = step.get("action_summary")
    for key in ("raw_visible_text", "observation_summary", "error_signal"):
        if duplicate_text(step.get(key), action_text):
            step.pop(key, None)
    return {key: value for key, value in step.items() if value not in (None, "")}


def duplicate_text(left: Any, right: Any) -> bool:
    """按内容判断两段文本是否重复，忽略空白差异。"""
    if not left or not right:
        return False
    return normalize_for_duplicate(str(left)) == normalize_for_duplicate(str(right))


def normalize_for_duplicate(text: str) -> str:
    """归一化空白，用于判断同源文本是否重复。"""
    return " ".join(text.split())


def build_prompt_index(config: OfflineSkillRCAConfig, bundle: dict[str, Any]) -> str:
    """生成本次运行的 prompt 索引说明文件。

    这个文件不发送给 LLM，只给人类审阅：说明每个阶段的 prompt 文件名、可见输入
    和被排除的信息类型。
    """
    lines = [
        "# Offline SkillRCA v2 Prompt Index",
        "",
        "This run uses a multi-round repair LLM protocol.",
        "",
        "Visible inputs:",
        "- task_description",
        "- skill_library",
        "- failed_trajectories",
        "- success: 0/1 for each trajectory",
        "- agent/harness runtime errors",
        "- archived or trajectory-reconstructed agent final artifacts",
        "",
        "Excluded from every repair LLM prompt:",
        "- all verifier files, fields, outputs, errors, rewards, tests, rubrics, metrics, and reviews",
        "- local static skill scoring or locally inferred repair advice",
        "",
        "Stages:",
        "1a. stage-01a-task-description-standardization.prompt.txt: task_description only.",
        "1b. stage-01b-skill-*.prompt.txt: one LLM call per Skill.",
        "2. stage-02-capability-graph.prompt.txt: capability graph and node-Skill coverage in one response.",
        "3. stage-03-traj-*.prompt.txt: one causal failure-event extraction call per trajectory, run in parallel.",
        "4. stage-04-failure-event-alignment.prompt.txt: align failure and cause events to nodes.",
        "5. stage-05-traj-*.prompt.txt: judge node execution facts per trajectory; local code computes status.",
        "6. stage-06-node-*.prompt.txt: one Skill repair-suggestion call per repairable node.",
        "7. stage-07-repair-action-merge.txt: conditionally cluster excessive add_new_skill actions.",
        "8. stage-08-skill-repair.txt + stage-08-skill-review.txt: transactional repair and review per repair unit.",
        "",
        f"Trace analysis workers: {config.trace_analysis_workers}",
        f"Trace count: {len(bundle.get('failed_trajectories', []))}",
    ]
    return "\n".join(lines).rstrip() + "\n"


def write_prepare_only_prompts(config: OfflineSkillRCAConfig, bundle: dict[str, Any]) -> None:
    """在不调用 LLM 的情况下写出可提前生成的 prompt。

    只有 Stage 1a/1b 能独立生成；后续阶段依赖前序 LLM 输出。
    """
    from .stages import stage_01_input_standardization

    write_prompt_file(config, stage_01_input_standardization.TASK_STAGE_NAME, stage_01_input_standardization.build_task_prompt(bundle, config.max_prompt_chars))
    for index, skill in enumerate(bundle.get("skill_library") or []):
        name = stage_01_input_standardization.skill_stage_name(index, skill)
        write_prompt_file(config, name, stage_01_input_standardization.build_skill_prompt(skill, config.max_prompt_chars))
    write_prompt_file(
        config,
        "stage-02-capability-graph",
        "This prompt requires Stage 1a and Stage 1b outputs. Run without --prepare-prompts-only to generate it.",
    )
    for index, traj in enumerate(bundle.get("failed_trajectories", []), start=1):
        name = f"stage-03-traj-{index:02d}-{sanitize(str(traj.get('traj_id') or index))}"
        write_prompt_file(
            config,
            name,
            "This prompt requires Stage 1a, Stage 1b, and Stage 2 outputs. Run without --prepare-prompts-only to generate it.",
        )
    for name in [
        "stage-04-failure-event-alignment",
        "stage-05-node-execution-assessment",
        "stage-06-skill-repair-suggestions",
        "stage-07-repair-action-merge",
        "stage-08-transactional-skill-repair",
    ]:
        write_prompt_file(config, name, "This prompt requires prior stage outputs. Run without --prepare-prompts-only to generate it.")


def run_multistage_repair(config: OfflineSkillRCAConfig, bundle: dict[str, Any]) -> dict[str, Any]:
    """按 Stage 1a/1b 到事务式 Stage 8 执行完整 repair LLM 协议。"""
    from .llm_client import log_progress
    from .stages import (
        stage_01_input_standardization,
        stage_02_capability_graph,
        stage_03_failure_event_extraction,
        stage_04_failure_event_alignment,
        stage_05_node_execution_assessment,
        stage_06_skill_repair_suggestions,
        stage_07_repair_action_merge,
        stage_08_transactional_skill_repair,
    )

    # 每个 Stage 只接收必要的前序输出；Stage 3 内部会并行分析每条轨迹。
    log_progress("Pipeline stage 1 start: task/Skill standardization")
    stage1 = stage_01_input_standardization.run(config, bundle)
    task_stage1 = stage1["stage_01a_task_description_standardization"]
    skill_stage1 = stage1["stage_01b_skill_standardizations"]
    log_progress("Pipeline stage 2 start: capability graph and Skill coverage")
    stage2 = stage_02_capability_graph.run(config, bundle, task_stage1, skill_stage1)
    log_progress("Pipeline stage 3 start: per-trajectory failure events")
    stage3 = stage_03_failure_event_extraction.run(config, bundle, task_stage1, skill_stage1, stage2)
    log_progress("Pipeline stage 4 start: failure-event alignment")
    stage4 = stage_04_failure_event_alignment.run(config, bundle, stage2, stage3)
    log_progress("Pipeline stage 5 start: node execution assessment and local status calculation")
    stage5 = stage_05_node_execution_assessment.run(config, bundle, stage2, stage3, stage4)
    log_progress("Pipeline stage 6 start: node-bound repair suggestions")
    stage6 = stage_06_skill_repair_suggestions.run(config, bundle, stage2, skill_stage1, stage3, stage4, stage5)
    log_progress("Pipeline stage 7 start: repair action merge")
    stage7 = stage_07_repair_action_merge.run(config, stage6)
    # Stage 8 自己按“修复 -> 审查 -> 提交”循环到完成；每次审查通过后才会
    # 修改复制出的技能库，因此无需额外静态审查或 final-package stage。
    log_progress("Pipeline stage 8 start: transactional Skill repair")
    # 完整流水线在 Stage 8 结束后才统一写 stage_outputs.json。Review 在 Stage 8
    # 内部就需要能力节点、失败事件、对齐和节点执行上下文，因此把这些已完成阶段的
    # 内存结果显式传入，避免 Review 因磁盘聚合文件尚不存在而误判为“缺少证据”。
    stage8_bundle = {
        **bundle,
        "stage_01b_skill_standardizations": skill_stage1,
        "review_stage_outputs": {
            "stage_02_capability_graph": stage2,
            "stage_03_failure_events_by_trace": stage3,
            "stage_04_failure_event_alignment": stage4,
            "stage_05_node_execution_assessments": stage5,
        },
    }
    stage8 = stage_08_transactional_skill_repair.run(config, stage8_bundle, stage7, mode="until-complete")
    log_progress("Pipeline complete")
    return {
        "stage_01a_task_description_standardization": task_stage1,
        "stage_01b_skill_standardizations": skill_stage1,
        "stage_01_input_standardization": stage1,
        "stage_02_capability_graph": stage2,
        "stage_03_failure_events_by_trace": stage3,
        "stage_04_failure_event_alignment": stage4,
        "stage_05_node_execution_assessments": stage5,
        "stage_06_skill_repair_suggestions": stage6,
        "stage_07_repair_action_merge": stage7,
        "stage_08_transactional_skill_repair": stage8,
    }


def run_stage(config: OfflineSkillRCAConfig, name: str, prompt: str) -> dict[str, Any]:
    """兼容旧代码的单阶段运行入口。

    新实现统一委托给 ``src.stages.common.run_llm_stage``，保留这个函数是为了不破坏
    旧脚本或外部调用。
    """
    from .stages.common import run_llm_stage

    return run_llm_stage(config, name, prompt)


def run_parallel_stage3_failure_events(
    config: OfflineSkillRCAConfig,
    bundle: dict[str, Any],
    task_standardization: dict[str, Any],
    skill_standardizations: list[dict[str, Any]],
    stage2: dict[str, Any],
) -> list[dict[str, Any]]:
    """Stage 3 失败事件抽取的并行入口。"""
    from .stages import stage_03_failure_event_extraction

    return stage_03_failure_event_extraction.run(config, bundle, task_standardization, skill_standardizations, stage2)


def build_stage1_input_prompt(bundle: dict[str, Any], max_chars: int) -> str:
    """构造 Stage 1a 任务描述标准化 prompt。"""
    from .stages import stage_01_input_standardization

    return stage_01_input_standardization.build_task_prompt(bundle, max_chars)


def build_stage2_capability_graph_prompt(
    bundle: dict[str, Any],
    task_standardization: dict[str, Any],
    skill_standardizations: list[dict[str, Any]],
    max_chars: int,
) -> str:
    """构造 Stage 2 能力图与 Skill 覆盖 prompt。"""
    from .stages import stage_02_capability_graph

    return stage_02_capability_graph.build_prompt(bundle, task_standardization, skill_standardizations, max_chars)


def build_stage3_failure_event_prompt(
    bundle: dict[str, Any],
    task_standardization: dict[str, Any],
    skill_standardizations: list[dict[str, Any]],
    stage2: dict[str, Any],
    trajectory: dict[str, Any],
    max_chars: int,
) -> str:
    """构造单条轨迹的 Stage 3 失败事件抽取 prompt。"""
    from .stages import stage_03_failure_event_extraction

    return stage_03_failure_event_extraction.build_prompt(
        bundle,
        task_standardization,
        skill_standardizations,
        stage2,
        trajectory,
        max_chars,
    )


def build_stage4_alignment_prompt(bundle: dict[str, Any], stage2: dict[str, Any], stage3: list[dict[str, Any]], max_chars: int) -> str:
    """构造 Stage 4 失败事件对齐 prompt。"""
    from .stages import stage_04_failure_event_alignment

    return stage_04_failure_event_alignment.build_prompt(bundle, stage2, stage3, max_chars)


def build_stage6_suggestions_prompt(
    bundle: dict[str, Any],
    stage2: dict[str, Any],
    skill_standardizations: list[dict[str, Any]],
    stage3: list[dict[str, Any]],
    stage4: dict[str, Any],
    stage5: list[dict[str, Any]],
    max_chars: int,
) -> str:
    """构造 Stage 6 逐节点 Skill 修复建议预览 prompt。"""
    from .stages import stage_06_skill_repair_suggestions

    return stage_06_skill_repair_suggestions.build_prompt(bundle, stage2, skill_standardizations, stage3, stage4, stage5, max_chars)


def trajectory_index(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """兼容旧流程的轻量轨迹索引。

    拆分后的 Stage 1 不使用 trajectory，也不接收 trajectory_index；这个
    函数仅为历史输出或旧脚本保留。
    """
    out = []
    for traj in bundle.get("failed_trajectories") or []:
        out.append(
            {
                "traj_id": traj.get("traj_id"),
                "task_id": traj.get("task_id"),
                "success": traj.get("success", 0),
                "step_count": len(traj.get("steps") or []),
            }
        )
    return out


def failure_event_alignment_schema() -> dict[str, Any]:
    """Stage 4 期望返回的 failure-event-to-node alignment schema。"""
    return {
        "alignments": [
            {
                "traj_id": "string",
                "event_id": "string",
                "event_kind": "failure|cause",
                "node_id": "N1|null",
                "confidence": 0.0,
                "reason": "string",
            }
        ],
    }


def transactional_skill_repair_schema() -> dict[str, Any]:
    """返回 Stage 8 的 repair/review 两种响应契约。"""
    from .stages import stage_08_transactional_skill_repair

    return {
        "repair": stage_08_transactional_skill_repair.repair_schema(),
        "review": stage_08_transactional_skill_repair.review_schema(),
    }


def make_llm(config: OfflineSkillRCAConfig, transcript_name: str) -> LLMClient:
    """兼容旧代码的 LLMClient 创建函数。"""
    is_stage3_trace = transcript_name.startswith("stage-03-traj-")
    timeout_sec = 600.0 if is_stage3_trace else 1800.0
    max_retries = 1 if is_stage3_trace else 3
    return LLMClient(
        config.strong_base_url,
        config.strong_api_key,
        config.strong_model,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        timeout_env_key="OFFLINE_SKILL_RCA_STAGE3_TIMEOUT_SEC" if is_stage3_trace else None,
        max_retries_env_key="OFFLINE_SKILL_RCA_STAGE3_MAX_RETRIES" if is_stage3_trace else None,
        transcript_dir=config.output_dir / "llm_transcript",
        transcript_name=transcript_name,
        reasoning_effort=getattr(config, "strong_reasoning_effort", ""),
    )


def write_prompt_file(config: OfflineSkillRCAConfig, name: str, prompt: str) -> None:
    """兼容旧代码的 prompt 落盘函数。"""
    prompts_dir = config.output_dir / "prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    (prompts_dir / f"{sanitize(name)}.prompt.txt").write_text(prompt, encoding="utf-8")


def trace_analysis_schema() -> dict[str, Any]:
    """Stage 3 单条轨迹分析的 JSON schema 示例。"""
    return {
        "traj_id": "string",
        "success": 0,
        "failure_events": [
            {
                "event_id": "string",
                "step_ids": [1],
                "observed_behavior": "string",
                "expected_behavior": "string",
                "failure_signal": "string",
                "downstream_consequence": "string",
                "severity": "minor|major|fatal",
                "evidence_refs": [
                    {
                        "source": "trajectory_step|failure_result|final_artifact",
                        "ref": "string",
                        "excerpt": "string",
                    }
                ],
            }
        ],
        "cause_events": [
            {
                "event_id": "string",
                "step_ids": [1],
                "observed_behavior": "string",
                "role": "direct|contributing|enabling",
                "evidence_refs": [
                    {
                        "source": "trajectory_step|failure_result|final_artifact",
                        "ref": "string",
                        "excerpt": "string",
                    }
                ],
            }
        ],
        "causal_links": [
            {
                "cause_event_id": "string",
                "failure_event_id": "string",
                "relation": "direct|contributing|enabling",
                "confidence": 0.0,
                "reason": "string",
            }
        ],
        "evidence_limits": [],
    }


def final_package_schema() -> dict[str, Any]:
    """最终修复包的 JSON schema 示例。

    多个 Stage 会复用这个 schema 的子结构，保证能力图、coverage、fault card、
    Stage 5 node repair recommendations、patch 和 draft 字段在全流程中保持一致。
    """
    return {
        "capability_graph": {
            "task_id": "string",
            "nodes": [
                {
                    "node_id": "N1",
                    "goal": "reusable capability",
                    "required_inputs": [],
                    "expected_outputs": [],
                    "required_operations": [],
                    "required_checks": [],
                    "common_failure_modes": [],
                    "dependencies": [],
                }
            ],
            "edges": [["N1", "N2"]],
        },
        "skill_coverage_matrix": [
            {
                "node_id": "N1",
                "node_goal": "reusable capability",
                "skill_id": "skill-id",
                "skill_title": "skill title",
                "directly_relevant": True,
                "direct_relevance_rationale": "why this skill directly supports or does not directly support this capability node",
                "node_requirement_fit": "number|null",
                "node_requirement_fit_rationale": "whether the skill's workflow, operations, assumptions, and checks satisfy this capability node; null when directly_relevant is false",
                "trigger_coverage": "number|null",
                "procedure_coverage": "number|null",
                "verification_coverage": "number|null",
                "recovery_coverage": "number|null",
                "execution_support_need": "not_needed|helpful|required|null",
                "execution_support_coverage": "number|null",
                "execution_support_rationale": "why executable support is not needed/helpful/required; null when directly_relevant is false",
                "overall_coverage": "computed_number|null; set null in LLM response, local code fills this field",
                "coverage_gap": "computed_number|null; set null in LLM response, local code fills this field as 1-overall_coverage",
                "coverage_labels": "computed_list|null; set null in LLM response, local code fills up to 3 labels sorted by their source score from low to high; full-score dimensions are ignored; labels come from not_relevant|partially_covered|under_specified|missing_verification|missing_recovery|missing_execution_support|conflicting_skills",
                "missing_slots": [],
                "calculation": "computed_object|null; set null in LLM response, local code fills formula, weights, inputs, and warnings",
                "evidence": {
                    "skill_sections": [],
                    "trace_step_refs": [],
                    "failure_event_refs": [],
                },
            }
        ],
        "fault_cards": [
            {
                "event_id": "E1",
                "traj_id": "string",
                "step_id": 1,
                "observed_behavior": "string",
                "expected_behavior": "string",
                "local_error_signal": "string",
                "downstream_consequence": "string",
                "suspected_capability_node": "N1",
                "suspected_skill_ids": [],
                "severity": "minor|major|fatal",
                "evidence_span": "short quote or summary",
                "first_actionable_fault_candidate": True,
            }
        ],
        "root_cause_hypotheses": [
            {
                "hypothesis_id": "H1",
                "root_cause_type": "skill_absent|skill_under_specified|skill_missing_trigger|skill_missing_verification|skill_missing_recovery|skill_conflict|skill_too_broad|skill_too_task_specific|model_needs_execution_support|non_skill_issue",
                "node_id": "N1",
                "target_skill_ids": [],
                "description": "string",
                "supporting_events": [],
                "affected_trajectories": [],
                "evidence_summary": "string",
                "score": "computed_number|null; set null in LLM response, local code fills and sorts this field",
                "score_factors": "computed_object|null; set null in LLM response, local code fills F/P/G/D/U/A",
                "score_calculation": "computed_object|null; set null in LLM response, local code fills formula, weights, inputs, and event refs",
                "proposed_action": "string",
            }
        ],
        "node_repair_recommendations": [
            {
                "node_id": "N1",
                "node_repair_action": "revise_existing_skill|add_new_skill",
                "node_issue_summary": "repair LLM summary for this node",
                "existing_skill_repairs": "filled only when node_repair_action=revise_existing_skill",
                "new_skill_proposal": "filled only when node_repair_action=add_new_skill",
                "skill_repair_suggestions": "compatibility suggestions derived by local code for Stage 6",
                "local_stage5_context": "local triage metadata used to generate this node prompt",
            }
        ],
        "node_skill_repair_suggestions": "alias of node_repair_recommendations in the per-node Stage 5 pipeline",
        "skill_repair_recommendations": [
            {
                "skill_id": "skill-id",
                "action": "revise_existing_skill",
                "affected_node_ids": [],
                "node_suggestions": "Stage 5 per-node LLM suggestions grouped by existing target skill",
                "priority_score": "computed_number",
            }
        ],
        "new_skill_recommendations": [
            {
                "action": "add_new_skill",
                "node_id": "N1",
                "new_skill_id": "suggested new skill id",
                "skill_repair_suggestion": "Stage 5 per-node LLM suggestion for adding a skill",
                "priority_score": "computed_number",
            }
        ],
        "skill_patch_plan": [
            {
                "patch_id": "P1",
                "action": "add_new_skill|revise_existing_skill|add_trigger|add_verification_section|add_recovery_section|add_execution_support|split_skill|merge_skills|deprecate_skill",
                "target_skill_id": "string or null",
                "new_skill_id": "string or null",
                "linked_node_ids": [],
                "linked_hypotheses": "legacy list; normally empty in deterministic Stage 5 pipeline",
                "linked_stage5_recommendations": {
                    "node_ids": [],
                    "skill_repair_recommendation_ids_or_skill_ids": [],
                    "new_skill_recommendation_node_ids": [],
                },
                "evidence_events": [],
                "problem_summary": "string",
                "proposed_change_summary": "string",
                "patch_content": "unified diff or full markdown",
                "risk_level": "low|medium|high",
                "reviewer_notes": [],
            }
        ],
        "updated_skill_drafts": [
            {
                "draft_id": "D1",
                "operation": "add|revise",
                "relative_path": "skill-id/SKILL.md",
                "skill_id": "skill-id",
                "title": "Skill title",
                "content": "complete SKILL.md content",
                "source_patch_ids": [],
            }
        ],
        "non_skill_blockers": [
            {
                "type": "task_input_or_verifier_mismatch|api_or_environment|insufficient_evidence|other",
                "description": "string",
                "evidence": [],
                "blocks_validation": True,
            }
        ],
        "patch_reviews": [
            {
                "patch_id": "string",
                "status": "accept|revise|reject",
                "failed_checks": [],
                "risk_level": "low|medium|high",
                "review_notes": [],
            }
        ],
        "diagnosis_report_markdown": "Chinese markdown report",
    }



















def stage_system_prompt(stage: str) -> str:
    """旧接口保留的 system prompt。

    新 stage 模块默认使用 ``src.stages.common.stage_system_prompt``；这个函数仍保留
    给历史 resume 脚本调用，避免断点续跑脚本需要同步大改。
    """
    from .stages.common import stage_system_prompt as render_stage_system_prompt

    return render_stage_system_prompt(stage)


def fit_prompt(instructions: str, payload: dict[str, Any], max_chars: int) -> str:
    """把 instructions 和 payload 拼成不超过上限的 prompt。

    Prompt 模板必须显式声明外部输入插入点，例如 ``<task_description>``、
    ``<stage_01b_skill_standardizations>`` 或 ``<stage_02_capability_graph>``。
    为兼容旧模板，如果没有任何字段级占位符，才回退到末尾追加 JSON。

    如果初始 payload 过长，会逐级降低 skill 文本、轨迹步骤和消息摘要长度；
    仍然超限时才终止运行，并提示用户提高 ``--max-prompt-chars`` 或减少轨迹冗余。
    """
    for skill_limit, step_limit, message_limit in [
        (24_000, 80, 1800),
        (14_000, 56, 1400),
        (8_000, 36, 1000),
        (4_500, 24, 700),
        (2_500, 16, 500),
    ]:
        candidate = compact_visible_payload(payload, skill_limit, step_limit, message_limit)
        prompt = inject_visible_payload(instructions, candidate)
        if len(prompt) <= max_chars:
            return prompt
    prompt = inject_visible_payload(instructions, compact_visible_payload(payload, 1_600, 10, 360))
    if len(prompt) > max_chars:
        raise SystemExit(
            f"Prompt is still too large after visible-input compaction ({len(prompt)} chars > {max_chars}). "
            "Increase --max-prompt-chars or reduce trajectory verbosity."
        )
    return prompt


def inject_visible_payload(instructions: str, payload: dict[str, Any]) -> str:
    """把 stage 外部输入填入 prompt 模板中的显式占位符。

    ``{{visible_evidence_json}}`` 是完整 payload 的规范占位符；``<field_name>``
    是字段级占位符。字段级占位符会被对应值的 JSON 表示替换，保证嵌入复杂结构时
    仍然可审计、可复制。
    """
    full_payload = json.dumps(payload, ensure_ascii=False, indent=2)
    rendered = instructions
    used_placeholder = False
    for marker in ["{{visible_evidence_json}}", "<visible_evidence_json>"]:
        if marker in rendered:
            rendered = rendered.replace(marker, full_payload)
            used_placeholder = True
    for key, value in payload.items():
        marker = f"<{key}>"
        if marker in rendered:
            rendered = rendered.replace(marker, json.dumps(value, ensure_ascii=False, indent=2))
            used_placeholder = True
    if used_placeholder:
        return rendered
    return instructions.rstrip() + "\n\nVisible evidence JSON:\n" + full_payload


def compact_visible_payload(payload: dict[str, Any], skill_limit: int, step_limit: int, message_limit: int) -> dict[str, Any]:
    """按给定限制压缩 prompt payload。

    目前只压缩 skill_library 和单条 trajectory；其它 stage 输出已经是 repair LLM
    生成的结构化摘要，通常不需要再做深度压缩。
    """
    compact = dict(payload)
    if "skill_library" in compact:
        compact["skill_library"] = [
            {
                **{key: value for key, value in skill.items() if key != "content"},
                "content": truncate(str(skill.get("content") or ""), skill_limit),
                "attached_files": compact_attached_files(skill.get("attached_files"), max(900, skill_limit // 3)),
            }
            for skill in compact.get("skill_library", [])
        ]
    if "skill_file" in compact and isinstance(compact["skill_file"], dict):
        skill = compact["skill_file"]
        compact["skill_file"] = {
            **{key: value for key, value in skill.items() if key not in {"content", "attached_files"}},
            "content": truncate(str(skill.get("content") or ""), skill_limit),
        }
    if "skill_attached_files" in compact:
        compact["skill_attached_files"] = compact_attached_files(compact.get("skill_attached_files"), max(900, skill_limit // 3))
    if "trajectory" in compact and isinstance(compact["trajectory"], dict):
        compact["trajectory"] = compact_visible_trajectory(compact["trajectory"], step_limit, message_limit)
    return compact


def truncate_attached_files(files: Any, per_file_limit: int = 18_000) -> list[dict[str, Any]] | None:
    """把 skill 附加文件压缩成可进入 input_bundle 的结构。"""
    compacted = compact_attached_files(files, per_file_limit)
    return compacted or None


def compact_attached_files(files: Any, per_file_limit: int) -> list[dict[str, Any]] | None:
    """压缩 attached_files 中的 content 字段，保留 path/type/content 三元组。"""
    if not files:
        return None
    out: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "path": item.get("path") or "",
                "type": item.get("type") or "others",
                "content": truncate(str(item.get("content") or ""), per_file_limit),
            }
        )
    return out or None


def compact_visible_trajectory(item: dict[str, Any], step_limit: int, message_limit: int) -> dict[str, Any]:
    """保留 Stage 2 的完整格式化 steps，同时保持字段语义纯净。

    这里不会生成 raw_trajectory_jsonl、messages、tool_calls、observations、
    final_output 等冗余字段，也不会截断 step 文本。若 prompt 预算不足，调用方会
    报错提示提高 ``--max-prompt-chars``，而不是把轨迹静默截断。
    """
    return {
        "traj_id": item.get("traj_id"),
        "task_id": item.get("task_id"),
        "success": item.get("success", 0),
        "steps": [compact_prompt_step(step, message_limit) for step in item.get("steps", [])],
    }


def compact_prompt_step(step: Any, text_limit: int) -> Any:
    """清理单个 prompt step；不截断轨迹文本。"""
    if not isinstance(step, dict):
        return step
    out = dict(step)
    return remove_duplicate_step_text(out)


def select_prompt_steps(steps: list[Any], limit: int) -> list[Any]:
    """从 prompt 轨迹步骤中挑选最值得保留的片段。

    与 ``io_utils.select_steps`` 类似，这里再次做一层 prompt 级压缩：保留头尾和
    错误/skill/验证/产物相关步骤，并去重。
    """
    if len(steps) <= limit:
        return steps
    important = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get("error_signal") or step.get("action_type") in {"skill_read", "verification", "artifact_generation"}:
            important.append(step)
    head_count = max(4, min(8, limit // 4))
    tail_count = max(6, min(10, limit // 3))
    selected = steps[:head_count] + important + steps[-tail_count:]
    out = []
    seen = set()
    for step in selected:
        key = json.dumps(step, sort_keys=True, ensure_ascii=False)[:200]
        if key in seen:
            continue
        seen.add(key)
        out.append(step)
        if len(out) >= limit:
            break
    return out


def truncate(text: str, limit: int) -> str:
    """截断字符串并显式标注被截掉的字符数。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def normalize_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    """归一化最终分析结果字段名。

    兼容早期 prompt 可能返回的旧字段名，并为缺失字段填入安全默认值。
    并为缺失字段填入安全默认值。
    """
    aliases = {
        "s0": "capability_graph",
        "S0_capability_dag": "capability_graph",
        "capability_dag": "capability_graph",
        "coverage_matrix": "skill_coverage_matrix",
        "failure_events": "fault_cards",
        "hypotheses": "root_cause_hypotheses",
        "patches": "skill_patch_plan",
        "report": "diagnosis_report_markdown",
    }
    for old, new in aliases.items():
        if old in analysis and new not in analysis:
            analysis[new] = analysis[old]
    for key, default in [
        ("capability_graph", {}),
        ("skill_coverage_matrix", []),
        ("fault_cards", []),
        ("root_cause_hypotheses", []),
        ("node_repair_recommendations", []),
        ("node_skill_repair_suggestions", []),
        ("skill_repair_recommendations", []),
        ("new_skill_recommendations", []),
        ("skill_patch_plan", []),
        ("updated_skill_drafts", []),
        ("non_skill_blockers", []),
    ]:
        analysis.setdefault(key, default)
    return analysis


def enrich_analysis_from_stage_outputs(analysis: dict[str, Any], stage_outputs: dict[str, Any]) -> None:
    """把前序 LLM stage 已经产生的结构化结果补进最终归档。

    Stage 8 的最终结果只负责记录逐建议事务式修复状态。这里把 Stage 2 至
    Stage 6 已有的诊断结果复制进最终 analysis，不额外新增 repair 判断。
    """
    stage2 = stage_outputs.get("stage_02_capability_graph") or {}
    stage4 = stage_outputs.get("stage_04_failure_event_alignment") or {}
    stage6 = stage_outputs.get("stage_06_skill_repair_suggestions") or {}
    if not analysis.get("capability_graph"):
        analysis["capability_graph"] = stage2.get("capability_graph") or {}
    if not analysis.get("fault_cards"):
        analysis["fault_cards"] = stage4.get("fault_cards") or stage4.get("failure_events") or []
    if not analysis.get("skill_coverage_matrix"):
        analysis["skill_coverage_matrix"] = stage2.get("skill_coverage_matrix") or stage2.get("coverage_matrix") or []
    if not analysis.get("root_cause_hypotheses"):
        analysis["root_cause_hypotheses"] = stage6.get("root_cause_hypotheses") or []
    if not analysis.get("node_repair_recommendations"):
        analysis["node_repair_recommendations"] = stage6.get("node_repair_recommendations") or []
    if not analysis.get("node_skill_repair_suggestions"):
        analysis["node_skill_repair_suggestions"] = stage6.get("node_skill_repair_suggestions") or analysis.get("node_repair_recommendations") or []
    if not analysis.get("skill_repair_recommendations"):
        analysis["skill_repair_recommendations"] = stage6.get("skill_repair_recommendations") or []
    if not analysis.get("new_skill_recommendations"):
        analysis["new_skill_recommendations"] = stage6.get("new_skill_recommendations") or []
    if not analysis.get("non_skill_blockers"):
        analysis["non_skill_blockers"] = stage6.get("non_skill_blockers") or []


def write_outputs(config: OfflineSkillRCAConfig, analysis: dict[str, Any]) -> None:
    """把最终 repair 产物写入 output_dir。

    结构化 JSON、coverage CSV、patch markdown、updated skill drafts 和中文报告都会
    在这里落盘，方便后续 web 页面和人工审阅读取。
    """
    write_json(config.output_dir / "capability_graph.json", analysis.get("capability_graph"))
    write_json(config.output_dir / "fault_cards.json", analysis.get("fault_cards"))
    write_json(config.output_dir / "root_cause_hypotheses.json", analysis.get("root_cause_hypotheses"))
    write_json(config.output_dir / "node_repair_recommendations.json", analysis.get("node_repair_recommendations"))
    write_json(config.output_dir / "node_skill_repair_suggestions.json", analysis.get("node_skill_repair_suggestions"))
    write_json(config.output_dir / "skill_repair_recommendations.json", analysis.get("skill_repair_recommendations"))
    write_json(config.output_dir / "new_skill_recommendations.json", analysis.get("new_skill_recommendations"))
    write_json(config.output_dir / "skill_patch_plan.json", analysis.get("skill_patch_plan"))
    write_json(config.output_dir / "updated_skill_drafts.json", analysis.get("updated_skill_drafts"))
    write_json(config.output_dir / "patch_reviews.json", analysis.get("patch_reviews"))
    write_json(config.output_dir / "offline_skill_rca_full.json", analysis)
    coverage = analysis.get("skill_coverage_matrix") or []
    if isinstance(coverage, list):
        write_csv(config.output_dir / "skill_coverage_matrix.csv", coverage)
        write_json(config.output_dir / "skill_coverage_matrix.json", coverage)
    patches_dir = config.output_dir / "patches"
    patches_dir.mkdir(parents=True, exist_ok=True)
    for patch in analysis.get("skill_patch_plan") or []:
        # Stage 7 精简 schema 不再强制输出 patch_content；如果缺失，就写入
        # 可审阅的 patch 摘要。真正应用到技能库的是 updated_skill_drafts。
        patch_id = str(patch.get("patch_id") or patch.get("new_skill_id") or patch.get("target_skill_id") or "patch")
        patch_text = patch.get("patch_content")
        if not patch_text:
            patch_text = "\n".join(
                [
                    f"# {patch_id}",
                    "",
                    f"- Action: {patch.get('action') or ''}",
                    f"- Target skill: {patch.get('target_skill_id') or ''}",
                    f"- New skill: {patch.get('new_skill_id') or ''}",
                    f"- Linked nodes: {', '.join(map(str, patch.get('linked_node_ids') or []))}",
                    f"- Risk: {patch.get('risk_level') or ''}",
                    "",
                    "## Problem",
                    str(patch.get("problem_summary") or ""),
                    "",
                    "## Change",
                    str(patch.get("change_summary") or patch.get("proposed_change_summary") or ""),
                ]
            )
        (patches_dir / f"{sanitize(patch_id)}.md").write_text(str(patch_text), encoding="utf-8")
    drafts_dir = config.output_dir / "updated_skill_drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    for draft in analysis.get("updated_skill_drafts") or []:
        # relative_path 来自 LLM 输出，必须经过 normalize_patch_path 防止越界。
        rel = normalize_patch_path(str(draft.get("relative_path") or f"{draft.get('skill_id', 'skill')}/SKILL.md"))
        target = drafts_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(draft.get("content") or ""), encoding="utf-8")
    report = render_report(analysis)
    (config.output_dir / "diagnosis_report.md").write_text(report, encoding="utf-8")


def sanitize(value: str) -> str:
    """把任意字符串转换成适合作为文件名的安全片段。"""
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value).strip("-") or "patch"


def apply_repaired_skills(config: OfflineSkillRCAConfig, analysis: dict[str, Any]) -> None:
    """把 LLM 生成的 updated skill drafts 应用到新的技能库目录。

    先复制完整原始 skills，再逐个覆盖或新增 LLM 返回的草稿文件。这样输出目录就是
    一份可直接用于 BenchFlow 测试的完整技能库。
    """
    assert config.output_skills_dir is not None
    copy_tree(config.skills_dir, config.output_skills_dir, config.force)
    applied = []
    for draft in analysis.get("updated_skill_drafts") or []:
        content = str(draft.get("content") or "").strip()
        if not content:
            continue
        rel = normalize_patch_path(str(draft.get("relative_path") or f"{draft.get('skill_id', 'skill')}/SKILL.md"))
        target = (config.output_skills_dir / rel).resolve()
        # 再次确认目标路径位于 output_skills_dir 内，防止恶意或异常 LLM 路径写出目录。
        ensure_inside(config.output_skills_dir, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content.rstrip() + "\n", encoding="utf-8")
        applied.append({"relativePath": rel.as_posix(), "operation": draft.get("operation") or "revise"})
    write_json(
        config.output_dir / "applied_repair_manifest.json",
        {
            "sourceSkillsDir": safe_rel(config.root, config.skills_dir),
            "outputSkillsDir": safe_rel(config.root, config.output_skills_dir),
            "applied": applied,
        },
    )


def blocker_descriptions(blockers: list[Any]) -> list[str]:
    """把 non_skill_blocker 统一转换成终端可打印的描述字符串。"""
    out = []
    for blocker in blockers:
        if isinstance(blocker, dict):
            out.append(str(blocker.get("description") or blocker.get("type") or blocker))
        else:
            out.append(str(blocker))
    return out
