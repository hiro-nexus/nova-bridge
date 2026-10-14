""".env の読み込みと、使うアダプターの選択。"""
import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
REQUIRED = ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "NOVA_CHANNEL_ID", "ALLOWED_USER_ID", "AGENT", "AGENT_WORKDIR"]


def load_dotenv(path: Path) -> None:
    """KEY=VALUE 形式だけを読む最小実装。すでに環境にある値（systemd の EnvironmentFile 等）を優先する。"""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


@dataclass
class Config:
    slack_bot_token: str
    slack_app_token: str
    nova_channel_id: str
    allowed_user_id: str
    agent: str
    agent_workdir: str
    agent_model: str | None
    state_dir: Path


def load_config() -> Config:
    load_dotenv(BASE_DIR / ".env")
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        raise SystemExit(f".env に未設定の項目があります: {', '.join(missing)}")
    return Config(
        slack_bot_token=os.environ["SLACK_BOT_TOKEN"],
        slack_app_token=os.environ["SLACK_APP_TOKEN"],
        nova_channel_id=os.environ["NOVA_CHANNEL_ID"],
        allowed_user_id=os.environ["ALLOWED_USER_ID"],
        agent=os.environ["AGENT"],
        agent_workdir=os.environ["AGENT_WORKDIR"],
        agent_model=os.environ.get("AGENT_MODEL") or "sonnet",
        state_dir=BASE_DIR / "state",
    )


def build_agent(cfg: Config):
    """AGENT= の値でアダプターを選ぶ。乗り換え時はここに1行と agents/<name>.py を追加する。"""
    if cfg.agent == "claude_code":
        from agents.claude_code import ClaudeCodeAdapter
        return ClaudeCodeAdapter(workdir=cfg.agent_workdir, model=cfg.agent_model)
    raise SystemExit(f"未対応の AGENT です: {cfg.agent}")
