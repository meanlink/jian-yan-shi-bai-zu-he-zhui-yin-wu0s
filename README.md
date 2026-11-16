# 检验失败组合追因

关联规则的真实业务分析小型项目。完整模拟客户、痛点、上下游、算法公式与一天工作剧本见 [BUSINESS.md](BUSINESS.md)。

## 模块与业务流程
数据接入（JSON/CSV、字段验证、批次指纹）→ 参数化分析（异步 Python worker）→ 结果复核（指标、图形、明细、解释、批准）→ 复检排程 → 定时分发（本地收件箱、CSV/JSON、SHA256）；另有业务总览和审计轨迹。
贯穿全栈的实体：Dataset 数据批次、Job 分析任务、RecheckOrder 复检工单、Report 分发计划；辅助 User、Audit、Delivery、Shift 班次、Instrument 仪器、Inspector 检验员、Downtime 停机、RecheckSchedule/RecheckClaim 排程与领工。

## 快速启动
```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```
访问 http://localhost:18211；后端文档 http://localhost:19211/docs；数据库宿主端口 17711。

### 复检排程（页面「复检排程」）
- 从**已完成分析**选择批次与检验项建复检工单；批次必须属于该分析的数据快照。
- 预排按仪器能力、仪器/检验员班次交集、单批制样时长、逐项检测时长与**制样后有效期**求最早可行时段；约束是 `先制样、后检测`，且 `检测结束 ≤ 制样结束 + 有效期`。
- 预览有 TTL；确认时整单占用**仪器与检验员**两类资源（半开区间）。并发确认由进程内互斥 + 数据库行锁串行化，同一预览或同一幂等键重试不重复占位。
- 预览过期、资源能力/班次/时长/停机/占用变化时，确认返回 409 并附带**重排后的新预览**，不会写入半截排程。
- 无可行时段时返回具体阻塞原因（缺仪器/检验员能力、班次无空闲、时长超过有效期等），工单标为阻塞。
- 仪器停机：未开工工单自动重排并作废旧领工记录；已开工工单转**异常待处置**，旧领工记录拒绝开工。停机提前恢复时可还原原排程（若原时段仍空闲），否则保留新排程。
- 中断恢复：服务启动时校验排程指针，找回仍有效的原排程；失效预览在确认时重排；确认重试按幂等键去重。
- 每次预排、确认、重排、恢复都写入 `recheck_revisions`，保留方案、资源版本、班次、占用与停机快照及依据指纹；工单详情可查看历次依据。
- 权限：负责人可维护班次/仪器/检验员、登记与恢复停机；负责人与分析员可建单、预排、确认、领工/开工/完工、处置异常；观察员只读。
`SEED_DEMO=true` 自动幂等 seed；不覆盖用户修改。手动 seed 命令可重复执行。

## 演示账号与权限
|账号|密码|真实权限|
|---|---|---|
|admin@demo.local|Demo123!|接入、执行、批准、分发、暂停、资源维护、停机登记/恢复、排程处置、读取与下载|
|analyst@demo.local|Demo123!|接入、执行、建单、预排确认、领工开工完工、异常重排、读取与下载；不能维护资源、登记停机、批准或安排分发|
|viewer@demo.local|Demo123!|只读与下载|

## 技术与本地开发
Python 3.12 / FastAPI / SQLAlchemy / NumPy / SciPy / scikit-learn；MySQL 8.0；Redis 7；Nginx；原生 HTML CSS JavaScript ES modules（用户指定）。后端所有校验、持久化和算法在服务端。无前端打包步骤。
后端：安装 backend/requirements.txt，设置 DATABASE_URL、REDIS_URL、JWT_SECRET、ARTIFACT_DIR 后在 backend/ 执行 `python -m migrations.initial`、`python -m scripts.seed_demo`、`uvicorn src.main:app`，另开 `python -m src.worker`。前端使用带 /api 反向代理的 Nginx，直接打开 index.html 不支持 API。

## 目录与工程约定
backend/src/{models,schemas,services,routes,controllers,middlewares,constants,config,utils}；backend/scripts/seed_demo.py；backend/migrations/initial.py。
frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}。
Role 枚举：admin/analyst/viewer；JobStatus：queued/running/completed/failed；RecheckOrderStatus：pending/previewed/scheduled/in_progress/completed/blocked/abnormal/cancelled；RecheckScheduleStatus 同排程状态；ClaimStatus：issued/started/completed/voided/interrupted/closed。后端 backend/src/constants/enums.py，前端 frontend/src/constants/enums.js；路由权限在 middlewares/auth.py；页面按相同角色控制操作入口。

## 环境变量与 Docker
COMPOSE_PROJECT_NAME=assay-rules；FRONTEND_PORT=18211；BACKEND_PORT=19211；DB_PORT=17711；DB_PASSWORD、JWT_SECRET 见 .env.example；SEED_DEMO 默认 true。共享命名产物卷供后端下载与 worker 写入，数据库/Redis 各持久卷。
恰好五服务均有健康检查。Redis 队列加 SQL 排队状态兜底；worker 按 next_run 生成本地收件箱文件。周期 60 秒至 30 天。分发结果为批准分析的固定快照；若需新数据重新计算并建立新计划。当前不连接邮件或企业聊天。
复检排程参数：`RECHECK_TZ_OFFSET_MINUTES`（业务时区偏移，默认 480）、`RECHECK_PREVIEW_TTL_SECONDS`（预览有效期，默认 300）、`RECHECK_HORIZON_DAYS`（搜索窗口，默认 21）、`RECHECK_DEFAULT_ITEM_MINUTES`（未配置单项检测时长，默认 10）。
错误响应、请求编号、Redis 每 IP 每分钟 240 次限流、JWT 和数据库操作审计。演示密钥与密码仅供本机模拟；生产部署前配置独立密钥与账号。

## 数据与验收
所有数据为固定随机种子生成的模拟业务数据，不能作为实际生产决策证据。项目独立算法、输入边界及业务局限详见 BUSINESS.md。测试与截图统一存放 ../../test-artifacts/gb-1012/，源码不含日志/截图/测试报告。
清理本项目演示数据：`docker compose down -v --remove-orphans`（会删除本项目命名卷）。

## License
MIT，见 LICENSE。
