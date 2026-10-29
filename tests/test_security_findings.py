"""安全审计（codex-security, 1fe7f9e）发现的回归测试。

- finding 1：混合隐私范围聚类 → 派生语义记忆扩大读者集合（两种顺序都要挡住）
- finding 2：回环服务的 Origin / Host / Content-Type 校验
- finding 3：持久化簇按访问域隔离，不跨主体复用
- finding 4：矛盾判定不跨访问域改写别人的可信状态
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
import sys
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from newborn import consolidate, db  # noqa: E402
from newborn import memory as memory_mod  # noqa: E402
from newborn import webui  # noqa: E402
from newborn.memory import AccessContext  # noqa: E402

NOW = int(time.time() // 60) * 60


def epi(conn, text, *, actor, privacy, ts, conv="c1"):
    import uuid
    mid = "epi_" + uuid.uuid4().hex[:10]
    conn.execute(
        "INSERT INTO memories (memory_id, instance_id, conversation_id, subject_ids,"
        " memory_type, content, source_event_ids, provenance, epistemic_status, confidence,"
        " importance, salience, occurred_at, created_at, updated_at, lifecycle_state,"
        " privacy_scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, "default", conv, json.dumps([actor], ensure_ascii=False), "episodic", text,
         json.dumps([mid], ensure_ascii=False), "user_statement", "reported",
         0.6, 0.4, 0.5, ts, ts, ts, "active", privacy))
    return mid


def as_list(value):
    if isinstance(value, list):
        return value
    return json.loads(value or "[]")


def retrieve(conn, who, conv="c1"):
    return memory_mod.retrieve(conn, AccessContext(
        instance_id="default", conversation_id=conv, subject_id=who))


class Finding1MixedScopeTest(unittest.TestCase):
    """Alice 私有 + Bob 公开/实例，两种顺序都不能让 Bob 读到 Alice 样本。"""

    def setUp(self) -> None:
        self.conn = db.connect(":memory:")

    def _seed(self, alice_first: bool) -> dict:
        rows = [("alice", "private", "SECRET_A 我私下的秘密"),
                ("bob", "public", "SECRET_A 我私下的秘密")]
        if not alice_first:
            rows.reverse()
        ids = {"alice": set(), "bob": set()}
        for i, (who, scope, text) in enumerate(rows):
            mid = epi(self.conn, text, actor=who, privacy=scope, ts=NOW - i * 10)
            ids[who].add(mid)
        return ids

    def _check(self, alice_first: bool) -> None:
        ids = self._seed(alice_first)
        res = consolidate.consolidate(self.conn, "default", window_hours=2, seed=1,
                                      min_days=1)
        self.assertEqual(res["status"], "done")
        for r in retrieve(self.conn, "bob"):
            if r["memory_type"] != "semantic":
                continue
            src = set(as_list(r["source_event_ids"]))
            self.assertTrue(src <= ids["bob"],
                            "Bob 看到的语义记忆混进了别人的来源事件：%s" % (src - ids["bob"]))
        for r in retrieve(self.conn, "alice"):
            if r["memory_type"] != "semantic" or r["privacy_scope"] != "private":
                continue          # 公开的那条本来就人人可见，不算问题
            self.assertEqual(as_list(r["subject_ids"]), ["alice"],
                             "私有语义的读者集合不能扩大")

    def test_alice_first(self) -> None:
        self._check(True)

    def test_bob_first(self) -> None:
        self._check(False)

    def test_no_cluster_mixes_scopes(self) -> None:
        self._seed(True)
        res = consolidate.consolidate(self.conn, "default", window_hours=2, seed=2,
                                      min_days=1)
        self.assertGreaterEqual(res["clusters"], 2,
                                "private 与 public 必须分成不同访问域")


class Finding3DomainReuseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = db.connect(":memory:")

    def test_same_text_different_domains_stay_separate(self) -> None:
        for i in range(3):
            epi(self.conn, f"今天很想躺平 第{i}次", actor="alice", privacy="private",
                ts=NOW - i * 60)
            epi(self.conn, f"今天很想躺平 第{i}次", actor="bob", privacy="private",
                ts=NOW - i * 60 - 30)
        res = consolidate.consolidate(self.conn, "default", window_hours=2, seed=3,
                                      min_days=1)
        self.assertEqual(res["status"], "done")
        sem = self.conn.execute(
            "SELECT cluster_id, subject_ids FROM memories WHERE memory_type='semantic'"
        ).fetchall()
        by_cluster = {}
        for row in sem:
            by_cluster.setdefault(row["cluster_id"], set()).update(
                json.loads(row["subject_ids"]))
        for cid, subjects in by_cluster.items():
            self.assertEqual(len(subjects), 1, f"簇 {cid} 混进了多个主体：{subjects}")


class Finding2WebOriginTest(unittest.TestCase):
    def _stub(self, headers: dict) -> types.SimpleNamespace:
        s = types.SimpleNamespace()
        s.ALLOWED_ORIGINS = webui.Handler.ALLOWED_ORIGINS
        s.headers = headers
        return s

    def test_local_origin_ok(self) -> None:
        self.assertTrue(webui.Handler._origin_ok(
            self._stub({"Origin": "http://127.0.0.1:8765", "Host": "127.0.0.1:8765"})))

    def test_foreign_origin_blocked(self) -> None:
        self.assertFalse(webui.Handler._origin_ok(
            self._stub({"Origin": "https://evil.example", "Host": "127.0.0.1:8765"})))

    def test_dns_rebinding_host_blocked(self) -> None:
        self.assertFalse(webui.Handler._origin_ok(
            self._stub({"Host": "evil.example:8765"})))

    def test_cli_exposes_token_flag(self) -> None:
        """审计 finding 2 点名的启动缺陷：main 必须定义 --token。"""
        import inspect
        src = inspect.getsource(webui.main)
        self.assertIn("--token", src)


class Finding4CrossDomainDisputeTest(unittest.TestCase):
    """跨访问域不许互相改写可信状态（finding 4 的残留：主流程矛盾循环）。"""

    def setUp(self) -> None:
        self.conn = db.connect(":memory:")

    def test_private_domains_do_not_dispute_each_other(self) -> None:
        for i in range(3):
            epi(self.conn, f"我喜欢开会 第{i}次", actor="alice", privacy="private",
                ts=NOW - i * 60)
            epi(self.conn, f"我不喜欢开会 第{i}次", actor="bob", privacy="private",
                ts=NOW - i * 60 - 20)
        res = consolidate.consolidate(self.conn, "default", window_hours=2, seed=4,
                                      min_days=1)
        self.assertEqual(res["status"], "done")
        rows = self.conn.execute(
            "SELECT subject_ids, epistemic_status FROM memories WHERE memory_type='semantic'"
        ).fetchall()
        for row in rows:
            self.assertNotEqual(row["epistemic_status"], "disputed",
                                "不同主体的私有语义不能互相判 disputed：%s" % row["subject_ids"])


if __name__ == "__main__":
    unittest.main()
