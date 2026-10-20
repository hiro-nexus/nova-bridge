"""毎朝8:00頃に、その日の質問を #journal に1通で投稿する。systemd timer から1回だけ実行して、終わる。

実行: ~/nova-bridge で  .venv/bin/python morning.py [--dry-run]
  --dry-run は、投稿せず、記録もせず、組み立てた本文を表示するだけ。

固定質問と日報の質問は、承認済みの文面（70_AI_Lab/71_Prompts/journal_questions.md）をコードに持つ。
変動質問（1〜2問）だけをエージェントに作らせ、形が合わなければ捨てる（固定質問だけで投稿する）。
今日の予定・プロジェクトタスク・日常タスクの要約は、Google・Notion・Todoist の連携ができてから足す。
"""
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
WEEKDAYS = "月火水木金土日"
SUMMARY_DIR = Path("70_AI_Lab/75_Context/10_self/slack")
JOURNAL_DIR = Path("05_Journal/daily")

FIXED = [
    "昨日の気分と体調を、それぞれ1から5で教えてください。1がかなり悪い、5がとても良い、です。",
    "昨日、うれしかったことと、もやもやしたことを、それぞれ教えてください。そのとき、どんな気持ちになりましたか。",
    "昨日は、やりたいことの実現に、どれくらい近づけた実感がありましたか。1から5で、理由も一言どうぞ。",
]
DAILY_REPORT = [
    "昨日、お客様とのやり取りで、印象に残ったことは何ですか。",
    "うまくいったことと、引っかかったことを、1つずつ教えてください。",
    "今、仕事で詰まっていることや、決めきれていないことはありますか。",
]
MAX_VARIABLE = 2
MAX_LINE = 80
BAD_CHARS = re.compile(r"[#*|`>\[\]{}<~]")
NUMBERING = re.compile(r"^\s*(?:[-・●•*]|\d+[.．)）、]|[①-⑩])\s*")

log = logging.getLogger("nova-morning")


def today_jst() -> datetime:
    return datetime.now(JST)


def parse_variable(text: str) -> list[str]:
    """エージェントの返事から、変動質問を取り出す。形が合わない行は捨てる。最大2問。"""
    found: list[str] = []
    for raw in text.splitlines():
        line = NUMBERING.sub("", raw).strip()
        if not line or len(line) > MAX_LINE:
            continue
        if BAD_CHARS.search(line):          # 記号・表・見出しは読み上げに向かない
            continue
        if not re.search(r"[？?]$|か[。]?$|ください[。]?$", line):
            continue                          # 質問の形でない行（前置きなど）
        found.append(line)
        if len(found) == MAX_VARIABLE:
            break
    return found


def compose(day: datetime, variable: list[str], missed_yesterday: bool) -> str:
    """朝の投稿の本文。番号付きの短い文だけ。記号や表は使わない（Claude アプリが読み上げるため）。"""
    head = f"おはようございます。{day.month}月{day.day}日{WEEKDAYS[day.weekday()]}曜日の、朝の質問です。順番に、声で答えてください。"
    lines = [head]
    if missed_yesterday:
        lines.append("昨日は回答がありませんでした。昨日の分も、あわせて教えてください。")
    lines.append("")
    n = 0
    for q in FIXED + variable:
        n += 1
        lines.append(f"{n}. {q}")
    lines += ["", "ここから先は、仕事の日報の質問です。やらない日は、飛ばしてください。", ""]
    for q in DAILY_REPORT:
        n += 1
        lines.append(f"{n}. {q}")
    lines += [
        "",
        "今日の予定とタスクは、まだ連携ができていないため、この投稿には入っていません。",
        "最後に、回答のまとめを、この投稿のスレッドに返信してください。ジャーナルと日報は、分けてまとめてください。",
    ]
    return "\n".join(lines)


def build_variable_prompt(day: datetime, recent_files: list[str]) -> str:
    recent = "\n".join(f"- {f}" for f in recent_files) or "- （まだありません）"
    return (
        f"【朝のジャーナルの変動質問の作成】{day:%Y-%m-%d} の変動質問を、1〜2問だけ作ってください。\n\n"
        "手順:\n"
        "1. `70_AI_Lab/71_Prompts/journal_questions.md` の『変動質問』のルールを読む。\n"
        "2. 材料として、次を読む。最近のジャーナル（過去14日に出した問いと、ほぼ同じ内容は出さない）:\n"
        f"{recent}\n"
        "   最近の context（`70_AI_Lab/75_Context/10_self/slack/` の直近のファイル）と、夢プロジェクト（`01_Projects/dream/`）。\n"
        "3. 事実の確認より、気持ちや考えを引き出す問いにする。責める言い方・評価する言い方はしない。1問につき1つのことだけ聞く。\n\n"
        "出力の形（厳守）:\n"
        "- 質問だけを、1行に1問、最大2行。前置き・説明・番号・記号・見出し・引用は書かない。\n"
        "- 1問は1文、全角で40字程度まで。声で読み上げるので、やさしい言葉で。「ですか。」「ください。」で終える。\n"
        "- 材料が足りなければ、1問だけでよい。ファイルは変更しない。"
    )


def recent_journal_files(vault: Path, day: datetime, days: int = 14) -> list[str]:
    out = []
    for i in range(1, days + 1):
        d = day - timedelta(days=i)
        p = vault / JOURNAL_DIR / f"{d:%Y}" / f"{d:%Y-%m-%d}.md"
        if p.exists():
            out.append(str(p.relative_to(vault)))
    return out


class PostLog:
    """投稿した日の記録（state/morning_posts.json）。同じ日に二重に投稿しない。昨日の回答の有無の判定にも使う。"""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, str] = {}
        if path.exists():
            try:
                self.data = dict(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError, ValueError):
                self.data = {}

    def posted(self, day: str) -> bool:
        return day in self.data

    def add(self, day: str, ts: str) -> None:
        self.data[day] = ts
        keep = dict(sorted(self.data.items())[-60:])
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(keep), encoding="utf-8")
        tmp.replace(self.path)
        self.data = keep


def missed_yesterday(vault: Path, posts: PostLog, day: datetime) -> bool:
    """昨日の朝の質問を出したのに、回答のまとめが届いていない。出していない日は、未実施とは言わない。"""
    y = (day - timedelta(days=1)).strftime("%Y-%m-%d")
    if not posts.posted(y):
        return False
    return not (vault / SUMMARY_DIR / f"{y}_journal_summary.md").exists()


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    base = Path(__file__).resolve().parent
    sys.path.insert(0, str(base))
    from slack_sdk import WebClient

    from agents.base import AgentAuthError
    from config import build_agent, load_config

    cfg = load_config()
    if not cfg.journal_channel_id:
        log.error("JOURNAL_CHANNEL_ID が .env にありません")
        return 1
    vault = Path(cfg.agent_workdir)
    day = today_jst()
    key = day.strftime("%Y-%m-%d")
    posts = PostLog(cfg.state_dir / "morning_posts.json")
    if posts.posted(key) and not dry:
        log.info("already posted for %s; nothing to do", key)
        return 0

    slack = WebClient(token=cfg.slack_bot_token)

    def notify(text: str) -> None:
        try:
            slack.chat_postMessage(channel=cfg.nova_channel_id, text=text)
        except Exception:
            log.exception("could not notify #nova-hub")

    variable: list[str] = []
    try:
        agent = build_agent(cfg)
        result = agent.run(build_variable_prompt(day, recent_journal_files(vault, day)), None)
        if result.is_error:
            raise RuntimeError("agent returned an error")
        variable = parse_variable(result.text)
        log.info("variable questions: %d", len(variable))
    except AgentAuthError:
        log.error("agent auth error")
        notify("朝の質問の変動質問を作れませんでした。認証が切れました。SSHで再ログインが必要です。固定質問だけで投稿します。")
    except Exception:
        log.exception("could not build variable questions")
        notify("朝の質問の変動質問を作れませんでした。固定質問だけで投稿します。")

    body = compose(day, variable, missed_yesterday(vault, posts, day))
    if dry:
        print(body)
        return 0
    try:
        res = slack.chat_postMessage(channel=cfg.journal_channel_id, text=body)
    except Exception as e:
        log.exception("could not post to #journal")
        notify(f"朝の質問を #journal に投稿できませんでした：{type(e).__name__}")
        return 1
    posts.add(key, res["ts"])
    log.info("posted morning questions: ts=%s", res["ts"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
