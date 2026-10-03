# 车端软件灰度发布控制

面向大规模商业化运营前的驾驶辅助软件分批发布：登记软件包、兼容条件、车辆快照、
发布波次与风险阈值，审批后按小流量逐级放量，并依据安装回执、健康指标与人工事件
报告自动决定继续、暂停、回滚或隔离。

## 核心保证

- **命令幂等**：安装/回滚命令的 `command_id` 由（计划， 波次， 车辆， 纪元， 尝试序号， 类型）
  确定性派生，重复下发返回同一条命令；回执按 `receipt_id` 幂等去重，重复上报返回首次
  受理结果并累计 `duplicate_count`。
- **纪元围栏**：回滚与批次隔离会提升车辆纪元（epoch），旧纪元的在途命令被取代、
  迟到回执被归类为 `stale_epoch` / `superseded_command`，不再推进车辆状态；
  已回滚计划为终态，任何旧波次都无法重新放量。
- **判定不可篡改**：每次风险判定连同规则版本与输入指标写入事件日志；规则换版
  （`register_policy` 新版本号 + `migrate_plan_policy` 显式迁移）只影响之后的判定，
  已完成波次的判定永不重算——包括服务重启回放之后。
- **崩溃恢复**：一切状态变更先追加到 `events.jsonl` 再应用，重启后完整回放，
  幂等键、纪元、进行中的波次与暂停状态全部保留。

## 风险判定（risk.py，纯函数）

风险率 = （失败回执 + 健康分低于阈值的车辆） / 评定车辆数，按优先级判定：

1. 未解决严重事件 ≥ `rollback_incident_count`，或风险率 ≥ `rollback_failure_rate` → **回滚**
2. 某硬件批次失败率 ≥ `batch_quarantine_rate`（样本 ≥ `min_batch_sample`）→ **隔离该批次**，
   该批次车辆从本计划全部波次移出并全局标记，新计划自动排除（`clear_quarantine` 可解除）
3. 未解决严重事件 ≥ `pause_incident_count`，或风险率 ≥ `pause_failure_rate` → **暂停**
4. 否则 → **继续**；证据不足（`min_wave_sample`）时不做结论

暂停后的恢复条件（阻塞项：严重事件清零、异常批次已隔离、运维手动确认；
建议项：风险率回落）可通过波次报告实时查看。

## 运行

```bash
python3 -m unittest discover -s tests -v          # 测试
python3 -m compileall -q src tests run_cli.py     # 编译检查
python3 run_cli.py                                # 端到端冒烟（临时数据目录）
PYTHONPATH=src python3 -m rollout_control.api --data-dir ./data --port 8080  # 启动服务
```

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/packages` `/policies` `/vehicles` | 登记软件包（含兼容条件）、风险规则版本、车辆快照 |
| POST | `/plans` | 创建计划：`{package_id, policy_version, wave_sizes, created_by, auto_promote?}`；`wave_sizes` 小数=占剩余车辆比例，整数=辆数，剩余车辆自动进入末波次 |
| POST | `/plans/{id}/approve` → `/start-next-wave` | 审批后逐级放量（`auto_promote` 开启时波次达标自动晋级） |
| POST | `/receipts` `/health` `/incidents` | 上报安装回执 / 健康指标 / 人工事件，自动触发风险判定 |
| POST | `/plans/{id}/evaluate` `/resume` `/rollback` | 显式判定 / 恢复暂停 / 手动回滚 |
| POST | `/plans/{id}/policy-migration` | 迁移到新规则版本（只影响后续判定） |
| POST | `/plans/{id}/waves/{wid}/retry` | 重试失败车辆（新尝试序号，旧命令被取代） |
| POST | `/incidents/{id}/resolve` `/quarantines/{batch}/clear` | 解除事件 / 解除批次隔离 |
| GET | `/plans/{id}/waves/{wid}/report` | **运维视图**：入组理由、实时风险预算、状态迁移、恢复条件、判定历史 |
| GET | `/plans/{id}` `/plans/{id}/decisions` `/vehicles/{id}` `/audit` | 计划详情 / 判定列表 / 车辆时间线 / 事件审计 |

## 目录结构

```
src/rollout_control/
  contracts.py   # 对外不可变契约（软件包、兼容条件、快照、回执、规则、事件）
  state.py       # 内部可变状态记录（波次、命令、判定、隔离）
  risk.py        # 风险判定纯函数
  service.py     # 核心服务：事件溯源 + 状态机 + 判定执行
  store.py       # 追加式事件日志（JSONL，重启回放）
  api.py         # 标准库 HTTP API
tests/           # 并发回执、部分失败、规则换版、回滚围栏、重启恢复、API
```
