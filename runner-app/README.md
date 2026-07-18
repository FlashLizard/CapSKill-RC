# SkillsBench Runner App

本地 Web 控制台，用于选择 SkillsBench 任务、切换 Claude Code/OpenHands 等 harness、并行重复运行、实时查看日志，从 `jobs/` 扫描已完成结果，并运行 skills 库自动修复流水线。

启动：

```powershell
cd <cloned-repository>
$env:DEEPSEEK_API_KEY = '<your-deepseek-api-key>'
node runner-app/server.mjs
```

Repair 页面保存的预设会加密保存 API Key。Windows 默认使用当前用户 DPAPI；Linux 和 macOS 默认使用 Node.js AES-256-GCM，不依赖 `powershell.exe`。

便携预设加密会在 `.runner-config/repair-stage-presets.key` 自动生成本机密钥，该目录已被 `.gitignore` 排除。若需要在另一台机器迁移预设，请通过环境变量设置相同的 `SKILLSBENCH_PRESET_SECRET`，或者安全地迁移这个密钥文件；不要把密钥提交到 Git。也可以设置 `SKILLSBENCH_REPAIR_PRESET_PATH` 和 `SKILLSBENCH_REPAIR_PRESET_KEY_PATH` 指定预设及密钥文件位置。

浏览器打开：

```text
http://localhost:5198
```

功能：

- 选择 `claude-agent-acp`、`openhands`、`opencode`、`codex-acp` 等 harness。
- 设置模型、供应商协议、Base URL、API key、重复次数、并行度、`no-skill` / `with-skill` / `force-skill` 和 skills 库。
- Claude Code 的自定义供应商需要提供 Anthropic Messages 兼容入口；页面还支持 reasoning effort 设置。
- 对 `bike-rebalance`，可在 `initial`、`s0-repaired`、`mip` 三套 skills 库之间切换；默认使用 `initial`。
- 实时查看每个 run 的 stdout/stderr 日志。
- 扫描 `jobs/**/summary.json`，点选历史结果查看 score、token、耗时、skills 库、artifact 路径和完整 summary。
- 打开 `/repair.html`，选择 task、源 skills 库和 jobs 轨迹证据，调用强模型生成新的 repaired skills variant。
- repair 输出写入 `skill-libraries/<task>/<variant>`，报告写入 `repair-runs/<task>/<variant>`，不会修改 `tasks/` 下的任务文件。

Skills 修复页：

```text
http://localhost:5198/repair.html
```

推荐强模型配置：

```text
Provider: anthropic
Base URL: https://api.camel-hub.com
Model: gpt-5.5
Weak model: deepseek-v4-flash
```

更多 CLI 和验证说明见根目录 `README.md` 与 `offline_skill_rca/README.md`。

可选端口：

```powershell
$env:SKILLSBENCH_RUNNER_PORT = '5200'
node runner-app/server.mjs
```
