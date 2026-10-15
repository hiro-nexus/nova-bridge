"""#context / #inbox の投稿を vault に保存する。Claude は呼ばず、コードが直接書く。"""
import re
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
CONTEXT_DIR = Path("70_AI_Lab/75_Context/10_self/slack")
INBOX_DIR = Path("00_Inbox")
SLUG_LEN = 15

_lock = threading.Lock()


def _jst(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), JST)


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)  # 途中で落ちても壊れない


def plain(text: str) -> str:
    """Slack の記法を最低限だけ戻す。"""
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"[\2](\1)", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def slug(text: str, n: int = SLUG_LEN) -> str:
    """ファイル名に使う先頭 n 文字。空白は _ にし、使えない文字は除く。"""
    lines = text.strip().splitlines()
    head = re.sub(r"\s+", "_", lines[0] if lines else "")
    head = re.sub(r'[\\/:*?"<>|#^\[\]]', "", head)
    return head[:n].strip("_") or "memo"


def save_context(vault: Path, channel_id: str, ts: str, text: str) -> Path:
    """その日のファイルに時刻付きで追記する。なければ作る。"""
    t = _jst(ts)
    day = t.strftime("%Y-%m-%d")
    path = vault / CONTEXT_DIR / f"{day}_context.md"
    entry = f"\n## {t:%H:%M:%S}\n\n{text.strip()}\n"
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            body = path.read_text(encoding="utf-8")
            body = re.sub(r"(?m)^updated: .*$", f"updated: {day}", body, count=1)
        else:
            body = (
                "---\n"
                f"title: {day} context\n"
                "source: slack\n"
                "speaker: self\n"
                f"date: {day}\n"
                f"origin_id: slack:{channel_id}:{day}\n"
                "type: context\n"
                f"created: {day}\n"
                f"updated: {day}\n"
                "tags: [context]\n"
                "status: raw\n"
                "---\n\n"
                f"# {day} context\n"
            )
        _write(path, body + entry)
    return path


def save_inbox(vault: Path, channel_id: str, ts: str, text: str) -> Path:
    """1投稿1ファイルで 00_Inbox/ に保存する。"""
    t = _jst(ts)
    day = t.strftime("%Y-%m-%d")
    lines = text.strip().splitlines()
    title = re.sub(r'["\\]', "", lines[0] if lines else "")[:60]
    base = f"{day}_{t:%H%M}_{slug(text)}"
    with _lock:
        folder = vault / INBOX_DIR
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{base}.md"
        n = 0
        while path.exists():  # 同じ分に同じ書き出しの投稿があるとき
            n += 1
            suffix = f"_{t:%S}" if n == 1 else f"_{t:%S}_{n}"
            path = folder / f"{base}{suffix}.md"
        _write(path, (
            "---\n"
            f'title: "{title}"\n'
            "type: fleeting\n"
            "source: slack\n"
            f"origin_id: slack:{channel_id}:{ts}\n"
            f"created: {day}\n"
            f"updated: {day}\n"
            "tags: [inbox]\n"
            "status: active\n"
            "---\n\n"
            f"{text.strip()}\n"
        ))
    return path
