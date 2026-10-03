"""事件存储：追加式 JSONL 日志，支持重启后完整回放。

所有状态变更都先落日志再应用，因此服务重启后可通过回放恢复到
崩溃前的精确状态（包括幂等键、纪元与已完成判定）。
"""

import json
import os
import threading
from pathlib import Path
from typing import Callable, Iterator


class EventStore:
    def __init__(self, data_dir: str | Path, fsync: bool = True) -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "events.jsonl"
        self._fsync = fsync
        self._lock = threading.Lock()
        self._seq = 0
        if self._path.exists():
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        self._seq = max(self._seq, json.loads(line)["seq"])

    @property
    def next_seq(self) -> int:
        return self._seq + 1

    def append(self, event: dict) -> dict:
        """为事件分配序号并原子追加。返回带 seq 的事件。"""
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, **event}
            line = json.dumps(event, ensure_ascii=False, sort_keys=True)
            # 单条写入保持原子性：先组装完整行再一次性写入并落盘。
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                if self._fsync:
                    os.fsync(fh.fileno())
            return event

    def replay(self) -> Iterator[dict]:
        """按序号顺序产出全部事件。"""
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def load_into(self, apply: Callable[[dict], None]) -> int:
        """回放全部事件到 apply 回调，返回事件数。"""
        count = 0
        for event in self.replay():
            apply(event)
            count += 1
        return count
