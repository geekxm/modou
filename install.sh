#!/usr/bin/env bash
# 墨斗安装脚本：检测 Claude Code / Codex，分别或同时安装。
#
#   bash install.sh                         # 检测到哪个装哪个（用户级）
#   bash install.sh --project /path/to/proj # 项目级（推荐：随项目走，交给 Agent 即自动集成）
#   bash install.sh --codex --project .     # 只装 Codex，项目级
#   bash install.sh --check [--project DIR] # 健康检查
#   bash install.sh --dry-run ...           # 只看不写
#   bash install.sh --uninstall ...         # 卸载（先备份）
#
# 覆盖任何已有文件前都会备份（默认 ~/.local/state/modou/backups/<时间戳>/）。
# 不修改 ~/.claude/settings.json 与 ~/.codex/config.toml。
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  done
fi
if [ -z "$PY" ]; then
  echo "需要 Python 3.8+（只用标准库）。" >&2
  exit 1
fi

exec "$PY" -I "$SRC_DIR/core/modou_install.py" "$@"
