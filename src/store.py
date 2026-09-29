"""事件存储：只追加的 JSONL 日志，重启后重放恢复。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class EventStore:
    """按聚合保存事件；文件存储每行一个事件信封。

    故障恢复时调用 :meth:`load` 重放全部事件即可，业务状态不另作快照依赖，
    因此待复核与待追回事项不会因进程重启丢失。
    """

    def __init__(self, directory: str | os.PathLike[str] | None = None) -> None:
        self._dir = Path(directory) if directory else None
        self._streams: dict[str, list[dict[str, Any]]] = {}
        if self._dir:
            self._dir.mkdir(parents=True, exist_ok=True)
            for path in self._dir.glob("*.jsonl"):
                aggregate_id = path.stem
                self._streams[aggregate_id] = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]

    def _path(self, aggregate_id: str) -> Path | None:
        if not self._dir:
            return None
        safe = aggregate_id.replace("/", "_")
        return self._dir / f"{safe}.jsonl"

    def load(self, aggregate_id: str) -> list[dict[str, Any]]:
        return list(self._streams.get(aggregate_id, ()))

    def append(self, event: dict[str, Any]) -> None:
        aggregate_id = event["aggregate_id"]
        stream = self._streams.setdefault(aggregate_id, [])
        stream.append(event)
        path = self._path(aggregate_id)
        if path is not None:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
