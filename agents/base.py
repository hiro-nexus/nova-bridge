"""エージェント・アダプターの共通の約束。

ブリッジ本体はこのファイルの型だけを使う。エージェント固有の処理はアダプター側に閉じ込める。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class AgentResult:
    text: str                      # Slack に返す本文
    session_id: str | None         # 次回の継続に使うセッションID
    denials: list[str] = field(default_factory=list)  # 権限がなく実行できなかった操作
    is_error: bool = False


class AgentAuthError(Exception):
    """認証切れなど、人がログインし直さないと解決しないエラー。"""


class AgentAdapter(ABC):
    @abstractmethod
    def run(self, prompt: str, session_id: str | None) -> AgentResult:
        """prompt を渡して結果を返す。session_id が None なら新しい会話を始める。"""
