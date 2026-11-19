#!/usr/bin/env python3
"""墨斗管理后台 (Modou Admin Console) —— Claude Code + Codex

零依赖的本地 Web 后台（Python 3 标准库，单进程）：
  1. 消耗检查 —— 解析本机用量记录，按 昨天 / 本周 / 本月 统计 token、费用与调配（委派）：
       · Claude Code：$CLAUDE_CONFIG_DIR（默认 ~/.claude）/projects/**/*.jsonl
       · Codex：$CODEX_HOME（默认 ~/.codex）/sessions/**/rollout-*.jsonl[.zst]
     墨斗按「子代理身份」精确识别（Claude：subagent_type；Codex：agent_role），
     不靠模型名猜。
  2. 模型设置 —— 只按「系列」设置，不钉死版本：
       · Claude 主线：settings.json 的 model 写系列别名（fable/opus/sonnet）
       · Claude 执行官：executor.md 的 model 写系列别名（opus/sonnet/haiku）
       · Codex 执行官：modou.toml 按系列解析为本机最新 id 后写入
       · Codex 主线：只读展示 config.toml 的 model（不改用户 Codex 配置）
     任何写入前先备份到 ~/.local/state/modou/backups/<时间戳>/。

用法:  python3 admin/modou_admin.py [--port 8848] [--project DIR]
仅监听 127.0.0.1，不向外发送任何数据，不读写任何密钥（不碰 auth.json）。
"""

import argparse
import json
import os
import re
import shutil
import sqlite3  # noqa: F401  (保留：将来可读 Codex state 库做交叉校验)
import subprocess
import sys
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR / "core"))
import modou_models as mm  # noqa: E402
import modou_install as mi  # noqa: E402

CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0
CACHE_READ = 0.1

# 执行官速度档位。Claude：frontmatter 的 effort；Codex：model_reasoning_effort。
EFFORT_CHOICES = ["low", "medium", "high", "xhigh", "max"]


def model_name(model_id):
    p = mm.price_of(model_id)
    return p["name"] if p else (model_id or "?")


def cost_usd(model_id, usage):
    """按单条（已归一的）usage 计费；未知模型 / Mythos 返回 None。

    归一后的 usage 键：input_tokens（未命中缓存的输入）、output_tokens、
    cache_creation_input_tokens、cache_read_input_tokens、cache_creation（Claude 明细，可选）。"""
    p = mm.price_of(model_id)
    if not p:
        return None
    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    cw = usage.get("cache_creation_input_tokens", 0) or 0
    cr = usage.get("cache_read_input_tokens", 0) or 0
    if mm.platform_of(model_id) == "codex":
        return (inp * p["in"] + out * p["out"] + cw * p.get("cw", p["in"])
                + cr * p.get("cr", CACHE_READ * p["in"])) / 1e6
    # Claude：有 5m/1h 明细时按明细计缓存写费用
    detail = usage.get("cache_creation") or {}
    w5 = detail.get("ephemeral_5m_input_tokens")
    w1 = detail.get("ephemeral_1h_input_tokens")
    if w5 is not None or w1 is not None:
        write_cost = ((w5 or 0) * CACHE_WRITE_5M + (w1 or 0) * CACHE_WRITE_1H) * p["in"]
    else:
        write_cost = cw * CACHE_WRITE_5M * p["in"]
    return (inp * p["in"] + out * p["out"] + write_cost + cr * p.get("cr", CACHE_READ * p["in"])) / 1e6


# ---------------------------------------------------------------------------
# 用量扫描：Claude Code 把每条 assistant 消息（含 usage）写进
# ~/.claude/projects/<project>/<session>.jsonl；子代理的记录（isSidechain=true，
# 带 agentId）写在 <session>/subagents/agent-<id>.jsonl（旧版在项目目录下）。
# 同一条消息可能因流式写入出现多行，用 (message.id, requestId) 去重。
#
# 墨斗识别：不再只看"模型是不是执行官模型"，而是按身份：
#   主线会话里 Task/Agent 工具调用带 subagent_type，其 tool_result 的
#   toolUseResult.agentId 指向子代理 → 得到 agentId -> 子代理类型 的映射；
#   另外兼容 agent-<id>.meta.json 里的 agentType。
# 查不到类型的子代理记录才退回按模型推断。
# ---------------------------------------------------------------------------
AGENT_TOOLS = ("Task", "Agent")
AGENT_FILE_RE = re.compile(r"^agent-([^.]+)$")


class ClaudeScanner:
    platform = "claude"

    def __init__(self, claude_dir):
        self.projects_dir = Path(claude_dir) / "projects"
        self._file_cache = {}  # path -> (mtime, size, parsed)

    def records(self):
        out, uses, results, meta_types = [], {}, {}, {}
        if not self.projects_dir.is_dir():
            return out
        for path in self.projects_dir.rglob("*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            cached = self._file_cache.get(str(path))
            if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                parsed = cached[2]
            else:
                parsed = self._parse_file(path)
                self._file_cache[str(path)] = (st.st_mtime, st.st_size, parsed)
            out.extend(parsed["recs"])
            uses.update(parsed["uses"])
            results.update(parsed["results"])
        for path in self.projects_dir.rglob("agent-*.meta.json"):
            t = self._read_meta_type(path)
            if t:
                meta_types[path.name[len("agent-"):-len(".meta.json")]] = t

        # agentId -> 子代理类型
        agent_types = dict(meta_types)
        for tool_use_id, agent_id in results.items():
            if tool_use_id in uses:
                agent_types.setdefault(agent_id, uses[tool_use_id])

        # 跨文件也去重一次（同一请求可能被多个视图文件记录）
        seen, dedup = set(), []
        for r in out:
            key = r["key"]
            if key in seen:
                continue
            seen.add(key)
            r["agent_type"] = agent_types.get(r["agent_id"]) if r["agent_id"] else None
            dedup.append(r)
        return dedup

    @staticmethod
    def _read_meta_type(path):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(obj, dict):
            return obj.get("agentType") or obj.get("subagent_type") or obj.get("agent_type")
        return None

    def _parse_file(self, path):
        recs, seen = [], set()
        uses, results = {}, {}  # tool_use_id -> subagent_type / agentId
        m = AGENT_FILE_RE.match(path.stem)
        file_agent_id = m.group(1) if m else None
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if ('"usage"' not in line and '"subagent_type"' not in line
                            and '"agentId"' not in line):
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    msg = obj.get("message") or {}
                    self._collect_links(obj, msg, uses, results)
                    usage = msg.get("usage")
                    model = msg.get("model")
                    ts = obj.get("timestamp")
                    if not usage or not model or not ts or model == "<synthetic>":
                        continue
                    key = (msg.get("id") or "", obj.get("requestId") or "", ts)
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        # 时间戳是 UTC ISO，转本地时区
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                    except ValueError:
                        continue
                    sidechain = bool(obj.get("isSidechain"))
                    recs.append({
                        "key": key,
                        "dt": dt,
                        "platform": "claude",
                        "model": mm.norm_model(model),
                        "raw_model": model,
                        "sidechain": sidechain,
                        "agent_id": (obj.get("agentId") or file_agent_id) if sidechain else None,
                        "usage": {
                            "input_tokens": usage.get("input_tokens", 0) or 0,
                            "output_tokens": usage.get("output_tokens", 0) or 0,
                            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
                            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0) or 0,
                            "cache_creation": usage.get("cache_creation"),
                        },
                    })
        except OSError:
            pass
        return {"recs": recs, "uses": uses, "results": results}

    @staticmethod
    def _collect_links(obj, msg, uses, results):
        """记录 Task/Agent 调用的 subagent_type，以及其结果里回报的 agentId。"""
        content = msg.get("content")
        if not isinstance(content, list):
            return
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use" and item.get("name") in AGENT_TOOLS:
                inp = item.get("input") or {}
                uses[item.get("id")] = inp.get("subagent_type") or "general-purpose"
            elif item.get("type") == "tool_result":
                tur = obj.get("toolUseResult")
                if isinstance(tur, dict) and tur.get("agentId"):
                    results[item.get("tool_use_id")] = tur["agentId"]


def range_bounds(name):
    now = datetime.now().astimezone()
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if name == "today":
        return today0, now
    if name == "yesterday":
        return today0 - timedelta(days=1), today0
    if name == "week":  # 本周（周一起）
        return today0 - timedelta(days=today0.weekday()), now
    if name == "month":
        return today0.replace(day=1), now
    return None, now  # all


def classify(r, executor_model, executor_name):
    """返回 (角色, 识别方式)。角色: main / modou / other；方式: exact / inferred。"""
    if not r["sidechain"]:
        return "main", "exact"
    t = r.get("agent_type")
    if t:
        return ("modou" if t == executor_name else "other"), "exact"
    # 子代理类型查不到（会话还在跑、或旧版日志）→ 按模型推断
    if executor_model and r["model"] == executor_model:
        return "modou", "inferred"
    return "other", "inferred"


def aggregate(records, start, end, main_model, executor_model, executor_name="executor"):
    by_model, daily = {}, {}
    totals = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
              "cost": 0.0, "requests": 0}
    exec_stat = {"cost": 0.0, "output": 0, "requests": 0, "cost_if_main": 0.0,
                 "exact": 0, "inferred": 0}
    main_stat = {"cost": 0.0, "output": 0, "requests": 0}
    other_stat = {"cost": 0.0, "output": 0, "requests": 0}
    other_agents = {}
    main_out_by_model = {}
    modou_recs = []

    for r in records:
        if start and r["dt"] < start:
            continue
        if r["dt"] >= end:
            continue
        u, m = r["usage"], r["model"]
        c = cost_usd(m, u) or 0.0
        slot = by_model.setdefault(m, {"model": m, "name": model_name(m),
                                       "input": 0, "output": 0, "cache_write": 0,
                                       "cache_read": 0, "cost": 0.0, "requests": 0,
                                       "cost_main": 0.0, "cost_modou": 0.0, "cost_other": 0.0})
        slot["input"] += u["input_tokens"]
        slot["output"] += u["output_tokens"]
        slot["cache_write"] += u["cache_creation_input_tokens"]
        slot["cache_read"] += u["cache_read_input_tokens"]
        slot["cost"] += c
        slot["requests"] += 1
        totals["input"] += u["input_tokens"]
        totals["output"] += u["output_tokens"]
        totals["cache_write"] += u["cache_creation_input_tokens"]
        totals["cache_read"] += u["cache_read_input_tokens"]
        totals["cost"] += c
        totals["requests"] += 1
        day = r["dt"].strftime("%m-%d")
        d = daily.setdefault(day, {"day": day, "cost": 0.0, "output": 0,
                                   "main": 0.0, "modou": 0.0, "other": 0.0})
        d["cost"] += c
        d["output"] += u["output_tokens"]

        role, how = classify(r, executor_model, executor_name)
        d[role] += c
        slot["cost_" + role] += c
        stat = {"main": main_stat, "modou": exec_stat, "other": other_stat}[role]
        stat["cost"] += c
        stat["output"] += u["output_tokens"]
        stat["requests"] += 1
        if role == "main":
            main_out_by_model[m] = main_out_by_model.get(m, 0) + u["output_tokens"]
        elif role == "modou":
            exec_stat[how] += 1
            modou_recs.append(r)
        else:
            name = r.get("agent_type") or "未识别子代理"
            a = other_agents.setdefault(name, {"type": name, "cost": 0.0, "output": 0,
                                               "requests": 0, "models": set()})
            a["cost"] += c
            a["output"] += u["output_tokens"]
            a["requests"] += 1
            a["models"].add(m)

    # 省钱基准：该时段主线实际用得最多的模型（按输出 token），没有则用设置里的主模型
    baseline = max(main_out_by_model, key=main_out_by_model.get) if main_out_by_model else main_model
    if not mm.price_of(baseline):
        baseline = main_model
    if mm.price_of(baseline):
        for r in modou_recs:
            exec_stat["cost_if_main"] += cost_usd(baseline, r["usage"]) or 0.0

    savings = max(0.0, exec_stat["cost_if_main"] - exec_stat["cost"])
    total_out = totals["output"]
    delegation_ratio = (exec_stat["output"] / total_out) if total_out else 0.0
    for a in other_agents.values():
        a["models"] = sorted(model_name(x) for x in a["models"])
    return {
        "start": start.isoformat() if start else None,
        "end": end.isoformat(),
        "totals": totals,
        "by_model": sorted(by_model.values(), key=lambda x: -x["cost"]),
        "daily": sorted(daily.values(), key=lambda x: x["day"]),
        "executor": exec_stat,
        "main": main_stat,
        "other": other_stat,
        "other_agents": sorted(other_agents.values(), key=lambda x: -x["cost"]),
        "baseline_model": baseline,
        "executor_name": executor_name,
        "savings": savings,
        "delegation_ratio": delegation_ratio,
    }


# ---------------------------------------------------------------------------
# Codex 用量扫描：$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl（旧会话会被压成
# .jsonl.zst）。每个线程（主线或子代理）一个文件：
#   session_meta.payload：id、thread_source（user/subagent）、agent_role（custom agent 名）、
#                         source.subagent.thread_spawn.parent_thread_id
#   turn_context.payload.model：本轮模型
#   token_usage_record.payload：每次响应一条，带 response_id、thread_id、usage
#     （OpenAI 口径：input_tokens 已含 cached_input_tokens）
# 老版本没有 token_usage_record 时，退回 event_msg/token_count 的 last_token_usage。
# 墨斗识别：子代理线程的 agent_role == modou.toml 的 name（默认 modou）。
# ---------------------------------------------------------------------------
def _zstd_lines(path):
    try:
        from compression import zstd  # Python 3.14+
        with zstd.open(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                yield line
        return
    except ImportError:
        pass
    exe = shutil.which("zstd")
    if not exe:
        raise OSError("no zstd decoder")
    out = subprocess.run([exe, "-dc", str(path)], capture_output=True, timeout=60)
    if out.returncode != 0:
        raise OSError("zstd failed")
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        yield line


def _norm_codex_usage(u):
    inp = u.get("input_tokens", 0) or 0
    cached = u.get("cached_input_tokens", 0) or 0
    cw = u.get("cache_write_input_tokens", 0) or 0
    return {
        "input_tokens": max(0, inp - cached - cw),
        "output_tokens": u.get("output_tokens", 0) or 0,
        "cache_creation_input_tokens": cw,
        "cache_read_input_tokens": cached,
        "cache_creation": None,
    }


class CodexScanner:
    platform = "codex"
    MARKERS = ('"session_meta"', '"turn_context"', '"token_usage_record"', '"token_count"')

    def __init__(self, codex_dir):
        self.codex_dir = Path(codex_dir)
        self.projects_dir = self.codex_dir / "sessions"
        self.dirs = [self.codex_dir / "sessions", self.codex_dir / "archived_sessions"]
        self._file_cache = {}
        self.unreadable = 0

    def files(self):
        for d in self.dirs:
            if not d.is_dir():
                continue
            for p in d.rglob("rollout-*"):
                if p.name.endswith(".jsonl") or p.name.endswith(".jsonl.zst"):
                    yield p

    def records(self):
        out, unreadable = [], 0
        for path in self.files():
            try:
                st = path.stat()
            except OSError:
                continue
            cached = self._file_cache.get(str(path))
            if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                parsed = cached[2]
            else:
                parsed = self._parse_file(path)
                self._file_cache[str(path)] = (st.st_mtime, st.st_size, parsed)
            if parsed is None:
                unreadable += 1
                continue
            out.extend(parsed)
        self.unreadable = unreadable
        seen, dedup = set(), []
        for r in out:
            if r["key"] in seen:
                continue
            seen.add(r["key"])
            dedup.append(r)
        return dedup

    def _parse_file(self, path):
        try:
            lines = _zstd_lines(path) if path.name.endswith(".zst") else \
                open(path, encoding="utf-8", errors="replace")
            recs, fallback = [], []
            meta, model, last_total = None, None, None
            for line in lines:
                if not any(m in line for m in self.MARKERS):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                t, p = obj.get("type"), obj.get("payload") or {}
                if t == "session_meta" and meta is None:
                    src = p.get("source")
                    spawn = (src.get("subagent") or {}).get("thread_spawn") or {} \
                        if isinstance(src, dict) else {}
                    sub = p.get("thread_source") == "subagent" or \
                        (isinstance(src, dict) and "subagent" in src)
                    meta = {"id": p.get("id"), "sidechain": bool(sub),
                            "role": p.get("agent_role") or spawn.get("agent_role"),
                            "parent": spawn.get("parent_thread_id")}
                elif t == "turn_context":
                    model = p.get("model") or model
                elif t == "token_usage_record":
                    if meta and p.get("thread_id") and meta["id"] and p["thread_id"] != meta["id"]:
                        continue  # fork 回放的别的线程记录
                    r = self._rec(obj, p.get("usage"), model, meta,
                                  p.get("response_id") or (obj.get("timestamp"), obj.get("ordinal")))
                    if r:
                        recs.append(r)
                elif t == "event_msg" and p.get("type") == "token_count":
                    info = p.get("info") or {}
                    tot = info.get("total_token_usage")
                    if info.get("last_token_usage") and tot != last_total:
                        r = self._rec(obj, info["last_token_usage"], model, meta,
                                      ("tc", meta and meta["id"], obj.get("timestamp")))
                        if r:
                            fallback.append(r)
                        last_total = tot
            if hasattr(lines, "close"):
                lines.close()
            return recs or fallback
        except OSError:
            return None

    @staticmethod
    def _rec(obj, usage, model, meta, key):
        ts = obj.get("timestamp")
        if not usage or not ts:
            return None
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        except ValueError:
            return None
        meta = meta or {"id": None, "sidechain": False, "role": None, "parent": None}
        return {
            "key": key,
            "dt": dt,
            "platform": "codex",
            "model": (model or "unknown").lower(),
            "raw_model": model,
            "sidechain": meta["sidechain"],
            "agent_id": meta["id"] if meta["sidechain"] else None,
            "agent_type": (meta["role"] or "default（fork）") if meta["sidechain"] else None,
            "usage": _norm_codex_usage(usage),
        }


# ---------------------------------------------------------------------------
# 模型设置（只按系列）
# ---------------------------------------------------------------------------
def _first_file(paths):
    for p in paths:
        if p and p.is_file():
            return p
    return None


class ClaudeConfig:
    platform = "claude"

    def __init__(self, claude_dir, project=None):
        self.claude_dir = Path(claude_dir)
        self.settings_path = self.claude_dir / "settings.json"
        self.executor_paths = ([Path(project) / ".claude" / "agents" / "executor.md"] if project else []) + \
            [self.claude_dir / "agents" / "executor.md"]

    def read_settings(self):
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def get_main_model(self):
        return self.read_settings().get("model")

    def set_main_family(self, family):
        if family not in mm.POLICY["claude"]["main_pref"]:
            raise ValueError("不支持的主线系列：%s" % family)
        settings = self.read_settings()
        settings["model"] = family
        w = mi.Writer()
        w.write(self.settings_path, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
        return w.log

    def executor_file(self):
        return _first_file(self.executor_paths)

    def _get(self, key):
        p = self.executor_file()
        if not p:
            return None
        m = re.search(r"^%s:\s*(\S+)\s*$" % key, p.read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else None

    def get_executor_model(self):
        p = self.executor_file()
        return self._get("model"), (str(p) if p else None)

    def get_executor_name(self):
        return self._get("name") or "executor"

    def get_executor_effort(self):
        return self._get("effort")

    def set_executor(self, family, effort):
        """family 为 opus/sonnet/haiku；effort 为档位或 ""（删除 → 跟随会话默认）。"""
        if family not in mm.POLICY["claude"]["executor_pref"]:
            raise ValueError("不支持的执行官系列：%s" % family)
        p = self.executor_file()
        if not p:
            raise FileNotFoundError("找不到已安装的 executor.md（先运行 bash install.sh --claude）")
        text = p.read_text(encoding="utf-8")
        text, n = re.subn(r"^model:\s*\S+\s*$", "model: %s" % family, text, count=1, flags=re.M)
        if n == 0:
            raise ValueError("executor.md frontmatter 里没有 model: 字段")
        text = re.sub(r"（执行官系列：[a-z]+）", "（执行官系列：%s）" % family, text, count=1)
        if effort:
            text, n = re.subn(r"^effort:\s*\S+\s*$", "effort: %s" % effort, text, count=1, flags=re.M)
            if n == 0:
                text = re.sub(r"^(model:\s*\S+\s*)$", r"\1\neffort: %s" % effort, text, count=1, flags=re.M)
        else:
            text = re.sub(r"^effort:\s*\S+\s*\n?", "", text, count=1, flags=re.M)
        w = mi.Writer()
        w.write(p, text)
        return str(p), w.log


class CodexConfig:
    platform = "codex"

    def __init__(self, codex_dir, project=None):
        self.codex_dir = Path(codex_dir)
        self.config_path = self.codex_dir / "config.toml"
        self.project = project
        self.executor_paths = ([Path(project) / ".codex" / "agents" / "modou.toml"] if project else []) + \
            [self.codex_dir / "agents" / "modou.toml"]

    def get_main_model(self):
        if self.project:
            m = mi.main_model_for("codex", "project", self.project)
            if m:
                return m
        return mm.codex_config_model()

    def executor_file(self):
        return _first_file(self.executor_paths)

    def _get(self, key):
        p = self.executor_file()
        if not p:
            return None
        m = re.search(r'^%s\s*=\s*"([^"]*)"' % key, p.read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else None

    def get_executor_model(self):
        p = self.executor_file()
        return self._get("model"), (str(p) if p else None)

    def get_executor_name(self):
        return self._get("name") or "modou"

    def get_executor_effort(self):
        return self._get("model_reasoning_effort")

    def set_executor(self, family, effort):
        if family not in mm.POLICY["codex"]["executor_pref"]:
            raise ValueError("不支持的执行官系列：%s" % family)
        p = self.executor_file()
        if not p:
            raise FileNotFoundError("找不到已安装的 modou.toml（先运行 bash install.sh --codex）")
        res = mm.resolve_codex(family)
        if not res:
            raise ValueError("本机 Codex 模型目录里没有 %s 系列" % family)
        effort = effort or mm.POLICY["codex"]["executor_effort"].get(family)
        w = mi.Writer()
        w.write(p, mi.render_codex(res["slug"], effort, family))
        return str(p), res["slug"], w.log


def family_prices(platform):
    out = {}
    for fam in mm.FAMILIES[platform]:
        p = mm.family_price(platform, fam)
        if p:
            out[fam] = {"in": p["in"], "out": p["out"], "ref": mm.latest_known(platform, fam),
                        "label": mm.FAMILIES[platform][fam]["label"]}
    return out



# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
def settings_payload(ctx):
    cc, xc = ctx["claude_cfg"], ctx["codex_cfg"]
    c_exec, c_path = cc.get_executor_model()
    c_main = cc.get_main_model()
    x_exec, x_path = xc.get_executor_model()
    x_main = xc.get_main_model()
    avail = ctx["codex_models"]()
    x_res = {f: (mm.resolve_codex(f, avail) or {}).get("slug") for f in mm.FAMILIES["codex"]}
    x_family = mm.family_of(x_exec) if x_exec else None
    x_latest = x_res.get(x_family) if x_family else None
    prices = {k: dict(v, platform=mm.platform_of(k)) for k, v in mm.PRICES.items()}
    return {
        "prices": prices,
        "effort_choices": EFFORT_CHOICES,
        "claude": {
            "main_model": c_main,
            "main_family": mm.family_of(c_main) if c_main else None,
            "main_choices": mm.POLICY["claude"]["main_pref"],
            "main_auto": mm.POLICY["claude"]["main_pref"][0],
            "executor_model": c_exec,
            "executor_family": mm.family_of(c_exec) if c_exec else None,
            "executor_path": c_path,
            "executor_effort": cc.get_executor_effort(),
            "executor_choices": mm.POLICY["claude"]["executor_pref"],
            "family_prices": family_prices("claude"),
            "settings_path": str(cc.settings_path),
            "usage_dir": str(ctx["scanners"]["claude"].projects_dir),
            "usage_found": ctx["scanners"]["claude"].projects_dir.is_dir(),
        },
        "codex": {
            "main_model": x_main,
            "main_family": mm.family_of(x_main) if x_main else None,
            "executor_model": x_exec,
            "executor_family": x_family,
            "executor_latest": x_latest,
            "executor_path": x_path,
            "executor_effort": xc.get_executor_effort(),
            "executor_choices": mm.POLICY["codex"]["executor_pref"],
            "resolved": x_res,
            "available": [m["slug"] for m in avail],
            "family_prices": family_prices("codex"),
            "config_path": str(xc.config_path),
            "usage_dir": str(ctx["scanners"]["codex"].projects_dir),
            "usage_found": ctx["scanners"]["codex"].projects_dir.is_dir(),
            "unreadable_files": ctx["scanners"]["codex"].unreadable,
        },
    }


def usage_payload(ctx, platform, rng):
    start, end = range_bounds(rng)
    cfg = ctx["claude_cfg"] if platform == "claude" else ctx["codex_cfg"]
    exec_model, _ = cfg.get_executor_model()
    main = cfg.get_main_model()
    main_family = mm.family_of(main) if main else mm.POLICY[platform]["main_pref"][0]
    if not main or not mm.selectable(main_family):
        main_family = mm.POLICY[platform]["main_pref"][0]
    main_model = mm.norm_model(main) if main and mm.parse(main)[2] else mm.latest_known(platform, main_family)
    exec_norm = mm.norm_model(exec_model) if exec_model and mm.parse(exec_model)[2] else None
    data = aggregate(ctx["scanners"][platform].records(), start, end,
                     main_model, exec_norm, cfg.get_executor_name())
    data["range"] = rng
    data["platform"] = platform
    data["baseline_name"] = model_name(data["baseline_model"]) if data["baseline_model"] else None
    return data


def make_handler(ctx):

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 安静一点
            pass

        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/health":
                s = settings_payload(ctx)
                self._json({
                    "ok": True,
                    "bind": "127.0.0.1",
                    "claude": {"executor_installed": bool(s["claude"]["executor_path"]),
                               "executor_model": s["claude"]["executor_model"],
                               "usage_dir_found": s["claude"]["usage_found"]},
                    "codex": {"executor_installed": bool(s["codex"]["executor_path"]),
                              "executor_model": s["codex"]["executor_model"],
                              "executor_latest": s["codex"]["executor_latest"],
                              "usage_dir_found": s["codex"]["usage_found"],
                              "models_available": len(s["codex"]["available"])},
                })
            elif url.path == "/api/usage":
                plat = (q.get("platform") or ["claude"])[0]
                if plat not in ("claude", "codex"):
                    return self._json({"error": "unknown platform"}, 400)
                self._json(usage_payload(ctx, plat, (q.get("range") or ["yesterday"])[0]))
            elif url.path == "/api/settings":
                self._json(settings_payload(ctx))
            else:
                self._json({"error": "not found"}, 404)

        def _same_origin(self):
            """只接受本页发起的 JSON POST：防止别的网页跨站改本机配置。"""
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return False
            origin = self.headers.get("Origin")
            if origin is None:
                return True  # curl 等本地工具
            port = self.server.server_address[1]
            return origin in ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port)

        def do_POST(self):
            url = urlparse(self.path)
            if not self._same_origin():
                return self._json({"error": "forbidden"}, 403)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            effort = payload.get("effort", "")
            if effort not in ("", None) and effort not in EFFORT_CHOICES:
                return self._json({"error": "未知 effort: %s" % effort}, 400)
            try:
                if url.path == "/api/settings/claude/main":
                    fam = mm.POLICY["claude"]["main_pref"][0] if payload.get("auto") else payload.get("family")
                    log = ctx["claude_cfg"].set_main_family(fam)
                    self._json({"ok": True, "family": fam, "log": log})
                elif url.path == "/api/settings/claude/executor":
                    path, log = ctx["claude_cfg"].set_executor(payload.get("family"), effort or "")
                    self._json({"ok": True, "family": payload.get("family"), "effort": effort,
                                "path": path, "log": log, "note": "新开会话后生效"})
                elif url.path == "/api/settings/codex/executor":
                    path, slug, log = ctx["codex_cfg"].set_executor(payload.get("family"), effort or "")
                    self._json({"ok": True, "family": payload.get("family"), "model": slug,
                                "path": path, "log": log, "note": "新开会话后生效"})
                else:
                    self._json({"error": "not found"}, 404)
            except (FileNotFoundError, ValueError) as e:
                self._json({"error": str(e)}, 400)

    return Handler


# ---------------------------------------------------------------------------
# 前端页面（内嵌，无外部依赖，自适应亮/暗色）
# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>墨斗管理后台</title>
<style>
:root {
  --bg:#f6f5f2; --card:#ffffff; --fg:#1e1e1c; --muted:#6f6d66;
  --line:#e4e2db; --accent:#0f6f5c; --accent-weak:#dcefe9; --warn:#a04d0c;
  --bar:#0f6f5c; --bar2:#8aa39b;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#161614; --card:#1f1f1c; --fg:#eceae4; --muted:#98958c;
    --line:#33322d; --accent:#4fbf9f; --accent-weak:#1d3a33; --warn:#e0954f;
    --bar:#4fbf9f; --bar2:#3d5a52; }
}
* { box-sizing:border-box; }
body { margin:0; font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg); color:var(--fg); }
.wrap { max-width:960px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:22px; margin:8px 0 2px; }
h1 small { color:var(--muted); font-weight:400; font-size:13px; margin-left:8px; }
h2 { font-size:16px; margin:0 0 12px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:16px 18px; margin-top:16px; }
.tabs { display:flex; gap:8px; margin:16px 0 0; flex-wrap:wrap; }
.tab { border:1px solid var(--line); background:var(--card); color:var(--fg);
  padding:6px 16px; border-radius:999px; cursor:pointer; font-size:14px; }
.tab.on { background:var(--accent); border-color:var(--accent); color:#fff; }
.kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
.kpi { border:1px solid var(--line); border-radius:8px; padding:10px 12px; }
.kpi .v { font-size:20px; font-weight:600; font-variant-numeric:tabular-nums; }
.kpi .l { color:var(--muted); font-size:12px; }
.kpi.hl { background:var(--accent-weak); border-color:transparent; }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
th,td { text-align:right; padding:6px 8px; border-bottom:1px solid var(--line); font-size:13px; }
th:first-child,td:first-child { text-align:left; }
th { color:var(--muted); font-weight:500; }
.scroll { overflow-x:auto; }
.bars { display:flex; align-items:flex-end; gap:4px; height:110px; margin-top:8px; }
.bcol { flex:1; display:flex; flex-direction:column; justify-content:flex-end;
  align-items:center; gap:4px; min-width:22px; }
.stack { width:100%; display:flex; flex-direction:column; min-height:2px;
  border-radius:3px 3px 0 0; overflow:hidden; }
.seg { width:100%; min-height:1px; }
.s-main { background:var(--bar2); } .s-modou { background:var(--bar); } .s-other { background:var(--warn); opacity:.6; }
.dot { display:inline-block; width:9px; height:9px; border-radius:2px; margin:0 2px 0 8px; vertical-align:middle; }
.blab { font-size:10px; color:var(--muted); white-space:nowrap; }
.row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin:8px 0; }
select,button { font:inherit; padding:6px 12px; border-radius:6px;
  border:1px solid var(--line); background:var(--card); color:var(--fg); }
button.primary { background:var(--accent); color:#fff; border-color:var(--accent); cursor:pointer; }
label { font-size:14px; }
.muted { color:var(--muted); font-size:13px; }
.ok { color:var(--accent); } .warn { color:var(--warn); }
#toast { position:fixed; bottom:20px; left:50%; transform:translateX(-50%);
  background:var(--fg); color:var(--bg); padding:8px 20px; border-radius:8px;
  opacity:0; transition:opacity .3s; pointer-events:none; font-size:14px; }
code { background:var(--accent-weak); padding:1px 5px; border-radius:4px; font-size:12px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>墨斗管理后台 <small>Modou Admin Console</small></h1>
  <div class="muted">主线弹线，工匠照线施工 —— 消耗检查 · 模型设置 · Claude Code + Codex</div>

  <div class="tabs" id="plats">
    <button class="tab" data-p="claude">Claude Code</button>
    <button class="tab" data-p="codex">Codex</button>
  </div>
  <div class="tabs" id="tabs">
    <button class="tab" data-r="yesterday">昨天</button>
    <button class="tab" data-r="week">本周</button>
    <button class="tab" data-r="month">本月</button>
    <button class="tab" data-r="today">今天</button>
    <button class="tab" data-r="all">全部</button>
  </div>

  <div class="card">
    <h2>消耗总览 <span class="muted" id="rangeLabel"></span></h2>
    <div class="kpis" id="kpis"></div>
  </div>

  <div class="card">
    <h2>调配情况（主线 vs 墨斗 vs 其他子代理）</h2>
    <div class="kpis" id="delegation"></div>
    <div class="muted" id="delegationNote" style="margin-top:8px"></div>
    <div class="muted" id="identNote" style="margin-top:4px"></div>
    <div class="scroll" style="margin-top:10px"><table id="agentTable"></table></div>
  </div>

  <div class="card">
    <h2>按模型明细</h2>
    <div class="scroll"><table id="modelTable"></table></div>
  </div>

  <div class="card">
    <h2>每日费用 <span class="muted"><span class="dot s-main"></span>主线
      <span class="dot s-modou"></span>墨斗 <span class="dot s-other"></span>其他子代理</span></h2>
    <div class="bars" id="bars"></div>
  </div>

  <div class="card">
    <h2>模型设置 <span class="muted" id="setPlat"></span></h2>
    <div class="muted">只按<b>系列</b>设置，不钉死版本：Claude 写系列别名（Claude Code 自动用该系列最新版）；
      Codex 按系列解析成本机 <code>codex debug models</code> 里的最新 id。写入前自动备份。</div>
    <div id="mainRow"></div>
    <div class="row" style="margin-top:14px">
      <label>执行官（墨斗）系列：</label>
      <select id="execSelect"></select>
      <label>速度档位：</label>
      <select id="effortSelect"></select>
      <button class="primary" onclick="saveExec()">保存执行官</button>
    </div>
    <div class="muted" id="effortNote"></div>
    <div class="muted" id="execInfo"></div>
    <div class="muted" id="pairWarn" style="margin-top:6px"></div>
  </div>

  <div class="card">
    <h2>健康检查 &amp; 价格表</h2>
    <div id="health"></div>
    <div class="scroll" style="margin-top:10px"><table id="priceTable"></table></div>
  </div>
</div>
<div id="toast"></div>

<script>
let S = null, currentRange = "yesterday", P = "claude";
const $ = id => document.getElementById(id);
const fmtT = n => n >= 1e9 ? (n/1e9).toFixed(2)+"B" : n >= 1e6 ? (n/1e6).toFixed(2)+"M"
              : n >= 1e3 ? (n/1e3).toFixed(1)+"K" : String(n);
const fmtC = n => "$" + n.toFixed(n >= 100 ? 0 : n >= 1 ? 2 : 4);
const rangeNames = {yesterday:"昨天", week:"本周", month:"本月", today:"今天", all:"全部"};

function toast(msg, isErr) {
  const t = $("toast"); t.textContent = msg;
  t.style.background = isErr ? "var(--warn)" : "var(--fg)";
  t.style.opacity = 1; setTimeout(() => t.style.opacity = 0, 2500);
}

async function loadUsage(range) {
  currentRange = range;
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("on", b.dataset.r === range));
  const d = await (await fetch(`/api/usage?platform=${P}&range=${range}`)).json();
  $("rangeLabel").textContent = "· " + (rangeNames[range] || range);
  const t = d.totals;
  $("kpis").innerHTML = [
    kpi(fmtC(t.cost), "总费用", true),
    kpi(fmtT(t.output), "输出 token"),
    kpi(fmtT(t.input), "输入 token"),
    kpi(fmtT(t.cache_read), "缓存读"),
    kpi(fmtT(t.cache_write), "缓存写"),
    kpi(String(t.requests), "请求数"),
  ].join("");

  const ex = d.executor, mn = d.main;
  $("delegation").innerHTML = [
    kpi((d.delegation_ratio*100).toFixed(1) + "%", "委派率（按输出 token）", true),
    kpi(fmtC(d.savings), "墨斗为你省下", true),
    kpi(fmtC(mn.cost), "主线花费"),
    kpi(fmtC(ex.cost), "墨斗花费"),
    kpi(fmtC(d.other.cost), "其他子代理花费"),
    kpi(`${mn.requests} / ${ex.requests} / ${d.other.requests}`, "请求数 主线/墨斗/其他"),
  ].join("");
  const baseName = d.baseline_name || d.baseline_model;
  $("delegationNote").textContent = ex.requests === 0
    ? "该时段没有墨斗（执行官）的调用记录。实施类任务多让主线委派给墨斗，省钱又省上下文。"
    : `若这些实施工作全由主线实际所用的 ${baseName} 完成需 ${fmtC(ex.cost_if_main)}，实际仅 ${fmtC(ex.cost)}。`;
  $("identNote").innerHTML = ex.requests === 0 ? "" :
    `墨斗识别：<b>${ex.exact}</b> 条按子代理身份（<code>${esc(d.executor_name)}</code>）精确识别` +
    (ex.inferred ? `，<span class="warn">${ex.inferred} 条查不到身份、按执行官模型推断</span>（会话仍在进行或日志较旧）` : "") + "。";
  $("agentTable").innerHTML = d.other_agents.length
    ? "<tr><th>其他子代理（不计入墨斗）</th><th>请求</th><th>输出</th><th>模型</th><th>费用</th></tr>" +
      d.other_agents.map(a => `<tr><td>${esc(a.type)}</td><td>${a.requests}</td><td>${fmtT(a.output)}</td>` +
        `<td class="muted">${a.models.map(esc).join(", ")}</td><td>${fmtC(a.cost)}</td></tr>`).join("")
    : "";

  const rows = d.by_model.map(m =>
    `<tr><td>${esc(m.name)}<div class="muted">${esc(m.model)}</div></td><td>${m.requests}</td>` +
    `<td>${fmtT(m.input)}</td><td>${fmtT(m.output)}</td><td>${fmtT(m.cache_read)}</td>` +
    `<td>${fmtT(m.cache_write)}</td><td><b>${fmtC(m.cost)}</b></td>` +
    `<td class="muted">${split(m)}</td></tr>`).join("");
  $("modelTable").innerHTML =
    "<tr><th>模型</th><th>请求</th><th>输入</th><th>输出</th><th>缓存读</th><th>缓存写</th><th>费用</th><th>主线/墨斗/其他</th></tr>" +
    (rows || "<tr><td colspan=8 class='muted'>该时段无记录</td></tr>");

  const days = d.daily.slice(-31);
  const maxC = Math.max(...days.map(x => x.cost), 1e-9);
  $("bars").innerHTML = days.length
    ? days.map(x => `<div class="bcol" title="${x.day}  ${fmtC(x.cost)}\n主线 ${fmtC(x.main)} · 墨斗 ${fmtC(x.modou)} · 其他 ${fmtC(x.other)}">` +
        `<div class="stack" style="height:${Math.max(2, x.cost/maxC*90)}px">` +
        ["other", "modou", "main"].map(k => x[k] > 0
          ? `<div class="seg s-${k}" style="flex:${x[k] / x.cost}"></div>` : "").join("") + `</div>` +
        `<div class="blab">${x.day.slice(3)}</div></div>`).join("")
    : "<div class='muted'>该时段无记录</div>";
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
}

function split(m) {
  if (!m.cost) return "—";
  const pct = v => Math.round(v / m.cost * 100) + "%";
  return `${pct(m.cost_main)} / ${pct(m.cost_modou)} / ${pct(m.cost_other)}`;
}

function kpi(v, l, hl) {
  return `<div class="kpi${hl ? " hl" : ""}"><div class="v">${v}</div><div class="l">${l}</div></div>`;
}

const famLabel = (plat, f) => (S[plat].family_prices[f] || {}).label || f || "—";
const famPrice = (plat, f) => { const p = S[plat].family_prices[f]; return p ? ` ($${p.in}/$${p.out})` : ""; };

async function loadSettings() {
  S = await (await fetch("/api/settings")).json();
  const C = S[P];
  $("setPlat").textContent = "· " + (P === "claude" ? "Claude Code" : "Codex");
  document.querySelectorAll("#plats .tab").forEach(b => b.classList.toggle("on", b.dataset.p === P));

  if (P === "claude") {
    $("mainRow").innerHTML = `<div class="row" style="margin-top:10px"><label>主线系列：</label>
      <label><input type="checkbox" id="autoMain"> 自动（最强可用：${famLabel("claude", C.main_auto)}）</label>
      <select id="mainSelect">${C.main_choices.map(f =>
        `<option value="${f}">${famLabel("claude", f)}${famPrice("claude", f)}</option>`).join("")}</select>
      <button class="primary" onclick="saveMain()">保存主线</button></div>
      <div class="muted">${C.main_model
        ? `当前 settings.json：<code>${esc(C.main_model)}</code>（${esc(C.settings_path)}）` +
          (C.main_family && !C.main_choices.includes(C.main_family) ? ` <span class="warn">（非可选主线系列）</span>` : "") +
          (/\d/.test(C.main_model) ? ` <span class="warn">钉死了具体版本，建议改成系列别名</span>` : "")
        : `settings.json 未设置 model，Claude Code 用其默认模型。`}</div>`;
    const auto = !C.main_model || (C.main_family === C.main_auto && !/\d/.test(C.main_model));
    $("autoMain").checked = auto;
    $("mainSelect").value = C.main_choices.includes(C.main_family) ? C.main_family : C.main_auto;
    $("mainSelect").disabled = auto;
    $("autoMain").addEventListener("change", e => { $("mainSelect").disabled = e.target.checked; updatePairWarn(); });
    $("mainSelect").addEventListener("change", updatePairWarn);
    $("effortNote").textContent = "速度档位即 effort：越低越快、越省 token。子代理不支持 /fast，effort 是官方提速手段；照单施工推荐 medium，嫌慢可调 low。";
  } else {
    $("mainRow").innerHTML = `<div class="row" style="margin-top:10px"><label>主线模型：</label>
      <code>${esc(C.main_model || "未设置")}</code>
      <span class="muted">（系列 ${famLabel("codex", C.main_family)}；读自 ${esc(C.config_path)}，后台不改 Codex 配置）</span></div>
      <div class="muted">本机 Codex 可选模型：${C.available.map(x => `<code>${esc(x)}</code>`).join(" ") || "<span class='warn'>读不到（codex 不在 PATH？）</span>"}</div>`;
    $("effortNote").textContent = "档位即 model_reasoning_effort。官方建议：显式使用 Luna 时从 high 起步（Luna 单价约为 Sol 的 1/20，high 依然很便宜）。";
  }

  $("execSelect").innerHTML = C.executor_choices.map(f =>
    `<option value="${f}">${famLabel(P, f)}${famPrice(P, f)}${P === "codex" && C.resolved[f] ? " → " + C.resolved[f] : ""}</option>`).join("");
  if (C.executor_family) $("execSelect").value = C.executor_family;
  $("effortSelect").innerHTML = (P === "claude" ? '<option value="">跟随会话默认</option>' : '<option value="">按系列推荐</option>') +
    S.effort_choices.map(e => `<option value="${e}">${e}</option>`).join("");
  $("effortSelect").value = C.executor_effort || "";
  $("execInfo").innerHTML = C.executor_path
    ? `当前：<code>${esc(C.executor_model || "继承主线")}</code>` + (C.executor_effort ? `，effort <code>${esc(C.executor_effort)}</code>` : "") +
      `（${esc(C.executor_path)}）— 修改后新开会话生效` +
      (P === "codex" && C.executor_latest && C.executor_latest !== C.executor_model
        ? ` <span class="warn">本机已有该系列更新版 ${esc(C.executor_latest)}，点保存即可更新</span>` : "")
    : `<span class="warn">未找到已安装的执行官，请先运行 <code>bash install.sh --${P}</code>。</span>`;
  updatePairWarn();

  $("health").innerHTML = [
    line(C.executor_path, `执行官已安装：${esc(C.executor_path || "")}`, `执行官未安装（bash install.sh --${P}）`),
    line(C.usage_found, `用量数据目录：${esc(C.usage_dir)}`, `找不到用量目录 ${esc(C.usage_dir)}`),
    P === "codex" ? line(C.available.length, `Codex 模型目录可读（${C.available.length} 个可选模型）`, "读不到 Codex 模型目录") : "",
    P === "codex" && C.unreadable_files ? line(false, "", `${C.unreadable_files} 个压缩会话文件无法解压（需 Python 3.14+ 或 zstd 命令），未计入`) : "",
    line(true, "价格表只用于对账；选择逻辑走「系列 → 当前环境最新」。新版本 id 会按系列归一估价。"),
  ].join("");

  $("priceTable").innerHTML =
    "<tr><th>模型</th><th>模型 ID</th><th>输入 $/M</th><th>缓存读 $/M</th><th>输出 $/M</th></tr>" +
    Object.entries(S.prices).filter(([k, p]) => p.platform === P).map(([k, p]) =>
      `<tr><td>${esc(p.name)}</td><td style="text-align:left"><code>${esc(k)}</code></td>` +
      `<td>$${p.in}</td><td>${p.cr != null ? "$" + p.cr : "—"}</td><td>$${p.out}</td></tr>`).join("");
}

function line(ok, okText, badText) {
  return `<div class="${ok ? "ok" : "warn"}">${ok ? "✓ " + okText : "✗ " + (badText || okText)}</div>`;
}

function updatePairWarn() {
  const C = S[P];
  let mainFam = C.main_family;
  if (P === "claude" && $("autoMain")) mainFam = $("autoMain").checked ? C.main_auto : $("mainSelect").value;
  const ex = $("execSelect").value;
  const pm = C.family_prices[mainFam], pe = C.family_prices[ex];
  if (!pm || !pe) { $("pairWarn").textContent = "主线系列未知，无法做配对检查。"; return; }
  $("pairWarn").innerHTML = (pe.out >= pm.out)
    ? `<span class="warn">⚠ 同级转包纯增开销：执行官 ${pe.label}（$${pe.out}/M 输出）不便宜于主线 ${pm.label}（$${pm.out}/M）——默认不要这样配对。</span>`
    : `执行官单价约为主线的 ${Math.round(pe.out / pm.out * 100)}%（按两系列最新已知价：${pe.ref} vs ${pm.ref}），委派实施可显著省钱。`;
}

async function post(url, body) {
  const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  return r.json();
}

async function saveMain() {
  const auto = $("autoMain").checked;
  const d = await post("/api/settings/claude/main", auto ? {auto: true} : {family: $("mainSelect").value});
  if (d.ok) { toast(`主线已设为 ${d.family}（新开会话生效）`); loadSettings(); }
  else toast(d.error || "失败", true);
}

async function saveExec() {
  const d = await post(`/api/settings/${P}/executor`, {family: $("execSelect").value, effort: $("effortSelect").value});
  if (d.ok) { toast(`执行官已设为 ${d.model || d.family}，新开会话生效`); loadSettings(); loadUsage(currentRange); }
  else toast(d.error || "失败", true);
}

$("execSelect").addEventListener("change", updatePairWarn);
$("tabs").addEventListener("click", e => { if (e.target.dataset.r) loadUsage(e.target.dataset.r); });
$("plats").addEventListener("click", e => {
  if (e.target.dataset.p) { P = e.target.dataset.p; loadSettings().then(() => loadUsage(currentRange)); }
});

loadSettings().then(() => loadUsage("yesterday"));
</script>
</body>
</html>
"""


def build_context(claude_dir, codex_dir, project=None):
    cache = {"t": None, "v": []}

    def codex_models():
        # codex debug models 要起子进程，缓存 5 分钟
        now = datetime.now()
        if cache["t"] is None or (now - cache["t"]).total_seconds() > 300:
            cache["v"] = mm.codex_available_models()
            cache["t"] = now
        return cache["v"]

    return {
        "scanners": {"claude": ClaudeScanner(claude_dir), "codex": CodexScanner(codex_dir)},
        "claude_cfg": ClaudeConfig(claude_dir, project),
        "codex_cfg": CodexConfig(codex_dir, project),
        "codex_models": codex_models,
    }


def main():
    ap = argparse.ArgumentParser(description="墨斗管理后台（Claude Code + Codex）")
    ap.add_argument("--port", type=int, default=8848)
    ap.add_argument("--claude-dir", default=str(mm.claude_home()),
                    help="Claude Code 配置目录（默认 $CLAUDE_CONFIG_DIR 或 ~/.claude）")
    ap.add_argument("--codex-dir", default=str(mm.codex_home()),
                    help="Codex 配置目录（默认 $CODEX_HOME 或 ~/.codex）")
    ap.add_argument("--project", default=None,
                    help="同时查看/修改该项目的项目级安装（.claude/agents、.codex/agents）")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    ctx = build_context(args.claude_dir, args.codex_dir,
                        str(Path(args.project).resolve()) if args.project else None)
    addr = ("127.0.0.1", args.port)
    httpd = ThreadingHTTPServer(addr, make_handler(ctx))
    url = "http://%s:%d" % addr
    print("墨斗管理后台已启动：%s  (Ctrl+C 退出)" % url, flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
