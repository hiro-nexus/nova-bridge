"""diet_nightly.py の単体テスト。Notion・Slack には触れない（読み取りと判定の関数だけ）。

実行: ~/nova-bridge で  .venv/bin/python tests/test_diet_nightly.py
"""
import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
JST = ZoneInfo("Asia/Tokyo")

spec = importlib.util.spec_from_file_location("diet_nightly", HERE.parent / "diet_nightly.py")
d = importlib.util.module_from_spec(spec)
sys.modules["diet_nightly"] = d
spec.loader.exec_module(d)

POSTED = datetime(2026, 10, 22, 12, 30, tzinfo=JST)


def test_parse_meal():
    r = d.parse_line("日付：2026-10-21 ／ 区分：昼 ／ 内容：ご飯、味噌汁 ／ kcal：650 ／ P：28g ／ F：12g ／ C：95g", POSTED)
    assert r == {"kind": "meal", "date": "2026-10-21", "meal": "昼", "content": "ご飯、味噌汁",
                 "vals": [650.0, 28.0, 12.0, 95.0], "inferred": False}, r
    # 半角の記号、日付の区切りが / 、約、内容の中の / と :
    r = d.parse_line("日付: 2026/10/2 / 区分: 朝 / 内容: ヨーグルト/バナナ 比率1:1 / kcal: 約300 / P: 10.5g / F: 5g / C: 50g", POSTED)
    assert r["date"] == "2026-10-02" and r["content"] == "ヨーグルト/バナナ 比率1:1" and r["vals"] == [300.0, 10.5, 5.0, 50.0], r


def test_parse_pipe_real_posts():
    """#diet-log の実際の投稿の形式（縦棒区切り）。2026-10-23 のドライランで確認した形。"""
    r = d.parse_line("2026-10-22|昼|弁当（唐揚げ2個、かぼちゃ煮物、オクラ和え、ゆで卵1個、ご飯なし）|430|25|20|34", POSTED)
    assert r["kind"] == "meal" and r["meal"] == "昼" and r["vals"] == [430.0, 25.0, 20.0, 34.0] and r["date"] == "2026-10-22", r
    assert r["inferred"] is False and "唐揚げ2個" in r["content"]
    # 小数、間食
    r = d.parse_line("2026-10-22|間食|プロテイン（標準：オーガーメイド＋黒糖きなこ）|125|27|1|2", POSTED)
    assert r["meal"] == "間食" and r["vals"] == [125.0, 27.0, 1.0, 2.0]
    r = d.parse_line("2026-10-21|昼|ラーメン（スープ4割残し）、ご飯146g＋ふりかけ|660|14.2|15.9|117", POSTED)
    assert r["vals"] == [660.0, 14.2, 15.9, 117.0]
    # 体重の行（残りの欄は「-」）
    r = d.parse_line("2026-10-22|体重|76.8kg（朝）|-|-|-|-", POSTED)
    assert r["kind"] == "weight" and r["weight"] == 76.8 and r["bodyfat"] is None and r["date"] == "2026-10-22"
    r = d.parse_line("2026-10-22|体重|76.8kg（体脂肪率 20.5%）|-|-|-|-", POSTED)
    assert r["bodyfat"] == 20.5
    # 全角の縦棒でも読める
    assert d.parse_line("2026-10-22｜朝｜パン｜300｜10｜8｜45", POSTED)["vals"] == [300.0, 10.0, 8.0, 45.0]
    # 見出し行・読めない行
    assert d.parse_line("日付|区分|内容|kcal|P|F|C", POSTED) is None
    assert "区分を読めない" in d.parse_line("2026-10-22|おやつ|飴|10|0|0|3", POSTED)["error"]
    assert "数字でない" in d.parse_line("2026-10-22|朝|パン|-|10|8|45", POSTED)["error"]


def test_real_message_with_suffix_and_many_lines():
    """1通に複数行、末尾に Claude アプリの定型文（_…_ の形）が付く投稿。"""
    sys.path.insert(0, str(HERE.parent))
    from connector import strip_connector_suffix
    text = ("2026-10-22|体重|76.8kg（朝）|-|-|-|-\n2026-10-22|朝|プロテイン（シェイカー）|130|19|3.5|6 _使用して送信されました_ Claude")
    recs = [d.parse_line(ln, POSTED) for ln in strip_connector_suffix(text).splitlines()]
    assert [r["kind"] for r in recs] == ["weight", "meal"] and recs[1]["vals"] == [130.0, 19.0, 3.5, 6.0], recs
    days = d.aggregate(recs)
    assert days["2026-10-22"]["weight"] == 76.8 and days["2026-10-22"]["meals"]["朝"] == [130.0, 19.0, 3.5, 6.0]
    assert days["2026-10-22"]["notes"] == []          # 日付が書いてあるので、「推定」の注記は付かない


def test_parse_inferred_and_errors():
    r = d.parse_line("区分：夜 ／ 内容：カレー ／ kcal：800 ／ P：20g ／ F：30g ／ C：100g", POSTED)
    assert r["date"] == "2026-10-22" and r["inferred"] is True
    # 読めないものは、推測で埋めず、error にする
    assert "どれかが無い" in d.parse_line("日付：2026-10-22 ／ 区分：朝 ／ kcal：300 ／ P：10g", POSTED)["error"]
    assert "区分を読めない" in d.parse_line("日付：2026-10-22 ／ 区分：おやつ ／ kcal：1 ／ P：1g ／ F：1g ／ C：1g", POSTED)["error"]
    assert "日付を読めない" in d.parse_line("日付：昨日 ／ 区分：朝 ／ kcal：1 ／ P：1g ／ F：1g ／ C：1g", POSTED)["error"]
    # 記録の行でないものは None
    assert d.parse_line("今日はよく歩いた", POSTED) is None
    assert d.parse_line("", POSTED) is None


def test_parse_weight():
    r = d.parse_line("日付：2026-10-22 ／ 体重：76.8kg（体脂肪率 20.5%）", POSTED)
    assert r["kind"] == "weight" and r["weight"] == 76.8 and r["bodyfat"] == 20.5, r
    r = d.parse_line("日付：2026-10-22 ／ 体重：76.8kg", POSTED)
    assert r["weight"] == 76.8 and r["bodyfat"] is None


def test_aggregate():
    recs = [
        d.parse_line("日付：2026-10-21 ／ 区分：朝 ／ 内容：パン ／ kcal：300 ／ P：10g ／ F：8g ／ C：45g", POSTED),
        d.parse_line("日付：2026-10-21 ／ 区分：朝 ／ 内容：牛乳 ／ kcal：130 ／ P：6g ／ F：7g ／ C：10g", POSTED),
        d.parse_line("日付：2026-10-22 ／ 区分：朝 ／ 内容：プロテイン ／ kcal：120 ／ P：24g ／ F：1g ／ C：3g", POSTED),
        d.parse_line("区分：昼 ／ 内容：うどん ／ kcal：500 ／ P：15g ／ F：5g ／ C：90g", POSTED),
        d.parse_line("日付：2026-10-22 ／ 区分：？ ／ kcal：1", POSTED),
    ]
    days = d.aggregate(recs)
    assert days["2026-10-21"]["meals"]["朝"] == [430.0, 16.0, 15.0, 55.0]   # 同じ区分は足す
    assert d.summary_text(days["2026-10-21"]["contents"]) == "朝：パン、牛乳"
    assert days["2026-10-22"]["meals"]["朝"] == [120.0, 24.0, 1.0, 3.0] and "昼" in days["2026-10-22"]["meals"]
    assert "日付は投稿時刻から推定" in days["2026-10-22"]["notes"]
    assert any(n.startswith("要確認") for n in days["2026-10-22"]["notes"])
    assert "2026-10-21" in days and len(days) == 2                         # 日をまたいで混ぜない


def test_decide():
    assert d.decide(None, 300.0, None) == "write"
    assert d.decide(300.0, 300.0, None) == "skip"
    assert d.decide(300.0, 450.0, 300.0) == "write"        # 前回このスクリプトが書いた値のままなら、更新してよい
    assert d.decide(300.0, 450.0, None) == "conflict"       # 手で入れた値は壊さない
    assert d.decide(300.0, 450.0, 200.0) == "conflict"
    assert d.decide("朝：パン", "朝：パン", None) == "skip"
    assert d.decide("手書き", "朝：パン", None) == "skip"          # 既存の文章は残す（要確認にしない）
    assert d.decide("朝：パン", "朝：パン、牛乳", "朝：パン") == "write"  # 前回このスクリプトが書いた文章なら、更新してよい
    assert d.decide("x", None, None) == "skip"


def test_plan_day_protects_existing():
    # 10/21：手入力の合計がある日。Slack の集計と食い違う区分は書き換えない
    agg = d.aggregate([d.parse_line("日付：2026-10-21 ／ 区分：朝 ／ 内容：パン ／ kcal：300 ／ P：10g ／ F：8g ／ C：45g", POSTED)])["2026-10-21"]
    row = {"朝kcal": 500.0, "朝P": 10.0, "朝F": 8.0, "朝C": 45.0, "食事内容": "手書きのメモ", "メモ": "手書き"}
    changes, lines, notes = d.plan_day("2026-10-21", agg, row, {}, False)
    assert changes == {}, changes                              # 何も書き換えない
    assert any("朝kcal" in n and "要確認" in n for n in notes)  # 数値の食い違いは、要確認
    assert not any("食事内容" in n for n in notes)              # 文章は、既存を残し、要確認にしない
    # 空欄の区分・列には書く
    row2 = {"朝kcal": None, "朝P": None, "朝F": None, "朝C": None}
    changes2, _, _ = d.plan_day("2026-10-21", agg, row2, {}, False)
    assert changes2["朝kcal"] == 300.0 and changes2["食事内容"] == "朝：パン"
    # 体脂肪率は、列がパーセント形式なら 1/100 にする
    aggw = d.aggregate([d.parse_line("日付：2026-10-21 ／ 体重：76.8kg（体脂肪率 20.5%）", POSTED)])["2026-10-21"]
    ch, _, _ = d.plan_day("2026-10-21", aggw, {}, {}, True)
    assert ch["体重"] == 76.8 and abs(ch["体脂肪率"] - 0.205) < 1e-9


def test_merge_memo():
    assert d.merge_memo("手書き\n[自動] 古い注記", ["要確認: A"]) == "手書き\n[自動] 要確認: A"
    assert d.merge_memo("", []) == ""
    assert d.merge_memo("手書き", []) == "手書き"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("OK: all tests passed")
