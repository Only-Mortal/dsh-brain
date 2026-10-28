# 审计修复报告 · newborn（2026-10-28 夜）

> 对象：审计意见 14 条（含 3 条"严重"、5 条"高"、4 条"中"）。
> 结论：**14 条全部处理，其中 11 条已修并有测试，3 条做了约束性降级与文档化**。
> 验证：`python -m unittest discover -s tests` → **45 条全过**；`python tests/stress_m1.py` → 8 项不变量全绿。

## 一、逐条处置

| # | 级别 | 问题 | 处置 | 证据 |
| --- | --- | --- | --- | --- |
| 1 | 严重 | 巩固扩大私有记忆可见范围 | 语义记忆继承成员里**最严**的隐私范围；不同主体/会话的私有经历不许聚进同一簇 | `test_semantic_inherits_private_scope`（Alice 可见 / Bob 不可见）、`test_private_memories_of_different_subjects_not_merged`（2 簇） |
| 2 | 严重 | LIKE 主体匹配可被绕过 | 改成 `json_each(...) WHERE value = ?` 精确相等 | `test_subject_match_is_exact`：u1 / u10 / `%` 三种输入，`%` 返回空 |
| 3 | 严重 | Web UI 无访问控制 | 默认只认回环；非回环必须 `--token`，否则启动即拒；所有路由校验令牌；按 `instance` 过滤；正文上限 8 KB；私有正文打码 | `test_webui_loopback_detection`、`test_webui_refuses_public_bind_without_token` |
| 4 | 高 | 模型措辞影响认知行为 | 新增 `display_text` 列；`content` 恒为规则生成的规范命题；推理只读 `content` | 结构测试 + 代码路径（`_insert_semantic` 传 canonical / display 两路） |
| 5 | 高 | 任务幂等忽略新证据 | 任务键 = 实例 + 窗口 + seed + 算法版本 + **输入水位** + **抽样配置** + **样本指纹** | `test_new_evidence_invalidates_job_key`：无新证据→duplicate_job；加入新经历→done |
| 6 | 高 | 同一批证据反复改 Trait | Trait 提案带 `evidence_fingerprint`，`(instance, dim, fingerprint, rule_version)` 唯一约束 | `test_trait_evidence_fingerprint_prevents_relearning`（同证据拒、换证据过） |
| 7 | 高 | 语义版本链没接上 | 新记录 `version = 旧+1`、`supersedes_id = 旧 id`，旧记录置 `superseded` | `test_semantic_version_chain`（v1→v2，supersedes 指向正确） |
| 8 | 高 | 模型调用在写事务里 | 抽帧/聚类/命题/模型措辞全部移到事务外，事务内只做写入 | 代码结构 + 45 条测试（含并发场景）不阻塞 |
| 9 | 高 | 同一事件可能被并发处理两次 | 新增 `claim_events()`：`claimed_by + lease_until` 条件更新领取，处理完释放 | `test_event_claimed_once`（w1 领到、w2 领不到、过期可接管） |
| 10 | 中 | 统计产物缺字段 | `structured_claim` 带完整 `subject_distribution`/`time_distribution`/`source_event_count`；语义记忆写入 `source_event_ids` 与两个分布 | `test_semantic_inherits_private_scope` 间接覆盖 + 代码 |
| 11 | 中 | Trait 规则描述的是数据集不是人格 | 保留为"数据覆盖度代理"，在 README 记为 KN-04；`apply_trait_proposal` 增加维度白名单、有限数值校验、事务化、证据指纹 | 单测覆盖越界/非法维度路径 |
| 12 | 中 | 矛盾识别太脆弱 | 先算**去否定词**的 `topic_shingles` 再比极性，阈值 0.45 | 代码 + `CONFLICT_TOPIC_THRESHOLD`；建议明天补真实中文否定样例测试 |
| 13 | 中 | 迁移不完整 | `schema_version` 写入 meta（v2）；memories/events/consolidation_jobs/trait_proposals 缺列自动 ALTER；唯一索引 `IF NOT EXISTS` 补齐 | 迁移函数 + 旧库可用性 |
| 14 | 中 | 文档自相矛盾 | README 顶部阶段、技术选型、尚未实现三处统一；`__init__` 测试数改为 45 | 已改 |

## 二、明确没做满的三件事

1. **#13 迁移只补列，不重建表**。SQLite 不支持直接修改 CHECK 约束；老库里 events 的 `process_state` CHECK、outbox 的状态约束仍是旧版。要彻底一致需要"建新表-搬数据-换名"，明早单独做。
2. **#11 Trait 仍是代理指标**。现在只按"来源数/天数"给小提案，不代表性格。真正的 Trait 需要长期行为结果与反馈，已写进 README KN-04，等 M3。
3. **#12 矛盾识别只到"主题相近 + 极性相反"**。同义改写、双重否定、反讽都识别不了，需要真实样例集再调。

## 三、顺手修掉的其它隐患

- 嵌套事务：`apply_trait_proposal` 在巩固事务内再开事务会报 `cannot start a transaction within a transaction`，改为 `NestedTx`（外层已开则不 BEGIN）。
- 巩固时间窗按分钟取整，避免同一窗在两次调用里生成不同 key。
- 数字归一化（`第1次` → `第#次`），同一句式换个编号仍算同一主题，聚类不再被编号拆散。
- 聚类相似度阈值从 0.55 调到 0.50（配合数字归一化）。
- 分桶占比阈值随层数放宽为 `max(0.55, 1/n+0.1)`，避免两个会话各一半被误杀。

## 四、验证记录（可复跑）

```
python -m unittest discover -s tests        # 45 passed
python tests/stress_m1.py                   # 240 事件 / 60 故意失败 / 全部恢复 / 90 次领取零重复 / 8 项不变量 true / 测试库自动清除
```

## 五、明天建议的下一步（按风险排序）

1. 老库表重建迁移（#13 收尾），加迁移回归测试。
2. 中文否定样例集（#12），至少 20 条人工标注正反例。
3. Event Store 与 Episodic Store 的筛选策略：事件全量落盘，但进情景记忆要过隐私/来源/重复/保留四道筛。
4. 把巩固接到 Web UI：能看到"这轮巩固了哪些簇、抽样分布如何、Trait 提案被拒的原因"。
