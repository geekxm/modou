# Codex 验收记录（2026-11-20）

环境：macOS，codex-cli 0.159.3（`/opt/homebrew/bin/codex`），主线 `gpt-6.1-sol`（effort low，用户原配置），墨斗由安装器按系列解析为 `gpt-6-luna`（effort high）。Claude Code 2.1.325。所有测试都在 `/tmp/modou-test-*` 临时项目里进行。完整日志在维护者本机的 overnight 目录，不入库。

| # | 考点 | 结果 | 关键证据 |
|---|---|---|---|
| T1 | 把仓库交给 Codex：只说"读 README，把墨斗以项目级安装集成到当前项目并自检" | ✅（需提权） | 默认 `workspace-write` 沙箱禁止写项目内 `.codex/`，Codex 如实报告失败、项目无改动；安装器随后加了明确提示（退出码 3）。在提权（`-s danger-full-access`，相当于交互模式下用户批准）后 Codex 按 README 执行 `install.sh --project . --codex` 与 `--check`，全部 ✓；原有 AGENTS.md 内容保留，原文件备份在项目外 |
| T2 | 已定案小任务（写 hello.py 并跑通），提示词里不提墨斗 | ✅ 分流 | 主线只调用 `spawn_agent{agent_type:"modou", fork_turns:"none"}` + `wait_agent`；子线程 `agent_role=modou`、`model=gpt-6-luna`、`effort=high` 完成全部施工，交六段式报告。成本：墨斗约 $0.003，主线调度约 $0.057 |
| T2b | 第二个样本（calc.py + 3 个 unittest） | ✅ 分流 | 同上；墨斗约 $0.004，主线约 $0.014（缓存命中），测试全过 |
| T3 | 任务单里留未定设计选择（导出格式 JSON/CSV、退款是否计入） | ✅ 停机 | 墨斗状态"待裁决"，列出两项选择与事实，未创建任何文件；主线原样转述，也未代为拍板 |
| T4 | 纯方案裁决（argparse 还是 click） | ✅ 不下放 | 无 spawn，主线自行裁决并注明"属于方案裁决，由主线完成" |
| T5 | 安装脚本可重复、不无备份覆盖 | ✅ | 首次运行把 3 个已有用户文件备份到项目外（哈希一致）；第二次全部 `unchanged`、零新备份；用户级安装（临时 `CODEX_HOME`/`CLAUDE_CONFIG_DIR`）不改 `config.toml`/`settings.json`；同级配对报警；卸载只删墨斗区块 |
| T6 | 管理后台 | ✅ | `/api/health` 正常，仅监听 127.0.0.1；今日 Codex 用量里墨斗 3 条请求全部按 `agent_role` 精确识别（0 条推断），探针 agent 另列为"其他子代理"；跨站 POST 返回 403；写入前备份（修复了同一秒内两次写入会覆盖备份的问题） |
| C1 | Claude Code 回归 | ✅ | 项目级安装与 `--check` 正常；`claude -p --model fable` 实跑：Fable 5.2 主线调用 `Agent{subagent_type:"executor"}`，子代理跑在 `claude-opus-5-5`（由 `model: opus` 别名解析） |

结论：Codex 侧"项目级安装 → AGENTS.md 驱动委派 → Luna 执行官施工 → 停机线生效"全链路可用。

已知限制：

- 让 Codex 自己做项目级安装需要提权：`.codex/` 受沙箱保护。
- 委派依赖主线遵守 AGENTS.md，属于模型行为。今晚 Sol 4/4 正确，但这不是硬保证。
- Claude 侧第一次实跑（description 旧措辞）时 Fable 自己动手做了 hello 任务；把 description 改成"任务再小也一样"之后委派成功。两种措辞各只跑了 1 次，样本很小。
