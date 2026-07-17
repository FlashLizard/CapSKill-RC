#!/usr/bin/env python3
"""SkillsBench 本地 Docker/BenchFlow 辅助命令。

Web 控制台适合交互式并行评测；本脚本为 Linux CI、迁移后的 smoke test 和单任务
容器预构建提供稳定的命令行入口。它不保存 API key，也不自行执行 verifier。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def root() -> Path:
    return Path(__file__).resolve().parents[1]


def task_dir(task: str) -> Path:
    value = Path(task)
    if value.is_absolute():
        path = value
    else:
        path = root() / "tasks" / value
        if not path.exists():
            path = root() / "tasks-extra" / value
    if not (path / "task.md").exists():
        raise SystemExit(f"Task not found or missing task.md: {path}")
    return path.resolve()


def require(command: str) -> str:
    value = shutil.which(command)
    if not value:
        raise SystemExit(f"Required command is not installed or not on PATH: {command}")
    return value


def check(_: argparse.Namespace) -> int:
    for command in ("git", "node", "docker"):
        print(f"{command}: {require(command)}")
    uv = shutil.which("uv")
    print(f"uv: {uv or '(missing; install uv for BenchFlow runs)'}")
    print(f"project: {root()}")
    return 0


def build(args: argparse.Namespace) -> int:
    require("docker")
    directory = task_dir(args.task) / "environment"
    dockerfile = directory / "Dockerfile"
    if not dockerfile.exists():
        raise SystemExit(f"Task has no environment/Dockerfile: {directory}")
    image = args.image or f"skillsbench/{args.task.replace('_', '-').lower()}:local"
    command = ["docker", "build", "--tag", image, "--file", str(dockerfile), str(directory)]
    print("+", " ".join(command))
    subprocess.run(command, check=True)
    print(f"Built image: {image}")
    return 0


def run_bench(args: argparse.Namespace) -> int:
    require("uv")
    task = task_dir(args.task)
    jobs_dir = Path(args.jobs_dir)
    if not jobs_dir.is_absolute():
        jobs_dir = root() / jobs_dir
    jobs_dir.mkdir(parents=True, exist_ok=True)
    command = [
        "uv", "run", "bench", "eval", "run",
        "--tasks-dir", str(task),
        "--agent", args.agent,
        "--model", args.model,
        "--sandbox", "docker",
        "--jobs-dir", str(jobs_dir),
        "--concurrency", str(max(1, args.concurrency)),
    ]
    if args.skills_dir:
        skills = Path(args.skills_dir)
        if not skills.is_absolute():
            skills = root() / skills
        command.extend(["--skills-dir", str(skills)])
    if args.skill_mode:
        effective_skill_mode = "with-skill" if args.skill_mode == "force-skill" else args.skill_mode
        command.extend(["--skill-mode", effective_skill_mode])
    if args.reasoning_effort:
        command.extend(["--reasoning-effort", args.reasoning_effort])
    env = os.environ.copy()
    if args.base_url:
        env["BENCHFLOW_PROVIDER_BASE_URL"] = args.base_url
        env["ANTHROPIC_BASE_URL"] = args.base_url
    env["BENCHFLOW_PROVIDER_TYPE"] = args.provider
    env["SKILLSBENCH_PROVIDER"] = args.provider
    if args.skill_mode == "force-skill":
        # 命令行工具不能自动改写 task 目录；这个环境变量供支持该约定的
        # BenchFlow/agent 适配器读取，同时明确把实际 CLI skill mode 保持为 with-skill。
        env["SKILLSBENCH_PROMPT_MODE"] = "force-all-skills"
    if args.api_key:
        env["BENCHFLOW_PROVIDER_API_KEY"] = args.api_key
        env["ANTHROPIC_API_KEY"] = args.api_key
    print("+", " ".join(command))
    return subprocess.run(command, cwd=root(), env=env).returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build task containers and run local SkillsBench evaluations")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--task", required=True)
    build_parser.add_argument("--image")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--task", required=True)
    run_parser.add_argument("--agent", default="claude-agent-acp")
    run_parser.add_argument("--model", required=True)
    run_parser.add_argument("--skills-dir")
    run_parser.add_argument(
        "--skill-mode",
        choices=["no-skill", "with-skill", "force-skill"],
        default="with-skill",
        help="no-skill 不注入 skill；with-skill 正常提供；force-skill 要求适配器强制调用全部 skill。",
    )
    run_parser.add_argument(
        "--provider",
        choices=["deepseek", "anthropic", "openai", "custom"],
        default=os.getenv("BENCHFLOW_PROVIDER_TYPE", "deepseek"),
        help="供应商标签；实际兼容协议由 --base-url 与 harness 决定。",
    )
    run_parser.add_argument("--jobs-dir", default="jobs/local")
    run_parser.add_argument("--concurrency", type=int, default=1)
    run_parser.add_argument("--base-url", default=os.getenv("BENCHFLOW_PROVIDER_BASE_URL", ""))
    run_parser.add_argument("--api-key", default=os.getenv("BENCHFLOW_PROVIDER_API_KEY", ""))
    run_parser.add_argument("--reasoning-effort", default=os.getenv("BENCHFLOW_REASONING_EFFORT", ""))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "check":
        return check(args)
    if args.command == "build":
        return build(args)
    return run_bench(args)


if __name__ == "__main__":
    raise SystemExit(main())
