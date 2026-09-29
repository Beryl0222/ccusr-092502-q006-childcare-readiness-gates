"""仅追加的领域事件存储（JSONL，单行一个信封）。

恢复语义：

* 每次追加在同一写事务内 flush + fsync，进程崩溃后已确认的命令不丢。
* 加载时若末行因断电撕裂（半截 JSON），回截到最后一个完整行；
  待复核、待追回等事项全部由事件日志重放派生，恢复后自动重现。
* event_id 全局去重；同一聚合版本号必须严格递增，作为重放完整性的防线。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class EventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seen_event_ids: set[str] = set()
        self._versions: dict[str, int] = {}
        self.recovered_tail_events = 0  # 最近一次加载回截的撕裂行数

    # ---- 写 ---------------------------------------------------------------

    def append_batch(self, envelopes: list[dict[str, Any]]) -> None:
        if not envelopes:
            return
        lines: list[str] = []
        with self._lock:
            expected = self._versions.get(
                envelopes[0]["aggregate_id"], 0)
            for env in envelopes:
                if env["event_id"] in self._seen_event_ids:
                    raise IntegrityError(f"event_id 重复: {env['event_id']}")
                if env["version"] != expected + 1:
                    raise IntegrityError(
                        f"聚合 {env['aggregate_id']} 版本断裂: "
                        f"期望 {expected + 1}，收到 {env['version']}")
                expected = env["version"]
                lines.append(json.dumps(env, ensure_ascii=False, sort_keys=True))
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
                fh.flush()
                import os
                os.fsync(fh.fileno())
            for env in envelopes:
                self._seen_event_ids.add(env["event_id"])
                self._versions[env["aggregate_id"]] = env["version"]

    # ---- 读 / 恢复 --------------------------------------------------------

    def load_all(self) -> dict[str, list[dict[str, Any]]]:
        """按聚合加载全部事件信封；自动回截撕裂的末行。"""
        self.recovered_tail_events = 0
        if not self.path.exists():
            return {}
        raw = self.path.read_bytes()
        if not raw:
            return {}
        good_lines: list[bytes] = []
        for line in raw.split(b"\n"):
            if not line.strip():
                continue
            try:
                json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                # 追加日志只可能在尾部撕裂；此前已完整 fsync 的行均合法
                self.recovered_tail_events += 1
                break
            good_lines.append(line)
        if self.recovered_tail_events:
            self._rewrite(good_lines)
        by_aggregate: dict[str, list[dict[str, Any]]] = {}
        self._seen_event_ids = set()
        self._versions = {}
        for line in good_lines:
            env = json.loads(line.decode("utf-8"))
            if env["event_id"] in self._seen_event_ids:
                raise IntegrityError(f"日志中 event_id 重复: {env['event_id']}")
            last = self._versions.get(env["aggregate_id"], 0)
            if env["version"] != last + 1:
                raise IntegrityError(
                    f"日志中聚合 {env['aggregate_id']} 版本断裂于 {env['event_id']}")
            self._seen_event_ids.add(env["event_id"])
            self._versions[env["aggregate_id"]] = env["version"]
            by_aggregate.setdefault(env["aggregate_id"], []).append(env)
        return by_aggregate

    def _rewrite(self, good_lines: list[bytes]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("wb") as fh:
            if good_lines:
                fh.write(b"\n".join(good_lines) + b"\n")
            fh.flush()
            import os
            os.fsync(fh.fileno())
        tmp.replace(self.path)


class IntegrityError(RuntimeError):
    """日志不可恢复地违反追加不变量（重复事件、版本断裂）。"""
