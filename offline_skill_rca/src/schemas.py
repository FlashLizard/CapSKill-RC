"""Offline SkillRCA 的轻量数据结构定义。

这些 dataclass 只描述“本地脚本能安全持有的数据”。真正的能力图、fault card、
patch plan 等结构由 repair LLM 按 prompt schema 输出，因此这里没有把所有
LLM 产物都建成强类型类，避免 schema 调整时需要同步修改大量 Python 类型。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class SkillFile:
    """从磁盘读取到的一份原始技能文件。

    ``content`` 是完整 SKILL.md 文本；``path`` 使用相对仓库根目录的形式，
    这样 prompt、报告和可视化页面中显示路径时更稳定。
    """
    skill_id: str
    title: str
    content: str
    path: str
    metadata: dict[str, Any] = field(default_factory=dict)
    attached_files: list[dict[str, Any]] | None = None


@dataclass
class SkillCard:
    """规则解析得到的技能静态画像。

    这个结构主要用于本地预览和静态检查；当前 v2 repair 流程中，面向 LLM 的
    skill 标准化结果由 Stage 1b 的 repair LLM 调用生成，脚本不把本地打分作为
    repair 证据发给 LLM，以避免“本地推断替 repair LLM 做决定”。
    """
    skill_id: str
    title: str
    intent: str = ""
    triggers: list[str] = field(default_factory=list)
    anti_triggers: list[str] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)
    procedure_steps: list[str] = field(default_factory=list)
    tools_or_code: list[str] = field(default_factory=list)
    validation_checks: list[str] = field(default_factory=list)
    recovery_steps: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    related_skills: list[str] = field(default_factory=list)
    raw_text: str = ""
    path: str = ""


@dataclass
class TraceStep:
    """从 ACP trajectory 中压缩出来的单步行为摘要。

    这里保存的是可见轨迹证据：动作类型、摘要、工具名、artifact 和显式错误
    信号。``raw_visible_text`` 是从原始轨迹事件中确定性抽取的可见文本，不是
    LLM 总结；不会加入 verifier 的隐藏过程信息。
    """
    step_id: int
    role: str
    action_type: str
    action_summary: str
    event_type: str = ""
    status: str = ""
    raw_visible_text: str | None = None
    tool_name: str | None = None
    tool_input: str | None = None
    observation_summary: str | None = None
    mentioned_skills: list[str] = field(default_factory=list)
    produced_artifacts: list[str] = field(default_factory=list)
    error_signal: str | None = None


@dataclass
class Trajectory:
    """一条失败 rollout 的轨迹输入。

    ``result`` 只保存 0/1 success 与 agent/harness 运行错误，不包含任何 verifier
    文件、字段、输出、reward、测试信息、metric、review 或本地推断。
    ``final_artifacts`` 优先保存 rollout 明确归档的 agent 最终文件，
    并可补充从完成的 Write/Edit 事件重建的文本文件；每项都会标注来源与完整性。
    文本文件保留内容，二进制文件只保留大小与 hash。面向 LLM 的输入使用本地代码
    格式化的 ``steps``，不会发送 messages、tool_calls 或 rollout_dir。
    """
    traj_id: str
    task_id: str
    raw_trajectory_jsonl: str
    messages: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    final_output: str | None = None
    steps: list[TraceStep] = field(default_factory=list)
    rollout_dir: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    final_artifacts: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class OfflineSkillRCAConfig:
    """一次 Offline SkillRCA 运行的全部配置。

    这里集中保存路径、模型参数和运行开关，方便主流程、断点续跑脚本和验证脚本
    复用相同的配置语义。
    """
    root: Path
    task_dir: Path | None
    skills_dir: Path | None
    trace_paths: list[Path | None]
    output_dir: Path
    output_skills_dir: Path | None
    strong_base_url: str
    strong_api_key: str
    strong_model: str
    # Stage 7 的 review 调用可选择独立模型。关闭时三个 review 字段不会参与
    # 路由，审查与修复共用 strong_* 配置；API key 不会写入运行 manifest。
    use_separate_review_llm: bool = False
    review_base_url: str = ""
    review_api_key: str = ""
    review_model: str = ""
    # Repair 与 Review 可以分别选择思维强度。off 表示不向兼容网关发送
    # reasoning_effort；minimal/low/medium/high/max/xhigh 由客户端按供应商规范化。
    strong_reasoning_effort: str = "minimal"
    review_reasoning_effort: str = "minimal"
    max_traces: int = 5
    max_prompt_chars: int = 170_000
    trace_analysis_workers: int = 5
    # Stage 6 中 0 表示使用基于建议总量的自动阈值/自动聚类数。
    add_skill_merge_threshold: int = 0
    add_skill_target_count: int = 0
    # Stage 6 的硬上限。只要 add_new_skill 数量超过该值，就必须先合并；
    # 0 表示不启用硬上限。当前实验默认最多新增 2 个 skill。
    max_new_skill_count: int = 2
    # 中文按单个汉字、英文按空白/标点分隔后的单词计数。
    skill_word_limit: int = 1200
    # “运行至完成”单次后台命令允许的 Repair/Review LLM 调用总数。
    # 达到上限后 Stage 7 自动暂停，避免反复拒绝造成无限运行。
    stage7_max_operations: int = 30
    # per_suggestion: 每条建议一个事务；skill_package: 同一已有 Skill 的修复
    # 建议按上限组成事务包。add_new_skill 在两种模式下始终独立执行。
    stage7_repair_mode: str = "per_suggestion"
    stage7_skill_package_size: int = 3
    force: bool = False
    prepare_prompts_only: bool = False
    apply_repaired_skills: bool = True


@dataclass
class OfflineSkillRCAResult:
    """主流程返回给 CLI 的精简结果。

    大型产物已经写入 output_dir，这里只返回最常用于终端展示的路径和 blocker。
    """
    output_dir: Path
    output_skills_dir: Path | None
    non_skill_blockers: list[str] = field(default_factory=list)


def to_plain(value: Any) -> Any:
    """把 dataclass、Path 等对象递归转换成可 JSON 序列化的普通对象。

    输出文件和 LLM prompt 都需要稳定的 JSON 表示；这个函数负责统一清理
    Python 专用类型，避免每个写文件的位置重复处理。
    """
    if is_dataclass(value):
        return {key: to_plain(val) for key, val in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): to_plain(val) for key, val in value.items()}
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value
