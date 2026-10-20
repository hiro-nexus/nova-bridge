"""morning.py の単体テスト。一時フォルダだけを使い、本番の vault や Slack、エージェントには触れない。

実行: ~/nova-bridge で  .venv/bin/python tests/test_morning.py
"""
import importlib.util
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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


morning = load("morning")
JST = ZoneInfo("Asia/Tokyo")
DAY = datetime(2026, 10, 21, 8, 0, tzinfo=JST)  # 水曜日


def test_parse_variable():
    p = morning.parse_variable
    assert p("最近、仕事の話をあまりしていません。今、どんな気持ちですか。\n昨日、気持ちが動いた瞬間はありましたか。") == [
        "最近、仕事の話をあまりしていません。今、どんな気持ちですか。",
        "昨日、気持ちが動いた瞬間はありましたか。",
    ]
    # 番号・箇条書きは外す。3問目以降は捨てる
    assert p("1. 一つ目ですか。\n- 二つ目ですか。\n三つ目ですか。") == ["一つ目ですか。", "二つ目ですか。"]
    # 前置き・見出し・記号・長すぎる行・空は捨てる
    assert p("質問を作りました。\n## 見出しですか。\n**太字ですか。**\n| 表 | ですか。 |") == []
    assert p("あ" * 90 + "ですか。") == []
    assert p("") == []
    # 疑問符で終わる行は通す
    assert p("今日はどんな気分？") == ["今日はどんな気分？"]


def test_compose():
    body = morning.compose(DAY, ["変動1ですか。", "変動2ですか。"], missed_yesterday=False)
    assert body.startswith("おはようございます。10月21日水曜日の、朝の質問です。")
    for n in range(1, 9):
        assert f"\n{n}. " in body, n
    assert "\n9. " not in body
    assert body.index("4. 変動1") > body.index("3. 昨日は、やりたいこと")
    assert body.index("5. 変動2") > body.index("4. 変動1")
    assert body.index("ここから先は、仕事の日報") < body.index("6. 昨日、お客様")
    assert "昨日は回答がありませんでした" not in body
    assert "スレッドに返信" in body
    for ch in "#*|`>":   # 記号や表は使わない
        assert ch not in body, ch
    # 変動質問がなくても、固定質問と日報は出る
    none = morning.compose(DAY, [], missed_yesterday=True)
    assert "昨日は回答がありませんでした" in none
    assert "\n6. " in none and "\n7. " not in none


def test_post_log(tmp: Path):
    p = tmp / "posts.json"
    log = morning.PostLog(p)
    assert not log.posted("2026-10-21")
    log.add("2026-10-21", "1.2")
    assert morning.PostLog(p).posted("2026-10-21")   # 再起動後も残る
    assert not list(tmp.glob("*.tmp"))
    p.write_text("壊れた内容", encoding="utf-8")      # 壊れていても落ちない
    assert not morning.PostLog(p).posted("2026-10-21")


def test_missed_yesterday(tmp: Path):
    log = morning.PostLog(tmp / "posts.json")
    # 昨日に質問を出していない（初日など）→ 未実施とは言わない
    assert not morning.missed_yesterday(tmp, log, DAY)
    # 出したのに、まとめが届いていない
    log.add("2026-10-20", "1.2")
    assert morning.missed_yesterday(tmp, log, DAY)
    # まとめが届いた
    f = tmp / "70_AI_Lab/75_Context/10_self/slack/2026-10-20_journal_summary.md"
    f.parent.mkdir(parents=True)
    f.write_text("x", encoding="utf-8")
    assert not morning.missed_yesterday(tmp, log, DAY)


def test_recent_journal_files(tmp: Path):
    d = tmp / "05_Journal/daily/2026"
    d.mkdir(parents=True)
    (d / "2026-10-20.md").write_text("x", encoding="utf-8")
    (d / "2026-10-10.md").write_text("x", encoding="utf-8")
    (d / "2026-10-21.md").write_text("今日は含めない", encoding="utf-8")
    (d / "2026-10-01.md").write_text("14日より前", encoding="utf-8")
    got = morning.recent_journal_files(tmp, DAY)
    assert got == ["05_Journal/daily/2026/2026-10-20.md", "05_Journal/daily/2026/2026-10-10.md"], got


def test_prompt():
    pr = morning.build_variable_prompt(DAY, ["05_Journal/daily/2026/2026-10-20.md"])
    assert "2026-10-21" in pr and "journal_questions.md" in pr and "05_Journal/daily/2026/2026-10-20.md" in pr
    assert "最大2行" in pr


if __name__ == "__main__":
    test_parse_variable()
    test_compose()
    test_prompt()
    for t in (test_post_log, test_missed_yesterday, test_recent_journal_files):
        with tempfile.TemporaryDirectory() as d:
            t(Path(d))
    print("OK: all tests passed")
