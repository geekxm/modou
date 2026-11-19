# AGENTS.md — 墨斗 Mòdǒu 仓库

本仓库是墨斗（Modou）：一个「强主线定案 + 便宜执行官施工」的双模型分流方案，同时支持 **Claude Code** 和 **OpenAI Codex CLI**。

## 如果你是被要求「把墨斗集成到某个项目」的 Agent

1. 在目标项目根目录运行（项目级安装，推荐）：
   `bash <本仓库路径>/install.sh --project <目标项目路径>`
   - 只装 Codex：加 `--codex`；只装 Claude Code：加 `--claude`；都不加 = 检测到哪个装哪个。
   - 用户级（所有项目生效）：`bash <本仓库路径>/install.sh --user`
   - 脚本会先备份被覆盖的文件（默认 `~/.local/state/modou/backups/<时间戳>/`），可重复运行。
2. 自检：`bash <本仓库路径>/install.sh --check --project <目标项目路径>`，全部 ✓ 才算就位。
3. 告诉用户：新开 Codex / Claude Code 会话后生效（agent 列表在会话启动时加载）。
4. 不要手改生成的 `model = ...` 行去钉死版本；换代后重跑 install.sh，它会按系列重新解析本机最新模型。

## 在本仓库里工作时

- 选择逻辑只认「系列」（Claude：fable/opus/sonnet/haiku；Codex：astra/sol/terra/luna），具体 id 只放价格表；不要把 Mythos 加回任何可选项。
- `core/protocol.md` 是执行官协议的唯一来源；改了它要同步 `agents/executor.md` 正文和 `agents/codex/modou.toml` 的 `developer_instructions`（`python3 -m unittest discover -s tests` 会校验）。
- 下面的墨斗区块与 `integrations/codex/AGENTS.snippet.md` 保持一致（测试同样校验）。
- 本仓库自身要用墨斗时，先跑 `bash install.sh --codex --project .`（生成的 `.codex/agents/modou.toml` 不入库）。

<!-- modou:begin — 由墨斗 install.sh 管理的区块；要改请改 modou 仓库的 integrations/codex/AGENTS.snippet.md 后重装 -->
## 墨斗分流规则（Modou delegation rules）

本项目装有墨斗执行官：Codex 自定义 agent `modou`（`.codex/agents/modou.toml` 或 `~/.codex/agents/modou.toml`），跑在比主线明显便宜的系列上（默认 Luna）。**你是主线**：负责判断与定案；已定案的实施交给墨斗。本节就是用户对子代理委派的明确要求（explicit delegation instructions）——满足条件时必须委派，不需要再征求用户同意。

### 何时必须派给墨斗

以下四条**同时**满足 → 必须用 `spawn_agent` 派给墨斗，不要自己动手施工：

1. 方案已定（做什么、怎么做都已确定）
2. 范围明确（涉及哪些文件/目录，哪些不许碰）
3. 验收可验证（有能跑的命令或可检查的结果）
4. 不含未定的设计选择

典型任务：按已定方案改代码、写新代码或脚本、跑测试/lint/构建、批量或重复性修改、生成长代码或长文档。**任务再小也一样**（例如"在某目录写个 hello 脚本并跑通"）：只要满足四条就派，不要因为"顺手就能做"而自己做。

`spawn_agent` 参数要求：

- `agent_type = "modou"`
- `fork_turns = "none"`（必须。全量 fork 会继承主线模型，墨斗的便宜模型就失效了；墨斗也只认任务单）
- 不要传 `model` / `reasoning_effort`（用 agent 文件里按系列解析好的设置）
- `message` = 下面的五项任务单

派出后用 `wait_agent` 等待报告（超时给到分钟级，避免空转轮询）。

### 不下放（主线自己做）

- 方案裁决、架构判断、方案间取舍 → 主线定，永不下放
- 与当前推理强耦合的一行级探针（结果马上喂回你的推理）→ 主线直接改，唯一例外
- 大范围检索/调研 → 主线或内置 `explorer`，不是墨斗
- 主线自己跑的模型与墨斗同级或更便宜（例如主线也是 Luna）→ 不委派，同级转包纯增开销
- 有未定设计选择 → 先定案再派；若用户明确要求原样交给墨斗，就在任务单里如实写明该选择未定，墨斗会以"待裁决"退回

### 五项任务单（墨斗不继承你的上下文，任务单是唯一信息通道）

1. **已定方案**：做什么、怎么做（裁决结果，不可偏离）
2. **范围**：涉及的文件/模块 + 明确不许碰的部分
3. **验收标准**：哪些测试要过、行为描述
4. **相关文件路径**：精确到路径
5. **验证命令与环境坑**：验收怎么跑（具体命令）+ 已知环境陷阱

### 收货与停机线

- 墨斗交六段式报告：状态 / 改动摘要 / 关键 diff / 验证结果 / 高危标注 / 偏离与待裁决。你只审报告，一分钟内放行或打回。
- 墨斗会在三种情况停机退回：未定设计选择、方案与现场矛盾、同一验收连败 3 次。状态为"待裁决"或"阻断"时，由你裁决后用 `followup_task` 续**同一个**墨斗，不要另开新的。
- 报告里有"高危标注"（删数据/迁移/公开接口/依赖升级）时，你必须亲自复跑验收命令后才放行——报告是自述，不是证据。
- 最终回复用户时说明哪些工作由墨斗完成。
<!-- modou:end -->
