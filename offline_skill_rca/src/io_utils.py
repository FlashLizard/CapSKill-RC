"""Offline SkillRCA 的文件、路径、技能库和轨迹处理工具。

本模块的核心原则是：把任务描述、Skills、agent 失败轨迹、agent/harness 运行错误
和 agent 产物暴露给 repair LLM，同时严格排除所有 verifier 数据和本地诊断。
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from .schemas import SkillCard, SkillFile, TraceStep, Trajectory, to_plain

MAX_TEXT = 12_000
MAX_TRAJ_TEXT = 26_000
MAX_FINAL_ARTIFACT_FILES = int(os.getenv("OFFLINE_SKILL_RCA_MAX_FINAL_ARTIFACT_FILES") or 40)
MAX_FINAL_ARTIFACT_FILE_CHARS = int(os.getenv("OFFLINE_SKILL_RCA_MAX_FINAL_ARTIFACT_FILE_CHARS") or 60_000)
MAX_FINAL_ARTIFACT_TOTAL_CHARS = int(os.getenv("OFFLINE_SKILL_RCA_MAX_FINAL_ARTIFACT_TOTAL_CHARS") or 240_000)
MAX_SKILL_ATTACHED_FILES = int(os.getenv("OFFLINE_SKILL_RCA_MAX_SKILL_ATTACHED_FILES") or 20)
MAX_SKILL_ATTACHED_FILE_CHARS = int(os.getenv("OFFLINE_SKILL_RCA_MAX_SKILL_ATTACHED_FILE_CHARS") or 20_000)
MAX_SKILL_ATTACHED_TOTAL_CHARS = int(os.getenv("OFFLINE_SKILL_RCA_MAX_SKILL_ATTACHED_TOTAL_CHARS") or 80_000)
ATTACHMENT_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
}
CODE_ATTACHMENT_EXTENSIONS = {
    ".bat",
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".jl",
    ".js",
    ".jsx",
    ".m",
    ".mjs",
    ".ps1",
    ".py",
    ".r",
    ".rs",
    ".scala",
    ".sh",
    ".sql",
    ".ts",
    ".tsx",
}
DOCUMENT_ATTACHMENT_EXTENSIONS = {
    ".csv",
    ".json",
    ".jsonl",
    ".md",
    ".rst",
    ".toml",
    ".tsv",
    ".txt",
    ".yaml",
    ".yml",
}
BINARY_ATTACHMENT_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".ico",
    ".jpg",
    ".jpeg",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".dll",
    ".exe",
    ".webp",
    ".xlsx",
    ".xls",
    ".zip",
}

AGENT_ONLY_EVIDENCE_POLICY_VERSION = "agent-only-v1"

# 这些字段描述 agent 或 harness 在 agent 执行期间遇到的运行问题。任何
# verifier_*、reward、metric、test 或 review 字段都故意不在白名单中。
AGENT_RUNTIME_SCALAR_FIELDS = (
    "error",
    "error_category",
    "export_error",
    "partial_trajectory",
    "trajectory_source",
)
AGENT_RUNTIME_STRUCTURED_FIELDS = (
    "idle_timeout_info",
    "agent_timeout_info",
    "sandbox_startup_info",
    "transport_error_info",
    "api_error_info",
    "suspected_api_error_info",
)


def safe_rel(root: Path, path: Path) -> str:
    """把路径尽量转成相对 root 的 POSIX 风格字符串。

    报告、manifest 和 prompt 中使用相对路径更可读；如果路径不在 root 下，则退回
    绝对路径字符串，避免异常影响诊断输出。
    """
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def ensure_inside(root: Path, path: Path) -> Path:
    """确认路径位于 root 内，防止写输出或应用 patch 时越界。"""
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise SystemExit(f"Path escapes root: {path}") from exc
    return resolved


def read_text(path: Path, limit: int | None = None) -> str:
    """读取 UTF-8 文本并按需截断。

    使用 ``errors="replace"`` 是为了兼容轨迹或技能文件中偶发的非法字节；截断
    信息会显式写入文本尾部，方便诊断 prompt 是否被压缩。
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    if limit and len(text) > limit:
        return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"
    return text


def read_json(path: Path | None) -> Any:
    """宽容读取 JSON 文件；不存在或解析失败时返回 None。

    轨迹目录中并非每个辅助文件都一定存在，所以这里选择宽容失败，由调用方决定
    缺失信息是否关键。
    """
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def write_json(path: Path, data: Any) -> None:
    """把对象写成缩进 JSON，并自动创建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_plain(data), ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """把字典列表写成 CSV。

    fieldnames 按首次出现顺序收集，便于 coverage matrix 这类 LLM 输出字段不完全
    固定时仍能稳定落盘。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: scalar(value) for key, value in row.items()})


def scalar(value: Any) -> str:
    """把 CSV 单元格中的复杂值序列化成字符串。"""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def parse_front_matter(text: str) -> dict[str, str]:
    """解析 SKILL.md 顶部的简单 YAML front matter。

    这里故意只做轻量解析，支持 ``key: value`` 这种常见形式即可；复杂 YAML 不
    是 repair 流程的关键依赖。
    """
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    metadata: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip("'\"")
    return metadata


def heading_title(text: str, fallback: str) -> str:
    """从 markdown 一级/多级标题或 front matter 中提取技能标题。"""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip() or fallback
    meta = parse_front_matter(text)
    return meta.get("name") or fallback


def list_skill_files(root: Path, skills_dir: Path) -> list[SkillFile]:
    """扫描 skills 目录下的 ``*/SKILL.md`` 文件。

    SkillsBench 的技能库约定每个 skill 是一个子目录；如果没有发现任何技能文件，
    说明输入路径很可能错误，因此直接终止。
    """
    skills = []
    for path in sorted(skills_dir.glob("*/SKILL.md")):
        text = read_text(path)
        skill_id = path.parent.name
        metadata = parse_front_matter(text)
        title = metadata.get("name") or heading_title(text, skill_id)
        attached_files = list_skill_attached_files(root, path.parent, path)
        skills.append(
            SkillFile(
                skill_id=skill_id,
                title=title,
                content=text,
                path=safe_rel(root, path),
                metadata=metadata,
                attached_files=attached_files or None,
            )
        )
    if not skills:
        raise SystemExit(f"No */SKILL.md files found under {safe_rel(root, skills_dir)}")
    return skills


def list_skill_attached_files(root: Path, skill_dir: Path, main_skill_path: Path) -> list[dict[str, str]]:
    """读取一个 skill 目录下除 ``SKILL.md`` 外的附加文件。

    附加文件是 skill 的一部分，例如代码模板、配置样例、参考说明或小型数据片段。
    repair LLM 需要看到这些文件，否则可能误判 skill 缺少可执行工具或模板。这里
    只读取当前 skill 目录内部的文件，并跳过常见缓存/依赖目录；二进制文件不展开
    原始字节，只提供大小和 hash 占位说明。
    """
    out: list[dict[str, str]] = []
    used_chars = 0
    main_resolved = main_skill_path.resolve()
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.resolve() == main_resolved:
            continue
        if any(part in ATTACHMENT_SKIP_DIRS for part in path.relative_to(skill_dir).parts[:-1]):
            continue
        if len(out) >= MAX_SKILL_ATTACHED_FILES:
            out.append(
                {
                    "path": safe_rel(root, path),
                    "type": "others",
                    "content": f"...[additional attached files omitted after {MAX_SKILL_ATTACHED_FILES} files]",
                }
            )
            break
        remaining = max(0, MAX_SKILL_ATTACHED_TOTAL_CHARS - used_chars)
        if remaining <= 0:
            out.append(
                {
                    "path": safe_rel(root, path),
                    "type": "others",
                    "content": f"...[attached file omitted because total attachment budget {MAX_SKILL_ATTACHED_TOTAL_CHARS} chars was reached]",
                }
            )
            break
        content = read_attached_file_content(path, min(MAX_SKILL_ATTACHED_FILE_CHARS, remaining))
        used_chars += len(content)
        out.append(
            {
                "path": safe_rel(root, path),
                "type": classify_attached_file(path),
                "content": content,
            }
        )
    return out


def classify_attached_file(path: Path) -> str:
    """按文件扩展名粗分附加文件类型，供 repair LLM 决定如何解读内容。"""
    ext = path.suffix.lower()
    if ext in CODE_ATTACHMENT_EXTENSIONS:
        return "code"
    if ext in DOCUMENT_ATTACHMENT_EXTENSIONS:
        return "document"
    return "others"


def read_attached_file_content(path: Path, limit: int) -> str:
    """读取附加文件内容；二进制或不可读文件以可审计占位文本表示。"""
    size = path.stat().st_size
    ext = path.suffix.lower()
    if ext in BINARY_ATTACHMENT_EXTENSIONS or looks_binary(path):
        return f"[binary or non-text attached file omitted; size={size} bytes; sha256={sha256_file(path)}]"
    try:
        return read_text(path, limit)
    except Exception as exc:
        return f"[attached file could not be read as text; size={size} bytes; error={type(exc).__name__}: {exc}]"


def looks_binary(path: Path) -> bool:
    """用前 4KB 是否包含 NUL 字节来粗略判断二进制文件。"""
    try:
        with path.open("rb") as handle:
            chunk = handle.read(4096)
    except Exception:
        return True
    return b"\x00" in chunk


def parse_skill_card(skill: SkillFile) -> SkillCard:
    """用本地规则粗略解析一个 SkillCard。

    这个函数主要给静态检查或调试用；正式 repair v2 中，发给 LLM 的 skill
    标准化结果来自 Stage 1b，不使用本地解析结果。
    """
    sections = split_sections(skill.content)
    return SkillCard(
        skill_id=skill.skill_id,
        title=skill.title,
        intent=first_nonempty(sections.get("description") or sections.get("overview") or [skill.metadata.get("description", "")]),
        triggers=section_items(sections, ["when to use", "triggers", "use when"]),
        anti_triggers=section_items(sections, ["do not use when", "anti-triggers"]),
        prerequisites=section_items(sections, ["inputs", "preconditions", "requirements", "prerequisites"]),
        procedure_steps=section_items(sections, ["procedure", "steps", "process", "implementation", "mpc application"]),
        tools_or_code=code_blocks(skill.content),
        validation_checks=section_items(sections, ["verification checklist", "validation", "checks", "testing"]),
        recovery_steps=section_items(sections, ["recovery", "common failure modes", "troubleshooting"]),
        examples=section_items(sections, ["examples", "minimal template", "code snippet", "python implementation"]),
        related_skills=section_items(sections, ["related skills"]),
        raw_text=skill.content,
        path=skill.path,
    )


def split_sections(text: str) -> dict[str, list[str]]:
    """按 Markdown 标题切分文档章节。"""
    sections: dict[str, list[str]] = {"description": []}
    current = "description"
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*$", line)
        if match:
            current = re.sub(r"[^a-z0-9 -]+", "", match.group(1).lower()).strip()
            sections.setdefault(current, [])
        else:
            sections.setdefault(current, []).append(line)
    return sections


def section_items(sections: dict[str, list[str]], names: list[str]) -> list[str]:
    """从多个候选章节名中提取清洗后的条目列表。"""
    out: list[str] = []
    for name in names:
        for key, lines in sections.items():
            if name in key:
                out.extend(clean_lines(lines))
    return out[:24]


def clean_lines(lines: list[str]) -> list[str]:
    """清理 markdown 列表符号、编号和空行，保留可读文本片段。"""
    out = []
    for line in lines:
        text = line.strip()
        if not text or text in {"```", "---"}:
            continue
        text = re.sub(r"^[-*]\s+", "", text)
        text = re.sub(r"^\d+[.)]\s+", "", text)
        if text:
            out.append(text[:500])
    return out


def first_nonempty(items: list[str]) -> str:
    """返回列表中第一个非空文本，常用于 description fallback。"""
    for item in items:
        if item:
            return item[:500]
    return ""


def code_blocks(text: str) -> list[str]:
    """提取 markdown fenced code block，作为工具模板或代码示例候选。"""
    blocks = re.findall(r"```(?:[A-Za-z0-9_-]+)?\n(.*?)```", text, flags=re.DOTALL)
    return [block.strip()[:1800] for block in blocks[:8] if block.strip()]


_TIMEOUT_TOKENS = (
    "timeout",
    "timed out",
    "time limit",
    "deadline exceeded",
    "idle timeout",
    "agent timeout",
)
_ENVIRONMENT_TOKENS = (
    "environment",
    "configuration",
    "config error",
    "infrastructure",
    "sandbox",
    "container",
    "docker",
    "image pull",
    "permission denied",
    "access denied",
    "api key",
    "authentication",
    "unauthorized",
    "forbidden",
    "rate limit",
    "quota",
    "connection refused",
    "network error",
    "transport error",
    "provider error",
    "proxy error",
)
_TIMEOUT_CATEGORIES = {"timeout", "idle_timeout", "agent_timeout", "verifier_timeout"}
_ENVIRONMENT_CATEGORIES = {
    "environment",
    "environment_error",
    "configuration",
    "configuration_error",
    "config_error",
    "infrastructure",
    "infrastructure_error",
    "sandbox",
    "sandbox_error",
    "setup_error",
    "transport_error",
    "api_error",
    "authentication_error",
    "permission_error",
    "provider_error",
    "network_error",
    "quota_error",
    "rate_limit",
}


def _has_explicit_outcome(result: dict[str, Any]) -> bool:
    """判断 result 是否给出了本地可确认的 0/1 运行结果。

    缺少 outcome 的目录不能被当成失败轨迹：它可能只是尚未完成、导出中断或目录
    结构不完整。这里仅识别 success/reward/score 这些字段是否存在且为标量，不读取
    verifier 文件，也不把 verifier 结构复制给 repair LLM。
    """
    success = result.get("success")
    if isinstance(success, (bool, int, float, str)) and str(success).strip() != "":
        return True
    rewards = result.get("rewards")
    if isinstance(rewards, dict):
        for key in ("reward", "score", "success"):
            value = rewards.get(key)
            if isinstance(value, (bool, int, float, str)) and str(value).strip() != "":
                return True
    for key in ("score", "score_excl_errors"):
        value = result.get(key)
        if isinstance(value, (bool, int, float, str)) and str(value).strip() != "":
            return True
    return False


def _signal_text(result: dict[str, Any]) -> str:
    """只拼接 agent/harness 运行错误字段，供本地分类使用。"""
    values: list[str] = []
    for key in (*AGENT_RUNTIME_SCALAR_FIELDS, *AGENT_RUNTIME_STRUCTURED_FIELDS):
        value = result.get(key)
        if value not in (None, "", [], {}):
            values.append(str(value))
    return " ".join(values).lower()


def _category_text(result: dict[str, Any]) -> str:
    value = result.get("error_category")
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def classify_rollout_for_repair(rollout_dir: Path) -> dict[str, Any]:
    """给一条 rollout 做确定性的本地筛选分类。

    选择条件是：必须有明确的 success=0/reward=0/score=0；同时不能是成功运行、
    超时、环境/配置/API/基础设施类失败。返回的记录仅用于 Web/manifest 诊断，
    不会被加入 ``input_bundle.json``，因此不会扩大 repair LLM 的可见信息。
    """
    result = read_json(rollout_dir / "result.json") or {}
    if not isinstance(result, dict) or not _has_explicit_outcome(result):
        return {
            "rolloutDir": str(rollout_dir.resolve()),
            "selected": False,
            "reason": "missing_explicit_outcome",
            "success": None,
            "taskName": "",
        }

    success = success_from_result(result)
    category = _category_text(result)
    signal_text = _signal_text(result)
    if success:
        reason = "success"
    elif category in _TIMEOUT_CATEGORIES or any(token in signal_text for token in _TIMEOUT_TOKENS):
        reason = "timeout"
    elif category in _ENVIRONMENT_CATEGORIES or any(token in signal_text for token in _ENVIRONMENT_TOKENS):
        reason = "environment_or_configuration_error"
    else:
        reason = "task_failure"
    return {
        "rolloutDir": str(rollout_dir.resolve()),
        "selected": reason == "task_failure",
        "reason": reason,
        "success": success,
        "taskName": str(result.get("task_name") or ""),
        "errorCategory": str(result.get("error_category") or ""),
    }


def discover_rollout_selection(paths: list[Path], max_traces: int) -> tuple[list[Path], list[dict[str, Any]]]:
    """扫描全部 rollout，再筛选明确的非基础设施类失败并应用上限。

    ``max_traces`` 是有效失败轨迹上限，不是扫描上限；因此成功、超时和环境错误
    不会占用名额，也不会导致“少于五条就失败”。
    """
    found: list[Path] = []
    seen: set[Path] = set()
    for input_path in paths:
        if not input_path.exists():
            raise SystemExit(f"Trace path does not exist: {input_path}")
        candidates: list[Path] = []
        if (input_path / "trajectory" / "acp_trajectory.jsonl").exists() or (input_path / "agent" / "acp_trajectory.jsonl").exists():
            candidates.append(input_path)
        for trajectory_path in sorted(input_path.rglob("trajectory/acp_trajectory.jsonl")):
            candidates.append(trajectory_path.parent.parent)
        for trajectory_path in sorted(input_path.rglob("agent/acp_trajectory.jsonl")):
            candidates.append(trajectory_path.parent.parent)
        for candidate in sorted(candidates, key=lambda item: str(item)):
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(resolved)
    records = [classify_rollout_for_repair(path) for path in sorted(found, key=lambda item: str(item))]
    selected = [
        Path(record["rolloutDir"])
        for record in records
        if record.get("selected")
    ][: max(1, int(max_traces))]
    for record in records:
        record["selectedForRepair"] = bool(record.get("selected")) and Path(record["rolloutDir"]) in selected
    if not selected:
        raise SystemExit(
            "No explicit task-failure trajectories found after excluding successful, timeout, "
            "environment, and configuration-error rollouts"
        )
    return selected, records


def discover_rollout_dirs(paths: list[Path], max_traces: int) -> list[Path]:
    """兼容旧调用方，返回新的有效失败轨迹选择结果。"""
    selected, _records = discover_rollout_selection(paths, max_traces)
    return selected


def load_trajectory(root: Path, rollout_dir: Path) -> Trajectory:
    """读取一条 rollout 轨迹。

    读取 ACP 轨迹、agent/harness 运行错误，以及归档或可重建的 agent 最终产物。
    verifier 文件、字段、结果、reward、测试信息和评分过程始终不会进入返回对象。
    """
    trajectory_path = rollout_dir / "trajectory" / "acp_trajectory.jsonl"
    if not trajectory_path.exists():
        trajectory_path = rollout_dir / "agent" / "acp_trajectory.jsonl"
    if not trajectory_path.exists():
        raise SystemExit(f"Missing trajectory jsonl under {safe_rel(root, rollout_dir)}")
    result = read_json(rollout_dir / "result.json") or {}
    task_id = result.get("task_name") or rollout_dir.name.split("__", 1)[0]
    steps, _messages, _tool_calls, _observations = compact_trajectory(trajectory_path)
    return Trajectory(
        traj_id=rollout_dir.name,
        task_id=task_id,
        raw_trajectory_jsonl="",
        steps=steps,
        rollout_dir=safe_rel(root, rollout_dir),
        result=visible_failure_result(root, rollout_dir, result),
        final_artifacts=collect_final_artifacts(root, rollout_dir, trajectory_path),
    )


def success_from_result(result: dict[str, Any]) -> int:
    """从 result.json 中提取 0/1 success。"""
    success = result.get("success")
    if isinstance(success, bool):
        return 1 if success else 0
    if isinstance(success, (int, float)):
        return 1 if success > 0 else 0
    if isinstance(success, str):
        try:
            return 1 if float(success.strip().rstrip("%")) > 0 else 0
        except ValueError:
            pass
    rewards = result.get("rewards")
    if isinstance(rewards, dict):
        for key in ("reward", "score", "success"):
            value = rewards.get(key)
            if isinstance(value, bool):
                return 1 if value else 0
            if isinstance(value, (int, float)):
                return 1 if value > 0 else 0
            if isinstance(value, str):
                try:
                    return 1 if float(value.strip().rstrip("%")) > 0 else 0
                except ValueError:
                    pass
    for key in ("score", "score_excl_errors"):
        value = result.get(key)
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, (int, float)):
            return 1 if value > 0 else 0
        if isinstance(value, str):
            try:
                return 1 if float(value.strip().rstrip("%")) > 0 else 0
            except ValueError:
                pass
    return 0


def visible_failure_result(root: Path, rollout_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    """构造 repair LLM 可见的 agent-only 运行结果。

    ``root`` 和 ``rollout_dir`` 为兼容既有调用签名而保留，但本函数绝不读取 rollout
    中的 verifier 目录。success 可以由本地代码从 reward 推导为单个 0/1 标量，
    reward 的名称、数值结构和其他 verifier 信息不会被复制到输出。
    """
    del root, rollout_dir
    visible: dict[str, Any] = {"success": success_from_result(result)}
    for key in (*AGENT_RUNTIME_SCALAR_FIELDS, *AGENT_RUNTIME_STRUCTURED_FIELDS):
        value = result.get(key)
        if value not in (None, "", [], {}):
            visible[key] = value
    visible["evidence_policy"] = agent_only_evidence_policy()
    return visible


def agent_only_evidence_policy() -> dict[str, str]:
    """返回可机器检查的 Repair LLM 证据策略声明。"""
    return {
        "version": AGENT_ONLY_EVIDENCE_POLICY_VERSION,
        "included": "success 0/1, agent/harness runtime errors, agent trajectory, and agent-produced artifacts",
        "excluded": "all verifier files, fields, results, rewards, tests, rubrics, metrics, reviews, and local RCA conclusions",
    }


def sanitize_agent_only_visible_result(value: Any) -> dict[str, Any]:
    """清洗旧 bundle 中的可见结果，阻断历史 verifier 数据继续传播。

    该兼容路径只保留已经存在的 0/1 success 与 agent/harness 运行字段。不能从旧
    bundle 恢复的信息不会猜测；有原 rollout 时调用方应重新执行 ``load_trajectory``。
    """
    source = value if isinstance(value, dict) else {}
    visible: dict[str, Any] = {"success": success_from_result(source)}
    for key in (*AGENT_RUNTIME_SCALAR_FIELDS, *AGENT_RUNTIME_STRUCTURED_FIELDS):
        item = source.get(key)
        if item not in (None, "", [], {}):
            visible[key] = item
    visible["evidence_policy"] = agent_only_evidence_policy()
    return visible


def sanitize_agent_artifacts(value: Any) -> list[dict[str, Any]]:
    """只保留可归因于 agent 的 artifact，拒绝 verifier 路径和来源标记。"""
    artifacts: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        path_parts = {part.lower() for part in re.split(r"[\\/]", str(item.get("path") or "")) if part}
        capture_source = str(item.get("capture_source") or "").lower()
        if "verifier" in path_parts or "verifier" in capture_source:
            continue
        artifacts.append(item)
    return artifacts


def collect_final_artifacts(root: Path, rollout_dir: Path, trajectory_path: Path | None = None) -> list[dict[str, Any]]:
    """收集 rollout 归档或可由写入轨迹重建的失败最终产物。

    BenchFlow 运行若导出了文件，通常位于 ``artifacts`` 目录。只扫描这些明确的
    artifact 根目录，避免误把容器快照、依赖或 verifier 文件当作 agent 产物。
    若运行没有导出 artifacts，则补充从已完成 Write/Edit 轨迹事件确定性重建的
    文本文件。重建文件会明确标注来源与完整性，不冒充容器快照。
    """
    artifact_roots = [rollout_dir / "artifacts", rollout_dir / "agent" / "artifacts"]
    files: list[Path] = []
    for artifact_root in artifact_roots:
        if artifact_root.is_dir():
            files.extend(path for path in sorted(artifact_root.rglob("*")) if path.is_file())
    out: list[dict[str, Any]] = []
    used_chars = 0
    for path in files[:MAX_FINAL_ARTIFACT_FILES]:
        remaining = MAX_FINAL_ARTIFACT_TOTAL_CHARS - used_chars
        if remaining <= 0:
            break
        item = captured_file(root, path, min(MAX_FINAL_ARTIFACT_FILE_CHARS, remaining))
        item["capture_source"] = "archived_artifact"
        item["content_status"] = "archived_complete" if "[binary file omitted;" not in str(item.get("content")) else "binary_metadata_only"
        used_chars += len(str(item.get("content") or ""))
        out.append(item)
    archived_signatures = {
        (
            Path(str(item.get("path") or "")).name,
            hashlib.sha256(str(item.get("content") or "").encode("utf-8")).hexdigest(),
        )
        for item in out
    }
    if trajectory_path and trajectory_path.is_file() and len(out) < MAX_FINAL_ARTIFACT_FILES:
        for item in reconstruct_written_files_from_trajectory(trajectory_path):
            signature = (
                Path(str(item.get("path") or "")).name,
                hashlib.sha256(str(item.get("content") or "").encode("utf-8")).hexdigest(),
            )
            if signature in archived_signatures:
                continue
            remaining = MAX_FINAL_ARTIFACT_TOTAL_CHARS - used_chars
            if remaining <= 0 or len(out) >= MAX_FINAL_ARTIFACT_FILES:
                break
            content = str(item.get("content") or "")
            if len(content) > min(MAX_FINAL_ARTIFACT_FILE_CHARS, remaining):
                limit = min(MAX_FINAL_ARTIFACT_FILE_CHARS, remaining)
                item["content"] = content[:limit] + f"\n...[truncated {len(content) - limit} chars]"
                item["content_status"] = "trajectory_reconstruction_truncated"
            used_chars += len(str(item.get("content") or ""))
            out.append(item)
    return out


def reconstruct_written_files_from_trajectory(path: Path) -> list[dict[str, Any]]:
    """从 ACP 的已完成 Write/Edit diff 中重建 agent 生成的文本文件。

    只有首次完整 Write 后可连续应用的 Edit 才标为完整；无法应用的局部 diff 会
    保留最佳可见内容并标记 partial，避免把推测结果伪装成最终容器文件。
    """
    files: dict[str, dict[str, Any]] = {}

    def visit(value: Any, event_step: int) -> None:
        if isinstance(value, list):
            for child in value:
                visit(child, event_step)
            return
        if not isinstance(value, dict):
            return
        file_path = value.get("path")
        new_text = value.get("newText")
        if value.get("type") == "diff" and isinstance(file_path, str) and isinstance(new_text, str):
            normalized_path = file_path.replace("\\", "/")
            if re.search(r"(?:^|/)(?:skills|verifier)(?:/|$)", normalized_path, re.I):
                return
            record = files.setdefault(
                normalized_path,
                {"content": "", "complete": False, "last_step": event_step, "unapplied_edits": 0},
            )
            old_text = value.get("oldText")
            if old_text is None:
                record["content"] = new_text
                record["complete"] = True
            elif isinstance(old_text, str) and old_text in record["content"]:
                record["content"] = record["content"].replace(old_text, new_text, 1)
            elif new_text in record["content"]:
                # ACP 可能同时记录同一次编辑的精确与扩展 diff；已应用时忽略重复项。
                pass
            else:
                if not record["content"]:
                    record["content"] = new_text
                record["complete"] = False
                record["unapplied_edits"] += 1
            record["last_step"] = event_step
            return
        for child in value.values():
            visit(child, event_step)

    for step_id, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "tool_call" or str(event.get("status") or "").lower() != "completed":
            continue
        visit(event.get("content"), step_id)

    out = []
    for file_path, record in files.items():
        content = str(record.get("content") or "")
        if not content:
            continue
        out.append(
            {
                "path": file_path,
                "type": classify_attached_file(Path(file_path)),
                "size_bytes": len(content.encode("utf-8")),
                "content": content,
                "capture_source": "trajectory_file_write",
                "content_status": (
                    "trajectory_reconstructed_complete"
                    if record.get("complete") and not record.get("unapplied_edits")
                    else "trajectory_reconstructed_partial"
                ),
                "last_observed_step_id": record.get("last_step"),
            }
        )
    return sorted(out, key=lambda item: str(item.get("path") or ""))


def captured_file(root: Path, path: Path, limit: int) -> dict[str, Any]:
    """把一个结果文件转换为可审计的文本或二进制摘要。"""
    size = path.stat().st_size
    if path.suffix.lower() in BINARY_ATTACHMENT_EXTENSIONS or looks_binary(path):
        content = f"[binary file omitted; size={size} bytes; sha256={sha256_file(path)}]"
    else:
        content = read_text(path, max(1, limit))
    return {
        "path": safe_rel(root, path),
        "type": classify_attached_file(path),
        "size_bytes": size,
        "content": content,
    }

def compact_trajectory(path: Path) -> tuple[list[TraceStep], list[str], list[dict[str, Any]], list[str]]:
    """把 ACP JSONL 轨迹格式化成 prompt 友好的 step 结构。

    该函数只遍历轨迹事件本身，用本地代码提取可见文本、事件类型、状态、artifact
    名称和显式错误标记；不会调用任何 LLM，也不会截断轨迹 step 内容。
    """
    raw_steps: list[TraceStep] = []
    messages: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    observations: list[str] = []
    skill_names = set(re.findall(r"[/\\]skills[/\\]([^/\\]+)[/\\]SKILL\.md", read_text(path, 300_000)))
    for idx, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
        try:
            event = json.loads(line)
        except Exception:
            continue
        event_type = str(event.get("type") or "")
        text = extract_text(event)
        mentioned = sorted(name for name in skill_names if name and name in text)
        status = str(event.get("status") or "")
        title = str(event.get("title") or event.get("kind") or event_type)
        action_type = map_action_type(event_type, title, text)
        action_text = text
        error_signal = None
        if status.lower() == "failed" or re.search(r"\b(error|failed|traceback|assertionerror|timeout|keyerror)\b", text, re.I):
            error_signal = text
        if event_type in {"user_message", "agent_message", "agent_thought"} and text:
            messages.append(summarize_text(text, 2200))
        if event_type == "tool_call":
            tool_calls.append({"step_id": idx, "title": title, "status": status, "summary": summarize_text(text, 900)})
            observations.append(text)
        raw_steps.append(
            TraceStep(
                step_id=idx,
                role=role_for_event(event_type),
                action_type=action_type,
                action_summary=action_text,
                event_type=event_type,
                status=status,
                raw_visible_text=text,
                tool_name=title if event_type == "tool_call" else None,
                tool_input=None,
                observation_summary=text if event_type == "tool_call" else None,
                mentioned_skills=mentioned,
                produced_artifacts=artifacts_from_text(text),
                error_signal=error_signal,
            )
        )
    return raw_steps, messages, tool_calls, observations


def role_for_event(event_type: str) -> str:
    """把 ACP event type 映射成粗粒度角色。"""
    if event_type.startswith("agent"):
        return "agent"
    if event_type == "user_message":
        return "user"
    if event_type == "tool_call":
        return "tool"
    return event_type or "unknown"


def map_action_type(event_type: str, title: str, text: str) -> str:
    """把原始事件文本映射到固定 action_type 枚举。

    这个分类是启发式的，目的不是精确重建执行，而是帮助 repair LLM 快速定位
    skill read、file inspection、verification、error recovery 等关键行为。
    """
    low = f"{event_type} {title} {text}".lower()
    if event_type == "user_message":
        return "planning"
    if "read" in low and "skill" in low:
        return "skill_read"
    if "skill" in low:
        return "skill_search"
    if "read" in low or "ls" in low or "cat" in low:
        return "file_inspection"
    if "bash" in low or "terminal" in low or "execute" in low:
        return "tool_execution"
    if "python" in low or ".py" in low:
        return "code_execution"
    if "write" in low or "edit" in low or ".json" in low:
        return "artifact_generation"
    if "pytest" in low or "verifier" in low or "test" in low:
        return "verification"
    if "error" in low or "failed" in low:
        return "error_recovery"
    if event_type == "agent_message":
        return "final_answer"
    return "planning"


def extract_text(value: Any, limit: int | None = None) -> str:
    """从嵌套 JSON 事件中递归抽取可读文本。

    只访问常见文本字段。Stage 2 轨迹要求完整保留，因此默认不截断；调用方如需
    限制长度，可以显式传入 ``limit``。
    """
    out: list[str] = []

    def visit(item: Any) -> None:
        """递归访问 JSON-like 对象，收集字符串叶子节点。"""
        if limit is not None and sum(len(part) for part in out) > limit:
            return
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            for key in ("text", "title", "status", "kind", "type", "content", "input", "output"):
                if key in item:
                    visit(item[key])
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    text = "\n".join(part for part in out if part)
    return text[:limit] if limit is not None else text


def summarize_text(text: str, limit: int) -> str:
    """压缩空白并按字符数截断文本。"""
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + f"...[truncated {len(cleaned) - limit} chars]"


def artifacts_from_text(text: str) -> list[str]:
    """从文本中提取常见产物文件名，用于轨迹摘要。"""
    names = sorted(set(re.findall(r"[\w.-]+\.(?:json|csv|xlsx|py|md|png|txt)", text)))
    return names[:12]


def select_steps(steps: list[TraceStep]) -> list[TraceStep]:
    """从长轨迹中选择最有诊断价值的步骤。

    策略是保留开头、结尾，以及带错误信号、skill_read、verification、
    artifact_generation 的步骤，最多控制在 80 条左右。
    """
    if len(steps) <= 80:
        return steps
    keep: dict[int, TraceStep] = {}
    for step in steps[:12] + steps[-18:]:
        keep[step.step_id] = step
    for step in steps:
        if step.error_signal or step.action_type in {"skill_read", "verification", "artifact_generation"}:
            keep[step.step_id] = step
        if len(keep) >= 80:
            break
    return [keep[key] for key in sorted(keep)]


def sha256_file(path: Path) -> str:
    """计算文件 SHA256，供需要做内容指纹时使用。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_tree(source: Path, target: Path, force: bool) -> None:
    """复制原始 skills 目录到修复输出目录。

    默认不覆盖已有目录；只有显式 ``force`` 时才删除旧输出，降低误覆盖风险。
    """
    if target.exists():
        if not force:
            raise SystemExit(f"Output skills dir already exists: {target} (use --force)")
        shutil.rmtree(target)
    shutil.copytree(source, target)


def normalize_patch_path(relative_path: str) -> Path:
    """把 LLM 返回的 skill 相对路径规范化为安全 Path。

    这里会拒绝 ``..`` 越界路径，并兼容 LLM 偶尔返回 ``skills/`` 或
    ``environment/skills/`` 前缀的情况。
    """
    value = relative_path.replace("\\", "/").strip().lstrip("/")
    if value.startswith("../") or "/../" in value:
        raise SystemExit(f"Unsafe patch path: {relative_path}")
    if value.startswith("skills/"):
        value = value[len("skills/") :]
    if "/environment/skills/" in value:
        value = value.split("/environment/skills/", 1)[1]
    return Path(value)
