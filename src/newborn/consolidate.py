"""巩固任务（皮层）——按 2026-10-28 审计意见修订。

关键约束（与审计一一对应）：
- 语义记忆继承成员里**最严格**的隐私范围；不同主体/会话的私有经历不聚进同一簇（#1）。
- 主体匹配走 json_each 精确相等（在 memory.py，#2）。
- 模型只生成 display_text，规范命题（content）由规则生成（#4）。
- 任务键包含水位、抽样配置与样本指纹（#5）。
- Trait 提案带证据指纹，同一批证据只学一次（#6）。
- 语义版本链：新记录 version = 旧 + 1，supersedes_id 指向旧记录（#7）。
- 模型调用全部在事务外，事务内只做验证后的写入（#8）。
- structured_claim 携带完整主体/时间分布与来源事件（#10）。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .db import NestedTx, Tx, dumps, loads
from .inference import Inference, RuleBackend

ALGORITHM_VERSION = "consolidate-2"
RULE_VERSION = "trait-rule-2"
SHINGLE_N = 3
FEATURE_BITS = 64
CLUSTER_THRESHOLD = 0.50
CONFLICT_TOPIC_THRESHOLD = 0.45
DEFAULT_PER_DAY = 20
DEFAULT_PER_CONV = 20
MAX_LAYER_SHARE = 0.55
MIN_DAYS = 2
MIN_CONVS = 1
TRAIT_DIMS = ("curiosity", "sociability", "stability")
TRAIT_STEP_CAP = 0.05
TRAIT_DAY_CAP = 0.10

NEGATIONS = ("不", "没", "别", "无", "never", "not", "no ")

# 隐私严格度：数值越小越严格
PRIVACY_RANK = {"private": 0, "conversation": 1, "instance": 2, "public": 3}


# --------------------------------------------------------------------- 特征

def _normalize(text: str) -> str:
    t = re.sub(r"\s+", "", (text or "").lower())
    return re.sub(r"\d+", "#", t)


def shingles(text: str) -> set[int]:
    t = _normalize(text)
    if len(t) < SHINGLE_N:
        t = (t + " " * SHINGLE_N)[:SHINGLE_N]
    out: set[int] = set()
    for i in range(len(t) - SHINGLE_N + 1):
        h = hashlib.blake2b(t[i:i + SHINGLE_N].encode("utf-8"), digest_size=8).digest()
        out.add(int.from_bytes(h, "big") % (1 << FEATURE_BITS))
    return out


def strip_negation(text: str) -> str:
    t = (text or "").lower()
    for n in NEGATIONS:
        t = t.replace(n, "")
    return t


def topic_shingles(text: str) -> set[int]:
    """去掉否定词后的主题指纹：用来判断「是不是在说同一件事但极性相反」。"""
    return shingles(strip_negation(text))


def jaccard(a: Iterable[int], b: Iterable[int]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def neg_count(text: str) -> int:
    """数否定词出现次数（同一句几个算几个）。"""
    t = (text or "").lower()
    return sum(t.count(n) for n in NEGATIONS)


def polarity(text: str) -> int:
    """极性 = 否定次数的奇偶：偶数次（含 0）为正向，奇数为负向。

    这样「我不喜欢开会」是负向，「我没有不喜欢开会」是正向，
    「我不讨厌开会」也归正向（否定 + 负面词 = 双重否定）。
    这是粗规则，反讽、反问、引述他人否定都识别不了，见 #12 待办。
    """
    return -1 if neg_count(text) % 2 == 1 else 1


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------- 采样

@dataclass
class SampleResult:
    rows: list[dict]
    distribution: dict[str, Any]
    watermark: float

    def to_dict(self) -> dict[str, Any]:
        return {"count": len(self.rows), "distribution": self.distribution,
                "watermark": self.watermark}


def sample_episodics(conn, instance_id: str, window_start: float, window_end: float, *,
                     seed: int = 7, per_day: int = DEFAULT_PER_DAY,
                     per_conv: int = DEFAULT_PER_CONV) -> SampleResult:
    rows = conn.execute(
        "SELECT memory_id, content, created_at, conversation_id, subject_ids,"
        " source_event_ids, privacy_scope"
        " FROM memories WHERE instance_id = ? AND memory_type = 'episodic'"
        " AND lifecycle_state IN ('active','candidate')"
        " AND created_at >= ? AND created_at <= ?",
        (instance_id, window_start, window_end),
    ).fetchall()
    buckets_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        d = dict(r)
        buckets_day[day_of(d["created_at"])].append(d)

    rng = random.Random(seed)
    picked: list[dict] = []
    day_counts: Counter = Counter()
    conv_counts: Counter = Counter()
    for day in sorted(buckets_day):
        items = buckets_day[day][:]
        rng.shuffle(items)
        taken = 0
        for it in items:
            conv = it.get("conversation_id") or "-"
            if taken >= per_day or conv_counts[conv] >= per_conv:
                continue
            picked.append(it)
            taken += 1
            day_counts[day] += 1
            conv_counts[conv] += 1
    rng.shuffle(picked)

    total = len(picked)
    dist = {
        "by_day": dict(day_counts),
        "by_conversation": dict(conv_counts),
        "days": len(day_counts),
        "conversations": len(conv_counts),
        "max_day_share": round(max(day_counts.values()) / total, 3) if total else 0.0,
        "max_conversation_share": round(max(conv_counts.values()) / total, 3) if total else 0.0,
        "per_day_cap": per_day,
        "per_conversation_cap": per_conv,
        "seed": seed,
        "algorithm_version": ALGORITHM_VERSION,
    }
    watermark = conn.execute(
        "SELECT MAX(received_at) FROM events WHERE instance_id = ?", (instance_id,)
    ).fetchone()[0] or 0.0
    return SampleResult(rows=picked, distribution=dist, watermark=float(watermark))


def check_sampling(dist: dict[str, Any], *, min_days: int = MIN_DAYS,
                   max_layer_share: float = MAX_LAYER_SHARE) -> list[str]:
    problems: list[str] = []
    total = sum(dist["by_day"].values()) if dist["by_day"] else 0
    if total == 0:
        return problems
    days = max(1, dist["days"])
    convs = max(1, dist["conversations"])
    day_limit = max(max_layer_share, 1.0 / days + 0.1)
    conv_limit = max(max_layer_share, 1.0 / convs + 0.1)
    if days < min_days:
        problems.append(f"覆盖天数不足：{days} < {min_days}")
    if dist["conversations"] < MIN_CONVS:
        problems.append("没有任何会话维度")
    if dist["max_day_share"] > day_limit and days > 1:
        problems.append(f"单日占比过高：{dist['max_day_share']} > {round(day_limit, 3)}")
    if dist["max_conversation_share"] > conv_limit and convs > 1:
        problems.append(f"单会话占比过高：{dist['max_conversation_share']} > {round(conv_limit, 3)}")
    return problems


def sample_fingerprint(rows: list[dict], per_day: int, per_conv: int) -> str:
    ids = sorted(str(r["memory_id"]) for r in rows)
    raw = f"{per_day}|{per_conv}|" + "|".join(ids)
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=12).hexdigest()


# --------------------------------------------------------------------- 聚类

@dataclass
class Cluster:
    members: list[dict]
    features: set[int]
    support: int = 0
    independent_sources: int = 0
    subject_distribution: dict[str, int] = field(default_factory=dict)
    time_distribution: dict[str, int] = field(default_factory=dict)
    polarity: Counter = field(default_factory=Counter)
    privacy_scope: str = "public"
    domain: tuple = ("instance",)
    conversation_id: str | None = None
    subject_ids: list[str] = field(default_factory=list)
    topic_features: set[int] = field(default_factory=set)

    def stats(self) -> dict[str, Any]:
        return {
            "support_count": self.support,
            "independent_source_count": self.independent_sources,
            "subject_distribution": dict(self.subject_distribution),
            "time_distribution": dict(self.time_distribution),
            "polarity": dict(self.polarity),
            "privacy_scope": self.privacy_scope,
        }


def _tighten(a: str, b: str) -> str:
    return a if PRIVACY_RANK[a] <= PRIVACY_RANK[b] else b


def domain_key(cluster_or_domain) -> str:
    """把访问域序列化成稳定的 JSON 串（集合转列表）。"""
    dom = getattr(cluster_or_domain, "domain", cluster_or_domain)
    return dumps([sorted(x) if isinstance(x, frozenset) else x for x in dom])


def access_domain(row: dict) -> tuple:
    """一条记忆的访问域，聚类与派生都必须按它分区。

    设计原则（审计 finding 1）：派生对象的读者集合不得大于任何来源的读者集合，
    所以只有**完全相同的域**才允许聚在一起；宽范围行不能挤进受限域，
    受限行也不能挤进宽范围域（对称）。
    """
    scope = row.get("privacy_scope") or "instance"
    if scope == "private":
        return ("private", frozenset(loads(row["subject_ids"]) or []))
    if scope == "conversation":
        return ("conversation", row.get("conversation_id") or "")
    if scope == "public":
        return ("public",)
    return ("instance",)


def _compatible(a: dict, members: list[dict]) -> bool:
    """对称兼容：域必须与簇内所有成员一致。"""
    dom = access_domain(a)
    return all(access_domain(m) == dom for m in members)


def build_clusters(rows: list[dict], threshold: float = CLUSTER_THRESHOLD) -> list[Cluster]:
    clusters: list[Cluster] = []
    for row in rows:
        feats = shingles(row["content"])
        best, best_sim = None, 0.0
        for c in clusters:
            if not _compatible(row, c.members):
                continue
            sim = jaccard(feats, c.features)
            if sim > best_sim:
                best, best_sim = c, sim
        if best is not None and best_sim >= threshold:
            best.members.append(row)
            best.features |= feats
        else:
            clusters.append(Cluster(members=[row], features=feats))

    for c in clusters:
        c.support = len(c.members)
        subjects = Counter()
        days = Counter()
        scopes = Counter()
        convs = set()
        for m in c.members:
            for sid in loads(m["subject_ids"]) or ["unknown"]:
                subjects[str(sid)] += 1
            days[day_of(m["created_at"])] += 1
            c.polarity[polarity(m["content"])] += 1
            scopes[m.get("privacy_scope") or "instance"] += 1
            if m.get("conversation_id"):
                convs.add(m["conversation_id"])
            c.topic_features |= topic_shingles(m["content"])
        c.subject_distribution = dict(subjects)
        c.time_distribution = dict(days)
        c.independent_sources = len(subjects)
        c.privacy_scope = min(scopes, key=lambda s: PRIVACY_RANK.get(s, 9))
        c.domain = access_domain(c.members[0])
        c.conversation_id = next(iter(convs)) if len(convs) == 1 else None
        c.subject_ids = sorted(subjects) if c.privacy_scope == "private" else []
    return clusters


def snapshot_fingerprint(members: list[dict]) -> str:
    ids = sorted(str(m["memory_id"]) for m in members)
    return hashlib.blake2b("|".join(ids).encode("utf-8"), digest_size=12).hexdigest()


def member_fingerprint(members: list[dict]) -> str:
    events: list[str] = []
    for m in members:
        events.extend(str(x) for x in (loads(m["source_event_ids"]) or [m["memory_id"]]))
    return hashlib.blake2b("|".join(sorted(events)).encode("utf-8"), digest_size=12).hexdigest()


# --------------------------------------------------------------------- 命题

def source_event_ids_of(cluster: Cluster) -> list[str]:
    ids: list[str] = []
    for m in cluster.members:
        ids.extend(str(x) for x in (loads(m["source_event_ids"]) or []))
    return sorted(set(ids))


def structured_claim(cluster: Cluster) -> dict[str, Any]:
    st = cluster.stats()
    top_subject = max(st["subject_distribution"], key=st["subject_distribution"].get) \
        if st["subject_distribution"] else "unknown"
    days = sorted(st["time_distribution"])
    return {
        "kind": "pattern",
        "support_count": st["support_count"],
        "independent_source_count": st["independent_source_count"],
        "subject_distribution": st["subject_distribution"],
        "time_distribution": st["time_distribution"],
        "privacy_scope": st["privacy_scope"],
        "top_subject": top_subject,
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "sample": (cluster.members[0]["content"][:60]
                   if cluster.privacy_scope in ("instance", "public")
                   else f"（{cluster.privacy_scope} 范围内的表达，已隐藏样本）"),
        "polarity": "negative" if cluster.polarity.get(-1, 0) > cluster.polarity.get(1, 0)
                    else "positive",
        "source_event_count": len(source_event_ids_of(cluster)),
        "algorithm_version": ALGORITHM_VERSION,
    }


def template_text(claim: dict[str, Any]) -> str:
    return (f"「{claim['sample']}」这类表达出现 {claim['support_count']} 次，"
            f"来自 {claim['independent_source_count']} 个来源"
            f"（主要来自 {claim['top_subject']}，"
            f"{claim['first_day']} 至 {claim['last_day']}，"
            f"范围 {claim['privacy_scope']}）")


def model_text(claim: dict[str, Any], backend: Inference | None) -> tuple[str, str]:
    """模型只产出 display_text；失败/离线/越界一律回退模板，不影响结构。"""
    if backend is None or isinstance(backend, RuleBackend):
        return template_text(claim), "template"
    system = ("你在为一段统计结构写一句展示用描述。只陈述统计事实，"
              "不许声称有情绪、意识或主观体验；一句话，中文，不超过 60 字。")
    prompt = json.dumps(claim, ensure_ascii=False)
    res = backend.complete(system, [{"role": "user", "content": prompt}], timeout=45)
    if res.error or not res.text.strip():
        return template_text(claim), f"template_fallback:{res.error or 'empty'}"
    text = res.text.strip().replace("\n", " ")
    if any(w in text for w in ("我感觉", "我很", "我好", "我感到")):
        return template_text(claim), "template_blocked_first_person"
    return text[:120], "model"


# --------------------------------------------------------------------- 写入

def _find_cluster(conn, instance_id: str, cluster: Cluster):
    snap = snapshot_fingerprint(cluster.members)
    memfp = member_fingerprint(cluster.members)
    dom = domain_key(cluster)
    row = conn.execute(
        "SELECT * FROM clusters WHERE instance_id = ? AND status = 'active'"
        " AND access_domain = ?"
        " AND (member_fingerprint = ? OR snapshot_fingerprint = ?)"
        " ORDER BY version DESC LIMIT 1",
        (instance_id, dom, memfp, snap),
    ).fetchone()
    if row is None:
        best, best_sim = None, 0.0
        for cand in conn.execute(
            "SELECT * FROM clusters WHERE instance_id = ? AND status = 'active'"
            " AND access_domain = ?", (instance_id, dom)
        ):
            sim = jaccard(set(loads(cand["features"]) or []), cluster.features)
            if sim > best_sim:
                best, best_sim = cand, sim
        if best is not None and best_sim >= CLUSTER_THRESHOLD:
            row = best
    return row


def _upsert_cluster(conn, instance_id: str, cluster: Cluster) -> tuple[str, str, int, int]:
    """返回 (cluster_id, snapshot_fp, version, 旧支持数)。"""
    snap = snapshot_fingerprint(cluster.members)
    memfp = member_fingerprint(cluster.members)
    stats = cluster.stats()
    ts = time.time()
    row = _find_cluster(conn, instance_id, cluster)
    if row:
        version = row["version"] if row["snapshot_fingerprint"] == snap else row["version"] + 1
        conn.execute(
            "UPDATE clusters SET snapshot_fingerprint = ?, member_fingerprint = ?, version = ?,"
            " members = ?, support_count = ?, independent_source_count = ?,"
            " subject_distribution = ?, time_distribution = ?, features = ?,"
            " access_domain = ?, updated_at = ? WHERE cluster_id = ?",
            (snap, memfp, version, dumps([m["memory_id"] for m in cluster.members]),
             stats["support_count"], stats["independent_source_count"],
             dumps(stats["subject_distribution"]), dumps(stats["time_distribution"]),
             dumps(sorted(cluster.features)), domain_key(cluster), ts,
             row["cluster_id"]),
        )
        return row["cluster_id"], snap, version, int(row["support_count"] or 0)

    cluster_id = "clu_" + uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO clusters (cluster_id, instance_id, algorithm_version, snapshot_fingerprint,"
        " member_fingerprint, version, members, support_count, independent_source_count,"
        " subject_distribution, time_distribution, features, access_domain,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cluster_id, instance_id, ALGORITHM_VERSION, snap, memfp, 1,
         dumps([m["memory_id"] for m in cluster.members]),
         stats["support_count"], stats["independent_source_count"],
         dumps(stats["subject_distribution"]), dumps(stats["time_distribution"]),
         dumps(sorted(cluster.features)), domain_key(cluster), ts, ts),
    )
    return cluster_id, snap, 1, 0


def _previous_semantic(conn, cluster_id: str):
    return conn.execute(
        "SELECT memory_id, version FROM memories WHERE cluster_id = ?"
        " AND memory_type = 'semantic' AND lifecycle_state = 'active'"
        " ORDER BY version DESC LIMIT 1", (cluster_id,)).fetchone()


def _insert_semantic(conn, instance_id: str, cluster: Cluster, claim: dict, canonical: str,
                     display: str, change_kind: str, watermark: float,
                     prev) -> str:
    mem_id = "sem_" + uuid.uuid4().hex[:12]
    ts = time.time()
    version = (prev["version"] + 1) if prev else 1
    supersedes = prev["memory_id"] if prev else None
    conn.execute(
        "INSERT INTO memories (memory_id, instance_id, conversation_id, subject_ids, memory_type,"
        " content, display_text, source_event_ids, provenance, epistemic_status, confidence,"
        " importance, salience, occurred_at, created_at, updated_at, lifecycle_state,"
        " privacy_scope, cluster_id, structured_claim, support_count, independent_source_count,"
        " subject_distribution, time_distribution, contradicting_event_ids, algorithm_version,"
        " input_watermark, change_kind, version, supersedes_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mem_id, instance_id, cluster.conversation_id, dumps(cluster.subject_ids), "semantic",
         canonical, display, dumps(source_event_ids_of(cluster)),
         "consolidation", "inferred", 0.5,
         min(1.0, 0.3 + 0.05 * claim["support_count"]), 0.5,
         ts, ts, ts, "active",
         cluster.privacy_scope,                        # ← 继承最严范围
         claim.get("cluster_id"), dumps(claim), claim["support_count"],
         claim["independent_source_count"], dumps(claim["subject_distribution"]),
         dumps(claim["time_distribution"]), "[]",
         ALGORITHM_VERSION, watermark, change_kind, version, supersedes),
    )
    if prev:
        conn.execute(
            "UPDATE memories SET lifecycle_state = 'superseded', updated_at = ? WHERE memory_id = ?",
            (ts, prev["memory_id"]),
        )
    return mem_id


# --------------------------------------------------------------------- Trait

def evidence_fingerprint(cluster_id: str, member_fp: str, rule_version: str) -> str:
    raw = f"{cluster_id}|{member_fp}|{rule_version}"
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=12).hexdigest()


def propose_trait_deltas(cluster_stats: list[dict[str, Any]]) -> dict[str, float]:
    """注意：这仍然只是「数据覆盖度」代理，不是人格测量。

    规则只给极小的提案量，真正的性格变化要靠长期稳定的行为结果与反馈，
    见 README「已知问题」KN-04。
    """
    if not cluster_stats:
        return {}
    total = sum(c["support_count"] for c in cluster_stats) or 1
    sources = sum(c["independent_source_count"] for c in cluster_stats)
    days = len({d for c in cluster_stats for d in c["time_distribution"]})
    social = min(1.0, sources / (total + 1))
    span = min(1.0, days / 5)
    return {
        "sociability": round(0.03 * social, 4),
        "stability": round(0.02 * span, 4),
        "curiosity": 0.0,
    }


def apply_trait_proposal(conn, instance_id: str, dim: str, delta: float, *,
                         cluster_id: str | None, evidence: list[str],
                         evidence_fp: str | None = None) -> dict[str, Any]:
    """事务化记账：单次 ≤0.05，当日绝对变化之和 ≤0.10；同一批证据只学一次。"""
    if dim not in TRAIT_DIMS:
        raise ValueError(f"未知 trait 维度: {dim}")
    if not isinstance(delta, (int, float)) or delta != delta or abs(delta) == float("inf"):
        raise ValueError("delta 必须是有限数值")
    day = day_of(time.time())

    with NestedTx(conn):
        if evidence_fp:
            dup = conn.execute(
                "SELECT proposal_id FROM trait_proposals WHERE instance_id = ? AND dim = ?"
                " AND evidence_fingerprint = ? AND rule_version = ? AND status = 'applied'",
                (instance_id, dim, evidence_fp, RULE_VERSION)).fetchone()
            if dup:
                return {"dim": dim, "delta": delta, "applied": False,
                        "reason": "同一批证据已经学过"}
        state_row = conn.execute(
            "SELECT data FROM states WHERE instance_id = ? AND layer = 'trait' AND scope = 'default'",
            (instance_id,)).fetchone()
        before = (loads(state_row["data"]).get(dim, 0.0) if state_row else 0.0)
        proposal_id = "tp_" + uuid.uuid4().hex[:10]

        def reject(reason: str) -> dict[str, Any]:
            conn.execute(
                "INSERT INTO trait_proposals (proposal_id, instance_id, dim, before_value, delta,"
                " after_value, cluster_id, evidence, rule_version, evidence_fingerprint, status,"
                " reject_reason, day, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,'rejected',?,?,?)",
                (proposal_id, instance_id, dim, before, delta, before, cluster_id,
                 dumps(evidence), RULE_VERSION, evidence_fp, reason, day, time.time()),
            )
            return {"dim": dim, "delta": delta, "applied": False, "reason": reason}

        if abs(delta) > TRAIT_STEP_CAP + 1e-9:
            return reject(f"单次变化超限：{abs(delta):.3f} > {TRAIT_STEP_CAP}")
        used = conn.execute(
            "SELECT used_abs FROM trait_ledger WHERE instance_id = ? AND day = ? AND dim = ?",
            (instance_id, day, dim)).fetchone()
        used_abs = used["used_abs"] if used else 0.0
        if used_abs + abs(delta) > TRAIT_DAY_CAP + 1e-9:
            return reject(f"当日累计超限：{used_abs + abs(delta):.3f} > {TRAIT_DAY_CAP}")

        after = max(-1.0, min(1.0, before + delta))
        data = loads(state_row["data"]) if state_row else {}
        data[dim] = round(after, 4)
        vrow = conn.execute(
            "SELECT version FROM states WHERE instance_id = ? AND layer='trait' AND scope='default'",
            (instance_id,)).fetchone()
        version = (vrow["version"] + 1) if vrow else 1
        ts = time.time()
        conn.execute(
            "INSERT INTO states (instance_id, layer, scope, version, updated_at, source_event_id, data)"
            " VALUES (?, 'trait', 'default', ?, ?, NULL, ?)"
            " ON CONFLICT(instance_id, layer, scope) DO UPDATE SET version=excluded.version,"
            " updated_at=excluded.updated_at, data=excluded.data",
            (instance_id, version, ts, dumps(data)),
        )
        conn.execute(
            "INSERT INTO trait_ledger (instance_id, day, dim, used_abs) VALUES (?,?,?,?)"
            " ON CONFLICT(instance_id, day, dim) DO UPDATE SET used_abs = used_abs + excluded.used_abs",
            (instance_id, day, dim, abs(delta)),
        )
        conn.execute(
            "INSERT INTO trait_proposals (proposal_id, instance_id, dim, before_value, delta,"
            " after_value, cluster_id, evidence, rule_version, evidence_fingerprint, status,"
            " reject_reason, day, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,'applied',NULL,?,?)",
            (proposal_id, instance_id, dim, before, delta, after, cluster_id,
             dumps(evidence), RULE_VERSION, evidence_fp, day, ts),
        )
        return {"dim": dim, "delta": delta, "applied": True, "before": before, "after": after}


# --------------------------------------------------------------------- 主流程

def consolidate(conn, instance_id: str = "default", *, window_hours: float = 24.0,
                seed: int = 7, per_day: int = DEFAULT_PER_DAY,
                per_conv: int = DEFAULT_PER_CONV, backend: Inference | None = None,
                now: float | None = None, min_days: int = MIN_DAYS) -> dict[str, Any]:
    now = now if now is not None else (int(time.time() // 60) * 60.0)
    start = now - window_hours * 3600
    sample = sample_episodics(conn, instance_id, start, now, seed=seed,
                              per_day=per_day, per_conv=per_conv)
    if not sample.rows:
        return {"status": "empty", "sampled": sample.to_dict()}

    gate = check_sampling(sample.distribution, min_days=min_days)
    if gate:
        return {"status": "sampling_rejected", "problems": gate, "sampled": sample.to_dict()}

    sfp = sample_fingerprint(sample.rows, per_day, per_conv)
    job_key = hashlib.blake2b(
        f"{instance_id}|{start}|{now}|{seed}|{ALGORITHM_VERSION}|{sample.watermark}|"
        f"{per_day}|{per_conv}|{sfp}".encode("utf-8"), digest_size=16).hexdigest()
    existing = conn.execute(
        "SELECT job_id, produced FROM consolidation_jobs WHERE job_key = ?", (job_key,)).fetchone()
    if existing:
        return {"status": "duplicate_job", "job_id": existing["job_id"],
                "produced": loads(existing["produced"]), "sampled": sample.to_dict()}

    # ---- 事务外：聚类 + 命题 + 模型措辞（模型再慢也不占写锁）
    clusters = build_clusters(sample.rows)
    prepared: list[dict[str, Any]] = []
    for cluster in clusters:
        claim = structured_claim(cluster)
        display, source = model_text(claim, backend)
        prepared.append({"cluster": cluster, "claim": claim,
                         "display": display, "display_source": source,
                         "canonical": template_text(claim)})

    # ---- 事务内：只做验证后的短写入
    job_id = "job_" + uuid.uuid4().hex[:12]
    ts = time.time()
    produced: list[dict[str, Any]] = []
    traits: list[dict[str, Any]] = []

    with Tx(conn):
        cluster_stats: list[dict[str, Any]] = []
        for item in prepared:
            cluster = item["cluster"]
            claim = item["claim"]
            cluster_id, snap, version, prev_support = _upsert_cluster(conn, instance_id, cluster)
            claim["cluster_id"] = cluster_id
            if prev_support == 0:
                kind = "new"
            elif claim["support_count"] > prev_support:
                kind = "strengthened"
            elif claim["support_count"] < prev_support:
                kind = "weakened"
            else:
                kind = "strengthened"
            # 同一簇里既有正说又有反说 → 直接判 disputed，并记下反证来源
            disputed_ids: list[str] = []
            if len([p for p, c in cluster.polarity.items() if c > 0]) > 1:
                majority = max(cluster.polarity, key=cluster.polarity.get)
                for m in cluster.members:
                    if polarity(m["content"]) != majority:
                        disputed_ids.extend(str(x) for x in (loads(m["source_event_ids"]) or []))
            prev = _previous_semantic(conn, cluster_id)
            if disputed_ids:
                claim["polarity"] = "mixed"
            mem_id = _insert_semantic(conn, instance_id, cluster, claim, item["canonical"],
                                      item["display"], kind, sample.watermark, prev)
            if disputed_ids:
                conn.execute(
                    "UPDATE memories SET epistemic_status = 'disputed',"
                    " contradicting_event_ids = ? WHERE memory_id = ?",
                    (dumps(sorted(set(disputed_ids))), mem_id))
            cluster_stats.append({**cluster.stats(),
                                  "time_distribution": cluster.time_distribution})
            produced.append({
                "cluster_id": cluster_id, "memory_id": mem_id, "version": version,
                "change_kind": kind, "support_count": claim["support_count"],
                "independent_source_count": claim["independent_source_count"],
                "privacy_scope": cluster.privacy_scope,
                "display_text": item["display"], "display_source": item["display_source"],
                "snapshot_fingerprint": snap,
            })

        # 矛盾识别：先用「去否定词」的主题指纹判断是否同一件事
        for i, item in enumerate(prepared):
            for j in range(i + 1, len(prepared)):
                a, b = item["cluster"], prepared[j]["cluster"]
                # 审计 finding 4 的残留：跨访问域不许互相改写可信状态
                if a.domain != b.domain:
                    continue
                if jaccard(a.topic_features, b.topic_features) < CONFLICT_TOPIC_THRESHOLD:
                    continue
                pa = polarity(a.members[0]["content"])
                pb = polarity(b.members[0]["content"])
                if pa == pb:
                    continue
                weaker = a if len(a.members) < len(b.members) else b
                target = produced[i if weaker is a else j]
                conn.execute(
                    "UPDATE memories SET epistemic_status = 'disputed',"
                    " contradicting_event_ids = ? WHERE memory_id = ?",
                    (dumps([m["memory_id"] for m in weaker.members]), target["memory_id"]),
                )

        for dim, delta in propose_trait_deltas(cluster_stats).items():
            fp = evidence_fingerprint(produced[0]["cluster_id"] if produced else "none",
                                      sfp, RULE_VERSION)
            traits.append(apply_trait_proposal(
                conn, instance_id, dim, delta,
                cluster_id=produced[0]["cluster_id"] if produced else None,
                evidence=[p["cluster_id"] for p in produced][:5], evidence_fp=fp))

        conn.execute(
            "INSERT INTO consolidation_jobs (job_id, job_key, instance_id, window_start,"
            " window_end, seed, algorithm_version, input_watermark, sampled, produced, status,"
            " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,'done',?)",
            (job_id, job_key, instance_id, start, now, seed, ALGORITHM_VERSION,
             sample.watermark, dumps(sample.distribution),
             dumps({"clusters": len(produced), "items": produced, "traits": traits}), ts),
        )

    return {
        "status": "done",
        "job_id": job_id,
        "sampled": sample.to_dict(),
        "clusters": len(produced),
        "items": produced,
        "traits": traits,
        "identity_touched": False,
    }
