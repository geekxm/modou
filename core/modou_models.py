#!/usr/bin/env python3
"""墨斗 · 模型系列注册表与解析器（Claude Code + Codex 共用，零依赖）。

设计原则（见 README「系列而不是钉死版本」）：

- 选择逻辑只认「系列」：Claude 侧 fable / opus / sonnet / haiku，
  Codex 侧 astra / sol / terra / luna。
- 具体版本 id 只出现在价格表里（用于对账）；运行时一律走
  「系列 → 当前环境里该系列最新的 id」。
- 未知/新出现的版本 id 会按名字归一到系列（例如将来的 claude-opus-6-1、
  gpt-7-luna），价格按该系列已知最新价估算，不会因为「不认识」而失败。
- Mythos 系列不参与任何选择：不进下拉、不进自动选择、不做推荐。

命令行：
    python3 core/modou_models.py codex-models          # 本机 Codex 可用模型（按系列归类）
    python3 core/modou_models.py resolve codex luna    # 解析某系列在本机的最新 id
    python3 core/modou_models.py recommend codex       # 主线/执行官推荐 + 配对检查
    python3 core/modou_models.py recommend claude --main opus
    python3 core/modou_models.py family gpt-7.3-luna   # 归一到系列
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 系列定义。rank 越大越强；cost_rank 越大越贵。两者目前同序，但分开写，
# 以便将来出现「更强但不更贵」的系列时只改这里。
# ---------------------------------------------------------------------------
EXCLUDED_FAMILIES = {"mythos"}  # 不可用、不参与选择

FAMILIES = {
    "claude": {
        "fable":  {"rank": 4, "label": "Fable"},
        "opus":   {"rank": 3, "label": "Opus"},
        "sonnet": {"rank": 2, "label": "Sonnet"},
        "haiku":  {"rank": 1, "label": "Haiku"},
    },
    "codex": {
        "astra": {"rank": 4, "label": "Astra"},
        "sol":   {"rank": 3, "label": "Sol"},
        "terra": {"rank": 2, "label": "Terra"},
        "luna":  {"rank": 1, "label": "Luna"},
    },
}

# 推荐策略（保持原项目精神：主线强、执行官明显更便宜）
POLICY = {
    "claude": {
        "main_pref": ["fable", "opus", "sonnet"],       # 没有 Fable 时退到可用的更强非 Mythos 系列
        "executor_pref": ["opus", "sonnet", "haiku"],   # 优先 Opus，必要时落到 Sonnet/Haiku
        "executor_effort": {"opus": "medium", "sonnet": "medium", "haiku": "medium"},
    },
    "codex": {
        "main_pref": ["sol", "astra", "terra"],         # 官方：Sol 做日常规划/工具/自检
        "executor_pref": ["luna", "terra"],             # 官方：Luna 做明确、可重复、范围窄的活
        # 官方建议：显式选 Luna 时 effort 从 high 起步
        "executor_effort": {"luna": "high", "terra": "medium", "sol": "medium"},
    },
}

# ---------------------------------------------------------------------------
# 价格表（美元 / 百万 token）——只用于对账，不参与「选哪个」。
#   in / out：输入 / 输出；cr：缓存读（绝对价）；cw：缓存写（绝对价，可选）。
# Claude：未写 cr 的按 0.1x 输入；缓存写按 1.25x（5m）/ 2x（1h）输入。
# Codex（OpenAI 口径）：input_tokens 已包含 cached_input_tokens；cw 为 30m 写入价。
# 数据来源：Anthropic 定价（2026-11）；OpenAI API pricing 页（2026-11-20 核对）。
# ---------------------------------------------------------------------------
PRICES = {
    # --- Claude ---
    "claude-fable-5-2":  {"in": 8.0,  "out": 40.0, "cr": 0.20, "name": "Fable 5.2"},
    "claude-fable-5-1":  {"in": 10.0, "out": 50.0, "cr": 0.25, "name": "Fable 5.1"},
    "claude-fable-5":    {"in": 10.0, "out": 50.0, "name": "Fable 5"},
    "claude-opus-5-5":   {"in": 4.0,  "out": 20.0, "cr": 0.20, "name": "Opus 5.5"},
    "claude-opus-5":     {"in": 5.0,  "out": 25.0, "name": "Opus 5"},
    "claude-opus-4-8":   {"in": 5.0,  "out": 25.0, "name": "Opus 4.8"},
    "claude-opus-4-7":   {"in": 5.0,  "out": 25.0, "name": "Opus 4.7"},
    "claude-opus-4-6":   {"in": 5.0,  "out": 25.0, "name": "Opus 4.6"},
    "claude-sonnet-5-5": {"in": 2.0,  "out": 10.0, "cr": 0.10, "name": "Sonnet 5.5"},
    "claude-sonnet-5":   {"in": 2.0,  "out": 10.0, "name": "Sonnet 5"},
    "claude-sonnet-4-6": {"in": 3.0,  "out": 15.0, "name": "Sonnet 4.6"},
    "claude-haiku-5-5":  {"in": 0.10, "out": 0.50, "name": "Haiku 5.5"},
    "claude-haiku-4-5":  {"in": 1.0,  "out": 5.0,  "name": "Haiku 4.5"},
    # --- Codex / OpenAI ---
    "gpt-6.2-astra": {"in": 8.0,  "out": 40.0, "cr": 0.40, "cw": 10.0,  "name": "GPT-6.2 Astra"},
    "gpt-6.2-sol":   {"in": 2.0,  "out": 10.0, "cr": 0.10, "cw": 2.50,  "name": "GPT-6.2 Sol"},
    "gpt-6.1-sol":   {"in": 2.0,  "out": 10.0, "cr": 0.10, "cw": 2.50,  "name": "GPT-6.1 Sol"},
    "gpt-6.1-terra": {"in": 1.0,  "out": 5.0,  "cr": 0.05, "cw": 1.25,  "name": "GPT-6.1 Terra"},
    "gpt-6-astra":   {"in": 10.0, "out": 50.0, "cr": 1.00, "cw": 12.50, "name": "GPT-6 Astra"},
    "gpt-6-sol":     {"in": 2.0,  "out": 10.0, "cr": 0.20, "cw": 2.50,  "name": "GPT-6 Sol"},
    "gpt-6-luna":    {"in": 0.10, "out": 0.50, "cr": 0.01, "cw": 0.125, "name": "GPT-6 Luna"},
    "gpt-5.6-sol":   {"in": 4.0,  "out": 20.0, "cr": 0.40, "cw": 5.0,   "name": "GPT-5.6 Sol"},
    "gpt-5.6-terra": {"in": 2.0,  "out": 12.0, "cr": 0.20, "cw": 2.50,  "name": "GPT-5.6 Terra"},
    "gpt-5.6-luna":  {"in": 0.20, "out": 1.20, "cr": 0.02, "cw": 0.25,  "name": "GPT-5.6 Luna"},
}

_CLAUDE_RE = re.compile(r"^(?:[a-z]+\.)?(?:anthropic\.)?claude-([a-z]+)-(\d+(?:-\d{1,2})*)(?:-(\d{8}))?(?:@\d+)?(\[1m\])?$")
_CODEX_RE = re.compile(r"^gpt-(\d+(?:\.\d+)*)-([a-z]+)(?:-.*)?$")


def platform_of(model_id):
    if not model_id:
        return None
    m = model_id.lower()
    if m in FAMILIES["claude"] or m in EXCLUDED_FAMILIES or "claude" in m:
        return "claude"
    if m in FAMILIES["codex"] or m.startswith("gpt-"):
        return "codex"
    return None


def parse(model_id):
    """返回 (platform, family, version_tuple)；不认识返回 (None, None, ())。

    支持别名本身（"opus"、"luna"）、带日期后缀、带 [1m] 等变体。"""
    if not model_id:
        return None, None, ()
    m = model_id.strip().lower()
    for plat, fams in FAMILIES.items():
        if m in fams:
            return plat, m, ()
    if m in EXCLUDED_FAMILIES:
        return "claude", m, ()
    cm = _CLAUDE_RE.match(m)
    if cm:
        fam = cm.group(1)
        ver = tuple(int(x) for x in cm.group(2).split("-"))
        return "claude", fam, ver
    gm = _CODEX_RE.match(m)
    if gm:
        ver = tuple(int(x) for x in gm.group(1).split("."))
        return "codex", gm.group(2), ver
    return None, None, ()


def family_of(model_id):
    return parse(model_id)[1]


def norm_model(model_id):
    """把带日期/[1m]/平台前缀的 Claude id 规整成价格表的键；Codex id 原样。"""
    if not model_id:
        return model_id
    plat, fam, ver = parse(model_id)
    if plat == "claude" and ver:
        return "claude-%s-%s" % (fam, "-".join(str(x) for x in ver))
    return model_id.strip().lower()


def selectable(family):
    return bool(family) and family not in EXCLUDED_FAMILIES


def rank(platform, family):
    return FAMILIES.get(platform, {}).get(family, {}).get("rank", 0)


def latest_known(platform, family, candidates=None):
    """在 candidates（默认价格表）里找该系列版本号最大的 id。"""
    pool = candidates if candidates is not None else PRICES.keys()
    best, best_ver = None, None
    for mid in pool:
        p, f, v = parse(mid)
        if p == platform and f == family and v:
            if best_ver is None or v > best_ver:
                best, best_ver = mid, v
    return best


def price_of(model_id):
    """精确价 → 否则按系列最新已知价估算（标记 estimated）。Mythos/未知系列返回 None。"""
    key = norm_model(model_id)
    if key in PRICES:
        return dict(PRICES[key], estimated=False, id=key)
    plat, fam, _ = parse(model_id)
    if not selectable(fam) or fam not in FAMILIES.get(plat, {}):
        return None
    ref = latest_known(plat, fam)
    if not ref:
        return None
    p = dict(PRICES[ref], estimated=True, id=ref)
    p["name"] = "%s（按 %s 估价）" % (model_id, PRICES[ref]["name"])
    return p


def family_price(platform, family):
    ref = latest_known(platform, family)
    return PRICES.get(ref) if ref else None


def cheaper(platform, exec_family, main_family):
    """执行官系列是否明显便宜于主线系列（按该系列最新已知输出价，其次输入价）。"""
    pe, pm = family_price(platform, exec_family), family_price(platform, main_family)
    if pe and pm:
        return (pe["out"], pe["in"]) < (pm["out"], pm["in"])
    return rank(platform, exec_family) < rank(platform, main_family)


# ---------------------------------------------------------------------------
# 运行环境探测
# ---------------------------------------------------------------------------
def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def claude_home():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def codex_available_models(codex_bin=None, timeout=60):
    """本机 Codex 当前可选模型（visibility=list），返回 [{slug, family, version, ...}]。

    优先 `codex debug models`（Codex 自己的模型目录，跟账号/版本走）；
    失败再读 $CODEX_HOME/models_cache.json。都拿不到返回 []。"""
    data = None
    exe = codex_bin or shutil.which("codex")
    if exe:
        try:
            out = subprocess.run([exe, "debug", "models"], capture_output=True, text=True,
                                 timeout=timeout, cwd=str(Path.home()))
            if out.returncode == 0 and out.stdout.strip():
                data = json.loads(out.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            data = None
    if data is None:
        cache = codex_home() / "models_cache.json"
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
    models = data.get("models", data) if isinstance(data, dict) else data
    out = []
    for m in models or []:
        if not isinstance(m, dict):
            continue
        slug = m.get("slug") or m.get("id")
        if not slug or m.get("visibility", "list") != "list":
            continue
        plat, fam, ver = parse(slug)
        out.append({"slug": slug, "family": fam if plat == "codex" else None, "version": ver,
                    "display_name": m.get("display_name") or slug,
                    "description": m.get("description") or "",
                    "default_reasoning_level": m.get("default_reasoning_level"),
                    "reasoning_levels": [x.get("effort") for x in m.get("supported_reasoning_levels") or []
                                         if isinstance(x, dict)]})
    return out


def resolve_codex(family, available=None):
    """系列 → 本机 Codex 该系列最新 id（只在 visibility=list 的模型里挑）。"""
    available = codex_available_models() if available is None else available
    best = None
    for m in available:
        if m["family"] == family and m["version"]:
            if best is None or m["version"] > best["version"]:
                best = m
    return best


def codex_config_model():
    """读 $CODEX_HOME/config.toml 顶层 model（只读，不碰其他键）。"""
    p = codex_home() / "config.toml"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            break  # 只看顶层
        m = re.match(r'^model\s*=\s*"([^"]+)"', s)
        if m:
            return m.group(1)
    return None


def claude_settings_model(settings_path=None):
    p = Path(settings_path) if settings_path else claude_home() / "settings.json"
    try:
        return (json.loads(p.read_text(encoding="utf-8")) or {}).get("model")
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 推荐与配对检查
# ---------------------------------------------------------------------------
PAIR_WARNING = "同级转包纯增开销：执行官（{e}）不便宜于主线（{m}），默认不应这样配对。"


def recommend(platform, main_model=None, executor_family=None, available=None):
    """返回推荐结果 dict：main_family / executor_family / executor_model / effort / warnings。

    - main_model：当前主线模型（id 或别名）；None 时按策略取首选可用系列。
    - executor_family：用户显式指定的执行官系列；None 时自动挑「比主线明显便宜」的首选系列。
    Claude 侧 executor_model 写别名（Claude Code 自己解析到最新版）；
    Codex 侧 executor_model 是解析出的本机最新 id。"""
    pol = POLICY[platform]
    warnings, notes = [], []
    if platform == "codex":
        available = codex_available_models() if available is None else available
        fams_avail = {m["family"] for m in available if m["family"]}
    else:
        fams_avail = set(FAMILIES["claude"])  # Claude Code 别名总能解析；账号可用性在会话里体现

    main_family = family_of(main_model) if main_model else None
    if main_family in EXCLUDED_FAMILIES:
        warnings.append("主线配置为 %s，墨斗不把 Mythos 作为可选主线；按 %s 系列推荐。"
                        % (main_model, pol["main_pref"][0]))
        main_family = None
    if main_family and main_family not in FAMILIES[platform]:
        notes.append("主线模型 %s 不属于已知系列，按首选主线系列推荐。" % main_model)
        main_family = None
    if not main_family:
        main_family = next((f for f in pol["main_pref"] if f in fams_avail), pol["main_pref"][0])
        notes.append("未读到主线模型，按策略假定主线为 %s 系列。" % FAMILIES[platform][main_family]["label"])

    explicit = executor_family is not None
    if explicit:
        if executor_family in EXCLUDED_FAMILIES or executor_family not in FAMILIES[platform]:
            raise ValueError("不支持的执行官系列：%s（可选：%s）"
                             % (executor_family, ", ".join(pol["executor_pref"])))
        if executor_family not in fams_avail:
            warnings.append("本机环境里没有 %s 系列模型。" % executor_family)
    else:
        executor_family = next((f for f in pol["executor_pref"]
                                if f in fams_avail and cheaper(platform, f, main_family)), None)
        if executor_family is None:
            executor_family = next((f for f in pol["executor_pref"] if f in fams_avail),
                                   pol["executor_pref"][0])
        if executor_family != pol["executor_pref"][0]:
            notes.append("主线为 %s，首选执行官系列 %s 不比它便宜，已自动落到 %s。"
                         % (FAMILIES[platform][main_family]["label"],
                            FAMILIES[platform][pol["executor_pref"][0]]["label"],
                            FAMILIES[platform][executor_family]["label"]))
            notes.append("推荐组合是主线 %s + 执行官 %s：把主线切到 %s 后用 --%s-executor-family %s 重装即可。"
                         % (FAMILIES[platform][pol["main_pref"][0]]["label"],
                            FAMILIES[platform][pol["executor_pref"][0]]["label"],
                            pol["main_pref"][0], platform, pol["executor_pref"][0]))

    same_tier = not cheaper(platform, executor_family, main_family)
    if same_tier:
        warnings.append(PAIR_WARNING.format(e=FAMILIES[platform][executor_family]["label"],
                                            m=FAMILIES[platform][main_family]["label"]))

    if platform == "codex":
        res = resolve_codex(executor_family, available)
        executor_model = res["slug"] if res else None
        if not res:
            warnings.append("本机 Codex 模型目录里找不到 %s 系列，执行官将继承主线模型。" % executor_family)
        main_res = resolve_codex(main_family, available)
        main_latest = main_res["slug"] if main_res else None
    else:
        executor_model = executor_family  # 别名，Claude Code 解析到该系列最新版
        main_latest = main_family

    return {
        "platform": platform,
        "main_model": main_model,
        "main_family": main_family,
        "main_latest": main_latest,
        "executor_family": executor_family,
        "executor_model": executor_model,
        "executor_effort": pol["executor_effort"].get(executor_family, "medium"),
        "explicit_executor": explicit,
        "same_tier": same_tier,
        "warnings": warnings,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="modou_models", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("codex-models")
    r = sub.add_parser("resolve")
    r.add_argument("platform", choices=["claude", "codex"])
    r.add_argument("family")
    rc = sub.add_parser("recommend")
    rc.add_argument("platform", choices=["claude", "codex"])
    rc.add_argument("--main", default=None, help="主线模型 id 或别名（缺省读本机配置）")
    rc.add_argument("--executor-family", default=None)
    f = sub.add_parser("family")
    f.add_argument("model_id")
    a = ap.parse_args(argv)

    if a.cmd == "codex-models":
        ms = codex_available_models()
        for m in ms:
            print("%-16s family=%-6s %s" % (m["slug"], m["family"] or "-", m["description"]))
        if not ms:
            print("（未找到 Codex 模型目录：codex 不在 PATH 且无 models_cache.json）")
        return 0 if ms else 1
    if a.cmd == "resolve":
        if a.family in EXCLUDED_FAMILIES:
            print("拒绝：%s 不参与选择" % a.family, file=sys.stderr)
            return 2
        if a.platform == "claude":
            print(a.family)  # Claude Code 别名即「该系列最新版」
            return 0
        m = resolve_codex(a.family)
        if not m:
            print("本机 Codex 无 %s 系列" % a.family, file=sys.stderr)
            return 1
        print(m["slug"])
        return 0
    if a.cmd == "recommend":
        main = a.main
        if main is None:
            main = codex_config_model() if a.platform == "codex" else claude_settings_model()
        print(json.dumps(recommend(a.platform, main, a.executor_family), ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "family":
        plat, fam, ver = parse(a.model_id)
        print(json.dumps({"platform": plat, "family": fam, "version": ver,
                          "selectable": selectable(fam), "price": price_of(a.model_id)},
                         ensure_ascii=False))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
