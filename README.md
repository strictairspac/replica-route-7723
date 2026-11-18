# 在线加唯一索引演练

栖原站点运维组（11人）在值班交接时，演练一次不锁业务的唯一索引上线：存量扫描与业务写入并发进行，扫描出的旧值不得覆盖并发写入的新值；同一扫描批次重试不得重复入索引；撞到重复值时，在“立即拦下写入”和“照收、等冲突消除再生效”之间做选择，并量化两种代价；索引存在重复值则不得生效；索引查询只落在已装好索引且位点满足会话要求的节点上。

**这是受控在线索引模型，不连接真实 DDL 集群。** MySQL 真实保存构建快照、事件日志与证据；Python 逐事件确定性重放；后台 worker 独立重放并比对校验和。位点是命名日志谱系中的单调整数，不含真实复制协议。

## 三个问题，模型如何回答

| 问题 | 机制 | 可见证据 |
|---|---|---|
| 旧扫描会不会盖掉新值 | 扫描读构建起点的快照值；索引条目带日志版本。条目已有更高版本时，旧批次跳过该行 | 时间线中“旧值跳过”及 `existing_version > scan_version` |
| 同批重试会不会重复入索引 | 条目以行号为键；同批次同版本再次落盘计为“重试跳过”，索引条目数不变 | `scan_inserted` 与 `scan_suppressed` 分开计数 |
| 重复值怎么处理 | `block`：撞值写入被拒绝，会话位点不前移；`accept`：写入成功并记冲突，索引保持无效、查询被拒，直到重复消除 | 冲突台账、`blocked_writes` / `accepted_conflicting_writes` / `finalize_failures` |

索引生效需同时满足：全部扫描批次已应用、每条活跃行都有不小于其版本的索引条目、无重复键值。任一不满足则构建保持 `building` 或转为 `failed`，查询一律拒绝读取。

## 页面与操作

- **索引构建台账**：查看每次构建的策略、扫描进度、状态与代价；右侧表单冻结节点快照、存量行、批次大小与重复值策略。
- **构建现场**：可逐条安排 —— 扫描批次（可重复执行演练重试）、业务写入、删除（用于消除冲突）、副本追赶、节点上下线、`finalize`、索引查询；可回退到前 N 条事件重放；可入队独立校验任务；可导出完整证据 JSON。
- 查询结果会显示命中的节点、会话所需位点与该节点索引位点；未生效时显示拒绝理由。

查询节点资格（四条件同时成立）：在线、索引状态 `valid`、索引安装位点 ≥ 会话最近写位点、节点已应用日志位点 ≥ 索引就绪点。多个候选按“副本优先、延迟升序、位点降序、名称字典序”选择。副本落后时按它自己的索引位点回答，不会读到尚未应用的新值。

## 快速启动

```sh
cp .env.example .env
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```

前端 http://localhost:18247 ，后端 http://localhost:19247/docs ，MySQL localhost:17747。

|角色|账号|密码|权限|
|---|---|---|---|
|admin|admin@qiyuan.test|Demo2026!|全部写入、构建、校验与审计|
|operator|operator@qiyuan.test|Demo2026!|构建、追加事件、回退、校验、导出|
|viewer|viewer@qiyuan.test|Demo2026!|只读和导出，写 API 403|

## 一天的工作剧本

09:00 operator 登录，打开台账；09:05 用默认快照新建一个 `accept` 构建（6 行、3 批、3 节点）；09:10 依次执行批次 0、1；09:15 写入行 3 的新值，再重试批次 0，观察“重试跳过 + 旧值跳过”；09:20 写入行 4 使用与行 3 相同的值，看到冲突被照收；09:25 `finalize` 失败，原因是重复索引值，查询被拒绝；09:30 删除行 4，再次 `finalize`，索引生效；09:35 让南站副本追赶到就绪位点，查询新值命中副本；09:40 把北站副本只追到更低位点，查询旧值命中北站、查询新值未命中；09:45 入队独立校验，10:00 admin 查看审计与证据 JSON。

种子数据包含 `accept` 与 `block` 两个对照构建，分别展示两种重复值策略的代价。

## 真实计算规则

- 事件字段：`at_ms`、`kind`（scan/write/delete/catch_up/finalize/query/node_down/node_up）、`session`，以及按类型需要的 `batch_no`、`row_id`、`value`、`node`、`position`。
- 构建快照：1–12 个节点（恰好一个主库），最多 500 条存量行；批次大小 1–100；单构建最多 200 条编排事件。
- 写入：主库在线才成功，位点 +1，行版本更新为新位点，索引条目同步写入新值。失败写入不推进位点与会话位点。
- 删除：主库在线时位点 +1，行标记删除并移除索引条目，用于消除重复值。
- 扫描：读取快照值。条目已存在更高版本 → 旧值跳过；同批次同版本重试 → 重试跳过；已删除行 → 删除跳过；否则安装条目。
- `finalize`：覆盖不全 → 保持构建中；条目缺失 → 失败；有活跃重复值 → 失败并列出冲突；否则主库索引生效，记录就绪位点，副本标记为安装中。
- `catch_up`：仅副本、不可回退、不得超过主库已提交位点。索引生效后追到就绪位点的副本变为可查询。
- 查询：索引未生效一律拒绝；否则在合格节点中按规则选一个，并按该节点的索引位点解析命中，返回行号与版本。
- 所有输入与状态均为 JSON，确定性重放，相同快照与事件产生相同 SHA256 摘要；不依赖随机数与外部网络。

## 工程说明

|服务|技术|职责|
|---|---|---|
|frontend|原生 HTML/CSS/JS + Nginx|构建台账、构建现场、共享组件、/api 代理|
|backend|Python3.11/FastAPI/SQLAlchemy/PyJWT|校验、权限、审计、快照与确定性重放|
|worker|Python3.11|独立重放校验与证据文件落盘|
|db|MySQL8.0|真实持久化构建快照、事件、结果与审计|
|frontend 代理|Nginx|/api 同源转发|

核心实体 `Build`（冻结快照 + 事件日志 + 最新状态/摘要）与 `BuildJob`（独立校验）位于 `backend/src/models/entities.py`；规则引擎在 `backend/src/services/online_index.py`，服务层 `builds.py` 负责持久化，路由 `routes/builds.py` 暴露 API。前端按 `api/stores/pages/components/hooks/utils/types/router` 分层。

本地开发（无需 Docker）：

```sh
cd backend
python3 -m pip install -r requirements.txt -r tests/requirements-dev.txt
DATABASE_URL=sqlite:///./local.db uvicorn src.main:app --port 8000
python -m src.worker            # 另一个终端，独立校验任务
python scripts/preview.py       # 打开 http://127.0.0.1:8080 ，代理 /api 到 8000
python -m pytest tests -q
```

`DATABASE_URL` 支持 MySQL 与 SQLite；compose 默认 MySQL。停止清空：`docker compose down -v --remove-orphans`。

限制：单 worker 串行；不模拟真实 DDL 锁、在线 rebase、网络抖动或自动选主；位点位模型不实现完整 GTID 集合。Google Fonts 离线回退，业务不依赖字体网络。

## License

MIT。
