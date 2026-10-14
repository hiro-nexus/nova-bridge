"""会話の継続に使うセッションIDの保存・読み出し（state/sessions.json）。"""
import json
import os
import tempfile
from pathlib import Path


class SessionStore:
    def __init__(self, path: Path, key: str):
        self.path = path
        self.key = key  # 例：チャンネルID。チャンネルごとに1つの会話を持つ

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 一時ファイルに書いてから置き換える（途中で落ちても壊れない）
        fd, tmp = tempfile.mkstemp(dir=self.path.parent)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get(self) -> str | None:
        return self._load().get(self.key)

    def set(self, session_id: str) -> None:
        data = self._load()
        data[self.key] = session_id
        self._save(data)

    def reset(self) -> None:
        data = self._load()
        data.pop(self.key, None)
        self._save(data)
