import {
  renderRawTextBlock,
  renderReadableContent,
  renderReadableLlmText,
} from "./repair-readable.js";

const TABS = [
  { id: "guide", label: "说明" },
  { id: "system", label: "System" },
  { id: "evidence", label: "Evidence" },
  { id: "prompt", label: "Prompt" },
  { id: "request", label: "Request" },
  { id: "response", label: "Response" },
  { id: "parsed", label: "Parsed" },
  { id: "template", label: "Template" },
  { id: "output", label: "Output" },
  { id: "logs", label: "Logs" },
];

const SYSTEM_TEMPLATE_PATH = "offline_skill_rca/prompt_templates/system-prompt.txt";

const TEMPLATE_STAGE_DEFS = [
  {
    id: "stage-01-input-standardization",
    key: "stage_01_input_standardization",
    label: "Stage 1 输入标准化",
    template: "stage-01-input-standardization.txt",
    deps: [],
    status: "template",
    depsReady: true,
    children: [
      {
        index: 0,
        kind: "task",
        id: "stage-01a-task-description-standardization",
        name: "stage-01a-task-description-standardization",
        label: "Stage 1a 任务描述标准化",
        template: "stage-01a-task-description-standardization.txt",
        status: "template",
      },
      {
        index: 1,
        kind: "skill",
        id: "stage-01b-skill-standardization",
        name: "stage-01b-skill-standardization",
        label: "Stage 1b 单个 Skill 标准化",
        template: "stage-01b-skill-standardization.txt",
        status: "template",
      },
    ],
  },
  {
    id: "stage-02-capability-graph",
    key: "stage_02_capability_graph",
    label: "Stage 2 能力图与技能覆盖",
    template: "stage-02-capability-graph.txt",
    deps: ["stage_01a_task_description_standardization", "stage_01b_skill_standardizations"],
    status: "template",
  },
  {
    id: "stage-03-failure-event-extraction",
    key: "stage_03_failure_events_by_trace",
    label: "Stage 3 逐轨迹失败因果抽取",
    template: "stage-03-failure-event-extraction.txt",
    deps: [
      "stage_01a_task_description_standardization",
    ],
    status: "template",
    parallel: true,
  },
  {
    id: "stage-04-failure-event-alignment",
    key: "stage_04_failure_event_alignment",
    label: "Stage 4 失败/原因事件到能力节点对齐",
    template: "stage-04-failure-event-alignment.txt",
    deps: ["stage_02_capability_graph", "stage_03_failure_events_by_trace"],
    status: "template",
  },
  {
    id: "stage-05-node-execution-assessment",
    key: "stage_05_node_execution_assessments",
    label: "Stage 5 逐轨迹能力节点执行判断",
    template: "stage-05-node-execution-assessment.txt",
    deps: ["stage_02_capability_graph", "stage_03_failure_events_by_trace", "stage_04_failure_event_alignment"],
    status: "template",
    parallel: true,
    calculable: true,
  },
  {
    id: "stage-06-skill-repair-suggestions",
    key: "stage_06_skill_repair_suggestions",
    label: "Stage 6 逐能力节点技能修复建议",
    template: "stage-06-skill-repair-suggestions.txt",
    deps: [
      "stage_01b_skill_standardizations",
      "stage_02_capability_graph",
      "stage_03_failure_events_by_trace",
      "stage_04_failure_event_alignment",
      "stage_05_node_execution_assessments",
    ],
    status: "template",
    parallel: true,
  },
  {
    id: "stage-07-repair-action-merge",
    key: "stage_07_repair_action_merge",
    label: "Stage 7 修复操作合并",
    template: "stage-07-repair-action-merge.txt",
    deps: ["stage_06_skill_repair_suggestions"],
    status: "template",
  },
  {
    id: "stage-08-transactional-skill-repair",
    key: "stage_08_transactional_skill_repair",
    label: "Stage 8 事务式技能修复",
    template: "stage-08-skill-repair.txt",
    deps: [
      "stage_01b_skill_standardizations",
      "stage_02_capability_graph",
      "stage_03_failure_events_by_trace",
      "stage_04_failure_event_alignment",
      "stage_05_node_execution_assessments",
      "stage_06_skill_repair_suggestions",
      "stage_07_repair_action_merge",
    ],
    status: "template",
    children: [
      {
        index: "template-repair",
        kind: "template",
        operation: "repair",
        id: "stage-08-skill-repair",
        label: "Stage 8 模板 · 生成候选文件",
        template: "stage-08-skill-repair.txt",
        status: "template",
      },
      {
        index: "template-review",
        kind: "template",
        operation: "review",
        id: "stage-08-skill-review",
        label: "Stage 8 模板 · 审查候选文件",
        template: "stage-08-skill-review.txt",
        status: "template",
      },
    ],
  },
];

const TEMPLATE_PLACEHOLDERS = {
  "stage-01-input-standardization": {
    variables: [],
    external: ["<stage_01a_task_description_standardization>", "<stage_01b_skill_standardizations>"],
  },
  "stage-01a-task-description-standardization": {
    variables: ["{{task_standardization_schema}}"],
    external: ["<task_description>"],
  },
  "stage-01b-skill-standardization": {
    variables: ["{{skill_standardization_schema}}"],
    external: ["<skill_file>", "<skill_attached_files>"],
  },
  "stage-02-capability-graph": {
    variables: ["{{stage2_schema}}"],
    external: [
      "<stage_01a_task_description_standardization>",
      "<stage_01b_skill_standardizations>",
    ],
  },
  "stage-03-failure-event-extraction": {
    variables: ["{{trace_analysis_schema}}"],
    external: [
      "<stage_01a_task_description_standardization>",
      "<trajectory>",
      "<visible_failure_result>",
      "<final_artifacts>",
    ],
  },
  "stage-04-failure-event-alignment": {
    variables: ["{{failure_event_alignment_schema}}"],
    external: ["<stage_02_capability_graph>", "<stage_03_failure_events_by_trace>"],
  },
  "stage-05-node-execution-assessment": {
    variables: ["{{node_execution_assessment_schema}}"],
    external: [
      "<stage_02_capability_graph>",
      "<trajectory>",
      "<stage_03_failure_causality>",
      "<stage_04_event_node_alignments>",
    ],
  },
  "stage-06-skill-repair-suggestions": {
    variables: ["{{stage6_schema}}"],
    external: [
      "<stage_02_capability_graph>",
      "<node_id>",
      "<node_repair_action>",
      "<node_bound_evidence>",
      "<node_related_skill_library>",
      "<stage_01b_skill_standardizations>",
    ],
  },
  "stage-07-repair-action-merge": {
    variables: ["{{repair_action_merge_schema}}"],
    external: ["<merge_config>", "<max_new_skill_count>", "<skill_word_limit>", "<add_new_skill_actions>"],
  },
  "stage-08-transactional-skill-repair": {
    variables: ["{{stage8_repair_schema}}", "{{stage8_review_schema}}", "{{claude_code_skill_spec}}"],
    external: [
      "<repair_unit_id>",
      "<suggestion_ids>",
      "<repair_action>",
      "<selected_stage6_suggestions>",
      "<allowed_skill_root>",
      "<current_related_files>",
      "<current_skill_library_inventory>",
      "<previous_review_feedback>",
      "<files_before_this_attempt>",
      "<candidate_modified_files>",
      "<current_skill_library_inventory>",
      "<related_skill_summaries>",
      "<capability_nodes>", "<suggestion_evidence>",
      "<node_execution_context>", "<coverage_context>",
      "<skill_word_limit>",
    ],
  },
  "stage-08-skill-repair": {
    variables: ["{{stage8_repair_schema}}", "{{claude_code_skill_spec}}"],
    external: [
      "<repair_unit_id>", "<suggestion_ids>",
      "<repair_action>", "<selected_stage6_suggestions>",
      "<allowed_skill_root>", "<current_related_files>",
      "<current_skill_library_inventory>", "<previous_review_feedback>",
      "<skill_word_limit>",
    ],
  },
  "stage-08-skill-review": {
    variables: ["{{stage8_review_schema}}", "{{claude_code_skill_spec}}"],
    external: [
      "<repair_unit_id>", "<suggestion_ids>",
      "<selected_stage6_suggestions>",
      "<files_before_this_attempt>", "<candidate_modified_files>",
      "<current_skill_library_inventory>", "<related_skill_summaries>",
      "<capability_nodes>", "<suggestion_evidence>",
      "<node_execution_context>", "<coverage_context>",
      "<skill_word_limit>",
    ],
  },
};

const SYSTEM_PLACEHOLDERS = {
  variables: ["{{stage_name}}"],
  external: [],
};

const STAGE_GUIDES = {
  "stage-01-input-standardization": {
    purpose: "Stage 1 是输入标准化 wrapper。它本身不直接调用 repair LLM，而是由 Stage 1a 和 Stage 1b 两类子调用组成。",
    inputs: ["原始 task_description", "原始 skill 文件及 skill 目录附加文件"],
    outputs: ["stage_01a_task_description_standardization", "stage_01b_skill_standardizations"],
    rules: [
      "Stage 1 不读取失败轨迹，也不读取 constraints。",
      "task 与每个 skill 分开对话，避免一次上下文过长。",
      "skill 附加文件会作为 attached_files 输入给对应 skill 标准化调用。",
    ],
    formulas: ["无数值评分公式；这是结构化抽取阶段。"],
  },
  "stage-01a-task-description-standardization": {
    purpose: "把原始任务描述标准化成可供后续能力图使用的任务约束、输入、输出、成功条件和禁止泄漏边界。",
    inputs: ["task_description"],
    outputs: ["task contract：task_id、summary、inputs、required_outputs、constraints、success_criteria、ambiguities"],
    rules: [
      "只使用任务文本，不看失败轨迹和 verifier 过程。",
      "不求解任务，不生成最终答案。",
    ],
    formulas: ["无数值评分公式；输出是任务语义的结构化表示。"],
  },
  "stage-01b-skill-standardization": {
    purpose: "逐个 skill 文件标准化为 SkillCard 风格结构，明确 intent、triggers、procedure、verification、recovery、tools/templates 和 attached_files。",
    inputs: ["单个 skill_file", "该 skill 目录下的 skill_attached_files"],
    outputs: ["单个标准化 skill 对象"],
    rules: [
      "每个 skill 单独调用 repair LLM。",
      "attached_files 作为 skill 的一部分，不单独进行任务推断。",
      "只归纳 skill 内容，不依据失败轨迹评价 skill 好坏。",
    ],
    formulas: [
      "本阶段不计算质量分数。",
      "可审阅槽位集合：S = {intent, triggers, inputs, outputs, procedure, verification, recovery, tools_or_templates, limits, attached_files}",
    ],
  },
  "stage-02-capability-graph": {
    purpose: "根据标准化任务与 Skills 构造能力图，并在同一语义上下文中分析每个能力节点与每个 Skill 的覆盖关系。",
    inputs: ["stage_01a_task_description_standardization", "stage_01b_skill_standardizations"],
    outputs: ["capability_graph", "coverage_pairs", "本地计算后的覆盖字段与 node_coverage_summary"],
    rules: [
      "不要解题，只分解能力。",
      "节点表示可复用能力，不表示一次性任务步骤。",
      "每个节点包含 inputs、outputs、operations 和 checks。",
      "必须为每个能力节点与每个 Skill 输出一条 node-skill 覆盖记录。",
      "本阶段不读取轨迹；LLM 不计算聚合覆盖分数，公式字段由本地代码补齐。",
    ],
    formulas: [
      "能力图：G = (V, E)",
      "节点：v ∈ V 表示一个能力节点 CapabilityNode",
      "依赖边：e = (u, v) ∈ E 表示必须先满足 u，才能可靠执行 v",
      "直接相关门控：directly_relevant(node, skill) ∈ {true, false}",
      "若直接相关：overall_coverage = Σ applicable_i × weight_i × score_i / Σ applicable_i × weight_i",
      "默认权重：requirement_fit=0.25, trigger=0.20, procedure=0.25, verification=0.20, recovery=0.10",
      "execution_support：not_needed 不计入；helpful 权重 0.10；required 权重 0.25",
      "coverage_gap = 1 - overall_coverage；coverage_labels 最多 3 个，按维度分数从低到高排列",
    ],
  },
  "stage-03-failure-event-extraction": {
    purpose: "逐条轨迹并行抽取具体失败事件、导致失败的原因事件，以及两者之间有证据支持的因果关系。",
    inputs: ["task contract", "单条 formatted agent trajectory", "agent-only visible_failure_result", "归档或轨迹重建的 agent artifacts"],
    outputs: ["failure_events", "cause_events", "causal_links", "evidence_limits"],
    rules: [
      "每条轨迹独立调用 repair LLM，可并行运行。",
      "提供给 repair LLM 的 trajectory 使用本地代码从 ACP JSONL 生成的 steps。",
      "只提供 0/1、agent/harness 运行错误和 agent 产物；所有 verifier 文件、字段、输出、错误、reward、测试、rubric、metric 与 review 均排除。",
      "agent_thought 的长文本不作为外部可观察证据传入，只保留 step 元数据。",
      "本阶段不查看能力图，不把事件归类到 node，也不输出 node status。",
    ],
    formulas: [
      "causal_link(c,f) = relation(c,f) ∈ {direct, contributing, enabling}",
      "confidence(c,f) ∈ [0,1]；没有证据支持时不得建立因果链。",
    ],
  },
  "stage-04-failure-event-alignment": {
    purpose: "把 Stage 3 抽出的 failure_events 与 cause_events 分别对齐到能力节点，或判定与任何节点无关。",
    inputs: ["capability_graph", "stage_03_failure_events_by_trace"],
    outputs: ["stage_04_failure_event_alignment，仅包含 alignments"],
    rules: [
      "每个 failure/cause event 必须恰好出现一次，并映射到一个能力节点或 null。",
      "原因事件可对齐到失败事件的上游节点，不要求二者映射到同一节点。",
      "如果事件与任何能力节点无关，则 node_id = null 且 confidence = 0。",
      "对齐理由必须来自 Stage 3 事件与能力节点定义；本阶段不输出节点状态。",
    ],
    formulas: [
      "对齐函数：align(e) = argmax_n score(e, n)，若所有节点均无关则 align(e) = null",
      "相关对齐分数：score(e, n) ∈ [0, 1]",
      "无关事件：node_id = null, confidence = 0",
    ],
  },
  "stage-05-node-execution-assessment": {
    purpose: "逐轨迹判断每个能力节点的四项执行事实；LLM 不直接给状态，程序再按固定公式计算 pass/fail/miss/blocked/unknown。",
    inputs: ["capability_graph", "formatted trajectory", "Stage 3 failure causality", "Stage 4 event-node alignments"],
    outputs: ["四项 node execution judgments", "逐 operation/check requirement audit", "本地计算的 node status"],
    rules: [
      "每条轨迹独立调用 repair LLM，并为能力图中每个节点给出四项判断与证据。",
      "capability_presence ∈ {none, partial, full, unknown}。",
      "fully_successful 与 prerequisites_satisfied 使用 true/false/null；success_judgeable 使用 true/false。",
      "Agent 的完成声明、最终答案、文件存在或同一代理指标的重复一致，不能单独支持 pass。",
      "LLM 必须逐项审计节点 operations/checks；缺项由本地代码补为 unverified。",
      "LLM response schema 不允许包含最终 node status；运行与“计算”按钮都调用同一本地公式。",
      "blocked 是前置条件未满足造成的上游传播，不计入节点直接失败。",
    ],
    formulas: [
      "fully_successful=true 与 presence≠full 或 prerequisites=false 冲突 ⇒ unknown",
      "prerequisites=false ∧ presence≠full ∧ fully_successful≠true ⇒ blocked",
      "否则 success_judgeable=false ⇒ unknown",
      "否则 presence=none ⇒ miss",
      "存在 violated requirement 且节点已出现 ⇒ fail",
      "否则 presence=full ∧ fully_successful=true ∧ 每个 requirement 均有 satisfied 证据 ⇒ pass",
      "fully_successful=true 但存在 unverified/无证据 requirement ⇒ unknown",
      "否则 presence∈{partial,full} ∧ fully_successful=false ⇒ fail",
      "其他不一致或不足组合 ⇒ unknown",
      "直接失败集合 D={fail,miss}；blocked∉D。",
    ],
  },
  "legacy-standalone-skill-coverage": {
    purpose: "旧版独立覆盖阶段说明，仅用于兼容历史页面数据；新流程已将覆盖分析并入 Stage 2。",
    inputs: [],
    outputs: [],
    rules: [
      "必须输出 node-skill pair matrix：每个能力节点与每个 Skill 都应有一行。",
      "先判断 directly_relevant；只有 directly_relevant=true 时，node_requirement_fit、trigger/procedure/verification/recovery/execution_support 等维度才有意义。",
      "directly_relevant=false 的行由本地代码写入 coverage_labels=[not_relevant]，fit/coverage 分数和 overall_coverage 设为 null。",
      "node_requirement_fit 评价 Skill 的流程、描述、操作、假设和检查是否符合能力节点要求。",
      "不要只判断相关性，要分别评价 node_requirement_fit、trigger、procedure、verification、recovery 和条件性的 execution support。",
      "execution support 不是强制维度；先判断 not_needed / helpful / required，再决定是否计入 overall。",
      "LLM 不计算 overall_coverage、coverage_gap、coverage_labels；这些字段由本地代码补齐并写入 stage output。",
      "若某个 node 对所有 skill 都 directly_relevant=false，后续阶段才可能推断该 node skill_absent。",
      "若多个 skill 给出相互冲突的操作，应标记 conflicting_skills。",
    ],
    formulas: [
      "直接相关门控：directly_relevant(node, skill) ∈ {true, false}",
      "本地计算：若 directly_relevant=false，则 coverage(node, skill)=null，coverage_labels=[not_relevant]",
      "若 directly_relevant=true：覆盖向量 c_{n,s} = (node_requirement_fit, trigger, procedure, verification, recovery, execution_support)",
      "各维度分数范围：c_i ∈ [0, 1]",
      "本地计算：overall_coverage(n,s) = Σ applicable_i(n,s) * weight_i(n,s) * score_i(n,s) / Σ applicable_i(n,s) * weight_i(n,s)",
      "默认权重：node_requirement_fit=0.25, trigger=0.20, procedure=0.25, verification=0.20, recovery=0.10",
      "execution_support 权重：not_needed=0 且不计入；helpful=0.10；required=0.25",
      "本地计算：coverage_gap(n,s) = 1 - overall_coverage(n,s)",
      "本地计算：coverage_labels(n,s) 可包含多个问题标签，最多 3 个，并按对应分数从低到高排序；满分维度不产生标签。",
    ],
  },
  "stage-06-skill-repair-suggestions": {
    purpose: "对每个需修复或需新增 Skill 的能力节点并行调用 repair LLM，输出直接面向 Skill 的修复建议。无需修复的节点不生成子任务。",
    inputs: ["stage_01b_skill_standardizations", "capability_graph", "Stage 3 causal events", "Stage 4 alignments", "Stage 5 computed node statuses"],
    outputs: ["逐 node 的 revise_existing_skill、add_new_skill 或 manual_review，以及本地兼容聚合结果"],
    rules: [
      "本地代码只筛选需修复 node，并整理绑定到该 node 的证据。",
      "每个 node 单独 prompt，不把所有 node 塞进同一次对话。",
      "prompt 中提供完整能力图、selected node id、node-bound evidence、该节点直接相关 raw Skill 文件和全量 Skill summaries。",
      "LLM 返回的是 skill 修复建议，不是最终 SKILL.md patch。",
      "返回与 node_repair_action 对应的独立 JSON 结构；证据不足时返回 manual_review。",
      "Stage 6 代码会把分支式输出转换成 Stage 7 可消费的 node_skill_repair_suggestions。",
      "不需要修复/新增 skill 的 node 不生成 prompt，也不在 Stage 6 输出中展示。",
    ],
    formulas: [
      "attempted_success_rate(node) = pass_count / (pass_count + fail_count)",
      "direct_failure_rate(node) = (fail_count + miss_count) / total_traces",
      "blocked_rate(node) = blocked_count / total_traces；只用于解释上游传播，不触发本节点修复",
      "bad_event_ratio(node) = affected_trace_count / total_traces",
      "只有 status∈{fail,miss} 的轨迹事件参与本节点修复触发；blocked/pass/unknown 事件只作上下文。",
      "needs_repair(node) 需要重复的直接失败证据；事件阈值、fatal 与 direct_failure_rate 都要求达到最小直接失败轨迹数。",
      "若最佳 Skill coverage ≥ 0.85，标记 requires_semantic_confirmation；Stage 6 只有在重复直接失败揭示具体可复用缺口时才能建议修复。",
      "node_pressure = max(1 - attempted_success_rate（有足够执行尝试时）, direct_failure_rate, direct_bad_event_ratio)",
      "existing-skill evidence weight = 0.25*coverage_gap + 0.20*severity + 0.20*node_pressure + 0.15*alignment + 0.10*first_fault + 0.05*label_signal + 0.05*usage_signal",
      "new-skill evidence weight = 0.30*coverage_absence + 0.25*severity + 0.20*node_pressure + 0.15*alignment + 0.10*first_fault",
      "LLM 不计算 priority、confidence 或证据权重；这些排序信息由本地代码生成。",
    ],
  },
  "stage-07-repair-action-merge": {
    purpose: "当 Stage 6 产生过多 add_new_skill 时，调用 repair LLM 对新增建议做语义聚类；代码保留每簇一个根新增操作，并把其余成员改写为对该新 skill 的后续修复。",
    inputs: ["Stage 6 repair actions", "新增 Skill 合并阈值", "目标新增 Skill 数量", "新增 Skill 硬上限", "Skill 文档字数上限"],
    outputs: ["有固定执行顺序的 repair_actions", "聚类映射和根建议选择", "原始/最终 add_new_skill 数量统计"],
    rules: [
      "未超过软阈值且未超过硬上限时不调用 LLM，原建议按原顺序直通。",
      "只要 add_new_skill 数量超过硬上限，就必须调用 LLM 聚类，即使没有超过自动软阈值。",
      "超过阈值时 LLM 只负责聚类与选择根建议，不能新增、删除或改写 suggestion id。",
      "代码校验每条 add_new_skill 恰好属于一个簇，且簇数等于配置目标。",
      "每簇根操作保留 add_new_skill；其他成员转换为 revise_existing_skill，并指向根操作的新 skill id。",
      "Stage 8 必须按 execution_order 执行，确保根 Skill 在簇内修复前创建。",
    ],
    formulas: [
      "自动软触发阈值 T = max(3, ceil(sqrt(repair_action_count)))",
      "硬上限 H = max_new_skill_count；H=0 表示不启用硬上限",
      "自动目标簇数 K_auto = min(T, ceil(add_new_skill_count / 2), H if enabled)",
      "最终目标簇数 K = configured_target_cluster_count 或 K_auto，并且 K ≤ H（若 H 启用）",
      "触发条件：add_new_skill_count > K 且 (add_new_skill_count > T 或 add_new_skill_count > H)",
      "最终新增 skill 数量 = K；最终 action 总数保持不变。",
    ],
  },
  "stage-08-transactional-skill-repair": {
    purpose: "按单条建议或同 Skill 建议包执行事务修复：先生成完整候选文件，再由可独立配置的 review LLM 审查；通过后才提交到技能库副本。",
    inputs: ["一个 Stage 8 repair unit（单建议或同 Skill 建议包）", "允许修改的 skill 根目录", "本次修改相关文件的完整原文", "上一轮拒绝意见（重试时）"],
    outputs: ["复制后的可测试 skill library", "逐次 repair/review 交互轨迹", "候选文件归档", "事务状态与 applied manifest"],
    rules: [
      "Stage 8 首次运行先完整复制源 Skill library，所有修改只发生在副本。",
      "逐建议模式下每个 repair unit 只含一条建议；建议包模式按同一 target_skill_id 和配置上限分包。",
      "add_new_skill 始终是单独 repair unit，既不与其他新增操作合并，也不并入已有 Skill 的建议包。",
      "每次 repair 返回该 unit 所需文件的完整最终文本，不接受 diff。",
      "候选先进行本地路径、标识、完整性和字数校验；通过后下一次 LLM 调用才进行语义审查。",
      "启用独立 Review LLM 后，只有审查调用使用单独 endpoint/model/key；其他阶段与候选生成仍使用 Repair LLM。",
      "本地校验失败时直接回到 Repair 并携带具体错误，不调用 Review LLM。",
      "LLM 审查通过后，才用整库目录交换原子提交。",
      "审查拒绝时工作库不变，候选仍保存在审计目录；下一轮 repair 自动携带 retry_instructions。",
      "当前 repair unit 通过后才进入下一个 unit。运行一步执行一个 LLM 调用；运行至完成按同一状态机循环。",
      "运行至完成最多执行表单中配置的 LLM 调用次数；达到上限且仍未完成时自动暂停。",
      "暂停会把正在进行的 interaction 标为 interrupted，恢复时重试同一逻辑操作。",
      "SKILL.md 超过配置字数上限时，本地校验直接拒绝候选。",
    ],
    formulas: [
      "review_called = local_validation_errors为空",
      "commit_allowed = review_called ∧ decision=accept ∧ suggestion语义检查全部通过 ∧ candidate检查全部通过 ∧ issues为空",
      "package_size(unit) ≤ stage7_skill_package_size；action=add_new_skill ⇒ package_size(unit)=1",
      "next(unit) 当当前 unit accepted、reject_suggestion 或 manual_review；后两者不提交候选。",
      "审查拒绝：working_library(t+1) = working_library(t)",
      "审查通过：working_library(t+1) = atomic_apply(candidate_files, working_library(t))",
      "自动停止：operations_executed ≥ max_operations ∧ stage.status ≠ completed ⇒ stage.status = paused",
      "word_units = 中文字符数 + 英文/数字单词数；word_units(SKILL.md) ≤ skill_word_limit",
    ],
  },
  "stage-08-skill-repair": {
    purpose: "编辑生成候选文件所使用的 Prompt 模板。该调用针对当前 repair unit 的全部建议返回完整文件，候选不会直接写入工作技能库。",
    inputs: ["repair_unit_id", "suggestion_ids", "selected_stage6_suggestions", "allowed_skill_root", "current_related_files 完整原文", "current_skill_library_inventory", "previous_review_feedback"],
    outputs: ["完整候选文件列表"],
    rules: ["只允许返回 allowed_skill_root 下的完整文件", "重试时必须逐项处理上一次 Review 或本地校验意见", "本地校验通过后才进入 Review"],
    formulas: ["local_valid ⇒ candidate.status = candidate_ready；¬local_valid ⇒ next_operation = repair。"],
  },
  "stage-08-skill-review": {
    purpose: "先审查修复建议是否正确，再审查候选文件是否正确执行有效建议；只有两层都通过才允许事务提交。",
    inputs: ["repair unit 与候选文件", "capability_nodes", "agent-only suggestion_evidence", "node_execution_context", "coverage_context", "Skill inventory 与相关 Skill 摘要"],
    outputs: ["accept / reject_candidate / reject_suggestion / manual_review", "八个语义与候选检查", "issues", "retry_instructions"],
    rules: ["审查调用不重写文件", "错误建议不能因为候选忠实执行而通过", "reject_candidate 才返回 Repair 重试", "reject_suggestion/manual_review 不提交并推进下一 unit"],
    formulas: ["accept = suggestion_supported ∧ capability_correct ∧ reusable_scope ∧ candidate_valid ∧ library_consistent ∧ issues为空"],
  },
};

const state = {
  tasks: [],
  presets: [],
  runs: [],
  jobs: [],
  selectedRunDir: "",
  selectedStageId: "stage-01-input-standardization",
  selectedChildIndex: "",
  selectedJobId: "",
  tab: "guide",
  status: null,
  fileCache: new Map(),
  jobLogs: new Map(),
  logRenderTimer: null,
  placeholderPreviewRequestId: 0,
};

// 浏览器仍保留最近 2500 行用于实时追踪，但 DOM 只展示末尾 500 行。
// 这可以防止长时间运行的 Stage 8 因重复构建超大 <pre> 而阻塞主线程。
const MAX_STORED_STAGE_LOG_LINES = 2500;
const MAX_VISIBLE_STAGE_LOG_LINES = 500;

const $ = (id) => document.getElementById(id);

const els = {
  subtitle: $("stageSubtitle"),
  initStatus: $("stageInitStatus"),
  runStatus: $("stageRunStatus"),
  presetSelect: $("stagePresetSelect"),
  presetName: $("stagePresetName"),
  presetStatus: $("stagePresetStatus"),
  presetLoad: $("stagePresetLoad"),
  presetSave: $("stagePresetSave"),
  presetDelete: $("stagePresetDelete"),
  taskSelect: $("stageTaskSelect"),
  skillsLibrary: $("stageSkillsLibrary"),
  variant: $("stageVariant"),
  maxTraces: $("stageMaxTraces"),
  tracePaths: $("stageTracePaths"),
  inferInputs: $("stageInferInputs"),
  inferStatus: $("stageInferStatus"),
  force: $("stageForce"),
  strongModel: $("stageStrongModel"),
  strongReasoningEffort: $("stageStrongReasoningEffort"),
  strongBaseUrl: $("stageStrongBaseUrl"),
  strongApiKey: $("stageStrongApiKey"),
  maxPromptChars: $("stageMaxPromptChars"),
  traceWorkers: $("stageTraceWorkers"),
  strongTimeout: $("stageStrongTimeout"),
  stage7MaxOperations: $("stage7MaxOperations"),
  stage7RepairMode: $("stage7RepairMode"),
  stage7SkillPackageSize: $("stage7SkillPackageSize"),
  separateReviewLlm: $("stageSeparateReviewLlm"),
  reviewModel: $("stageReviewModel"),
  reviewReasoningEffort: $("stageReviewReasoningEffort"),
  reviewBaseUrl: $("stageReviewBaseUrl"),
  reviewApiKey: $("stageReviewApiKey"),
  addSkillMergeThreshold: $("stageAddSkillMergeThreshold"),
  addSkillTargetCount: $("stageAddSkillTargetCount"),
  maxNewSkills: $("stageMaxNewSkills"),
  skillWordLimit: $("stageSkillWordLimit"),
  runSelect: $("stageRunSelect"),
  outputDir: $("stageOutputDir"),
  init: $("stageInit"),
  refresh: $("stageRefresh"),
  stageList: $("stageList"),
  stageCount: $("stageCount"),
  jobs: $("stageJobs"),
  stopJob: $("stageStopJob"),
  title: $("stageDetailTitle"),
  meta: $("stageDetailMeta"),
  viewTrajectory: $("stageViewTrajectory"),
  prompt: $("stagePrompt"),
  run: $("stageRun"),
  runUntil: $("stageRunUntil"),
  pause: $("stagePause"),
  tabs: $("stageTabs"),
  detail: $("stageDetail"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#096;");
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function scoreText(value) {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(2) : String(value);
}

function formatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatBytes(value) {
  const size = Number(value || 0);
  if (!size) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let n = size;
  let index = 0;
  while (n >= 1024 && index < units.length - 1) {
    n /= 1024;
    index += 1;
  }
  return `${n.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatChars(value) {
  const size = Number(value || 0);
  if (size < 1000) return `${size} chars`;
  if (size < 1000 * 1000) return `${(size / 1000).toFixed(1)}k chars`;
  return `${(size / 1000 / 1000).toFixed(1)}m chars`;
}

function stringify(value) {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value ?? "");
  }
}

async function fetchJson(url) {
  const res = await fetch(url);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function postJson(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function deleteJson(url) {
  const res = await fetch(url, { method: "DELETE" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function selectedTask() {
  return state.tasks.find((task) => task.key === els.taskSelect.value) || state.tasks[0] || null;
}

function taskLibraries(task) {
  return task?.skillLibraries || [];
}

function defaultVariant() {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  return `stage-debug-${stamp}`;
}

function updateOutputDirFromForm() {
  const task = selectedTask();
  const variant = (els.variant.value.trim() || defaultVariant()).replace(/[^a-zA-Z0-9._-]+/g, "-");
  if (task) els.outputDir.value = `repair-runs/${task.name}/${variant}`;
}

function currentRunDir() {
  return (els.outputDir.value.trim() || state.selectedRunDir || "").replace(/\\/g, "/");
}

function syncStage7OptionControls() {
  const packageMode = els.stage7RepairMode.value === "skill_package";
  els.stage7SkillPackageSize.disabled = !packageMode;
  const separateReview = els.separateReviewLlm.checked;
  for (const field of [els.reviewModel, els.reviewBaseUrl, els.reviewApiKey, els.reviewReasoningEffort]) {
    field.disabled = !separateReview;
  }
}

function presetSettingsFromForm() {
  return {
    taskKey: els.taskSelect.value,
    sourceSkillsLibraryId: els.skillsLibrary.value,
    outputVariant: els.variant.value.trim(),
    maxTraces: Number(els.maxTraces.value || 5),
    tracePaths: els.tracePaths.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean),
    force: els.force.checked,
    strongModel: els.strongModel.value.trim(),
    strongReasoningEffort: els.strongReasoningEffort.value,
    strongBaseUrl: els.strongBaseUrl.value.trim(),
    strongApiKey: els.strongApiKey.value,
    traceAnalysisWorkers: Number(els.traceWorkers.value || 5),
    maxPromptChars: Number(els.maxPromptChars.value || 220000),
    strongTimeout: Number(els.strongTimeout.value || 1800),
    stage7MaxOperations: Number(els.stage7MaxOperations.value || 30),
    stage7RepairMode: els.stage7RepairMode.value,
    stage7SkillPackageSize: Number(els.stage7SkillPackageSize.value || 3),
    addSkillMergeThreshold: Number(els.addSkillMergeThreshold.value || 0),
    addSkillTargetCount: Number(els.addSkillTargetCount.value || 0),
    maxNewSkills: Number(els.maxNewSkills.value || 2),
    skillWordLimit: Number(els.skillWordLimit.value || 1200),
    separateReviewLlm: els.separateReviewLlm.checked,
    reviewModel: els.reviewModel.value.trim(),
    reviewReasoningEffort: els.reviewReasoningEffort.value,
    reviewBaseUrl: els.reviewBaseUrl.value.trim(),
    reviewApiKey: els.reviewApiKey.value,
  };
}

function renderPresetOptions(selectedId = els.presetSelect.value) {
  els.presetSelect.innerHTML = [
    '<option value="">新建预设</option>',
    ...state.presets.map((preset) => `<option value="${escapeAttr(preset.id)}">${escapeHtml(preset.name)} · ${escapeHtml(formatDate(preset.updatedAt))}</option>`),
  ].join("");
  if (state.presets.some((preset) => preset.id === selectedId)) els.presetSelect.value = selectedId;
  els.presetDelete.disabled = !els.presetSelect.value;
  els.presetLoad.disabled = !els.presetSelect.value;
}

async function loadPresetList(selectedId = "") {
  const data = await fetchJson("/api/repair-stage/presets");
  state.presets = data.presets || [];
  renderPresetOptions(selectedId);
}

function applyPresetSettings(settings) {
  if (settings.taskKey && Array.from(els.taskSelect.options).some((option) => option.value === settings.taskKey)) {
    els.taskSelect.value = settings.taskKey;
    renderSkillLibraries();
  }
  if (settings.sourceSkillsLibraryId && hasSkillLibraryOption(settings.sourceSkillsLibraryId)) {
    els.skillsLibrary.value = settings.sourceSkillsLibraryId;
  }
  els.variant.value = settings.outputVariant || defaultVariant();
  els.maxTraces.value = settings.maxTraces || 5;
  els.tracePaths.value = asArray(settings.tracePaths).join("\n");
  els.force.checked = Boolean(settings.force);
  els.strongModel.value = settings.strongModel || "";
  els.strongReasoningEffort.value = settings.strongReasoningEffort || "minimal";
  els.strongBaseUrl.value = settings.strongBaseUrl || "";
  els.strongApiKey.value = settings.strongApiKey || "";
  els.traceWorkers.value = settings.traceAnalysisWorkers || 5;
  els.maxPromptChars.value = settings.maxPromptChars || 220000;
  els.strongTimeout.value = settings.strongTimeout || 1800;
  els.stage7MaxOperations.value = settings.stage7MaxOperations || 30;
  els.stage7RepairMode.value = settings.stage7RepairMode === "skill_package" ? "skill_package" : "per_suggestion";
  els.stage7SkillPackageSize.value = settings.stage7SkillPackageSize || 3;
  els.addSkillMergeThreshold.value = settings.addSkillMergeThreshold ?? 0;
  els.addSkillTargetCount.value = settings.addSkillTargetCount ?? 0;
  els.maxNewSkills.value = settings.maxNewSkills ?? 2;
  els.skillWordLimit.value = settings.skillWordLimit || 1200;
  els.separateReviewLlm.checked = Boolean(settings.separateReviewLlm);
  els.reviewModel.value = settings.reviewModel || "";
  els.reviewReasoningEffort.value = settings.reviewReasoningEffort || "minimal";
  els.reviewBaseUrl.value = settings.reviewBaseUrl || "";
  els.reviewApiKey.value = settings.reviewApiKey || "";
  syncStage7OptionControls();
  updateOutputDirFromForm();
}

async function loadSelectedPreset() {
  const id = els.presetSelect.value;
  if (!id) throw new Error("请选择一个预设");
  els.presetStatus.textContent = "正在解密并加载...";
  const data = await fetchJson(`/api/repair-stage/presets?id=${encodeURIComponent(id)}`);
  applyPresetSettings(data.preset.settings || {});
  els.presetName.value = data.preset.name || "";
  els.presetStatus.textContent = `已加载：${data.preset.name}`;
}

async function saveCurrentPreset() {
  const name = els.presetName.value.trim();
  if (!name) throw new Error("请输入预设名称");
  els.presetStatus.textContent = "正在加密保存...";
  const data = await postJson("/api/repair-stage/presets", {
    id: els.presetSelect.value || null,
    name,
    settings: presetSettingsFromForm(),
  });
  state.presets = data.presets || [];
  renderPresetOptions(data.preset.id);
  els.presetStatus.textContent = `已加密保存：${data.preset.name}`;
}

async function deleteSelectedPreset() {
  const id = els.presetSelect.value;
  if (!id) throw new Error("请选择要删除的预设");
  const preset = state.presets.find((item) => item.id === id);
  const data = await deleteJson(`/api/repair-stage/presets?id=${encodeURIComponent(id)}`);
  state.presets = data.presets || [];
  els.presetName.value = "";
  renderPresetOptions("");
  els.presetStatus.textContent = `已删除：${preset?.name || id}`;
}

function currentPayload(extra = {}) {
  return {
    outputDir: currentRunDir(),
    outputSkillsDir: currentRunDir().replace(/^repair-runs\//, "skill-libraries/"),
    strongBaseUrl: els.strongBaseUrl.value.trim(),
    strongModel: els.strongModel.value.trim(),
    strongReasoningEffort: els.strongReasoningEffort.value,
    strongApiKey: els.strongApiKey.value.trim(),
    maxPromptChars: Number(els.maxPromptChars.value || 220000),
    traceAnalysisWorkers: Number(els.traceWorkers.value || 5),
    strongTimeout: Number(els.strongTimeout.value || 1800),
    stage7MaxOperations: Number(els.stage7MaxOperations.value || 30),
    stage7RepairMode: els.stage7RepairMode.value === "skill_package" ? "skill_package" : "per_suggestion",
    stage7SkillPackageSize: Number(els.stage7SkillPackageSize.value || 3),
    separateReviewLlm: els.separateReviewLlm.checked,
    reviewModel: els.reviewModel.value.trim(),
    reviewReasoningEffort: els.reviewReasoningEffort.value,
    reviewBaseUrl: els.reviewBaseUrl.value.trim(),
    reviewApiKey: els.reviewApiKey.value.trim(),
    addSkillMergeThreshold: Number(els.addSkillMergeThreshold.value || 0),
    addSkillTargetCount: Number(els.addSkillTargetCount.value || 0),
    maxNewSkills: Number(els.maxNewSkills.value || 2),
    skillWordLimit: Number(els.skillWordLimit.value || 1200),
    ...extra,
  };
}

async function loadTasks() {
  const data = await fetchJson("/api/tasks");
  state.tasks = data.tasks || [];
  els.taskSelect.innerHTML = state.tasks.map((task) => `
    <option value="${escapeAttr(task.key)}">${escapeHtml(task.name)} · ${escapeHtml(task.rootLabel)}</option>
  `).join("");
  renderSkillLibraries();
  updateOutputDirFromForm();
}

function renderSkillLibraries() {
  const task = selectedTask();
  const libs = taskLibraries(task);
  els.skillsLibrary.innerHTML = libs.map((library) => `
    <option value="${escapeAttr(library.id)}">${escapeHtml(library.label)} · ${library.skillCount || 0} skills</option>
  `).join("");
  if (task?.defaultSkillsLibraryId) els.skillsLibrary.value = task.defaultSkillsLibraryId;
}

function hasSkillLibraryOption(libraryId) {
  return Array.from(els.skillsLibrary.options).some((option) => option.value === libraryId);
}

function inferenceTooltip(data) {
  const lines = [];
  const selected = data.selected || {};
  if (selected.reason) lines.push(`选择依据：${selected.reason}`);
  for (const rollout of data.rollouts || []) {
    const library = rollout.inferredSkillsLibrary || {};
    lines.push(`${rollout.path}: ${rollout.taskName || "-"} / ${library.label || library.skillsDir || "未识别 skills"}`);
    if (rollout.observedSkillPaths?.length) {
      lines.push(`  observed: ${rollout.observedSkillPaths.join(", ")}`);
    }
  }
  for (const warning of data.warnings || []) lines.push(`warning: ${warning}`);
  return lines.join("\n");
}

async function inferInputsFromTracePaths() {
  const tracePaths = els.tracePaths.value.trim();
  if (!tracePaths) {
    els.inferStatus.textContent = "请先填写轨迹或 job 路径";
    return;
  }

  els.inferInputs.disabled = true;
  els.inferStatus.textContent = "推断中...";
  try {
    const data = await postJson("/api/repair-stage/infer-inputs", { tracePaths });
    const selected = data.selected || null;
    const warnings = data.warnings || [];
    if (selected?.taskKey && state.tasks.some((task) => task.key === selected.taskKey)) {
      els.taskSelect.value = selected.taskKey;
      renderSkillLibraries();
      if (selected.sourceSkillsLibraryId && hasSkillLibraryOption(selected.sourceSkillsLibraryId)) {
        els.skillsLibrary.value = selected.sourceSkillsLibraryId;
      }
      updateOutputDirFromForm();
      const pct = Math.round(Number(selected.confidence || 0) * 100);
      els.inferStatus.textContent = `已填充：${selected.taskName} / ${selected.sourceSkillsLibraryLabel || selected.sourceSkillsDir}（${pct}%）${warnings.length ? "，有警告" : ""}`;
    } else {
      els.inferStatus.textContent = warnings[0] || "未能唯一推断，请查看候选后手动选择";
    }
    els.inferStatus.title = inferenceTooltip(data);
  } catch (error) {
    els.inferStatus.textContent = `推断失败：${error.message}`;
    els.inferStatus.title = "";
  } finally {
    els.inferInputs.disabled = false;
  }
}

async function loadRuns() {
  const data = await fetchJson("/api/repair-stage/runs");
  state.runs = data.runs || [];
  state.jobs = data.jobs || [];
  if (!state.selectedRunDir && state.runs[0]) state.selectedRunDir = state.runs[0].runDir;
  renderRunSelect();
  renderJobs();
  if (state.selectedRunDir) {
    els.outputDir.value = state.selectedRunDir;
    await loadStatus();
  } else {
    renderStages();
    renderDetail();
  }
}

function renderRunSelect() {
  const options = state.runs.map((run) => `
    <option value="${escapeAttr(run.runDir)}">${escapeHtml(run.taskName)} · ${escapeHtml(run.variant || run.runDir)} · ${formatDate(run.modifiedAt)}</option>
  `);
  els.runSelect.innerHTML = [
    `<option value="">手动输入运行目录</option>`,
    ...options,
  ].join("");
  els.runSelect.value = state.selectedRunDir;
}

async function loadStatus() {
  const outputDir = currentRunDir();
  if (!outputDir) {
    state.status = null;
    renderStages();
    renderDetail();
    return;
  }
  els.subtitle.textContent = `正在读取 ${outputDir}...`;
  try {
    const params = new URLSearchParams({ outputDir });
    const data = await fetchJson(`/api/repair-stage/status?${params.toString()}`);
    state.status = data.status;
    state.selectedRunDir = data.status?.runDir || outputDir;
    els.outputDir.value = state.selectedRunDir;
    applyManifestToForm(state.status?.manifest || {});
    els.subtitle.textContent = `当前运行目录：${state.selectedRunDir}`;
    renderStages();
    renderDetail();
  } catch (error) {
    state.status = null;
    els.subtitle.textContent = `读取失败：${error.message}`;
    renderStages();
    renderDetail();
  }
}

function applyManifestToForm(manifest) {
  if (manifest.strongBaseUrl) els.strongBaseUrl.value = manifest.strongBaseUrl;
  if (manifest.strongModel) els.strongModel.value = manifest.strongModel;
  if (manifest.maxPromptChars) els.maxPromptChars.value = manifest.maxPromptChars;
  if (manifest.traceAnalysisWorkers) els.traceWorkers.value = manifest.traceAnalysisWorkers;
  if (manifest.stage7MaxOperations || manifest.stage6MaxOperations) {
    els.stage7MaxOperations.value = manifest.stage7MaxOperations || manifest.stage6MaxOperations;
  }
  els.stage7RepairMode.value = manifest.stage7RepairMode === "skill_package" ? "skill_package" : "per_suggestion";
  if (manifest.stage7SkillPackageSize) els.stage7SkillPackageSize.value = manifest.stage7SkillPackageSize;
  els.separateReviewLlm.checked = Boolean(manifest.separateReviewLlm);
  els.reviewBaseUrl.value = manifest.reviewBaseUrl || "";
  els.reviewModel.value = manifest.reviewModel || "";
  syncStage7OptionControls();
  if (manifest.addSkillMergeThreshold !== undefined) els.addSkillMergeThreshold.value = manifest.addSkillMergeThreshold;
  if (manifest.addSkillTargetCount !== undefined) els.addSkillTargetCount.value = manifest.addSkillTargetCount;
  if (manifest.maxNewSkillCount !== undefined) els.maxNewSkills.value = manifest.maxNewSkillCount;
  if (manifest.skillWordLimit) els.skillWordLimit.value = manifest.skillWordLimit;
}

function selectedStage() {
  const stages = state.status?.stages?.length ? state.status.stages : TEMPLATE_STAGE_DEFS;
  return stages.find((stage) => stage.id === state.selectedStageId) || stages[0] || null;
}

function selectedTraceChild(stage) {
  if (!stage?.children || state.selectedChildIndex === "") return null;
  return stage.children.find((child) => String(child.index) === String(state.selectedChildIndex)) || null;
}

function selectedStageSubject(stage) {
  return selectedTraceChild(stage) || stage;
}

function stageNeedsLocalCalculation(stage) {
  return ["stage-02-capability-graph", "stage-05-node-execution-assessment"].includes(stage?.id);
}

function calculatedOutputPath(stage) {
  if (!stageNeedsLocalCalculation(stage)) return "";
  const subject = selectedStageSubject(stage);
  return subject?.output?.path || stage?.output?.path || "";
}

function renderCalculationAction(stage, tab) {
  if (!stageNeedsLocalCalculation(stage) || !["response", "parsed", "output"].includes(tab)) return "";
  const isNodeStatus = stage?.id === "stage-05-node-execution-assessment";
  const label = isNodeStatus ? "计算节点状态" : "计算 coverage 聚合字段";
  const hint = isNodeStatus
    ? "根据四项 LLM 事实判断，本地计算 pass、fail、miss、blocked 或 unknown。"
    : "根据 LLM 返回的维度分数，本地计算 overall_coverage、coverage_gap 和 coverage_labels。";
  return `
    <section class="repair-trace-section">
      <div class="repair-stage-file-head">
        <span>${escapeHtml(hint)}</span>
        <button id="stageCalculateLocal" class="primary-button" type="button">${escapeHtml(label)}</button>
      </div>
    </section>
  `;
}

function renderCalculatedOutputPreview(stage, tab) {
  if (!stageNeedsLocalCalculation(stage) || !["response", "parsed"].includes(tab)) return "";
  const outputPath = calculatedOutputPath(stage);
  if (!outputPath) {
    return `
      <section class="repair-trace-section">
        <h3>计算后 Stage Output</h3>
        <div class="detail-empty">还没有计算后的 output。点击上方计算按钮生成。</div>
      </section>
    `;
  }
  const cached = state.fileCache.get(outputPath);
  if (!cached) {
    return `
      <section class="repair-trace-section">
        <h3>计算后 Stage Output</h3>
        <div class="detail-empty">正在读取计算后的 output...</div>
      </section>
    `;
  }
  const parsed = parseStageJsonFile(cached.text || "");
  if (!parsed) {
    return `
      <section class="repair-trace-section">
        <h3>计算后 Stage Output</h3>
        <div class="detail-error">计算后的 output 不是有效 JSON。</div>
      </section>
    `;
  }
  const selected = selectedStageOutputValue(parsed, stage);
  return `
    <section class="repair-trace-section">
      <div class="repair-stage-file-head">
        <span>计算后 Stage Output · ${escapeHtml(outputPath)}</span>
        <button class="secondary-button" data-tab-shortcut="output" type="button">查看完整 Output</button>
      </div>
      ${renderLocalCalculationSummary(selected) || renderReadableContent(selected, { emptyText: "没有计算结果。" })}
    </section>
  `;
}

function ensureCalculatedOutputPreviewLoaded(stage, tab) {
  if (!stageNeedsLocalCalculation(stage) || !["response", "parsed"].includes(tab)) return;
  const outputPath = calculatedOutputPath(stage);
  if (!outputPath || state.fileCache.has(outputPath)) return;
  loadFile(outputPath).then(() => {
    if (selectedStage()?.id === stage.id && state.tab === tab) renderDetail();
  }).catch(() => {});
}

function rolloutViewerTarget(stage) {
  if (!["stage-03-failure-event-extraction", "stage-05-node-execution-assessment"].includes(stage?.id)) return null;
  const child = selectedTraceChild(stage);
  if (!child) return null;

  const index = Number(child.index);
  const rolloutDir = state.status?.manifest?.rolloutDirs?.[index];
  if (!Number.isFinite(index) || !rolloutDir) return null;

  const normalized = String(rolloutDir).replace(/\\/g, "/").replace(/\/+$/g, "");
  const parts = normalized.split("/").filter(Boolean);
  if (parts.length < 2) return null;

  const rollout = parts[parts.length - 1];
  const artifactDir = parts.slice(0, -1).join("/");
  const params = new URLSearchParams({ artifactDir, rollout });
  return {
    href: `/trajectory.html?${params.toString()}`,
    label: child.trajId || rollout || `trace ${index}`,
    rollout,
    artifactDir,
  };
}

function updateTrajectoryAction(stage) {
  const target = rolloutViewerTarget(stage);
  if (!target) {
    els.viewTrajectory.href = "#";
    els.viewTrajectory.classList.add("disabled");
    els.viewTrajectory.setAttribute("aria-disabled", "true");
    els.viewTrajectory.title = "请选择 Stage 2 下的一条轨迹子项。";
    return;
  }

  els.viewTrajectory.href = target.href;
  els.viewTrajectory.classList.remove("disabled");
  els.viewTrajectory.setAttribute("aria-disabled", "false");
  els.viewTrajectory.title = `${target.label}\n${target.artifactDir}/${target.rollout}`;
}

function stageBadge(status) {
  const classes = {
    done: "status-ok",
    partial: "status-warn",
    prompted: "status-warn",
    error: "status-error",
    "running-or-requested": "status-warn",
    response: "status-warn",
    template: "status-warn",
    paused: "status-warn",
    interrupted: "status-warn",
  };
  return `<span class="status-pill ${classes[status] || ""}">${escapeHtml(status || "missing")}</span>`;
}

function renderStages() {
  const stages = state.status?.stages?.length ? state.status.stages : TEMPLATE_STAGE_DEFS;
  const isTemplateOnly = !state.status?.stages?.length;
  const rowCount = stages.reduce((count, stage) => count + 1 + (stage.children?.length || 0), 0);
  const doneCount = stages.reduce((count, stage) => {
    if (!stage.children?.length) return count + (stage.status === "done" ? 1 : 0);
    return count + stage.children.filter((child) => child.status === "done").length;
  }, 0);
  els.stageCount.textContent = isTemplateOnly ? `${rowCount} templates` : `${doneCount}/${rowCount}`;
  if (!stages.length) {
    els.stageList.innerHTML = `<div class="detail-empty">没有已初始化的 stage 运行。</div>`;
    return;
  }
  const rows = [];
  for (const stage of stages) {
    const selected = stage.id === state.selectedStageId && state.selectedChildIndex === "" ? " selected" : "";
    const childText = stage.children ? `${stage.doneCount || 0}/${stage.totalCount || 0}` : "";
    const depText = stage.depsReady ? "ready" : "deps";
    rows.push(`
      <button class="repair-stage-row${selected}" data-stage-id="${escapeAttr(stage.id)}">
        <div>
          <strong>${escapeHtml(stage.label)}</strong>
          <span>${escapeHtml(stage.template || stage.id)}</span>
        </div>
        <div class="repair-stage-row-meta">
          ${stageBadge(stage.status)}
          <span>${escapeHtml(childText || depText)}</span>
        </div>
      </button>
    `);
    for (const child of stage.children || []) {
      const childSelected = stage.id === state.selectedStageId && String(child.index) === String(state.selectedChildIndex) ? " selected" : "";
      const label = child.label || child.trajId || child.name || `子项 ${child.index}`;
      const childMeta = child.reviewDecision
        || child.repairAction
        || child.localRecommendedAction
        || child.nodeRepairAction
        || child.kind
        || child.trajId
        || "";
      rows.push(`
        <button class="repair-stage-row repair-stage-subrow${childSelected}" data-stage-id="${escapeAttr(stage.id)}" data-child-index="${escapeAttr(child.index)}">
          <div>
            <strong>${escapeHtml(label)}</strong>
            <span>${escapeHtml(child.template || child.name || "")}</span>
          </div>
          <div class="repair-stage-row-meta">
            ${stageBadge(child.status)}
            <span>${escapeHtml(childMeta)}</span>
          </div>
        </button>
      `);
    }
  }
  els.stageList.innerHTML = rows.join("");
}

function fileForTab(stage, tab) {
  if (!stage) return "";
  const subject = selectedStageSubject(stage);
  const files = subject?.files || stage.files || {};
  if (tab === "prompt") return files.prompt?.path || "";
  if (tab === "request") return files.request?.path || "";
  if (tab === "response") return files.response?.path || "";
  if (tab === "parsed") return files.parsed?.path || subject?.fallbackFiles?.parsed?.path || "";
  if (tab === "output") return subject?.output?.path || stage.output?.path || state.status?.stageOutputs?.path || "";
  if (tab === "template") {
    const template = subject?.template || stage.template;
    return template ? `offline_skill_rca/prompt_templates/${template}` : "";
  }
  return "";
}

function renderTabs() {
  els.tabs.innerHTML = TABS.map((tab) => `
    <button class="${tab.id === state.tab ? "active" : ""}" data-tab="${tab.id}">${tab.label}</button>
  `).join("");
}

function renderDetail() {
  renderTabs();
  const stage = selectedStage();
  if (!stage) {
    els.title.textContent = "详情";
    els.meta.textContent = "";
    updateTrajectoryAction(null);
    els.detail.innerHTML = `<div class="detail-empty">初始化一个 stage 运行后开始查看。</div>`;
    return;
  }
  state.selectedStageId = stage.id;
  const subject = selectedStageSubject(stage);
  els.title.textContent = subject?.label || stage.label;
  els.meta.textContent = `${subject?.name || subject?.id || stage.id} · ${subject?.template || stage.template || ""} · ${subject?.status || stage.status}`;
  updateTrajectoryAction(stage);
  const isTransactionalStage = stage.id === "stage-08-transactional-skill-repair";
  els.run.textContent = isTransactionalStage ? "运行一步" : "运行 Stage";
  els.runUntil.hidden = !isTransactionalStage;
  els.pause.hidden = !isTransactionalStage;
  if (!state.status?.stages?.length && !["guide", "template", "system"].includes(state.tab)) {
    state.tab = "template";
    renderTabs();
  }
  if (state.tab === "guide") {
    renderStageGuide(stage);
    return;
  }
  if (state.tab === "system") {
    renderSystemPrompt(stage);
    return;
  }
  if (state.tab === "evidence") {
    renderEvidence(stage);
    return;
  }
  if (state.tab === "logs") {
    renderLogs();
    return;
  }
  const path = fileForTab(stage, state.tab);
  if (!path) {
    els.detail.innerHTML = `<div class="detail-empty">当前 tab 没有对应文件。</div>`;
    return;
  }
  renderFile(path, state.tab === "prompt" || state.tab === "template");
}

function guideForSubject(stage) {
  const subject = selectedStageSubject(stage);
  const templateKey = subject?.template ? subject.template.replace(/\.txt$/, "") : "";
  return STAGE_GUIDES[subject?.id]
    || STAGE_GUIDES[subject?.name]
    || STAGE_GUIDES[templateKey]
    || STAGE_GUIDES[stage?.id]
    || {
      purpose: "这个 stage 暂无专门说明。",
      inputs: stage?.deps || [],
      outputs: [stage?.key || subject?.name || stage?.id || "unknown"],
      rules: ["可查看 Template / Prompt tab 了解具体调用内容。"],
      formulas: ["无已登记公式。"],
    };
}

function renderStageGuide(stage) {
  const subject = selectedStageSubject(stage);
  const guide = guideForSubject(stage);
  const deps = subject?.deps || stage?.deps || [];
  const badges = [
    subject?.template || stage?.template,
    subject?.parallel || stage?.parallel ? "parallel" : "",
    deps.length ? `deps: ${deps.length}` : "deps: 0",
  ].filter(Boolean);
  els.detail.innerHTML = `
    <div class="repair-stage-guide">
      <div class="repair-stage-guide-hero">
        <div>
          <h3>${escapeHtml(subject?.label || stage?.label || "Stage")}</h3>
          <p>${escapeHtml(guide.purpose || "")}</p>
        </div>
        <div class="repair-stage-guide-badges">
          ${badges.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}
        </div>
      </div>
      <div class="repair-stage-guide-grid">
        ${renderGuideBlock("输入", guide.inputs)}
        ${renderGuideBlock("输出", guide.outputs)}
      </div>
      ${renderGuideBlock("执行规则", guide.rules, "wide")}
      ${renderGuideBlock("公式 / 判定", guide.formulas, "wide formula")}
      ${renderStage6RunLimit(stage)}
    </div>
  `;
}

function renderStage6RunLimit(stage) {
  if (stage?.id !== "stage-08-transactional-skill-repair") return "";
  const limit = Number(stage.lastRunOperationLimit || 0);
  const executed = Number(stage.lastRunOperationsExecuted || 0);
  const total = Number(stage.totalLlmOperations || 0);
  const values = [
    stage.repairMode === "skill_package"
      ? `修复粒度：同 Skill 建议包（每包最多 ${Number(stage.skillPackageSize || 1)} 条，共 ${Number(stage.repairUnitCount || 0)} 个 repair units）`
      : `修复粒度：逐建议（共 ${Number(stage.repairUnitCount || stage.totalCount || 0)} 个 repair units）`,
    stage.separateReviewLlm
      ? `审查模型：${stage.reviewModel || "复用 Repair 模型名"} · ${stage.reviewBaseUrl || "复用 Repair Base URL"}`
      : "审查模型：复用 Repair LLM",
    limit ? `最近一次连续运行：${executed}/${limit} 次 LLM 调用` : "最近尚未执行连续运行",
    `Stage 8 累计交互：${total} 次`,
    stage.operationLimitReached ? "已达到调用上限并自动暂停" : "未触发调用上限",
    stage.pauseReason || "",
  ].filter(Boolean);
  return renderGuideBlock("连续运行状态", values, "wide");
}

function renderGuideBlock(title, items, extraClass = "") {
  const values = Array.isArray(items) ? items.filter(Boolean) : [items].filter(Boolean);
  return `
    <section class="repair-stage-guide-block ${extraClass}">
      <h4>${escapeHtml(title)}</h4>
      ${values.length
        ? `<ul>${values.map((item) => `<li>${escapeHtml(String(item))}</li>`).join("")}</ul>`
        : `<p class="muted">无</p>`}
    </section>
  `;
}

function templatePathForStage(stage) {
  const subject = selectedStageSubject(stage);
  return subject?.template ? `offline_skill_rca/prompt_templates/${subject.template}` : "";
}

function stageSystemPrompt(name) {
  const cached = state.fileCache.get(SYSTEM_TEMPLATE_PATH);
  const template = cached?.text || [
    "You are the repair LLM for Offline SkillRCA v2 ({{stage_name}}).",
    "Only use the visible prompt data. Hidden evaluator/verifier outputs are not available and must not be inferred.",
    "Return strict JSON only. Do not expose chain-of-thought; summarize evidence and decisions.",
  ].join(" ");
  return template.replaceAll("{{stage_name}}", name).trim();
}

function stageSystemNames(stage) {
  const child = selectedTraceChild(stage);
  if (child?.name) return [child.name];
  if (stage?.children?.length) {
    return stage.children.map((item) => item.name).filter(Boolean);
  }
  return [stage?.id].filter(Boolean);
}

function actualSystemMessagesFromRequest(text) {
  try {
    const parsed = JSON.parse(text);
    return (parsed.messages || [])
      .filter((message) => message?.role === "system")
      .map((message) => String(message.content || ""));
  } catch {
    return [];
  }
}

function renderSystemPrompt(stage) {
  const requestPath = fileForTab(stage, "request");
  const cached = requestPath ? state.fileCache.get(requestPath) : null;
  const templateCached = state.fileCache.get(SYSTEM_TEMPLATE_PATH);
  if (!templateCached) {
    els.detail.innerHTML = `<div class="detail-empty">正在读取 ${escapeHtml(SYSTEM_TEMPLATE_PATH)}...</div>`;
    loadFile(SYSTEM_TEMPLATE_PATH).then(() => renderDetail()).catch((error) => {
      els.detail.innerHTML = `<div class="detail-error">读取 system prompt 模板失败：${escapeHtml(error.message)}</div>`;
    });
    return;
  }
  if (requestPath && !cached) {
    els.detail.innerHTML = `<div class="detail-empty">正在读取 ${escapeHtml(requestPath)}...</div>`;
    loadFile(requestPath).then(() => renderDetail()).catch(() => {
      renderExpectedSystemPrompt(stage, []);
    });
    return;
  }
  const actual = cached?.text ? actualSystemMessagesFromRequest(cached.text) : [];
  renderExpectedSystemPrompt(stage, actual);
}

function renderExpectedSystemPrompt(stage, actualMessages) {
  const names = stageSystemNames(stage);
  const templateCached = state.fileCache.get(SYSTEM_TEMPLATE_PATH) || {};
  const templateInfo = templateCached.info || {};
  const templateText = templateCached.text || "";
  const helper = renderSystemTemplateHelper();
  const expectedBlocks = names.map((name) => `
    <div class="repair-stage-system-block">
      <div class="repair-stage-file-head">
        <span>${escapeHtml(name)}</span>
        <span>computed</span>
      </div>
      <pre class="repair-trace-pre">${escapeHtml(stageSystemPrompt(name))}</pre>
    </div>
  `).join("");
  const actualBlocks = actualMessages.length
    ? actualMessages.map((message, index) => `
      <div class="repair-stage-system-block">
        <div class="repair-stage-file-head">
          <span>request.json system #${index + 1}</span>
          <span>actual</span>
        </div>
        <pre class="repair-trace-pre">${escapeHtml(message)}</pre>
      </div>
    `).join("")
    : `<div class="detail-empty">当前 stage 还没有 request.json，下面展示的是将要发送的 system prompt。</div>`;
  els.detail.innerHTML = `
    <div class="repair-stage-system-grid">
      <div class="repair-stage-system-block">
        <div class="repair-stage-file-head">
          <span title="${escapeAttr(SYSTEM_TEMPLATE_PATH)}">${escapeHtml(SYSTEM_TEMPLATE_PATH)}</span>
          <span>${formatBytes(templateInfo.size)} · ${formatDate(templateInfo.modifiedAt)}</span>
        </div>
        ${helper}
        <textarea id="stageFileEditor" class="input repair-stage-editor repair-stage-system-editor">${escapeHtml(templateText)}</textarea>
        <div class="repair-stage-editor-actions">
          <button id="stageSaveFile" class="primary-button">保存 System 模板</button>
          <span id="stageSaveStatus" class="muted"></span>
        </div>
      </div>
      ${actualBlocks}
      ${expectedBlocks}
    </div>
  `;
  $("stageSaveFile").addEventListener("click", () => saveCurrentFile(SYSTEM_TEMPLATE_PATH));
  for (const button of els.detail.querySelectorAll("[data-insert-placeholder]")) {
    button.addEventListener("click", () => insertIntoEditor(button.dataset.insertPlaceholder || ""));
  }
}

function renderSystemTemplateHelper() {
  const renderButtons = (items, kind) => items.map((item) => `
    <button class="repair-stage-placeholder" data-insert-placeholder="${escapeAttr(item)}" title="${kind === "variable" ? "插入真实 system 模板变量" : "插入说明性占位符"}">${escapeHtml(item)}</button>
  `).join("");
  return `
    <div class="repair-stage-template-helper">
      <div>
        <strong>System 模板变量</strong>
        <div class="repair-stage-placeholder-row">${renderButtons(SYSTEM_PLACEHOLDERS.variables, "variable")}</div>
      </div>
      <div>
        <strong>说明性占位符</strong>
        <div class="repair-stage-placeholder-row">${renderButtons(SYSTEM_PLACEHOLDERS.external, "external")}</div>
      </div>
    </div>
  `;
}

async function loadFile(path) {
  if (state.fileCache.has(path)) return state.fileCache.get(path);
  const params = new URLSearchParams({ path });
  const data = await fetchJson(`/api/repair-stage/file?${params.toString()}`);
  state.fileCache.set(path, data);
  return data;
}

async function loadJsonArtifact(path) {
  if (!path) return null;
  const file = await loadFile(path);
  try {
    return JSON.parse(file.text || "null");
  } catch {
    return { __parseError: true, path };
  }
}

function trajectoryIndex(bundle) {
  return (bundle?.failed_trajectories || []).map((traj, index) => ({
    index,
    traj_id: traj.traj_id,
    task_id: traj.task_id,
    success: traj.success,
    step_count: Array.isArray(traj.steps) ? traj.steps.length : 0,
  }));
}

function stage2TrajectoryForPrompt(traj) {
  if (!traj) return null;
  return {
    traj_id: traj.traj_id,
    task_id: traj.task_id,
    success: traj.success || 0,
    step_formatting_provenance: traj.step_formatting_provenance || "generated_by_local_code_from_acp_trajectory_jsonl; no LLM summarization",
    steps: Array.isArray(traj.steps) ? traj.steps.map(stage2StepForPrompt) : [],
    visible_failure_result: traj.visible_failure_result || { success: traj.success || 0 },
    final_artifacts: Array.isArray(traj.final_artifacts) ? traj.final_artifacts : [],
  };
}

function stage2StepForPrompt(step) {
  if (!step || typeof step !== "object") return step;
  const out = { ...step };
  if (out.event_type === "agent_thought") {
    delete out.action_summary;
    delete out.raw_visible_text;
    delete out.observation_summary;
    delete out.error_signal;
    return removeEmptyStepFields(out);
  }
  for (const key of ["raw_visible_text", "observation_summary", "error_signal"]) {
    if (duplicateStepText(out[key], out.action_summary) || containsTruncationMarker(out[key])) {
      delete out[key];
    }
  }
  if (containsTruncationMarker(out.action_summary)) delete out.action_summary;
  return removeEmptyStepFields(out);
}

function skillIdentifiers(skillLibrary) {
  const names = new Set();
  for (const skill of skillLibrary || []) {
    for (const key of ["skill_id", "title"]) {
      const value = String(skill?.[key] || "").trim();
      if (value) names.add(value);
    }
    const path = String(skill?.path || "").replace(/\\/g, "/");
    const parts = path.split("/").filter((part) => part && part !== "SKILL.md");
    if (parts.length) names.add(parts[parts.length - 1]);
    const metadata = skill?.metadata && typeof skill.metadata === "object" ? skill.metadata : {};
    for (const key of ["name", "id", "title"]) {
      const value = String(metadata[key] || "").trim();
      if (value) names.add(value);
    }
  }
  return Array.from(names).sort((a, b) => b.length - a.length || a.localeCompare(b));
}

function skillStepText(step) {
  return [
    step?.action_summary,
    step?.raw_visible_text,
    step?.observation_summary,
    step?.error_signal,
    step?.tool_name,
    step?.tool_input,
    ...(step?.mentioned_skills || []),
  ].filter(Boolean).join("\n");
}

function stepMentionsSkill(step, skillNames) {
  if (step?.mentioned_skills?.length) return true;
  const actionType = String(step?.action_type || "").toLowerCase();
  const toolName = String(step?.tool_name || "").toLowerCase();
  if (actionType.includes("skill") || toolName === "skill") return true;
  const text = skillStepText(step).toLowerCase();
  if (!text.includes("skill") && !text.includes("skill.md")) return false;
  return skillNames.some((name) => {
    const escaped = String(name || "").toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return escaped && new RegExp(`(?<![A-Za-z0-9_.-])${escaped}(?![A-Za-z0-9_.-])`).test(text);
  });
}

function textExcerpt(value, limit = 1800) {
  const text = String(value ?? "");
  return {
    text: text.length <= limit ? text : text.slice(0, limit),
    text_was_shortened: text.length > limit,
    original_text_chars: text.length,
  };
}

function compactSkillRelatedStep(step) {
  const action = textExcerpt(step?.action_summary, 1800);
  const out = {
    step_id: step?.step_id,
    role: step?.role,
    action_type: step?.action_type,
    tool_name: step?.tool_name,
    mentioned_skills: step?.mentioned_skills || [],
    produced_artifacts: step?.produced_artifacts || [],
    action_text: action.text,
    action_text_was_shortened: action.text_was_shortened,
    action_text_chars: action.original_text_chars,
  };
  if (step?.tool_input) out.tool_input = textExcerpt(step.tool_input, 800);
  if (step?.observation_summary && !duplicateStepText(step.observation_summary, step.action_summary)) {
    const observation = textExcerpt(step.observation_summary, 1200);
    out.observation_text = observation.text;
    out.observation_text_was_shortened = observation.text_was_shortened;
    out.observation_text_chars = observation.original_text_chars;
  }
  if (step?.error_signal) {
    const error = textExcerpt(step.error_signal, 800);
    out.error_signal = error.text;
    out.error_signal_was_shortened = error.text_was_shortened;
    out.error_signal_chars = error.original_text_chars;
  }
  return removeEmptyStepFields(out);
}

function skillRelatedTraceSteps(bundle) {
  const skillNames = skillIdentifiers(bundle?.skill_library || []);
  return (bundle?.failed_trajectories || []).map((traj) => {
    const steps = (traj.steps || [])
      .filter((step) => stepMentionsSkill(step, skillNames))
      .map(compactSkillRelatedStep);
    return {
      traj_id: traj.traj_id,
      task_id: traj.task_id,
      success: traj.success,
      skill_related_step_count: steps.length,
      steps,
    };
  });
}

function duplicateStepText(left, right) {
  if (!left || !right) return false;
  return normalizeStepText(left) === normalizeStepText(right);
}

function normalizeStepText(value) {
  return String(value || "").split(/\s+/).filter(Boolean).join(" ");
}

function containsTruncationMarker(value) {
  return typeof value === "string" && value.includes("[truncated");
}

function removeEmptyStepFields(step) {
  return Object.fromEntries(Object.entries(step).filter(([, value]) => value !== null && value !== undefined && value !== ""));
}

function missingStageOutput(key) {
  return { __missing: true, key, note: "Run the dependency stage first." };
}

async function currentPreviewContext(stage) {
  const inputPath = state.status?.inputBundle?.path;
  const bundle = inputPath ? await loadJsonArtifact(inputPath) : null;
  const stageOutputsPath = state.status?.stageOutputs?.path;
  const outputs = stageOutputsPath ? await loadJsonArtifact(stageOutputsPath) : {};
  return {
    bundle: bundle || {},
    outputs: outputs || {},
    stage,
  };
}

function stageOutput(outputs, key) {
  return outputs?.[key] ?? missingStageOutput(key);
}

function compactPreviewText(value, limit = 900) {
  if (value === null || value === undefined) return value;
  const text = String(value);
  return text.length > limit ? `${text.slice(0, limit)}...[truncated ${text.length - limit} chars]` : value;
}

function compactPreviewEvidenceItems(items, limit = 6) {
  return asArray(items)
    .filter((item) => item && typeof item === "object")
    .slice()
    .sort((a, b) => Number(b.weight || 0) - Number(a.weight || 0))
    .slice(0, limit)
    .map((item) => ({
      evidence_type: item.evidence_type,
      event_id: item.event_id,
      traj_id: item.traj_id,
      step_id: item.step_id,
      severity: item.severity,
      observed_behavior: compactPreviewText(item.observed_behavior),
      expected_behavior_from_task_and_skills: compactPreviewText(item.expected_behavior_from_task_and_skills),
      downstream_consequence: compactPreviewText(item.downstream_consequence),
      evidence_span: compactPreviewText(item.evidence_span),
      status_counts: item.status_counts,
      attempted_success_rate: item.attempted_success_rate,
      direct_failure_rate: item.direct_failure_rate,
      blocked_rate: item.blocked_rate,
      weight: item.weight,
      weight_basis: item.weight_basis,
    }));
}

function compactStage5ForStage6Preview(value) {
  if (!value || value.__missing || typeof value !== "object") return value;
  if (value.stage_type === "node_parallel_skill_repair_suggestions") {
    return {
      stage_type: value.stage_type,
      thresholds: value.thresholds,
      node_prompt_jobs: value.node_prompt_jobs || [],
      node_skill_repair_suggestions: value.node_skill_repair_suggestions || value.node_repair_recommendations || [],
      skill_repair_recommendations: value.skill_repair_recommendations || [],
      new_skill_recommendations: value.new_skill_recommendations || [],
      stage_notes: value.stage_notes || [],
    };
  }
  const nodeRecommendations = asArray(value.node_repair_recommendations)
    .filter((item) => item?.classification?.needs_repair)
    .map((item) => {
      const status = item.execution_success_analysis || {};
      const coverage = item.skill_coverage || {};
      return {
        node_id: item.node_id,
        node_goal: item.node_goal,
        node_definition: item.node_definition,
        classification: item.classification,
        recommended_action: item.recommended_action,
        target_skill_ids: item.target_skill_ids || [],
        execution_success_analysis: {
          status_counts: status.status_counts,
          attempted_success_rate: status.attempted_success_rate,
          direct_failure_rate: status.direct_failure_rate,
          blocked_rate: status.blocked_rate,
        },
        bad_event_list: compactPreviewEvidenceItems(item.bad_event_list || [], 8),
        skill_coverage_summary: {
          directly_relevant_skill_count: coverage.directly_relevant_skill_count,
          best_overall_coverage: coverage.best_overall_coverage,
          node_gap: coverage.node_gap,
          best_skill_ids: coverage.best_skill_ids,
        },
      };
    });
  const skillRepairs = asArray(value.skill_repair_recommendations).map((item) => ({
    skill_id: item.skill_id,
    skill_title: item.skill_title,
    action: item.action,
    affected_node_ids: item.affected_node_ids || [],
    recommendation_summary: item.recommendation_summary,
    priority_score: item.priority_score,
    repair_targets: asArray(item.repair_targets).map((target) => {
      const row = target.coverage_row || {};
      return {
        node_id: target.node_id,
        node_goal: target.node_goal,
        node_definition: target.node_definition,
        node_pressure: target.node_pressure,
        coverage: {
          overall_coverage: row.overall_coverage,
          coverage_gap: row.coverage_gap,
          coverage_labels: row.coverage_labels,
          missing_slots: row.missing_slots,
          dimension_scores: row.dimension_scores,
          low_score_dimensions: row.low_score_dimensions,
        },
        repair_basis_summary: target.repair_basis_summary,
        classification_reasons: target.classification_reasons || [],
        evidence_items: compactPreviewEvidenceItems(target.evidence_items || [], 8),
        priority_score: target.priority_score,
      };
    }),
  }));
  const newSkills = asArray(value.new_skill_recommendations).map((item) => ({
    action: item.action,
    node_id: item.node_id,
    node_goal: item.node_goal,
    new_skill_id_suggestion: item.new_skill_id_suggestion,
    definition_basis: item.definition_basis,
    recommendation_summary: item.recommendation_summary,
    priority_score: item.priority_score,
    evidence_items: compactPreviewEvidenceItems(item.evidence_items || [], 8),
  }));
  return {
    stage_type: value.stage_type,
    thresholds: value.thresholds,
    node_repair_recommendations: nodeRecommendations,
    skill_repair_recommendations: skillRepairs,
    new_skill_recommendations: newSkills,
    calculation_notes: value.calculation_notes || [],
    stage_notes: value.stage_notes || [],
  };
}

function stagePayloadForPreview(stage, bundle, outputs) {
  const child = selectedTraceChild(stage);
  const trajectories = bundle.failed_trajectories || [];
  const trajectory = child ? trajectories[child.index] : trajectories[0] || null;
  const constraints = bundle.constraints;
  if (stage?.id === "stage-01-input-standardization") {
    const skills = bundle.skill_library || [];
    if (!child) {
      return {
        stage_01a_task_description_standardization: stageOutput(outputs, "stage_01a_task_description_standardization"),
        stage_01b_skill_standardizations: stageOutput(outputs, "stage_01b_skill_standardizations"),
      };
    }
    if (child?.kind === "task" || child?.name === "stage-01a-task-description-standardization" || child?.index === 0) {
      return {
        task_description: bundle.task_description,
      };
    }
    if (child?.kind === "skill" || String(child?.name || "").startsWith("stage-01b") || (child && child.index > 0 && child.index <= skills.length)) {
      const skillIndex = Number.isFinite(Number(child?.skillIndex)) ? Number(child.skillIndex) : Math.max(0, Number(child?.index || 1) - 1);
      const skill = skills[skillIndex] || missingStageOutput(`skill_library[${skillIndex}]`);
      const skillFile = skill && !skill.__missing
        ? Object.fromEntries(Object.entries(skill).filter(([key]) => key !== "attached_files"))
        : skill;
      return {
        skill_file: skillFile,
        skill_attached_files: skill?.attached_files || null,
      };
    }
    return {};
  }
  if (stage?.id === "stage-02-capability-graph") {
    return {
      stage_01a_task_description_standardization: stageOutput(outputs, "stage_01a_task_description_standardization"),
      stage_01b_skill_standardizations: stageOutput(outputs, "stage_01b_skill_standardizations"),
    };
  }
  if (stage?.id === "stage-03-failure-event-extraction") {
    const formatted = stage2TrajectoryForPrompt(trajectory);
    return {
      stage_01a_task_description_standardization: stageOutput(outputs, "stage_01a_task_description_standardization"),
      trajectory: formatted ? Object.fromEntries(Object.entries(formatted).filter(([key]) => !["visible_failure_result", "final_artifacts"].includes(key))) : null,
      visible_failure_result: formatted?.visible_failure_result || null,
      final_artifacts: formatted?.final_artifacts || [],
    };
  }
  if (stage?.id === "stage-05-node-execution-assessment") {
    const trajId = trajectory?.traj_id;
    const stage3 = asArray(stageOutput(outputs, "stage_03_failure_events_by_trace"));
    const alignments = asArray(stageOutput(outputs, "stage_04_failure_event_alignment")?.alignments);
    return {
      stage_02_capability_graph: stageOutput(outputs, "stage_02_capability_graph")?.capability_graph || stageOutput(outputs, "stage_02_capability_graph"),
      trajectory: stage2TrajectoryForPrompt(trajectory),
      stage_03_failure_causality: stage3.find((item) => String(item?.traj_id || "") === String(trajId || "")) || null,
      stage_04_event_node_alignments: alignments.filter((item) => String(item?.traj_id || "") === String(trajId || "")),
    };
  }
  if (stage?.id === "stage-04-failure-event-alignment") {
    return {
      stage_02_capability_graph: stageOutput(outputs, "stage_02_capability_graph")?.capability_graph || stageOutput(outputs, "stage_02_capability_graph"),
      stage_03_failure_events_by_trace: stageOutput(outputs, "stage_03_failure_events_by_trace"),
    };
  }
  if (stage?.id === "stage-06-skill-repair-suggestions") {
    const child = selectedTraceChild(stage);
    return {
      stage_02_capability_graph: stageOutput(outputs, "stage_02_capability_graph")?.capability_graph || stageOutput(outputs, "stage_02_capability_graph"),
      node_id: child?.nodeId || "<selected Stage 6 node>",
      node_repair_action: child?.nodeRepairAction || child?.localRecommendedAction || "<selected Stage 6 node repair action>",
      node_bound_evidence: child?.nodeBoundEvidence || "Generated by local Stage 6 code for each repair-needed node. Select a Stage 6 node child to inspect the exact node-bound evidence.",
      node_related_skill_library: child?.nodeRelatedSkillLibrary || "Filtered raw Skill files directly relevant to the selected node. Empty when Stage 2 found no directly relevant Skill.",
      stage_01b_skill_standardizations: stageOutput(outputs, "stage_01b_skill_standardizations"),
    };
  }
  if (stage?.id === "stage-08-transactional-skill-repair") {
    const stage7 = stageOutput(outputs, "stage_08_transactional_skill_repair");
    const currentIndex = Number(stage7?.current_suggestion_index || 0);
    const suggestion = asArray(stage7?.suggestions)[currentIndex];
    const operation = stage7TemplateOperation(stage) || stage7?.next_operation || "repair";
    const common = {
      repair_unit_id: suggestion?.repair_unit_id || suggestion?.suggestion_id || "Generate Prompt once to initialize Stage 8 and preview the exact next inputs.",
      suggestion_ids: suggestion?.suggestion_ids || (suggestion?.suggestion_id ? [suggestion.suggestion_id] : []),
      repair_action: suggestion?.action || null,
      selected_stage6_suggestions: suggestion?.source_suggestions || (suggestion?.source_suggestion ? [suggestion.source_suggestion] : []),
    };
    if (operation === "review") {
      const unavailable = { __not_available_until: "Run one repair operation to generate a candidate, then generate the review prompt." };
      return {
        ...common,
        files_before_this_attempt: unavailable,
        candidate_modified_files: unavailable,
        current_skill_library_inventory: unavailable,
        related_skill_summaries: unavailable,
        capability_nodes: unavailable,
        suggestion_evidence: unavailable,
        node_execution_context: unavailable,
        coverage_context: unavailable,
      };
    }
    const unavailable = { __not_available_until: "Generate the Stage 8 repair prompt to materialize this exact input." };
    return {
      ...common,
      allowed_skill_root: unavailable,
      current_related_files: unavailable,
      current_skill_library_inventory: unavailable,
      previous_review_feedback: null,
    };
  }
  return {};
}

function stage7TemplateOperation(stage) {
  if (stage?.id !== "stage-08-transactional-skill-repair") return "";
  const subject = selectedStageSubject(stage);
  if (subject?.operation) return subject.operation;
  if (subject?.id === "stage-08-skill-review" || subject?.template === "stage-08-skill-review.txt") return "review";
  if (subject?.id === "stage-08-skill-repair" || subject?.template === "stage-08-skill-repair.txt") return "repair";
  return "";
}

async function archivedPayloadForPreview(stage) {
  if (stage?.id !== "stage-08-transactional-skill-repair") return null;
  const subject = selectedStageSubject(stage);
  if (subject?.evidence?.path) return loadJsonArtifact(subject.evidence.path);
  const desiredOperation = stage7TemplateOperation(stage);
  if (stage.nextEvidence?.path && (!desiredOperation || desiredOperation === stage.nextOperation)) {
    return loadJsonArtifact(stage.nextEvidence.path);
  }
  return null;
}

function valueForExternalPlaceholder(token, payload) {
  if (token === "{{visible_evidence_json}}" || token === "<visible_evidence_json>") return payload;
  const match = String(token || "").match(/^<(.+)>$/);
  if (!match) return undefined;
  return payload[match[1]];
}

function evidenceEntries(stage, payload, templateText = "") {
  const keys = Object.keys(payload || {});
  const entries = keys.map((key) => {
    const token = `<${key}>`;
    const value = payload[key];
    const text = stringify(value);
    return {
      key,
      token,
      used: templateText.includes(token),
      size: text.length,
      value,
      text,
    };
  });
  entries.unshift({
    key: "visible_evidence_json",
    token: "{{visible_evidence_json}}",
    used: templateText.includes("{{visible_evidence_json}}") || templateText.includes("<visible_evidence_json>"),
    size: stringify(payload).length,
    value: payload,
    text: stringify(payload),
    fullPayload: true,
  });
  return entries;
}

async function renderEvidence(stage) {
  if (!state.status?.inputBundle?.path) {
    els.detail.innerHTML = `<div class="detail-empty">需要先初始化运行目录，才能预览当前 stage 的 visible evidence。</div>`;
    return;
  }
  const templatePath = templatePathForStage(stage);
  const templateCached = templatePath ? state.fileCache.get(templatePath) : null;
  if (templatePath && !templateCached) {
    els.detail.innerHTML = `<div class="detail-empty">正在读取 evidence 结构...</div>`;
    try {
      await loadFile(templatePath);
      renderEvidence(stage);
    } catch (error) {
      els.detail.innerHTML = `<div class="detail-error">读取模板失败：${escapeHtml(error.message)}</div>`;
    }
    return;
  }
  try {
    const selectedChild = selectedTraceChild(stage);
    if (selectedChild?.evidence?.path) {
      const payload = await loadJsonArtifact(selectedChild.evidence.path);
      const templateText = templateCached?.text || "";
      const entries = evidenceEntries(stage, payload || {}, templateText);
      els.detail.innerHTML = `
        <div class="repair-stage-evidence-head">
          <strong>${escapeHtml(selectedChild.label || selectedChild.name)}</strong>
          <span>${escapeHtml(selectedChild.evidence.path)}</span>
        </div>
        <div class="repair-stage-evidence-list">
          ${entries.map(renderEvidenceEntry).join("")}
        </div>
      `;
      return;
    }
    const desiredOperation = stage7TemplateOperation(stage);
    if (
      stage?.id === "stage-08-transactional-skill-repair"
      && stage.nextEvidence?.path
      && (!desiredOperation || desiredOperation === stage.nextOperation)
    ) {
      const payload = await loadJsonArtifact(stage.nextEvidence.path);
      const entries = evidenceEntries(stage, payload || {}, templateCached?.text || "");
      els.detail.innerHTML = `
        <div class="repair-stage-evidence-head">
          <strong>Stage 8 下一次 ${escapeHtml(stage.nextOperation || "operation")}</strong>
          <span>${escapeHtml(stage.nextEvidence.path)}</span>
        </div>
        <div class="repair-stage-evidence-list">
          ${entries.map(renderEvidenceEntry).join("")}
        </div>
      `;
      return;
    }
    const { bundle, outputs } = await currentPreviewContext(stage);
    const subject = selectedStageSubject(stage);
    const payload = stagePayloadForPreview(stage, bundle, outputs);
    const entries = evidenceEntries(stage, payload, templateCached?.text || "");
    els.detail.innerHTML = `
      <div class="repair-stage-evidence-head">
        <strong>${escapeHtml(subject?.label || stage.label)}</strong>
        <span>${escapeHtml(templatePath || "-")}</span>
      </div>
      <div class="repair-stage-evidence-list">
        ${entries.map(renderEvidenceEntry).join("")}
      </div>
    `;
  } catch (error) {
    els.detail.innerHTML = `<div class="detail-error">生成 evidence 预览失败：${escapeHtml(error.message)}</div>`;
  }
}

function renderEvidenceEntry(entry) {
  const status = entry.used ? "已使用" : "未使用";
  const statusClass = entry.used ? "status-ok" : "status-warn";
  const title = entry.fullPayload ? "完整 payload" : entry.key;
  return `
    <details class="repair-stage-evidence-card" ${entry.used ? "open" : ""}>
      <summary>
        <div>
          <strong>${escapeHtml(title)}</strong>
          <span>${escapeHtml(entry.token)}</span>
        </div>
        <div class="repair-stage-row-meta">
          <span class="status-pill ${statusClass}">${status}</span>
          <span>${formatChars(entry.size)}</span>
        </div>
      </summary>
      <pre class="repair-stage-placeholder-preview">${escapeHtml(entry.text)}</pre>
    </details>
  `;
}

async function previewExternalPlaceholder(token) {
  const target = $("stagePlaceholderPreview");
  if (!target) return;
  const requestId = ++state.placeholderPreviewRequestId;
  const stage = selectedStage();
  if (!state.status?.inputBundle?.path) {
    target.textContent = "需要先初始化运行目录，才能预览外部输入。";
    return;
  }
  target.textContent = "正在读取预览...";
  try {
    const archivedPayload = await archivedPayloadForPreview(stage);
    if (requestId !== state.placeholderPreviewRequestId || target !== $("stagePlaceholderPreview")) return;
    if (archivedPayload) {
      const archivedValue = valueForExternalPlaceholder(token, archivedPayload);
      target.textContent = stringify(archivedValue === undefined ? { __missing_placeholder: token } : archivedValue);
      return;
    }
    const { bundle, outputs } = await currentPreviewContext(stage);
    if (requestId !== state.placeholderPreviewRequestId || target !== $("stagePlaceholderPreview")) return;
    const payload = stagePayloadForPreview(stage, bundle, outputs);
    const value = valueForExternalPlaceholder(token, payload);
    target.textContent = stringify(value === undefined ? { __missing_placeholder: token } : value);
  } catch (error) {
    if (requestId !== state.placeholderPreviewRequestId || target !== $("stagePlaceholderPreview")) return;
    target.textContent = `预览失败：${error.message}`;
  }
}

function templateVariableName(token) {
  const match = String(token || "").match(/^\{\{([a-zA-Z0-9_]+)\}\}$/);
  return match ? match[1] : "";
}

async function loadTemplateVariable(name) {
  const params = new URLSearchParams({ name });
  return fetchJson(`/api/repair-stage/template-variable?${params.toString()}`);
}

async function renderTemplateVariableEditor(token) {
  const target = $("stagePlaceholderPreview");
  if (!target) return;
  const requestId = ++state.placeholderPreviewRequestId;
  const name = templateVariableName(token);
  if (!name) {
    target.textContent = `无法识别模板变量：${token}`;
    return;
  }
  target.textContent = `正在读取 ${name}...`;
  try {
    const variable = await loadTemplateVariable(name);
    if (requestId !== state.placeholderPreviewRequestId || target !== $("stagePlaceholderPreview")) return;
    const currentText = variable.text ?? variable.defaultText ?? "";
    target.innerHTML = `
      <div class="repair-stage-variable-editor-head">
        <div>
          <strong>${escapeHtml(token)}</strong>
          <span>${escapeHtml(variable.exists ? "已保存 override" : "使用内置默认值")}</span>
        </div>
        <span title="${escapeAttr(variable.path)}">${escapeHtml(variable.path)}</span>
      </div>
      <textarea id="stageVariableEditor" class="input repair-stage-variable-editor">${escapeHtml(currentText)}</textarea>
      <div class="repair-stage-editor-actions">
        <button id="stageSaveVariable" class="primary-button">保存变量</button>
        <button id="stageResetVariable" class="secondary-button">恢复默认内容</button>
        <span id="stageVariableStatus" class="muted"></span>
      </div>
    `;
    $("stageSaveVariable").addEventListener("click", () => saveTemplateVariableOverride(variable));
    $("stageResetVariable").addEventListener("click", () => {
      const editor = $("stageVariableEditor");
      if (editor) editor.value = variable.defaultText || "";
    });
  } catch (error) {
    if (requestId !== state.placeholderPreviewRequestId || target !== $("stagePlaceholderPreview")) return;
    target.innerHTML = `<div class="detail-error">读取模板变量失败：${escapeHtml(error.message)}</div>`;
  }
}

async function saveTemplateVariableOverride(variable) {
  const editor = $("stageVariableEditor");
  const status = $("stageVariableStatus");
  if (!editor || !status) return;
  status.textContent = "保存中...";
  try {
    await postJson("/api/repair-stage/file", { path: variable.path, text: editor.value });
    state.fileCache.delete(variable.path);
    status.textContent = "已保存，之后生成 prompt 会使用这个变量值。";
  } catch (error) {
    status.textContent = `保存失败：${error.message}`;
  }
}

function parseStageJsonFile(text) {
  try {
    return JSON.parse(text || "null");
  } catch {
    return null;
  }
}

function selectedStageOutputValue(value, stage = selectedStage()) {
  if (!stage || value === null || value === undefined) return value;
  let selected = value;
  // 完整 pipeline 通常只保存一个聚合 stage_outputs.json；Debug 页应先抽取
  // 当前 stage/轨迹，避免把整个流程的输出误当成当前 response 的计算结果。
  if (selected && typeof selected === "object" && !Array.isArray(selected) && stage.key in selected) {
    selected = selected[stage.key];
  }
  const child = selectedTraceChild(stage);
  if (child?.trajId && Array.isArray(selected)) {
    const traceValue = selected.find((item) => String(item?.traj_id || "") === String(child.trajId));
    if (traceValue) selected = traceValue;
  }
  return selected;
}

function renderStageReadableMeta(rows) {
  return `
    <div class="repair-trace-kv">
      ${rows.map(([label, value]) => `
        <div>
          <span>${escapeHtml(label)}</span>
          <strong>${escapeHtml(value === undefined || value === null || value === "" ? "-" : String(value))}</strong>
        </div>
      `).join("")}
    </div>
  `;
}

function responseUsage(response) {
  return response?.raw_response?.usage || response?.usage || {};
}

function renderStageResponseFile(response, rawText, info) {
  const stage = selectedStage();
  const raw = response?.raw_response || {};
  const usage = responseUsage(response);
  const choices = Array.isArray(raw.choices) ? raw.choices.length : 0;
  const extractedJson = parseStageJsonFile(response?.extracted_content || "");
  const extractedView = stage?.id === "stage-05-node-execution-assessment" && extractedJson?.node_assessments
    ? renderNodeExecutionAssessment(extractedJson, false)
    : renderReadableLlmText(response?.extracted_content || "", { emptyText: "没有 extracted content。" });
  return `
    ${renderCalculationAction(stage, "response")}
    ${renderCalculatedOutputPreview(stage, "response")}
    <section class="repair-trace-section">
      <h3>响应元信息</h3>
      ${renderStageReadableMeta([
        ["HTTP", response?.status_code ?? "-"],
        ["Response ID", raw.id || "-"],
        ["Model", raw.model || "-"],
        ["Choices", choices],
        ["Prompt Tokens", usage.prompt_tokens ?? usage.input_tokens ?? "-"],
        ["Completion Tokens", usage.completion_tokens ?? usage.output_tokens ?? "-"],
        ["Total Tokens", usage.total_tokens ?? "-"],
        ["Modified", formatDate(info.modifiedAt)],
      ])}
    </section>
    <section class="repair-trace-section">
      <h3>Extracted Content · 可读视图</h3>
      ${extractedView}
    </section>
    <section class="repair-trace-section">
      <details class="repair-trace-details">
        <summary>Raw response.json</summary>
        ${renderRawTextBlock(rawText)}
      </details>
    </section>
  `;
}

function judgmentValue(judgment) {
  const value = judgment?.value;
  if (value === true) return "true";
  if (value === false) return "false";
  if (value === null || value === undefined || value === "") return "null";
  return String(value);
}

function renderNodeExecutionAssessment(value, includeStatus = true) {
  const items = asArray(value?.node_assessments);
  if (!items.length) return `<div class="detail-empty">没有 node assessments。</div>`;
  return `
    <div class="repair-trace-table-wrap">
      <table class="repair-trace-table">
        <thead><tr><th>Node</th><th>Presence</th><th>Successful</th><th>Prerequisites</th><th>Judgeable</th>${includeStatus ? "<th>Status</th>" : ""}</tr></thead>
        <tbody>${items.map((item) => `
          <tr>
            <td><strong>${escapeHtml(item.node_id || "-")}</strong></td>
            <td>${escapeHtml(judgmentValue(item.capability_presence))}</td>
            <td>${escapeHtml(judgmentValue(item.fully_successful))}</td>
            <td>${escapeHtml(judgmentValue(item.prerequisites_satisfied))}</td>
            <td>${escapeHtml(judgmentValue(item.success_judgeable))}</td>
            ${includeStatus ? `<td><strong>${escapeHtml(item.status || "-")}</strong></td>` : ""}
          </tr>
          <tr><td colspan="${includeStatus ? 6 : 5}">${renderReadableContent({
            presence_reason: item.capability_presence?.reason,
            success_reason: item.fully_successful?.reason,
            prerequisite_reason: item.prerequisites_satisfied?.reason,
            judgeability_reason: item.success_judgeable?.reason,
            status_calculation: includeStatus ? item.status_calculation : undefined,
          })}</td></tr>
        `).join("")}</tbody>
      </table>
    </div>
  `;
}

function renderCoverageCalculationSummary(rows) {
  const items = asArray(rows);
  if (!items.length) return "";
  return `
    <section class="repair-trace-section">
      <h3>本地计算摘要 · Coverage</h3>
      <div class="repair-trace-table-wrap">
        <table class="repair-trace-table">
          <thead>
            <tr>
              <th>Node</th><th>Skill</th><th>Relevant</th><th>Fit</th><th>Trig</th><th>Proc</th><th>Verif</th><th>Recov</th><th>Exec</th><th>Overall</th><th>Gap</th><th>Labels</th>
            </tr>
          </thead>
          <tbody>
            ${items.map((row) => `
              <tr>
                <td>${escapeHtml(row.node_id || "")}</td>
                <td>${escapeHtml(row.skill_id || "-")}</td>
                <td>${escapeHtml(row.directly_relevant === true ? "yes" : row.directly_relevant === false ? "no" : "-")}</td>
                <td>${scoreText(row.node_requirement_fit)}</td>
                <td>${scoreText(row.trigger_coverage)}</td>
                <td>${scoreText(row.procedure_coverage)}</td>
                <td>${scoreText(row.verification_coverage)}</td>
                <td>${scoreText(row.recovery_coverage)}</td>
                <td>${escapeHtml(row.execution_support_need || "-")} ${scoreText(row.execution_support_coverage)}</td>
                <td><strong>${scoreText(row.overall_coverage)}</strong></td>
                <td>${scoreText(row.coverage_gap)}</td>
                <td>${escapeHtml(asArray(row.coverage_labels).filter(Boolean).join(", ") || "-")}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
      <p class="muted">overall_coverage、coverage_gap、coverage_labels 由本地代码计算；LLM 只提供相关性、维度分数和证据。</p>
    </section>
  `;
}

function renderRootCauseCalculationSummary(hypotheses) {
  const items = asArray(hypotheses);
  if (!items.length) return "";
  return `
    <section class="repair-trace-section">
      <h3>本地计算摘要 · Root Cause Score</h3>
      <div class="repair-trace-table-wrap">
        <table class="repair-trace-table">
          <thead>
            <tr>
              <th>ID</th><th>Type</th><th>Node</th><th>F</th><th>P</th><th>G</th><th>D</th><th>U</th><th>A</th><th>Score</th>
            </tr>
          </thead>
          <tbody>
            ${items.map((row) => {
              const factors = row.score_factors || {};
              return `
                <tr>
                  <td>${escapeHtml(row.hypothesis_id || "-")}</td>
                  <td>${escapeHtml(row.root_cause_type || "-")}</td>
                  <td>${escapeHtml(row.node_id || "-")}</td>
                  <td>${scoreText(factors.F)}</td>
                  <td>${scoreText(factors.P)}</td>
                  <td>${scoreText(factors.G)}</td>
                  <td>${scoreText(factors.D)}</td>
                  <td>${scoreText(factors.U)}</td>
                  <td>${scoreText(factors.A)}</td>
                  <td><strong>${scoreText(row.score)}</strong></td>
                </tr>
              `;
            }).join("")}
          </tbody>
        </table>
      </div>
      <p class="muted">Score(h)=0.25F+0.20P+0.20G+0.15D+0.10U+0.10A，由本地代码计算并排序。</p>
    </section>
  `;
}

function renderLocalCalculationSummary(value) {
  if (!value || typeof value !== "object") return "";
  const stage = selectedStage();
  if (stage?.id === "stage-05-node-execution-assessment") {
    const selected = selectedStageOutputValue(value, stage);
    if (selected?.node_assessments) {
      return `
        <section class="repair-trace-section">
          <h3>本地计算摘要 · Node Status</h3>
          ${renderNodeExecutionAssessment(selected, true)}
        </section>
      `;
    }
  }
  if (value.stage_type === "node_parallel_skill_repair_suggestions") {
    return renderNodeRepairSuggestionSummary(value);
  }
  return [
    renderCoverageCalculationSummary(value.coverage_pairs || value.skill_coverage_matrix || value.coverage_matrix),
    renderRootCauseCalculationSummary(value.root_cause_hypotheses),
  ].filter(Boolean).join("");
}

function renderNodeRepairSuggestionSummary(value) {
  const nodeItems = Array.isArray(value.node_skill_repair_suggestions)
    ? value.node_skill_repair_suggestions
    : Array.isArray(value.node_repair_recommendations)
      ? value.node_repair_recommendations
      : [];
  const existingItems = Array.isArray(value.skill_repair_recommendations)
    ? value.skill_repair_recommendations
    : [];
  const newItems = Array.isArray(value.new_skill_recommendations)
    ? value.new_skill_recommendations
    : [];
  const rows = nodeItems.map((item) => {
    const suggestions = Array.isArray(item.skill_repair_suggestions) ? item.skill_repair_suggestions : [];
    const context = item.local_stage6_context || item.local_stage5_context || {};
    const action = item.node_repair_action || context.local_recommended_action || "-";
    const confidenceValues = suggestions
      .map((suggestion) => Number(suggestion.confidence))
      .filter((value) => Number.isFinite(value));
    const avgConfidence = confidenceValues.length
      ? (confidenceValues.reduce((sum, value) => sum + value, 0) / confidenceValues.length).toFixed(2)
      : "-";
    return `
      <tr>
        <td>${escapeHtml(item.node_id || context.node_id || "-")}</td>
        <td>${escapeHtml(action)}</td>
        <td>${suggestions.length}</td>
        <td>${avgConfidence}</td>
      </tr>
    `;
  }).join("");
  return `
    <section class="calc-panel">
      <h4>Stage 6 Node Repair Suggestions</h4>
      <p class="muted">Only repair-needed nodes are shown here. Each row corresponds to one repair-LLM prompt and response.</p>
      <div class="metric-grid">
        <div><strong>${nodeItems.length}</strong><span>node prompts</span></div>
        <div><strong>${existingItems.length}</strong><span>existing skill groups</span></div>
        <div><strong>${newItems.length}</strong><span>new skill suggestions</span></div>
      </div>
      <table class="mini-table">
        <thead><tr><th>Node</th><th>Action</th><th>Suggestions</th><th>Avg confidence</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="4" class="muted">No node repair suggestions yet.</td></tr>`}</tbody>
      </table>
    </section>
  `;
}

function renderStageJsonFile(value, rawText, info, title) {
  const stage = selectedStage();
  return `
    ${renderCalculationAction(stage, state.tab)}
    ${renderCalculatedOutputPreview(stage, state.tab)}
    ${renderLocalCalculationSummary(value)}
    <section class="repair-trace-section">
      <h3>${escapeHtml(title)} · 可读视图</h3>
      ${renderReadableContent(value, { emptyText: "没有 JSON 内容。" })}
    </section>
    <section class="repair-trace-section">
      <details class="repair-trace-details">
        <summary>Raw JSON · ${escapeHtml(formatBytes(info.size))}</summary>
        ${renderRawTextBlock(rawText)}
      </details>
    </section>
  `;
}

function renderStageReadableFile(path, tab, text, info) {
  const parsed = parseStageJsonFile(text);
  if (!parsed) return null;
  if (tab === "response") return renderStageResponseFile(parsed, text, info);
  if (tab === "parsed") return renderStageJsonFile(parsed, text, info, "Parsed JSON");
  if (tab === "output") {
    const selected = selectedStageOutputValue(parsed);
    const selectedText = selected === parsed ? text : JSON.stringify(selected, null, 2);
    return renderStageJsonFile(selected, selectedText, info, "Stage Output");
  }
  if (tab === "request") {
    return `
      <section class="repair-trace-section">
        <h3>Request JSON · 可读视图</h3>
        ${renderReadableContent(parsed, { emptyText: "没有 request 内容。" })}
      </section>
      <section class="repair-trace-section">
        <details class="repair-trace-details">
          <summary>Raw request.json</summary>
          ${renderRawTextBlock(text)}
        </details>
      </section>
    `;
  }
  if (path.toLowerCase().endsWith(".json")) return renderStageJsonFile(parsed, text, info, "JSON 文件");
  return null;
}

function bindCalculationAction() {
  const button = $("stageCalculateLocal");
  if (!button) return;
  button.addEventListener("click", async () => {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "计算中...";
    try {
      await startStageCommand("calculate");
    } catch (error) {
      els.runStatus.textContent = `计算失败：${error.message}`;
      button.disabled = false;
      button.textContent = original;
    }
  });
}

function bindTabShortcuts() {
  document.querySelectorAll("[data-tab-shortcut]").forEach((button) => {
    button.addEventListener("click", () => {
      state.tab = button.dataset.tabShortcut;
      renderDetail();
    });
  });
}

function renderFile(path, editable) {
  const cached = state.fileCache.get(path);
  if (!cached) {
    els.detail.innerHTML = `<div class="detail-empty">正在读取 ${escapeHtml(path)}...</div>`;
    loadFile(path).then(() => renderDetail()).catch((error) => {
      els.detail.innerHTML = `<div class="detail-error">读取失败：${escapeHtml(error.message)}</div>`;
    });
    return;
  }
  const info = cached.info || {};
  const text = cached.text ?? "";
  if (editable) {
    const stage = selectedStage();
    const helper = state.tab === "template" ? renderTemplateHelper(selectedStageSubject(stage), text) : "";
    els.detail.innerHTML = `
      <div class="repair-stage-file-head">
        <span title="${escapeAttr(path)}">${escapeHtml(path)}</span>
        <span>${formatBytes(info.size)} · ${formatDate(info.modifiedAt)}</span>
      </div>
      ${helper}
      <textarea id="stageFileEditor" class="input repair-stage-editor">${escapeHtml(text)}</textarea>
      <div class="repair-stage-editor-actions">
        <button id="stageSaveFile" class="primary-button">保存</button>
        <span id="stageSaveStatus" class="muted"></span>
      </div>
    `;
    $("stageSaveFile").addEventListener("click", () => saveCurrentFile(path));
    bindTemplateHelperActions();
    return;
  }
  const readable = renderStageReadableFile(path, state.tab, text, info);
  ensureCalculatedOutputPreviewLoaded(selectedStage(), state.tab);
  els.detail.innerHTML = `
    <div class="repair-stage-file-head">
      <span title="${escapeAttr(path)}">${escapeHtml(path)}</span>
      <span>${formatBytes(info.size)} · ${formatDate(info.modifiedAt)}</span>
    </div>
    ${readable || `<pre class="repair-trace-pre">${escapeHtml(text)}</pre>`}
  `;
  bindCalculationAction();
  bindTabShortcuts();
}

function placeholdersForSubject(subject) {
  const templateKey = subject?.template ? subject.template.replace(/\.txt$/, "") : "";
  return TEMPLATE_PLACEHOLDERS[subject?.id]
    || TEMPLATE_PLACEHOLDERS[subject?.name]
    || TEMPLATE_PLACEHOLDERS[templateKey]
    || { variables: [], external: [] };
}

function renderTemplateHelper(stage, templateText = "") {
  const placeholders = placeholdersForSubject(stage);
  const variables = placeholders.variables || [];
  const external = placeholders.external || [];
  const renderButtons = (items, kind) => items.map((item) => {
    const used = templateText.includes(item);
    const usedClass = used ? " used" : "";
    const usedLabel = used ? "已使用" : "未使用";
    const primaryAttr = kind === "variable"
      ? `data-edit-variable="${escapeAttr(item)}"`
      : `data-preview-placeholder="${escapeAttr(item)}"`;
    const primaryLabel = kind === "variable" ? "编辑" : "预览";
    const title = kind === "variable" ? "编辑这个模板变量的保存值" : "预览外部输入，不修改模板";
    return `
      <span class="repair-stage-placeholder-group">
        <button class="repair-stage-placeholder${usedClass}" ${primaryAttr} title="${title}">
          <span>${escapeHtml(item)}</span>
          <small>${primaryLabel} · ${usedLabel}</small>
        </button>
        <button class="repair-stage-placeholder-action" data-insert-placeholder="${escapeAttr(item)}" title="把占位符插入到当前光标位置">插入</button>
      </span>
    `;
  }).join("");
  return `
    <div class="repair-stage-template-helper">
      <div>
        <strong>模板变量</strong>
        <div class="repair-stage-placeholder-row">${renderButtons(variables, "variable") || `<span class="muted">无</span>`}</div>
      </div>
      <div>
        <strong>外部输入占位符</strong>
        <div class="repair-stage-placeholder-row">${renderButtons(external, "external") || `<span class="muted">无</span>`}</div>
      </div>
      <div>
        <strong>预览 / 变量编辑</strong>
        <div id="stagePlaceholderPreview" class="repair-stage-placeholder-preview">选择外部输入占位符预览，或选择模板变量进行编辑。</div>
      </div>
    </div>
  `;
}

function bindTemplateHelperActions() {
  for (const button of els.detail.querySelectorAll("[data-insert-placeholder]")) {
    button.addEventListener("click", () => insertIntoEditor(button.dataset.insertPlaceholder || ""));
  }
  for (const button of els.detail.querySelectorAll("[data-preview-placeholder]")) {
    button.addEventListener("click", () => previewExternalPlaceholder(button.dataset.previewPlaceholder || ""));
  }
  for (const button of els.detail.querySelectorAll("[data-edit-variable]")) {
    button.addEventListener("click", () => renderTemplateVariableEditor(button.dataset.editVariable || ""));
  }
}

function insertIntoEditor(text) {
  const editor = $("stageFileEditor");
  if (!editor || !text) return;
  const start = editor.selectionStart ?? editor.value.length;
  const end = editor.selectionEnd ?? editor.value.length;
  const before = editor.value.slice(0, start);
  const after = editor.value.slice(end);
  editor.value = `${before}${text}${after}`;
  const next = start + text.length;
  editor.focus();
  editor.setSelectionRange(next, next);
}

async function saveCurrentFile(path) {
  const editor = $("stageFileEditor");
  const status = $("stageSaveStatus");
  if (!editor) return;
  const nextText = editor.value;
  status.textContent = "保存中...";
  try {
    await postJson("/api/repair-stage/file", { path, text: nextText });
    state.fileCache.delete(path);
    status.textContent = "已保存";
    await loadFile(path);
    window.setTimeout(() => renderDetail(), 500);
  } catch (error) {
    status.textContent = `保存失败：${error.message}`;
  }
}

function renderJobs() {
  const jobs = state.jobs.slice().sort((a, b) => new Date(b.createdAt || 0) - new Date(a.createdAt || 0));
  if (!jobs.length) {
    els.jobs.innerHTML = `<div class="detail-empty">没有后台 job。</div>`;
    return;
  }
  els.jobs.innerHTML = jobs.map((job) => {
    const selected = job.id === state.selectedJobId ? " selected" : "";
    return `
      <button class="repair-stage-job${selected}" data-job-id="${escapeAttr(job.id)}">
        <strong>${escapeHtml(job.command)}${job.stage ? ` · ${escapeHtml(job.stage)}` : ""}</strong>
        <span>${stageBadge(job.status)} ${escapeHtml(job.outputDir || "")}</span>
        <small>${formatDate(job.createdAt)} · ${job.logLineCount || 0} lines</small>
      </button>
    `;
  }).join("");
}

async function loadJobLogs(jobId) {
  if (!jobId) return;
  const data = await fetchJson(`/api/repair-stage/jobs/${encodeURIComponent(jobId)}/logs`);
  state.jobLogs.set(jobId, (data.logs || []).slice(-MAX_STORED_STAGE_LOG_LINES));
  if (state.tab === "logs") renderLogs();
}

function renderLogs() {
  const logs = state.jobLogs.get(state.selectedJobId) || [];
  if (!state.selectedJobId) {
    els.detail.innerHTML = `<div class="detail-empty">选择一个后台 job 查看日志。</div>`;
    return;
  }
  const hiddenCount = Math.max(0, logs.length - MAX_VISIBLE_STAGE_LOG_LINES);
  const visibleLogs = logs.slice(-MAX_VISIBLE_STAGE_LOG_LINES);
  const omission = hiddenCount
    ? `<div class="detail-note">为保持页面流畅，已隐藏更早的 ${hiddenCount} 行；完整日志仍保存在后台 job 中。</div>`
    : "";
  els.detail.innerHTML = `${omission}
    <pre class="repair-log">${visibleLogs.map((line) => `[${formatDate(line.at)}] ${line.text}`).map(escapeHtml).join("\n") || "暂无日志"}</pre>
  `;
}

function scheduleLogRender() {
  if (state.logRenderTimer !== null) return;
  state.logRenderTimer = window.setTimeout(() => {
    state.logRenderTimer = null;
    if (state.tab === "logs") renderLogs();
  }, 100);
}

function showSelectedJobLogs() {
  state.tab = "logs";
  renderTabs();
  renderLogs();
}

async function startInit() {
  const task = selectedTask();
  if (!task) return;
  const libraryId = els.skillsLibrary.value;
  const variant = els.variant.value.trim() || defaultVariant();
  els.variant.value = variant;
  updateOutputDirFromForm();
  const body = {
    taskKey: task.key,
    sourceSkillsLibraryId: libraryId,
    outputVariant: variant,
    tracePaths: els.tracePaths.value,
    maxTraces: Number(els.maxTraces.value || 5),
    force: els.force.checked,
    ...currentPayload(),
  };
  els.initStatus.textContent = "启动中...";
  const data = await postJson("/api/repair-stage/init", body);
  state.selectedJobId = data.job.id;
  state.selectedRunDir = data.job.outputDir;
  els.outputDir.value = data.job.outputDir;
  els.initStatus.textContent = `已启动 ${data.job.id}`;
  showSelectedJobLogs();
  await loadRuns();
  await loadJobLogs(data.job.id);
}

async function startStageCommand(command, stageRunMode = "step") {
  const stage = selectedStage();
  if (!stage && command !== "finalize") return;
  const child = selectedTraceChild(stage);
  const body = currentPayload({
    stage: stage?.id,
    traceIndex: child ? Number(child.index) : null,
    stageRunMode,
  });
  els.runStatus.textContent = "启动中...";
  const endpoint = command === "prompt"
    ? "/api/repair-stage/prompt"
    : command === "calculate"
      ? "/api/repair-stage/calculate"
      : "/api/repair-stage/run";
  const data = await postJson(endpoint, body);
  state.selectedJobId = data.job.id;
  els.runStatus.textContent = `已启动 ${data.job.id}`;
  showSelectedJobLogs();
  await loadRuns();
  await loadJobLogs(data.job.id);
}

async function stopSelectedJob() {
  if (!state.selectedJobId) return;
  await postJson(`/api/repair-stage/jobs/${encodeURIComponent(state.selectedJobId)}/stop`, {});
  await loadRuns();
}

async function pauseStage7() {
  const active = state.jobs.find((job) => (
    job.outputDir === currentRunDir()
    && job.stage === "stage-08-transactional-skill-repair"
    && ["running", "stopping"].includes(job.status)
  ));
  if (active) {
    state.selectedJobId = active.id;
    await postJson(`/api/repair-stage/jobs/${encodeURIComponent(active.id)}/stop`, {});
  } else {
    await postJson("/api/repair-stage/pause", { outputDir: currentRunDir() });
  }
  els.runStatus.textContent = "Stage 8 已暂停，可从当前建议继续。";
  state.fileCache.clear();
  await loadRuns();
}

function bindEvents() {
  els.presetSelect.addEventListener("change", () => {
    const preset = state.presets.find((item) => item.id === els.presetSelect.value);
    els.presetName.value = preset?.name || "";
    els.presetDelete.disabled = !preset;
    els.presetLoad.disabled = !preset;
  });
  els.presetLoad.addEventListener("click", () => loadSelectedPreset().catch((error) => {
    els.presetStatus.textContent = `加载失败：${error.message}`;
  }));
  els.presetSave.addEventListener("click", () => saveCurrentPreset().catch((error) => {
    els.presetStatus.textContent = `保存失败：${error.message}`;
  }));
  els.presetDelete.addEventListener("click", () => deleteSelectedPreset().catch((error) => {
    els.presetStatus.textContent = `删除失败：${error.message}`;
  }));
  els.stage7RepairMode.addEventListener("change", syncStage7OptionControls);
  els.separateReviewLlm.addEventListener("change", syncStage7OptionControls);
  els.taskSelect.addEventListener("change", () => {
    renderSkillLibraries();
    updateOutputDirFromForm();
  });
  els.variant.addEventListener("input", updateOutputDirFromForm);
  els.runSelect.addEventListener("change", async () => {
    state.selectedRunDir = els.runSelect.value;
    els.outputDir.value = state.selectedRunDir;
    state.fileCache.clear();
    await loadStatus();
  });
  els.outputDir.addEventListener("change", async () => {
    state.selectedRunDir = currentRunDir();
    state.fileCache.clear();
    await loadStatus();
  });
  els.refresh.addEventListener("click", async () => {
    state.fileCache.clear();
    await loadRuns();
  });
  els.inferInputs.addEventListener("click", () => inferInputsFromTracePaths().catch((error) => {
    els.inferStatus.textContent = `推断失败：${error.message}`;
  }));
  els.viewTrajectory.addEventListener("click", (event) => {
    if (els.viewTrajectory.getAttribute("aria-disabled") === "true") event.preventDefault();
  });
  els.init.addEventListener("click", () => startInit().catch((error) => {
    els.initStatus.textContent = `失败：${error.message}`;
  }));
  els.prompt.addEventListener("click", () => startStageCommand("prompt").catch((error) => {
    els.runStatus.textContent = `失败：${error.message}`;
  }));
  els.run.addEventListener("click", () => startStageCommand("run").catch((error) => {
    els.runStatus.textContent = `失败：${error.message}`;
  }));
  els.runUntil.addEventListener("click", () => startStageCommand("run", "until-complete").catch((error) => {
    els.runStatus.textContent = `连续运行失败：${error.message}`;
  }));
  els.pause.addEventListener("click", () => pauseStage7().catch((error) => {
    els.runStatus.textContent = `暂停失败：${error.message}`;
  }));
  els.stopJob.addEventListener("click", () => stopSelectedJob().catch((error) => {
    els.runStatus.textContent = `停止失败：${error.message}`;
  }));
  els.stageList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-stage-id]");
    if (!button) return;
    state.selectedStageId = button.dataset.stageId;
    state.selectedChildIndex = button.dataset.childIndex ?? "";
    state.tab = String(state.selectedChildIndex).startsWith("template-") ? "template" : "prompt";
    renderStages();
    renderDetail();
  });
  els.tabs.addEventListener("click", (event) => {
    const button = event.target.closest("[data-tab]");
    if (!button) return;
    state.tab = button.dataset.tab;
    renderDetail();
  });
  els.jobs.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-job-id]");
    if (!button) return;
    state.selectedJobId = button.dataset.jobId;
    state.tab = "logs";
    renderJobs();
    renderDetail();
    await loadJobLogs(state.selectedJobId);
  });
}

function connectEvents() {
  const source = new EventSource("/api/events");
  source.onmessage = async (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "snapshot") {
      state.jobs = data.stageJobs || state.jobs;
      renderJobs();
    }
    if (data.type === "repair_stage_update" && data.stageJob) {
      const idx = state.jobs.findIndex((job) => job.id === data.stageJob.id);
      if (idx >= 0) state.jobs[idx] = data.stageJob;
      else state.jobs.unshift(data.stageJob);
      renderJobs();
      if (data.stageJob.outputDir === currentRunDir()) {
        const calculationCompleted = data.stageJob.command === "calculate"
          && data.stageJob.status === "completed"
          && data.stageJob.id === state.selectedJobId;
        const calculationFailed = data.stageJob.command === "calculate"
          && data.stageJob.status === "error"
          && data.stageJob.id === state.selectedJobId;
        if (calculationCompleted) {
          state.selectedStageId = data.stageJob.stage || state.selectedStageId;
          state.selectedChildIndex = "";
          state.tab = "output";
          els.runStatus.textContent = "计算完成，已切换到 Output。";
        } else if (calculationFailed) {
          els.runStatus.textContent = `计算失败：${data.stageJob.error || "请查看日志"}`;
        }
        window.setTimeout(() => {
          state.fileCache.clear();
          loadStatus().catch(() => {});
        }, 500);
      }
    }
    if (data.type === "repair_stage_log") {
      const logs = state.jobLogs.get(data.jobId) || [];
      logs.push(data.line);
      state.jobLogs.set(data.jobId, logs.slice(-MAX_STORED_STAGE_LOG_LINES));
      if (state.selectedJobId === data.jobId && state.tab === "logs") scheduleLogRender();
    }
  };
}

async function init() {
  bindEvents();
  syncStage7OptionControls();
  connectEvents();
  els.variant.value = defaultVariant();
  await loadTasks();
  await loadPresetList();
  await loadRuns();
}

init().catch((error) => {
  els.subtitle.textContent = `初始化失败：${error.message}`;
});
