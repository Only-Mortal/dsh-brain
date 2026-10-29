# 变更记录

## v1.1.0 — 2026-10-30

从「能跑通」到「能维护」的一版。核心是把 V1.0 明确列为未做的几项补上，并把维护动作收进可验证的边界里。

### 新增能力

- **维护调度** `scheduler.py`：`newborn worker --once` / `--interval 600`。按预算判断「该不该巩固」，跑完记一条任务；前台事件路径不受影响。
- **物理清理** `newborn purge`：把已删除的正文真正从库里删掉（`secure_delete` + `wal_checkpoint(TRUNCATE)` + `VACUUM`），必须 `--confirm`。只承诺本文件，不承诺 WAL 之外、宿主快照或 SSD 磨损层。
- **崩溃点验收** `tests/test_v11.py`：模拟「领取后、处理中挂掉」，验证事件留在 pending、没有半条回复，租约过期后恢复只处理一次。
- **删除撤销账本回放** `newborn replay-ledger`：恢复到旧备份后重放账本，已删内容不会复活。
- **任务视图** `newborn jobs` 与 `GET /api/jobs`：窗口、seed、抽样分布、产出计数、删除请求状态；不含私有正文。

### 变更

- Trait 只接受**反馈证据**（`correct` / `reject` / `confirm`）；巩固的数据覆盖度降为指标，不再直接改状态。没有反馈时巩固会明确返回 `skipped: no_feedback_evidence`。
- 巩固提交前复核 `privacy_epoch`：抽样期间发生删除/撤权就整轮放弃，重新抽样。
- 质量评测固定验收集：开发集与验收集分开，极性准确率与矛盾判定设门槛，已知误判写死在测试里。

### 修复（来自 V1.1 开发过程中的核验）

- 并发的事件情绪增量会互相覆盖（读-改-写丢失）——改为事务内重读最新值再叠加。
- 重建表时 M2 字段丢失（列只在 ALTER 清单里、没进建表语句）——补进 SCHEMA。
- 多线程 `db.connect()` 抢写锁报 `database is locked`——先查 schema 版本，必要时退避重试。

### 验证

- `python -m unittest discover -s tests` → **102 passed**
- `python tests/stress_m1.py` → 8 项不变量全绿

### 已知边界

- 调度器是「检查-执行」的轻循环，没有工作流引擎、没有多实例协调。
- 物理清理只处理数据库文件；恢复旧备份仍需人工重放账本。
- 检索仍是关键词 + 重要性排序，没有向量索引。
- 否定规则仍是奇偶粗规则；引述、反问、反讽会误判（测试里标为已知不覆盖）。

---

## v1.0.0 — 2026-10-29

首个正式版本：事件闭环、自传式记忆与访问域、巩固皮层（分层抽样/指纹聚类/语义版本链）、情绪模型、监控台、备份恢复、删除与派生一致性、反馈驱动的 Trait、质量验收集、两轮安全审计修复。

- 测试：95 条全过。
- 详见 `docs/AUDIT-FIX-REPORT.md`、`docs/SECURITY-FIXES.md`、`docs/VERIFICATION-2026-10-29.md`、`docs/NEXT-STEPS.md`。
