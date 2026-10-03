"""风险判定：纯函数，输入指标快照与规则版本，输出结论与理由。

判定逻辑集中在这里，保证同一 (metrics, policy) 永远得到同一结论；
服务层负责采集指标、落库判定结果并执行状态迁移。
"""

from dataclasses import dataclass, field

from .contracts import RiskPolicy, Verdict


@dataclass(frozen=True)
class BatchMetrics:
    hardware_batch: str
    terminal: int
    failures: int

    @property
    def failure_rate(self) -> float:
        return self.failures / self.terminal if self.terminal else 0.0


@dataclass(frozen=True)
class WaveMetrics:
    """某波次在当前时刻的风险输入快照。"""

    cohort_size: int          # 参与评定的车辆数（不含已隔离/已排除）
    dispatched: int           # 已下发命令的车辆数
    terminal: int             # 已有终态结果的命令数（成功+失败，按每车最新尝试计）
    successes: int
    failures: int
    pending: int              # 已下发未回报
    unhealthy: int            # 最新健康分低于阈值的车辆数（风险当量）
    critical_incidents: int   # 波及本波次的未解决严重事件数
    batches: tuple[BatchMetrics, ...] = ()

    @property
    def risk_units(self) -> int:
        return self.failures + self.unhealthy

    @property
    def evidence(self) -> int:
        """可用于判定的证据量：终态回执 + 异常健康报告。"""
        return self.terminal + self.unhealthy

    @property
    def risk_rate(self) -> float:
        """风险率 = (失败 + 不健康) / 评定车辆数。"""
        return self.risk_units / self.cohort_size if self.cohort_size else 0.0

    def to_dict(self) -> dict:
        return {
            "cohort_size": self.cohort_size,
            "dispatched": self.dispatched,
            "terminal": self.terminal,
            "successes": self.successes,
            "failures": self.failures,
            "pending": self.pending,
            "unhealthy": self.unhealthy,
            "critical_incidents": self.critical_incidents,
            "risk_units": self.risk_units,
            "risk_rate": round(self.risk_rate, 6),
            "batches": [
                {
                    "hardware_batch": b.hardware_batch,
                    "terminal": b.terminal,
                    "failures": b.failures,
                    "failure_rate": round(b.failure_rate, 6),
                }
                for b in self.batches
            ],
        }


@dataclass(frozen=True)
class RiskVerdict:
    verdict: Verdict
    reasons: tuple[str, ...] = ()
    anomalous_batches: tuple[str, ...] = ()
    # 隔离后服务层会剔除对应车辆重新评定，这里携带剔除后的建议结论。
    follow_up: "RiskVerdict | None" = field(default=None, compare=False)


def evaluate(metrics: WaveMetrics, policy: RiskPolicy) -> RiskVerdict:
    """按优先级判定：回滚 > 隔离 > 暂停 > 继续 / 数据不足。"""
    reasons: list[str] = []

    if metrics.critical_incidents >= policy.rollback_incident_count:
        return RiskVerdict(
            Verdict.ROLLBACK,
            (f"未解决严重事件 {metrics.critical_incidents} 起，达到回滚门槛 "
             f"{policy.rollback_incident_count}",),
        )

    if metrics.evidence >= policy.min_wave_sample:
        if metrics.risk_rate >= policy.rollback_failure_rate:
            return RiskVerdict(
                Verdict.ROLLBACK,
                (f"风险率 {metrics.risk_rate:.2%} 达到回滚阈值 "
                 f"{policy.rollback_failure_rate:.2%}",),
            )

    anomalous = tuple(
        b.hardware_batch
        for b in metrics.batches
        if b.terminal >= policy.min_batch_sample
        and b.failure_rate >= policy.batch_quarantine_rate
    )
    if anomalous:
        for batch in anomalous:
            b = next(x for x in metrics.batches if x.hardware_batch == batch)
            reasons.append(
                f"硬件批次 {batch} 失败率 {b.failure_rate:.2%}（样本 {b.terminal}）"
                f"达到隔离阈值 {policy.batch_quarantine_rate:.2%}"
            )
        return RiskVerdict(Verdict.QUARANTINE_BATCH, tuple(reasons), anomalous)

    if metrics.critical_incidents >= policy.pause_incident_count:
        return RiskVerdict(
            Verdict.PAUSE,
            (f"未解决严重事件 {metrics.critical_incidents} 起，达到暂停门槛 "
             f"{policy.pause_incident_count}",),
        )

    if metrics.evidence >= policy.min_wave_sample:
        if metrics.risk_rate >= policy.pause_failure_rate:
            return RiskVerdict(
                Verdict.PAUSE,
                (f"风险率 {metrics.risk_rate:.2%} 达到暂停阈值 "
                 f"{policy.pause_failure_rate:.2%}",),
            )
        return RiskVerdict(Verdict.CONTINUE, ("风险率处于预算内",))

    return RiskVerdict(
        Verdict.INSUFFICIENT_DATA,
        (f"有效证据 {metrics.evidence} 条，不足判定样本 {policy.min_wave_sample}",),
    )
