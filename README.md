# 车端软件灰度发布控制

面向大规模商业化运营前的驾驶辅助软件推送场景，解决车辆在线状态、硬件批次与健康回报不同步时的安全放量问题。纯 Python 标准库实现（SQLite 持久化），服务实例无状态，进程重启后可在同一数据库文件上继续执行。

## 核心能力

- **登记**：软件包与兼容条件（硬件批次 / 可升级版本 / 最低电量 / 在线要求 / 健康分 / 快照新鲜度）、车辆快照、版本化风险策略、发布波次。
- **审批后逐级放量**：波次按目标比例从合格车队中确定性选车，前序波次完结（或已回滚）后才允许下一波次启动。
- **风险驱动决策**：依据迟到 / 重复安装回执、健康指标与人工事件报告，自动决定继续、暂停或完结；支持人工暂停、恢复（含审计的强制恢复）、回滚与隔离。
- **安全保证**：
  - 每辆车的安装命令幂等（确定性命令标识 + 唯一约束），重复回执不重复计数，冲突回执保留先到者并记录异常；
  - 回滚后车辆按软件包封锁，不得被旧波次重新推进，已装车辆收到幂等回滚命令；
  - 风险策略按版本固化到波次，决策追加式落库，规则换版不会静默改写已完成的决策。
- **运维 API**：查看任一波次的入组理由、实时风险预算、状态迁移与恢复条件。

## 代码结构

- `src/rollout_control/contracts.py` — 领域契约（枚举、软件包、兼容条件、风险策略、运维视图）
- `src/rollout_control/storage.py` — SQLite 持久化（WAL、BEGIN IMMEDIATE 串行化并发写）
- `src/rollout_control/service.py` — 核心服务 `RolloutControlService`
- `src/rollout_control/api.py` — 运维 HTTP JSON API（`make_server`）
- `tests/` — 单元与端到端测试（并发回执、部分失败、规则换版、服务重启续跑）

## 波次状态机

`awaiting_approval → approved → running → paused → running`，终态为 `completed / rolled_back / quarantined`；`completed` 可被召回式回滚或隔离。全部迁移由状态机校验并落审计日志。

## HTTP API 摘要

查询：`GET /api/waves/{id}`、`/members`（入组理由）、`/risk`（实时风险预算）、`/transitions`（状态迁移）、`/decisions`（决策记录）、`/recovery`（恢复条件）、`/commands`、`/receipts`、`/api/vehicles/{id}`、`/api/packages`、`/api/policies`、`/api/anomalies`。

操作：`POST /api/packages`、`/api/policies`、`/api/snapshots`、`/api/waves`、`/api/waves/{id}/approve|start|pause|resume|rollback|quarantine`、`/api/receipts`、`/api/incidents`、`/api/health-reports`、`/api/vehicles/{id}/quarantine`。

启动服务示例：

```python
from rollout_control import RolloutControlService, make_server
make_server(RolloutControlService("rollout.db"), port=8080).serve_forever()
```

## 运行

测试：`python3 -m unittest discover -s tests -v`

编译检查：`python3 -m compileall -q src tests run_cli.py`

命令行冒烟（登记→审批→小流量→事件暂停→回滚全流程）：`python3 run_cli.py`
