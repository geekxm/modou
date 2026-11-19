"""墨斗单元测试（标准库 unittest，无网络、不调用 codex/claude）。

    python3 -m unittest discover -s tests -v
"""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "core"))
sys.path.insert(0, str(REPO / "admin"))

import modou_models as mm  # noqa: E402
import modou_install as mi  # noqa: E402

FAKE_CODEX = [
    {"slug": "gpt-6.1-terra", "family": "terra", "version": (6, 1)},
    {"slug": "gpt-6.1-sol", "family": "sol", "version": (6, 1)},
    {"slug": "gpt-6-astra", "family": "astra", "version": (6,)},
    {"slug": "gpt-6-sol", "family": "sol", "version": (6,)},
    {"slug": "gpt-6-luna", "family": "luna", "version": (6,)},
    {"slug": "gpt-5.6-luna", "family": "luna", "version": (5, 6)},
]


class Families(unittest.TestCase):
    def test_parse_and_normalize(self):
        self.assertEqual(mm.parse("claude-opus-5-5")[:2], ("claude", "opus"))
        self.assertEqual(mm.parse("claude-haiku-4-5-20251001"), ("claude", "haiku", (4, 5)))
        self.assertEqual(mm.norm_model("claude-haiku-4-5-20251001"), "claude-haiku-4-5")
        self.assertEqual(mm.norm_model("claude-opus-5-5[1m]"), "claude-opus-5-5")
        self.assertEqual(mm.parse("gpt-6.1-sol"), ("codex", "sol", (6, 1)))
        self.assertEqual(mm.parse("opus"), ("claude", "opus", ()))
        self.assertEqual(mm.parse("luna"), ("codex", "luna", ()))

    def test_unknown_versions_fold_into_family(self):
        for mid, fam in [("claude-opus-6-1", "opus"), ("claude-fable-7", "fable"),
                         ("gpt-7.3-luna", "luna"), ("gpt-9-sol", "sol")]:
            self.assertEqual(mm.family_of(mid), fam)
            p = mm.price_of(mid)
            self.assertIsNotNone(p, mid)
            self.assertTrue(p["estimated"])

    def test_mythos_never_selectable(self):
        self.assertFalse(mm.selectable("mythos"))
        self.assertIsNone(mm.price_of("claude-mythos-5-2"))
        for plat in ("claude", "codex"):
            pol = mm.POLICY[plat]
            self.assertNotIn("mythos", pol["main_pref"] + pol["executor_pref"])
        self.assertFalse(any("mythos" in k for k in mm.PRICES))
        rec = mm.recommend("claude", "claude-mythos-5-1")
        self.assertEqual(rec["main_family"], "fable")
        self.assertTrue(any("Mythos" in w for w in rec["warnings"]))

    def test_latest_resolution(self):
        self.assertEqual(mm.resolve_codex("sol", FAKE_CODEX)["slug"], "gpt-6.1-sol")
        self.assertEqual(mm.resolve_codex("luna", FAKE_CODEX)["slug"], "gpt-6-luna")
        newer = FAKE_CODEX + [{"slug": "gpt-6.2-luna", "family": "luna", "version": (6, 2)}]
        self.assertEqual(mm.resolve_codex("luna", newer)["slug"], "gpt-6.2-luna")
        self.assertEqual(mm.latest_known("claude", "fable"), "claude-fable-5-2")


class Recommend(unittest.TestCase):
    def test_claude_default_pair(self):
        r = mm.recommend("claude", "fable")
        self.assertEqual((r["main_family"], r["executor_family"], r["executor_model"]),
                         ("fable", "opus", "opus"))
        self.assertFalse(r["same_tier"])
        self.assertEqual(r["warnings"], [])

    def test_claude_opus_main_falls_back(self):
        r = mm.recommend("claude", "opus")
        self.assertEqual(r["executor_family"], "sonnet")
        self.assertFalse(r["same_tier"])

    def test_claude_explicit_same_tier_warns(self):
        r = mm.recommend("claude", "claude-opus-5-5", "opus")
        self.assertTrue(r["same_tier"])
        self.assertTrue(any("同级转包纯增开销" in w for w in r["warnings"]))

    def test_codex_default_pair(self):
        r = mm.recommend("codex", "gpt-6.1-sol", available=FAKE_CODEX)
        self.assertEqual((r["executor_family"], r["executor_model"], r["executor_effort"]),
                         ("luna", "gpt-6-luna", "high"))
        self.assertEqual(r["warnings"], [])

    def test_codex_luna_main_warns(self):
        r = mm.recommend("codex", "gpt-6-luna", "luna", available=FAKE_CODEX)
        self.assertTrue(r["same_tier"])

    def test_codex_missing_family(self):
        r = mm.recommend("codex", "gpt-6.1-sol", available=[m for m in FAKE_CODEX if m["family"] != "luna"])
        self.assertEqual(r["executor_family"], "terra")
        self.assertEqual(r["executor_model"], "gpt-6.1-terra")


class ProtocolSync(unittest.TestCase):
    """协议只有一份：core/protocol.md。"""

    def setUp(self):
        self.protocol = (REPO / "core" / "protocol.md").read_text(encoding="utf-8")

    def test_protocol_has_stop_lines_and_report(self):
        for s in ["已定方案", "验证命令与环境坑", "连续失败 3 次", "待裁决", "## 状态", "## 改动摘要",
                  "## 关键 diff", "## 验证结果", "## 高危标注", "## 偏离与待裁决"]:
            self.assertIn(s, self.protocol)

    def test_claude_agent_body(self):
        text = (REPO / "agents" / "executor.md").read_text(encoding="utf-8")
        self.assertEqual(text.split("---", 2)[-1].lstrip("\n"), self.protocol)
        fm = text.split("---", 2)[1]
        self.assertRegex(fm, r"(?m)^model: (fable|opus|sonnet|haiku)$")
        self.assertNotRegex(fm, r"claude-[a-z]+-\d")

    def test_codex_template(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib 需要 Python 3.11+")
        d = tomllib.loads((REPO / "agents" / "codex" / "modou.toml").read_text(encoding="utf-8"))
        self.assertEqual(d["developer_instructions"], self.protocol)
        self.assertEqual(d["name"], "modou")
        self.assertNotIn(d["name"], ("default", "worker", "explorer"))
        self.assertNotIn("model", d)  # 仓库模板不钉死版本

    def test_agents_md_block_matches_snippet(self):
        snippet = (REPO / "integrations" / "codex" / "AGENTS.snippet.md").read_text(encoding="utf-8")
        self.assertIn(snippet.rstrip("\n"), (REPO / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn('fork_turns = "none"', snippet)
        self.assertIn('agent_type = "modou"', snippet)

    def test_no_pinned_versions_in_agent_definitions(self):
        for p in [REPO / "agents" / "executor.md", REPO / "agents" / "codex" / "modou.toml",
                  REPO / "integrations" / "codex" / "AGENTS.snippet.md"]:
            t = p.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"claude-(fable|opus|sonnet|haiku|mythos)-\d|gpt-\d", t), p)


class Installer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="modou-test-unit-")
        t = Path(self.tmp.name)
        self.proj = t / "proj"
        self.proj.mkdir()
        self.env = {"MODOU_BACKUP_DIR": str(t / "bak"), "CODEX_HOME": str(t / "codex"),
                    "CLAUDE_CONFIG_DIR": str(t / "claude")}
        self.old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self._orig = mm.codex_available_models
        mm.codex_available_models = lambda *a, **k: list(FAKE_CODEX)
        (t / "codex").mkdir()
        (t / "codex" / "config.toml").write_text('model = "gpt-6.1-sol"\n')
        (t / "claude").mkdir()
        (t / "claude" / "settings.json").write_text(json.dumps({"model": "fable", "theme": "x"}))

    def tearDown(self):
        mm.codex_available_models = self._orig
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def run_install(self, *args):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mi.main(list(args))
        return rc, buf.getvalue()

    def test_project_install_idempotent_with_backup(self):
        (self.proj / "AGENTS.md").write_text("# mine\n\nkeep me\n")
        rc, out = self.run_install("--claude", "--codex", "--project", str(self.proj))
        self.assertEqual(rc, 0, out)
        toml = (self.proj / ".codex" / "agents" / "modou.toml").read_text()
        self.assertIn('model = "gpt-6-luna"', toml)
        self.assertIn('model_reasoning_effort = "high"', toml)
        md = (self.proj / "AGENTS.md").read_text()
        self.assertTrue(md.startswith("# mine\n\nkeep me\n"))
        self.assertEqual(md.count(mi.BEGIN), 1)
        claude = (self.proj / ".claude" / "agents" / "executor.md").read_text()
        self.assertIn("model: opus", claude)
        baks = list(Path(self.env["MODOU_BACKUP_DIR"]).rglob("AGENTS.md"))
        self.assertEqual(len(baks), 1)
        self.assertEqual(baks[0].read_text(), "# mine\n\nkeep me\n")

        n_before = len([x for x in Path(self.env["MODOU_BACKUP_DIR"]).rglob("*") if x.is_file()])
        rc, out2 = self.run_install("--claude", "--codex", "--project", str(self.proj))
        self.assertEqual(rc, 0)
        self.assertEqual(out2.count("unchanged"), 3, out2)
        n_after = len([x for x in Path(self.env["MODOU_BACKUP_DIR"]).rglob("*") if x.is_file()])
        self.assertEqual(n_before, n_after)  # 内容没变 → 不写、不备份
        self.assertEqual((self.proj / "AGENTS.md").read_text().count(mi.BEGIN), 1)

        rc, out3 = self.run_install("--check", "--claude", "--codex", "--project", str(self.proj))
        self.assertEqual(rc, 0, out3)

    def test_user_install_does_not_touch_user_config(self):
        cfg = Path(self.env["CODEX_HOME"]) / "config.toml"
        st = Path(self.env["CLAUDE_CONFIG_DIR"]) / "settings.json"
        before = (cfg.read_text(), st.read_text())
        user_agent = Path(self.env["CODEX_HOME"]) / "agents" / "modou.toml"
        user_agent.parent.mkdir()
        user_agent.write_text("# someone else's file\n")
        rc, out = self.run_install("--claude", "--codex", "--user")
        self.assertEqual(rc, 0, out)
        self.assertEqual((cfg.read_text(), st.read_text()), before)
        baks = list(Path(self.env["MODOU_BACKUP_DIR"]).rglob("modou.toml"))
        self.assertEqual([b.read_text() for b in baks], ["# someone else's file\n"])
        self.assertIn(mi.BEGIN, (Path(self.env["CODEX_HOME"]) / "AGENTS.md").read_text())

    def test_model_refresh_when_new_version_appears(self):
        self.run_install("--codex", "--project", str(self.proj))
        mm.codex_available_models = lambda *a, **k: FAKE_CODEX + [
            {"slug": "gpt-6.2-luna", "family": "luna", "version": (6, 2)}]
        rc, out = self.run_install("--codex", "--project", str(self.proj))
        self.assertIn("updated", out)
        self.assertIn('model = "gpt-6.2-luna"', (self.proj / ".codex" / "agents" / "modou.toml").read_text())

    def test_uninstall_keeps_user_text(self):
        (self.proj / "AGENTS.md").write_text("# mine\n")
        self.run_install("--codex", "--project", str(self.proj))
        rc, out = self.run_install("--uninstall", "--codex", "--project", str(self.proj))
        self.assertEqual(rc, 0)
        self.assertFalse((self.proj / ".codex" / "agents" / "modou.toml").exists())
        self.assertEqual((self.proj / "AGENTS.md").read_text(), "# mine\n")

    def test_backups_never_overwrite_each_other(self):
        f = self.proj / "x.txt"
        f.write_text("v1")
        w = mi.Writer()
        w.write(f, "v2")
        w.write(f, "v3")  # 同一个 Writer = 同一个时间戳目录
        baks = sorted(p.read_text() for p in Path(self.env["MODOU_BACKUP_DIR"]).rglob("x.txt*"))
        self.assertEqual(baks, ["v1", "v2"])

    def test_dry_run_writes_nothing(self):
        rc, out = self.run_install("--dry-run", "--claude", "--codex", "--project", str(self.proj))
        self.assertEqual(rc, 0)
        self.assertEqual(list(self.proj.iterdir()), [])


class AdminCodexScanner(unittest.TestCase):
    def test_parse_rollouts(self):
        import modou_admin as ma
        with tempfile.TemporaryDirectory(prefix="modou-test-unit-") as d:
            sess = Path(d) / "sessions" / "2026" / "11" / "20"
            sess.mkdir(parents=True)

            def rollout(name, meta, model, usages):
                lines = [{"timestamp": "2026-11-20T01:00:00Z", "type": "session_meta", "payload": meta},
                         {"timestamp": "2026-11-20T01:00:01Z", "type": "turn_context", "payload": {"model": model}}]
                for i, u in enumerate(usages):
                    lines.append({"timestamp": "2026-11-20T01:00:0%dZ" % (i + 2), "type": "token_usage_record",
                                  "payload": {"thread_id": meta["id"], "response_id": "%s-%d" % (name, i),
                                              "usage": u}})
                (sess / ("rollout-%s.jsonl" % name)).write_text("\n".join(json.dumps(x) for x in lines))

            u = {"input_tokens": 1000, "cached_input_tokens": 400, "cache_write_input_tokens": 0,
                 "output_tokens": 100}
            rollout("main", {"id": "T1", "thread_source": "user", "source": "exec"}, "gpt-6.1-sol", [u, u])
            rollout("modou", {"id": "T2", "thread_source": "subagent", "agent_role": "modou",
                              "source": {"subagent": {"thread_spawn": {"parent_thread_id": "T1",
                                                                       "agent_role": "modou"}}}},
                    "gpt-6-luna", [u])
            rollout("explorer", {"id": "T3", "thread_source": "subagent", "agent_role": "explorer"},
                    "gpt-6-luna", [u])
            recs = ma.CodexScanner(d).records()
            self.assertEqual(len(recs), 4)
            agg = ma.aggregate(recs, None, ma.datetime.now().astimezone() + ma.timedelta(days=1),
                               "gpt-6.1-sol", "gpt-6-luna", "modou")
            self.assertEqual(agg["main"]["requests"], 2)
            self.assertEqual(agg["executor"]["requests"], 1)
            self.assertEqual(agg["executor"]["exact"], 1)
            self.assertEqual([a["type"] for a in agg["other_agents"]], ["explorer"])
            # 600 未缓存输入 + 400 缓存读 + 100 输出，按 gpt-6-luna 价
            expect = (600 * 0.10 + 400 * 0.01 + 100 * 0.50) / 1e6
            self.assertAlmostEqual(agg["executor"]["cost"], expect, places=12)
            self.assertGreater(agg["savings"], 0)


if __name__ == "__main__":
    unittest.main()
