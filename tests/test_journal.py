"""journal.py の単体テスト。一時フォルダだけを使い、本番の vault や Slack には触れない。

実行: ~/nova-bridge で  .venv/bin/python tests/test_journal.py
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent


def load(name: str):
    """本番（tests/ の1つ上）か、Vault 内の下書き（接頭辞つき。新しい版を優先）から読み込む。"""
    candidates = [HERE.parent / f"{name}.py"]
    candidates += sorted(HERE.glob(f"nova-bridge_phase2*_{name}.py"), reverse=True)
    for p in candidates:
        if p.exists():
            spec = importlib.util.spec_from_file_location(name, p)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError(name)


journal = load("journal")

# 2026-10-20 08:00:00 JST と、その30分後
POST_TS = "1792450800.000100"
REPLY_TS = "1792452600.000200"


def test_day_of():
    assert journal.day_of(POST_TS) == "2026-10-20", journal.day_of(POST_TS)
    # 日本時間の日付で数える（UTC では前日になる時刻）
    assert journal.day_of("1792422000.0") == "2026-10-20", journal.day_of("1792422000.0")


def test_is_morning_post():
    bot = "UBOT"
    assert journal.is_morning_post({"user": bot, "ts": "1.0"}, bot)
    assert journal.is_morning_post({"user": bot, "ts": "1.0", "thread_ts": "1.0"}, bot)
    assert not journal.is_morning_post({"user": "UHIRO", "ts": "1.0"}, bot)       # 他の人の投稿
    assert not journal.is_morning_post({"user": bot, "ts": "2.0", "thread_ts": "1.0"}, bot)  # スレッド内の返信
    assert not journal.is_morning_post(None, bot)
    assert not journal.is_morning_post({}, bot)


def test_done_store(tmp: Path):
    p = tmp / "done.json"
    s = journal.DoneStore(p)
    assert not s.seen("a")
    s.add("a")
    s.add("a")
    assert s.seen("a")
    assert journal.DoneStore(p).seen("a")            # 再起動後も残る
    assert not list(tmp.glob("*.tmp"))
    for i in range(journal.KEEP_DONE + 20):          # 古いものから忘れる
        s.add(f"x{i}")
    assert not s.seen("a") and s.seen(f"x{journal.KEEP_DONE + 19}")
    p.write_text("壊れた内容", encoding="utf-8")      # 壊れていても落ちない
    assert not journal.DoneStore(p).seen("a")


def test_save_summary(tmp: Path):
    p1 = journal.save_summary(tmp, "CJ", POST_TS, REPLY_TS, "1. 気分は4\n2. うれしい")
    p2 = journal.save_summary(tmp, "CJ", POST_TS, "1792452700.000300", "日報のまとめ")
    assert p1 == p2
    assert p1.name == "2026-10-20_journal_summary.md", p1.name
    assert p1.parent == tmp / "70_AI_Lab/75_Context/10_self/slack"
    body = p1.read_text(encoding="utf-8")
    assert body.startswith("---\ntitle: 2026-10-20 journal summary\n")
    assert "status: raw" in body and "origin_id: slack:CJ:" + POST_TS in body
    assert body.count("updated: 2026-10-20") == 1
    assert body.index("## 08:30:00") < body.index("気分は4") < body.index("日報のまとめ")
    assert not list(p1.parent.glob("*.tmp"))


def test_build_prompt():
    pr = journal.build_prompt("2026-10-20", "70_AI_Lab/75_Context/10_self/slack/2026-10-20_journal_summary.md", "本文です")
    assert "05_Journal/daily/2026/2026-10-20.md" in pr
    assert "05_Journal/daily/2026/2026-10-20_日報.md" in pr
    assert "journal_questions.md" in pr and "未回答" in pr
    assert pr.rstrip().endswith("--- （ここまで） ---")
    assert "本文です" in pr


if __name__ == "__main__":
    test_day_of()
    test_is_morning_post()
    test_build_prompt()
    for t in (test_done_store, test_save_summary):
        with tempfile.TemporaryDirectory() as d:
            t(Path(d))
    print("OK: all tests passed")
