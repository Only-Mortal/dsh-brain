# NEXT-STEPS 2.1 核验报告（2026-10-29）

> 基线：`3fd7176`，核验后新增提交见 git log。
> 方法：为每一项写**能失败的**测试（`tests/test_verification.py`），先让它红，再修，再看它绿。
> 结果：六项全部通过；其中 **三项一开始是失败的**，对应三个真 bug，已修。

## 逐项结果

| # | 核验项 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1 | 跨域矛盾不互相改写 | 失败 → 已修 | `consolidate.py` 主流程矛盾循环补 `a.domain != b.domain` 跳过；`test_private_domains_do_not_dispute_each_other`（在 `test_security_findings.py`） |
| 2 | 实时与恢复重叠 | 通过 | 4 个 worker 同时 `process_pending` 同一事件：事件 processed 1 次、outbox 恰好 1 条、结果列表里只有 1 个 `processed` |
| 3 | 状态并发合并 | **失败 → 已修** | 修前：一条「谢谢」social=0.5，两条并发仍是 **0.5**（丢了一次增量）。修后 **0.6** |
| 4 | 升级保真 | **失败 → 已修** | 修前：老库重建后 `cluster_id` 变成 NULL（M2 字段只在 ALTER 清单里，没写进建表语句，重建时被当新列丢掉）。修后老行字段、版本链、访问域全在 |
| 5 | 证据与版本身份 | 通过 | 同一批证据换 seed 再跑，第二轮 Trait 提案全部被拒（证据指纹绑定证据而非抽样） |
| 6 | 完整 Web 使用路径 | 通过 | 带令牌：`POST /api/ingest` 200、`GET /api/state` 返回 stats；不带令牌 401；外部 Origin 401 |

## 修掉的三个真问题

1. **情绪并发丢增量**（V3）
   `_handle` 在事务外读情绪快照，事务里把整份快照写回；两个事件并发时后写的覆盖先写的。
   修法：推理仍用快照，落库时在事务内**重新读取最新值再加一次增量**。
   验证：一条 0.5、两条并发 0.6（修前两者都是 0.5）。

2. **重建表丢 M2 字段**（V4）
   `memories` 的 M2 列（`cluster_id`、`structured_claim`、`support_count` 等）只存在于 ALTER 迁移清单，没进 `CREATE TABLE`。
   `_ddl_for()` 从 SCHEMA 取建表语句，于是重建出的新表没有这些列，拷贝时被判为"新字段"而丢失。
   修法：把这些列写进 SCHEMA 的建表语句；ALTER 清单保留作为老库兜底。
   教训：**任何只靠 ALTER 加上的列，都必须在建表语句里有对等定义**，否则重建会静默丢数据。

3. **并发建库报 database is locked**（V2/V3 暴露）
   每个 `db.connect()` 都跑一遍建表脚本抢写锁，多线程同时连会失败。
   修法：先查 `meta.schema_version`，已是当前版本就跳过建表；仍需建库时对 locked/busy 做指数退避重试。

## 复核方式

```
python -m unittest tests.test_verification -v   # 7 条
python -m unittest discover -s tests            # 全量
```

## 尚未覆盖（留给下一阶段）

- 崩溃点注入：进程在「领取后、提交前」被杀，恢复后是否只产生一个结果。
- 状态层 cognitive/affective 之外的一致性（trait 由巩固任务写，尚未与并发事件交叉）。
- Web 页面本身（HTML/JS）未在浏览器里跑，只验了 API 路径。

## 阶段 A 追加：备份与恢复（ackup.py）

- 快照用 SQLite 官方 ackup() API，并发写入时也能过完整性检查（有测试）。
- 清单字段：备份 ID、UTC 时间、代码修订、schema 版本、算法版本、实例范围、文件 SHA-256、输入水位、格式版本；不含令牌。
- 校验：清单存在 → 哈希匹配 → integrity_check → schema 版本不高于当前代码。
- 恢复：必须 --confirm；先校验，再拷到 <target>.restore-tmp 做完整性检查，通过后才换名，旧库保留为 <target>.pre-restore-<UTC>。
- 备份**不改源库**：旧库若还是 schema 0，清单就记 0，恢复时 
eeds_upgrade=true，由首次 db.connect() 完成升级。
- 命令：ackup --dest DIR、erify-backup --from DIR、estore --from DIR [--to PATH] --confirm。
- 测试：	ests/test_backup.py 10 条（一致性、篡改拒绝、缺清单拒绝、目标非空拒绝、confirm 必填、旧库保留、数据完整）。