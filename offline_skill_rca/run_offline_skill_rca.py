#!/usr/bin/env python3
"""Offline SkillRCA 主入口脚本。

这个文件只负责命令行参数解析和配置对象组装，真正的 repair 流程在
``src.pipeline.run_offline_skill_rca`` 中执行。保持入口脚本轻量，可以让
调试时更容易区分“参数/路径问题”和“多阶段 repair 逻辑问题”。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from src.pipeline import OfflineSkillRCAConfig, run_offline_skill_rca

ROOT = Path(__file__).resolve().parents[1]


def root_path(value: str | None) -> Path | None:
    """把用户传入的相对路径统一解析到仓库根目录下。

    命令行里经常会混用相对路径和绝对路径。这里统一转换成绝对路径，
    后续 pipeline 才能可靠地做越界检查、输出相对路径和复制技能库。
    """
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def parse_args() -> argparse.Namespace:
    """解析 repair 主流程的命令行参数。

    注意：这里不会读取或验证文件内容，只定义 CLI 表面。实际的路径存在性、
    skills/traces 数量和输出目录安全检查都留给 pipeline 层处理。
    """
    parser = argparse.ArgumentParser(description="Run Offline SkillRCA on a task, skills library, and five failed trajectories.")
    parser.add_argument("--task-dir", required=True, help="Task directory containing task.md.")
    parser.add_argument("--skills-dir", required=True, help="Current skills library root containing */SKILL.md.")
    parser.add_argument(
        "--traces",
        nargs="+",
        required=True,
        help="One or more job/run/rollout directories. Exactly five failed rollouts are selected by default.",
    )
    parser.add_argument("--output-dir", required=True, help="Directory for RCA artifacts.")
    parser.add_argument("--output-skills-dir", help="Directory for repaired skills. Defaults to <output-dir>/repaired-skills.")
    parser.add_argument("--strong-base-url", default=os.getenv("OFFLINE_SKILL_RCA_BASE_URL") or "https://api.camel-hub.com")
    parser.add_argument(
        "--strong-api-key", default=os.getenv("OFFLINE_SKILL_RCA_API_KEY") or os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    )
    parser.add_argument("--strong-model", default=os.getenv("OFFLINE_SKILL_RCA_MODEL") or "gpt-5.5")
    parser.add_argument("--use-separate-review-llm", action="store_true")
    parser.add_argument("--review-base-url", default=os.getenv("OFFLINE_SKILL_RCA_REVIEW_BASE_URL") or "")
    parser.add_argument("--review-api-key", default=os.getenv("OFFLINE_SKILL_RCA_REVIEW_API_KEY") or "")
    parser.add_argument("--review-model", default=os.getenv("OFFLINE_SKILL_RCA_REVIEW_MODEL") or "")
    parser.add_argument(
        "--strong-reasoning-effort",
        choices=["off", "minimal", "low", "medium", "high", "max", "xhigh"],
        default=os.getenv("OFFLINE_SKILL_RCA_REASONING_EFFORT") or "minimal",
        help="Repair LLM thinking intensity; off omits reasoning fields.",
    )
    parser.add_argument(
        "--review-reasoning-effort",
        choices=["off", "minimal", "low", "medium", "high", "max", "xhigh"],
        default=os.getenv("OFFLINE_SKILL_RCA_REVIEW_REASONING_EFFORT") or "minimal",
        help="Review LLM thinking intensity when a separate Review LLM is enabled.",
    )
    parser.add_argument("--max-traces", type=int, default=5)
    parser.add_argument("--max-prompt-chars", type=int, default=220_000)
    parser.add_argument(
        "--trace-analysis-workers",
        type=int,
        default=int(os.getenv("OFFLINE_SKILL_RCA_TRACE_WORKERS") or 5),
        help="Parallel repair-LLM calls for Stage 3 per-trajectory failure-event extraction.",
    )
    parser.add_argument(
        "--add-skill-merge-threshold",
        type=int,
        default=int(os.getenv("OFFLINE_SKILL_RCA_ADD_SKILL_MERGE_THRESHOLD") or 0),
        help="Run Stage 6 when add_new_skill exceeds this count; 0 uses the automatic threshold.",
    )
    parser.add_argument(
        "--add-skill-target-count",
        type=int,
        default=int(os.getenv("OFFLINE_SKILL_RCA_ADD_SKILL_TARGET_COUNT") or 0),
        help="Target number of add_new_skill clusters; 0 selects it automatically.",
    )
    parser.add_argument(
        "--max-new-skills",
        type=int,
        default=int(os.getenv("OFFLINE_SKILL_RCA_MAX_NEW_SKILLS") or 2),
        help="Hard maximum number of final add_new_skill actions after Stage 6; 0 disables the cap.",
    )
    parser.add_argument(
        "--skill-word-limit",
        type=int,
        default=int(os.getenv("OFFLINE_SKILL_RCA_SKILL_WORD_LIMIT") or 1200),
        help="Maximum counted units in each generated or revised SKILL.md.",
    )
    parser.add_argument(
        "--stage7-max-operations",
        type=int,
        default=int(os.getenv("OFFLINE_SKILL_RCA_STAGE7_MAX_OPERATIONS") or os.getenv("OFFLINE_SKILL_RCA_STAGE6_MAX_OPERATIONS") or 30),
        help="Maximum repair/review LLM operations in the Stage 7 run-until-complete loop.",
    )
    parser.add_argument(
        "--stage7-repair-mode",
        choices=["per_suggestion", "skill_package"],
        default=os.getenv("OFFLINE_SKILL_RCA_STAGE7_REPAIR_MODE") or "per_suggestion",
        help="Stage 7 transaction granularity: one suggestion or a same-Skill suggestion package.",
    )
    parser.add_argument(
        "--stage7-skill-package-size",
        type=int,
        default=int(os.getenv("OFFLINE_SKILL_RCA_STAGE7_SKILL_PACKAGE_SIZE") or 3),
        help="Maximum revise_existing_skill suggestions in one same-Skill package.",
    )
    parser.add_argument("--force", action="store_true", help="Replace output directories if they already exist.")
    parser.add_argument("--prepare-prompts-only", action="store_true", help="Write the evidence bundle and prompt without calling the LLM.")
    parser.add_argument("--no-apply", action="store_true", help="Do not copy/apply repaired skills; only produce the diagnosis package.")
    return parser.parse_args()


def main() -> int:
    """创建配置并启动 Offline SkillRCA。

    ``--prepare-prompts-only`` 模式不会访问 repair LLM，因此不要求 API key；
    正式运行时必须有 strong-model key，否则无法完成多阶段对话。
    """
    args = parse_args()
    if not args.prepare_prompts_only and not args.strong_api_key:
        print("Missing strong-model API key. Set OFFLINE_SKILL_RCA_API_KEY or pass --strong-api-key.", file=sys.stderr)
        return 2

    output_dir = root_path(args.output_dir)
    assert output_dir is not None
    # 未显式指定输出技能库目录时，默认把修复后的 skills 放在本次输出目录下，
    # 这样不会覆盖原始技能库，便于之后对比和回滚。
    output_skills_dir = root_path(args.output_skills_dir) if args.output_skills_dir else output_dir / "repaired-skills"
    config = OfflineSkillRCAConfig(
        root=ROOT,
        task_dir=root_path(args.task_dir),
        skills_dir=root_path(args.skills_dir),
        trace_paths=[root_path(item) for item in args.traces],
        output_dir=output_dir,
        output_skills_dir=output_skills_dir,
        strong_base_url=args.strong_base_url,
        strong_api_key=args.strong_api_key or "",
        strong_model=args.strong_model,
        use_separate_review_llm=bool(args.use_separate_review_llm),
        review_base_url=args.review_base_url,
        review_api_key=args.review_api_key,
        review_model=args.review_model,
        strong_reasoning_effort=args.strong_reasoning_effort,
        review_reasoning_effort=args.review_reasoning_effort,
        max_traces=args.max_traces,
        max_prompt_chars=args.max_prompt_chars,
        trace_analysis_workers=args.trace_analysis_workers,
        add_skill_merge_threshold=max(0, args.add_skill_merge_threshold),
        add_skill_target_count=max(0, args.add_skill_target_count),
        max_new_skill_count=max(0, args.max_new_skills),
        skill_word_limit=max(100, min(20_000, args.skill_word_limit)),
        stage7_max_operations=max(1, min(1000, args.stage7_max_operations)),
        stage7_repair_mode=args.stage7_repair_mode,
        stage7_skill_package_size=max(1, min(100, args.stage7_skill_package_size)),
        force=args.force,
        prepare_prompts_only=args.prepare_prompts_only,
        apply_repaired_skills=not args.no_apply,
    )
    result = run_offline_skill_rca(config)
    print(f"Output: {result.output_dir}")
    if result.output_skills_dir:
        print(f"Repaired skills: {result.output_skills_dir}")
    if result.non_skill_blockers:
        print("Non-skill blockers:")
        for blocker in result.non_skill_blockers:
            print(f"- {blocker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
