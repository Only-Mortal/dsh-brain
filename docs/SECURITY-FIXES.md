# 安全审计修复（codex-security · 1fe7f9e）

> 扫描：`1fe7f9e30e000effe4f339b0155a818cb5b30287`，范围 `src/newborn`，4 条发现（medium 1 / low 3）。
> 本文件记录处置状态；数字与代码会变，最终以 `git log` 与测试为准。

## 一、逐条处置

| # | 级别 | 结论 | 处置 |
| --- | --- | --- | --- |
| 1 | medium | **成立**。`_compatible` 是单向的：新来的 public/instance 行能挤进已有 private 簇，随后成员主体取并集，私有语义的读者被扩大（Alice→Bob）。 | 引入**访问域** `access_domain`：`(private, 主体集合)` / `(conversation, 会话)` / `(instance,)` / `(public,)`。聚类要求**域完全相同**（对称）；派生语义只继承该域；持久化簇也带域字段，跨域不复用。受限域的样本不再写进规范命题（改成"已隐藏样本"）。 |
| 2 | low | **部分成立**。回环无令牌时仅凭来源 IP 授权，不校验 Origin/Host/媒体类型；另外 `main` 读 `a.token` 却没定义 `--token`，CLI 启动会崩（这条是真 bug）。 | 补 `--token`（含环境变量）；新增 `_origin_ok()`：Origin 必须是本机来源、Host 必须是回环名（挡跨站与 DNS 重绑定）；POST 强制 `application/json`（挡 text/plain 简单请求）。 |
| 3 | low | **成立**。持久化簇按特征相似度找回时没看隐私域，别的域的历史簇可能被复用并升版本。 | `clusters` 增加 `access_domain` 列，`_find_cluster` 的指纹路径与特征路径都按域过滤。 |
| 4 | low | **成立**（同上根因）。跨域矛盾判定可能改写别人私有语义的 `disputed` 状态。 | 域隔离后，一个簇内只可能同域，矛盾判定也不跨域；簇内正反并存仍会判 disputed 并记反证。 |

## 二、新增测试（`tests/test_security_findings.py`，8 条）

- `test_alice_first` / `test_bob_first`：两种顺序都必须让 Bob 看到的语义**只来自 Bob 自己的来源事件**，私有语义的 `subject_ids` 不扩大。
- `test_no_cluster_mixes_scopes`：private 与 public 必须分成不同访问域（≥2 簇）。
- `test_same_text_different_domains_stay_separate`：同一句话、不同主体的私有，不许进同一簇。
- `test_local_origin_ok` / `test_foreign_origin_blocked` / `test_dns_rebinding_host_blocked`：Origin 与 Host 校验。
- `test_cli_exposes_token_flag`：`main` 必须定义 `--token`（防回归）。

## 三、验证

```
python -m unittest discover -s tests     # 64 passed
```

## 四、还没做（审计点出的方向，不是本次缺陷）

- 删除工作流尚不存在，"巩固写回已删除数据"目前不可达，也没有对应测试。
- 巩固的最终来源版本/删除状态未做提交前重验证（README 里有这条设计目标）。
- 推理接收方没有独立策略检查，仍是操作员配置责任。
- 威胁模型按本地单实例校准；多租户远程部署前需要重新评估。
