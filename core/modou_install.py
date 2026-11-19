#!/usr/bin/env python3
"""墨斗安装器（Claude Code + Codex）。由 install.sh 调用，也可直接运行。

    python3 core/modou_install.py [--claude] [--codex] [--project DIR | --user]
                                  [--claude-executor-family opus|sonnet|haiku]
                                  [--codex-executor-family luna|terra]
                                  [--dry-run] [--check] [--uninstall]

安全约定：
- 覆盖/修改任何已有文件前先备份到 $MODOU_BACKUP_DIR
  （默认 ${XDG_STATE_HOME:-~/.local/state}/modou/backups/<时间戳>/），备份不落在项目里。
- 内容不变就不写、不备份 → 可重复运行。
- AGENTS.md 只增改 <!-- modou:begin --> … <!-- modou:end --> 区块，其余内容原样保留。
- 不碰 ~/.claude/settings.json 与 ~/.codex/config.toml（只读取主线模型用于配对检查）。
"""
import argparse
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
import modou_models as mm  # noqa: E402

CLAUDE_TEMPLATE = REPO / "agents" / "executor.md"
CODEX_TEMPLATE = REPO / "agents" / "codex" / "modou.toml"
AGENTS_SNIPPET = REPO / "integrations" / "codex" / "AGENTS.snippet.md"
BEGIN, END = "<!-- modou:begin", "<!-- modou:end -->"
MODEL_MARK = "# @modou:model"


# ---------------------------------------------------------------------------
# 文件操作：备份 + 幂等写
# ---------------------------------------------------------------------------
class Writer:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = os.environ.get("MODOU_BACKUP_DIR") or os.path.join(
            os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "modou", "backups")
        self.backup_root = Path(base) / stamp
        self.log = []

    def backup(self, path):
        path = Path(path)
        if not path.exists():
            return None
        rel = str(path.resolve()).lstrip("/")
        dest = self.backup_root / rel
        n = 1
        while dest.exists():  # 同一秒内多次写同一文件：绝不覆盖已有备份
            dest = self.backup_root / ("%s.%d" % (rel, n))
            n += 1
        if not self.dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        return dest

    def write(self, path, content):
        """返回 'created' / 'updated' / 'unchanged'。"""
        path = Path(path)
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if old == content:
            self.log.append(("unchanged", str(path), None))
            return "unchanged"
        bak = self.backup(path) if old is not None else None
        if not self.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".modou-tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
        status = "updated" if old is not None else "created"
        self.log.append((status, str(path), str(bak) if bak else None))
        return status

    def remove(self, path):
        path = Path(path)
        if not path.exists():
            return "absent"
        bak = self.backup(path)
        if not self.dry_run:
            path.unlink()
        self.log.append(("removed", str(path), str(bak)))
        return "removed"


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def render_claude(family, effort):
    text = CLAUDE_TEMPLATE.read_text(encoding="utf-8")
    text = re.sub(r"^model:\s*\S+\s*$", "model: %s" % family, text, count=1, flags=re.M)
    if effort:
        text = re.sub(r"^effort:\s*\S+\s*$", "effort: %s" % effort, text, count=1, flags=re.M)
    else:
        text = re.sub(r"^effort:\s*\S+\s*\n", "", text, count=1, flags=re.M)
    text = re.sub(r"（执行官系列：[a-z]+）", "（执行官系列：%s）" % family, text, count=1)
    return text


def render_codex(model, effort, family):
    text = CODEX_TEMPLATE.read_text(encoding="utf-8")
    text = re.sub(r'^# modou-executor-family = "[a-z]+"$', '# modou-executor-family = "%s"' % family,
                  text, count=1, flags=re.M)
    if model:
        lines = ['model = "%s"' % model]
        if effort:
            lines.append('model_reasoning_effort = "%s"' % effort)
        repl = MODEL_MARK + "（系列 %s → 本机最新，解析于安装时）\n%s" % (family, "\n".join(lines))
    else:
        repl = MODEL_MARK + "（本机未解析到 %s 系列，未写 model → 继承主线模型）" % family
    text = text.replace(MODEL_MARK, repl, 1)
    try:
        import tomllib
        tomllib.loads(text)
    except ImportError:
        pass
    return text


def snippet():
    return AGENTS_SNIPPET.read_text(encoding="utf-8").rstrip("\n") + "\n"


def upsert_block(existing, block):
    """把 block 写入 existing（AGENTS.md 文本）；已有区块就替换，否则追加。"""
    if existing is None or not existing.strip():
        return block
    s, e = existing.find(BEGIN), existing.find(END)
    if s != -1 and e != -1 and e > s:
        return existing[:s] + block.rstrip("\n") + existing[e + len(END):]
    sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return existing + sep + block


def strip_block(existing):
    s, e = existing.find(BEGIN), existing.find(END)
    if s == -1 or e == -1:
        return existing
    out = existing[:s].rstrip("\n") + existing[e + len(END):].lstrip("\n")
    return (out.rstrip("\n") + "\n") if out.strip() else ""


# ---------------------------------------------------------------------------
# 目标位置
# ---------------------------------------------------------------------------
def targets(platform, scope, project):
    if platform == "claude":
        root = (Path(project) / ".claude") if scope == "project" else mm.claude_home()
        return {"agent": root / "agents" / "executor.md", "settings": root / "settings.json"}
    root = (Path(project) / ".codex") if scope == "project" else mm.codex_home()
    agents_md = (Path(project) / "AGENTS.md") if scope == "project" else (mm.codex_home() / "AGENTS.md")
    return {"agent": root / "agents" / "modou.toml", "agents_md": agents_md, "config": root / "config.toml"}


def detect():
    return {
        "claude": bool(shutil.which("claude")) or mm.claude_home().is_dir(),
        "codex": bool(shutil.which("codex")) or mm.codex_home().is_dir(),
    }


def main_model_for(platform, scope, project):
    """主线模型：项目级配置优先，其次用户级。只读。"""
    if platform == "claude":
        if scope == "project":
            for name in ("settings.local.json", "settings.json"):
                m = mm.claude_settings_model(Path(project) / ".claude" / name)
                if m:
                    return m
        return mm.claude_settings_model()
    if scope == "project":
        p = Path(project) / ".codex" / "config.toml"
        if p.is_file():
            m = re.search(r'^model\s*=\s*"([^"]+)"', p.read_text(encoding="utf-8").split("\n[")[0], re.M)
            if m:
                return m.group(1)
    return mm.codex_config_model()


# ---------------------------------------------------------------------------
# 安装 / 检查 / 卸载
# ---------------------------------------------------------------------------
def say(msg=""):
    print(msg, flush=True)


def report_rec(rec):
    for n in rec["notes"]:
        say("  · " + n)
    for w in rec["warnings"]:
        say("  ⚠ " + w)


def install_claude(w, scope, project, family):
    t = targets("claude", scope, project)
    main = main_model_for("claude", scope, project)
    rec = mm.recommend("claude", main, family)
    say("[Claude Code] 主线模型：%s（系列 %s）→ 执行官系列：%s，effort %s"
        % (main or "未设置（Claude Code 默认）", rec["main_family"], rec["executor_family"],
           rec["executor_effort"]))
    report_rec(rec)
    st = w.write(t["agent"], render_claude(rec["executor_family"], rec["executor_effort"]))
    say("  %s %s" % (st, t["agent"]))
    return rec


def install_codex(w, scope, project, family):
    t = targets("codex", scope, project)
    main = main_model_for("codex", scope, project)
    available = mm.codex_available_models()
    if not available:
        say("[Codex] ⚠ 读不到本机 Codex 模型目录（codex debug models / models_cache.json），无法解析系列。")
    rec = mm.recommend("codex", main, family, available=available)
    say("[Codex] 主线模型：%s（系列 %s）→ 执行官系列：%s → 本机最新：%s，effort %s"
        % (main or "未设置", rec["main_family"], rec["executor_family"],
           rec["executor_model"] or "（未解析）", rec["executor_effort"]))
    report_rec(rec)
    st = w.write(t["agent"], render_codex(rec["executor_model"], rec["executor_effort"],
                                          rec["executor_family"]))
    say("  %s %s" % (st, t["agent"]))
    md = t["agents_md"]
    old = md.read_text(encoding="utf-8") if md.exists() else None
    st = w.write(md, upsert_block(old, snippet()))
    say("  %s %s（墨斗分流规则区块）" % (st, md))
    return rec


def check(scope, project, platforms):
    ok = True
    for plat in platforms:
        t = targets(plat, scope, project)
        a = t["agent"]
        if not a.is_file():
            say("✗ [%s] 未安装：%s" % (plat, a))
            ok = False
            continue
        text = a.read_text(encoding="utf-8")
        if plat == "claude":
            m = re.search(r"^model:\s*(\S+)", text, re.M)
            model = m.group(1) if m else None
            name = re.search(r"^name:\s*(\S+)", text, re.M)
            main = main_model_for("claude", scope, project)
            say("✓ [claude] %s  name=%s model=%s" % (a, name.group(1) if name else "?", model))
            fam = mm.family_of(model) if model else None
            if fam in mm.EXCLUDED_FAMILIES:
                say("  ✗ 执行官配置了 Mythos，墨斗不支持")
                ok = False
            mf = mm.family_of(main) if main else None
            if fam and mf and mf in mm.FAMILIES["claude"] and not mm.cheaper("claude", fam, mf):
                say("  ⚠ " + mm.PAIR_WARNING.format(e=fam, m=mf))
            body = text.split("---", 2)[-1].lstrip("\n")
            if body != (REPO / "core" / "protocol.md").read_text(encoding="utf-8"):
                say("  ⚠ 已安装的协议正文与仓库 core/protocol.md 不一致（旧版本？重跑 install.sh 更新）")
        else:
            try:
                import tomllib
                d = tomllib.loads(text)
            except ImportError:  # Python < 3.11：只做够用的字段提取
                d = {}
                for k in ("name", "description", "model", "model_reasoning_effort"):
                    m = re.search(r'^%s\s*=\s*"((?:[^"\\]|\\.)*)"' % k, text, re.M)
                    if m:
                        d[k] = m.group(1)
                if re.search(r"^developer_instructions\s*=\s*('''|\"\"\")\n?\S", text, re.M):
                    d["developer_instructions"] = True
            except Exception as e:  # tomllib.TOMLDecodeError
                say("✗ [codex] %s TOML 解析失败：%s" % (a, e))
                ok = False
                continue
            missing = [k for k in ("name", "description", "developer_instructions") if not d.get(k)]
            if missing:
                say("✗ [codex] %s 缺少必需字段：%s" % (a, ", ".join(missing)))
                ok = False
            if d.get("name") in ("default", "worker", "explorer"):
                say("✗ [codex] agent 名与 Codex 内置名冲突：%s" % d.get("name"))
                ok = False
            model = d.get("model")
            say("✓ [codex] %s  name=%s model=%s effort=%s"
                % (a, d.get("name"), model or "（继承主线）", d.get("model_reasoning_effort") or "-"))
            fam = mm.family_of(model) if model else None
            avail = mm.codex_available_models()
            if fam and avail:
                latest = mm.resolve_codex(fam, avail)
                if latest and latest["slug"] != model:
                    say("  ⚠ 本机 %s 系列已有更新版本 %s（当前 %s）→ 重跑 install.sh --codex 即可更新"
                        % (fam, latest["slug"], model))
                elif not any(m["slug"] == model for m in avail):
                    say("  ⚠ %s 不在本机 Codex 可选模型里" % model)
            main = main_model_for("codex", scope, project)
            mf = mm.family_of(main) if main else None
            if fam and mf and mf in mm.FAMILIES["codex"] and not mm.cheaper("codex", fam, mf):
                say("  ⚠ " + mm.PAIR_WARNING.format(e=fam, m=mf))
            md = t["agents_md"]
            mdt = md.read_text(encoding="utf-8") if md.is_file() else ""
            if BEGIN not in mdt:
                say("✗ [codex] %s 缺少墨斗分流规则区块（Codex 不会自动委派）" % md)
                ok = False
            elif snippet().rstrip("\n") not in mdt:
                say("⚠ [codex] %s 的墨斗区块不是最新（重跑 install.sh --codex 更新）" % md)
            else:
                say("✓ [codex] %s 含墨斗分流规则区块" % md)
    return ok


def uninstall(w, scope, project, platforms):
    for plat in platforms:
        t = targets(plat, scope, project)
        say("[%s] %s %s" % (plat, w.remove(t["agent"]), t["agent"]))
        if plat == "codex":
            md = t["agents_md"]
            if md.is_file():
                old = md.read_text(encoding="utf-8")
                new = strip_block(old)
                if new != old:
                    if new:
                        w.write(md, new)
                        say("  移除墨斗区块：%s" % md)
                    else:
                        w.remove(md)
                        say("  %s 只含墨斗区块，已删除" % md)


def main(argv=None):
    ap = argparse.ArgumentParser(description="墨斗安装器（Claude Code + Codex）")
    ap.add_argument("--claude", action="store_true", help="安装到 Claude Code")
    ap.add_argument("--codex", action="store_true", help="安装到 Codex")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--project", metavar="DIR", help="项目级安装（<DIR>/.claude、<DIR>/.codex、<DIR>/AGENTS.md）")
    g.add_argument("--user", action="store_true", help="用户级安装（默认）")
    ap.add_argument("--claude-executor-family", choices=sorted(mm.FAMILIES["claude"]))
    ap.add_argument("--codex-executor-family", choices=sorted(mm.FAMILIES["codex"]))
    ap.add_argument("--dry-run", action="store_true", help="只显示将做什么，不写文件")
    ap.add_argument("--check", action="store_true", help="只做健康检查")
    ap.add_argument("--uninstall", action="store_true", help="卸载（同样先备份）")
    a = ap.parse_args(argv)

    scope = "project" if a.project else "user"
    project = str(Path(a.project).expanduser().resolve()) if a.project else None
    if project and not Path(project).is_dir():
        say("项目目录不存在：%s" % project)
        return 2
    det = detect()
    platforms = [p for p in ("claude", "codex") if getattr(a, p)]
    if not platforms:
        platforms = [p for p in ("claude", "codex") if det[p]]
        if not platforms:
            say("没检测到 Claude Code 或 Codex（PATH 里没有 claude/codex，也没有配置目录）。"
                "可用 --claude / --codex 强制指定。")
            return 1
        say("检测到：%s" % ", ".join(platforms))
    say("安装范围：%s" % ("项目 " + project if project else "用户级"))

    if a.check:
        return 0 if check(scope, project, platforms) else 1

    w = Writer(dry_run=a.dry_run)
    if a.uninstall:
        uninstall(w, scope, project, platforms)
    else:
        for plat in platforms:
            try:
                if plat == "claude":
                    install_claude(w, scope, project, a.claude_executor_family)
                else:
                    install_codex(w, scope, project, a.codex_executor_family)
            except ValueError as e:
                say("✗ %s" % e)
                return 2
            except PermissionError as e:
                say("✗ 没有写权限：%s" % (e.filename or e))
                if os.environ.get("CODEX_SANDBOX") or ".codex" in str(e.filename or ""):
                    say("  Codex 的 workspace-write 沙箱禁止写项目里的 .codex/ 目录。请批准该命令以提权运行，"
                        "或在普通终端里直接执行：bash %s/install.sh %s"
                        % (REPO, " ".join(sys.argv[1:]) if sys.argv[1:] else ""))
                return 3
    baks = [b for _, _, b in w.log if b]
    if baks:
        say("已备份 %d 个原有文件到：%s" % (len(baks), w.backup_root))
    if a.dry_run:
        say("（dry-run：未写入任何文件）")
    elif not a.uninstall:
        say("完成。agent 列表在会话启动时加载：新开 Claude Code / Codex 会话后生效。")
        say("自检：bash install.sh --check %s" % ("--project " + project if project else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
