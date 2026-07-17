# SkillsBench Portable

这是从实验工作区整理出的可迁移版本，包含：

- `runner-app/`：本地 Web 控制台，支持任务运行、harness/skills 库切换、jobs 扫描、轨迹查看和 Offline SkillRCA 调试。
- `offline_skill_rca/`：多阶段、可审计的 skills 修复流程，以及外置 prompt/schema。
- `skill-libraries/`：当前工作区已有的少量技能库样本；完整任务数据和 jobs 不随仓库提交。
- `tools/`：数据集下载、容器构建、BenchFlow 运行和 OpenAI 兼容 LLM 探针。
- `scripts/`：Windows/Linux 启动脚本。

## 快速启动

要求：Node.js 18+、Python 3.12+、Git；实际运行 Docker sandbox 任务还需要 Docker 和 uv。

```bash
cp .env.example .env.local
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python tools/dataset.py install --repo https://github.com/benchflow-ai/skillsbench.git
./scripts/start-web.sh
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env.local
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
py -3.12 tools\dataset.py install --repo https://github.com/benchflow-ai/skillsbench.git
.\scripts\start-web.ps1
```

浏览器打开 `http://localhost:5198/`。没有下载数据集时 web 仍可启动，但任务列表为空。

## 数据集管理

数据默认下载到 `.data/skillsbench`，然后以目录链接接入项目根目录；因此不会把数 GB 的任务环境、视频或 jobs 写进 git：

```bash
python3 tools/dataset.py status
python3 tools/dataset.py install --repo https://github.com/benchflow-ai/skillsbench.git --ref main
python3 tools/dataset.py install --mode copy --force
```

`--mode link` 在 Linux 使用符号链接，在 Windows 使用 junction；遇到权限策略时改用 `--mode copy`。

## 本地构建任务容器

```bash
python3 tools/bench.py check
python3 tools/bench.py build --task r2r-mpc-control
```

这只构建指定 task 的 `environment/Dockerfile`，不会扫描和构建整个数据集。构建结果的镜像名默认为 `skillsbench/<task>:local`。

## 用自定义 OpenAI 兼容供应商探针测试

`tools/llm_probe.py` 不绑定某个厂商，只调用 `/v1/chat/completions`，适合先验证 URL、模型和 key，再启动 repair：

```bash
export LLM_API_KEY='...'
python3 tools/llm_probe.py \
  --provider openai \
  --base-url https://api.example.com \
  --model example-model \
  --prompt 'Return JSON only: {"ok": true}'
```

`offline_skill_rca` 使用同一类 OpenAI-compatible 请求。正式 repair 请在 Web 的 Stage Debug 页面填写 repair LLM 配置，并把审查 transcript 保存在 `repair-runs/<task>/<variant>/llm_transcript/`。

## 运行评测

一次运行示例：

```bash
python3 tools/bench.py run \
  --task r2r-mpc-control \
  --agent claude-agent-acp \
  --provider deepseek \
  --model deepseek-v4-flash \
  --skills-dir skill-libraries/r2r-mpc-control/initial \
  --skill-mode with-skill \
  --reasoning-effort low \
  --jobs-dir jobs/local-r2r
```

评测 Web 页支持 `deepseek`、`anthropic`、`openai`、`custom` 四类供应商标签，以及
`no-skill`、`with-skill`、`force-skill` 三种模式。Claude Code 使用自定义供应商时，
`Base URL` 必须提供 Anthropic Messages 兼容入口；API key 可以只在当前表单使用，也可以
选择保存到浏览器配置。思维强度可选 `off`、`minimal`、`low`、`medium`、`high`、`max`、`xhigh`。
`force-skill` 会自动启用全部 skill 的 prompt overlay，并向 BenchFlow 传递 `with-skill`。

重复运行、并行度、force skills、轨迹查看和 group 管理优先使用 Web 控制台；CLI 适合 Linux CI 或脚本化实验。

## 迁移注意事项

1. API key 只放 `.env.local` 或环境变量，不放 prompt、git、URL 查询参数或 jobs 日志。
2. Docker 在 Linux 下通常要求当前用户属于 `docker` 组；否则先用 `sudo docker` 验证权限，再调整 daemon 权限。
3. Windows 的目录链接可能被策略阻止，数据安装脚本会提示改用 `--mode copy`。
4. DeepSeek 对 `reasoning_effort` 的取值与部分网关不同；便携版默认使用 `low`，可通过 `OFFLINE_SKILL_RCA_REASONING_EFFORT=off` 关闭。
5. `.data/`、`tasks/`、`jobs/`、`repair-runs/` 都是运行态数据，故意不进入新仓库。
