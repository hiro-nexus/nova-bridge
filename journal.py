"""#journal の朝の投稿へのスレッド返信（Claude アプリが投稿する回答のまとめ）を受け取る。

流れ: 返信を原文のまま保存（コード）→ 清書をエージェントに頼む（ジャーナルと日報を別々に保存）。
質問の投稿（毎朝 8:00）は別の処理。ここは受け取りだけ。
"""
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
SUMMARY_DIR = Path("70_AI_Lab/75_Context/10_self/slack")
KEEP_DONE = 300  # 処理済みの返信を覚えておく件数

_lock = threading.Lock()


def day_of(ts: str) -> str:
    """Slack の ts を、日本時間の YYYY-MM-DD にする。"""
    return datetime.fromtimestamp(float(ts), JST).strftime("%Y-%m-%d")


def is_morning_post(parent: dict | None, bot_user_id: str) -> bool:
    """スレッドの親が、Nova 自身の投稿（スレッドの返信ではない通常の投稿）か。"""
    if not parent:
        return False
    if parent.get("user") != bot_user_id:
        return False
    thread_ts = parent.get("thread_ts")
    return thread_ts is None or thread_ts == parent.get("ts")


class DoneStore:
    """処理済みの返信の ts を記録する。Slack の再配信や、再起動後の二重処理を防ぐ。"""

    def __init__(self, path: Path):
        self.path = path
        self._ids: list[str] = []
        if path.exists():
            try:
                self._ids = list(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                self._ids = []

    def seen(self, ts: str) -> bool:
        return ts in self._ids

    def add(self, ts: str) -> None:
        with _lock:
            if ts in self._ids:
                return
            self._ids = (self._ids + [ts])[-KEEP_DONE:]
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._ids), encoding="utf-8")
            tmp.replace(self.path)


def save_summary(vault: Path, channel_id: str, post_ts: str, reply_ts: str, text: str) -> Path:
    """回答のまとめを、原文のまま、朝の投稿の日付のファイルに追記する。清書の前の控え。"""
    day = day_of(post_ts)
    t = datetime.fromtimestamp(float(reply_ts), JST)
    path = vault / SUMMARY_DIR / f"{day}_journal_summary.md"
    entry = f"\n## {t:%H:%M:%S}\n\n{text.strip()}\n"
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            body = path.read_text(encoding="utf-8")
            body = re.sub(r"(?m)^updated: .*$", f"updated: {day}", body, count=1)
        else:
            body = (
                "---\n"
                f"title: {day} journal summary\n"
                "source: slack\n"
                "speaker: self\n"
                f"date: {day}\n"
                f"origin_id: slack:{channel_id}:{post_ts}\n"
                "type: context\n"
                f"created: {day}\n"
                f"updated: {day}\n"
                "tags: [context, journal]\n"
                "status: raw\n"
                "---\n\n"
                f"# {day} journal summary\n"
            )
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(body + entry, encoding="utf-8")
        tmp.replace(path)
    return path


def build_prompt(day: str, summary_path: str, text: str) -> str:
    """清書の依頼。保存先と形式は journal_questions.md に従う。"""
    return (
        f"【ジャーナルの清書の依頼】{day} の朝のジャーナル（Claude アプリの音声モードで行ったもの）の回答のまとめが、"
        "Hiroさん本人から #journal のスレッドに届きました。原文は次のファイルに控えとして保存済みです（変更しない）: "
        f"{summary_path}\n\n"
        "手順:\n"
        "1. `70_AI_Lab/71_Prompts/journal_questions.md` を読み、その『記録の形』と保存先に従う。\n"
        f"2. 次のまとめがジャーナルなら `05_Journal/daily/{day[:4]}/{day}.md`、仕事の日報なら `05_Journal/daily/{day[:4]}/{day}_日報.md` に保存する。"
        "両方が含まれるときは、別々のファイルに分ける。すでにファイルがあるときは、内容を失わないように統合する。\n"
        "3. 答えがなかった項目は「未回答」と書く。推測で埋めない。`emotions` は本人が使った言葉だけ。誤変換だけを補正する。\n"
        "4. ジャーナルでも日報でもない内容（あいさつ、確認など）なら、保存せず、その旨だけを返す。\n"
        "5. 機密情報（パスワード、口座、契約金額、連絡先など）は `[機密情報を除外]` に置き換える。\n"
        "6. 保存したファイルだけを `git add`、`git commit`、`git pull --rebase --autostash`、`git push` する。\n"
        "7. 返事は2〜3行で。保存したファイルのパスと、未回答の項目があればその数だけを書く。\n\n"
        "--- 回答のまとめ（ここから） ---\n"
        f"{text.strip()}\n"
        "--- （ここまで） ---"
    )
