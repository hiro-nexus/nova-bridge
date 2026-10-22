"""#diet-log の食事・体重の投稿を、毎晩集計して、Notion の既存DB「ダイエット記録」に書く。

実行: ~/nova-bridge で  .venv/bin/python diet_nightly.py [--dry-run] [--notify]
  --dry-run  Notion には書かず、#nova-hub にも投稿せず、書く予定の内容だけを表示する。
  --notify   --dry-run のときも、結果を #nova-hub に（「ドライラン」と明記して）投稿する。
毎晩 23:30 JST に、systemd の timer から1回だけ動いて終わる。Claude（エージェント）は呼ばない（コードだけで集計する。費用ゼロ）。

守ること:
- DB は新規作成しない。既存DB（database id は下の DB_ID）にだけ書く。数式の列（合計、差）には書かない。
- 1日1行。日付が当日の行を検索し、あれば更新、なければ作る。重複行は作らない（同じ日に複数行あれば、触らず「要確認」）。
- 投稿内の「日付：」で振り分ける。日付がなければ投稿時刻の日本時間の日付とし、メモに注記する。日をまたいだ混在はしない。
- 読み取れない投稿は、推測で埋めず、メモに「要確認」と書く。
- 既存の値を壊さない: 空欄、または、このスクリプトが前回書いた値のままのときだけ上書きする。Hiroさんが手で入れた値と食い違うときは、書き換えず、メモに「要確認」と書く。
- 毎回、直近の投稿を全部読み直して、日ごとに合計し直す（何度実行しても同じ結果になる）。
"""
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
DB_ID = "901fdc6d-bc7d-4b58-9160-0e643acc76da"
CHANNEL = "C0CAPLDBASK"
NOTION = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MEALS = ["朝", "昼", "夜", "間食"]
KEYS = {"日付", "区分", "内容", "kcal", "p", "f", "c", "体重", "体脂肪率"}
NUM = r"-?\d+(?:\.\d+)?"
AUTO = "[自動]"
LOOKBACK_DAYS = 3        # 今日から3日前の 0:00 以降の投稿を読む。この範囲の日付だけを処理する
EPS = 0.05

MACROS = ["kcal", "P", "F", "C"]
FORMULA_TOTAL = ["合計kcal", "合計P", "合計F", "合計C"]
FORMULA_DIFF = ["kcal差", "P差", "F差", "C差"]


# ---------- 読み取り（純粋な関数。テストする） ----------

def norm(s: str) -> str:
    """全角の数字・記号を半角にそろえる（「／」→「/」、「：」→「:」、全角数字→半角）。"""
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"(\d{4})/(\d{1,2})/(\d{1,2})", lambda m: f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}", s)


def num(v: str) -> float | None:
    m = re.search(NUM, v)
    return float(m.group()) if m else None


def split_fields(line: str) -> dict[str, str]:
    """「日付:… / 区分:… / 内容:…」を項目に分ける。項目名でない所（内容の中の / や :）は、直前の値の続きとして扱う。"""
    fields: dict[str, str] = {}
    last = None
    for seg in line.split("/"):
        key, sep, val = seg.partition(":")
        k = key.strip()
        if sep and k.lower() in KEYS:
            last = k.lower() if k.lower() in {"kcal", "p", "f", "c"} else k
            fields[last] = val.strip()
        elif last is not None:
            fields[last] += "/" + seg.strip()
    return fields


def parse_pipe(s: str, posted: datetime) -> dict | None:
    """縦棒区切りの1行（実際に Claude アプリが投稿している形式）を読む。
    日付|区分|内容|kcal|P|F|C        例: 2026-10-22|昼|弁当（唐揚げ2個…）|430|25|20|34
    日付|体重|76.8kg（朝）|-|-|-|-    （体脂肪率があれば、体重の欄に併記）
    先頭が日付でない行（見出し行など）は、記録ではないので None。"""
    parts = [p.strip() for p in s.split("|")]
    if len(parts) < 7:
        return None
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", parts[0])
    if not m:
        return None
    day = f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}"
    kind, content, vals = parts[1], "|".join(parts[2:-4]), parts[-4:]
    if kind == "体重":
        w = num(content)
        if w is None:
            return {"date": day, "error": "体重を読めない", "raw": s[:60], "inferred": False}
        bf = re.search(r"体脂肪率\D*(" + NUM + ")", content)
        return {"kind": "weight", "date": day, "weight": w, "bodyfat": float(bf[1]) if bf else None, "inferred": False}
    if kind not in MEALS:
        return {"date": day, "error": f"区分を読めない（{kind[:10]}）", "raw": s[:60], "inferred": False}
    nums = [num(v) for v in vals]       # 「-」は数字ではないので None になる
    if any(v is None for v in nums):
        return {"date": day, "error": f"{kind}の kcal／P／F／C のどれかが数字でない", "raw": s[:60], "inferred": False}
    return {"kind": "meal", "date": day, "meal": kind, "content": content, "vals": nums, "inferred": False}


def parse_line(line: str, posted: datetime) -> dict | None:
    """1行を読む。記録の行でなければ None。読めなければ error 付きで返す（推測で埋めない）。
    縦棒区切りの形式と、「日付：… ／ 区分：… ／ …」の形式の、どちらも読める。"""
    s = norm(line)
    if s.count("|") >= 6:
        return parse_pipe(s, posted)
    f = split_fields(s)
    if not f or ("区分" not in f and "体重" not in f):
        return None
    day, inferred = posted.strftime("%Y-%m-%d"), True
    if "日付" in f:
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", f["日付"])
        if not m:
            return {"date": day, "error": f"日付を読めない（{f['日付'][:20]}）", "raw": line[:60], "inferred": True}
        day, inferred = f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}", False
    if "体重" in f:
        w = num(f["体重"])
        if w is None:
            return {"date": day, "error": "体重を読めない", "raw": line[:60], "inferred": inferred}
        bf = f.get("体脂肪率") or (re.search(r"体脂肪率\D*(" + NUM + ")", f["体重"]) or [None, None])[1]
        return {"kind": "weight", "date": day, "weight": w, "bodyfat": num(bf) if bf else None, "inferred": inferred}
    meal = f["区分"].strip()
    if meal not in MEALS:
        return {"date": day, "error": f"区分を読めない（{meal[:10]}）", "raw": line[:60], "inferred": inferred}
    vals = [num(f.get(k, "")) if f.get(k) is not None else None for k in ("kcal", "p", "f", "c")]
    if any(v is None for v in vals):
        return {"date": day, "error": f"{meal}の kcal／P／F／C のどれかが無い", "raw": line[:60], "inferred": inferred}
    return {"kind": "meal", "date": day, "meal": meal, "content": f.get("内容", "").strip(), "vals": vals,
            "inferred": inferred}


def aggregate(records: list[dict]) -> dict[str, dict]:
    """日ごとに合計する。同じ区分に複数の投稿があれば足す。"""
    days: dict[str, dict] = {}
    for r in records:
        d = days.setdefault(r["date"], {"meals": {}, "contents": {}, "weight": None, "bodyfat": None, "notes": []})
        if r.get("error"):
            d["notes"].append(f"要確認: {r['error']}")
            continue
        if r.get("inferred"):
            d["notes"].append("日付は投稿時刻から推定")
        if r["kind"] == "weight":
            d["weight"], d["bodyfat"] = r["weight"], r["bodyfat"] if r["bodyfat"] is not None else d["bodyfat"]
        else:
            cur = d["meals"].setdefault(r["meal"], [0.0, 0.0, 0.0, 0.0])
            for i, v in enumerate(r["vals"]):
                cur[i] += v
            if r["content"]:
                d["contents"].setdefault(r["meal"], []).append(r["content"])
    for d in days.values():
        d["notes"] = sorted(set(d["notes"]))
    return days


def summary_text(contents: dict[str, list[str]]) -> str:
    return "／".join(f"{m}：{'、'.join(contents[m])}" for m in MEALS if m in contents)


def same(a, b) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) < EPS


def decide(existing, new, previously_written) -> str:
    """'write'（書く）／'skip'（同じなので不要）／'conflict'（既存の手入力と食い違う。書かない）。"""
    if new is None:
        return "skip"
    if existing is None or existing == "":
        return "write"
    if isinstance(new, str):
        # 文章（食事内容）は、既存の文章を優先して残す。空欄のとき、または前回このスクリプトが書いた文章のままのときだけ書く。
        # 書き方が違うだけの文章を「食い違い」にしない（要確認を積み重ねない）
        return "skip" if existing == new else ("write" if previously_written == existing else "skip")
    if same(existing, new):
        return "skip"
    return "write" if same(existing, previously_written) else "conflict"


def merge_memo(existing: str, notes: list[str]) -> str:
    """メモ欄の、手書きの部分は残し、自動の行（[自動] で始まる）だけを入れ替える。"""
    kept = [ln for ln in (existing or "").splitlines() if not ln.startswith(AUTO)]
    return "\n".join(kept + [f"{AUTO} {n}" for n in notes]).strip()


def plan_day(day: str, agg: dict, row: dict | None, written: dict, pct_bodyfat: bool) -> tuple[dict, list[str], list[str]]:
    """1日分の書き込み内容を決める。戻り値: (Notion に書く値, 表示用の説明, メモに足す注記)"""
    changes: dict[str, object] = {}
    lines: list[str] = []
    notes = list(agg["notes"])
    cur = row or {}

    def apply(prop: str, new, label: str) -> None:
        verdict = decide(cur.get(prop), new, written.get(prop))
        if verdict == "write":
            changes[prop] = new
            lines.append(f"  {label}: {cur.get(prop)} → {new}")
        elif verdict == "conflict":
            notes.append(f"要確認: {label}は既存の値（{cur.get(prop)}）と#diet-logの集計（{new}）が食い違うため、書き換えていない")
            lines.append(f"  {label}: 既存 {cur.get(prop)} と集計 {new} が食い違う → 書かない（要確認）")

    for meal, vals in agg["meals"].items():
        for name, v in zip(MACROS, vals):
            apply(f"{meal}{name}", round(v, 1), f"{meal}{name}")
    apply("食事内容", summary_text(agg["contents"]) or None, "食事内容")
    if agg["weight"] is not None:
        apply("体重", agg["weight"], "体重")
    if agg["bodyfat"] is not None:
        apply("体脂肪率", agg["bodyfat"] / 100 if pct_bodyfat else agg["bodyfat"], "体脂肪率")
    return changes, lines, sorted(set(notes))


# ---------- Notion ----------

def prop_value(p: dict):
    t = p.get("type")
    v = p.get(t)
    if t == "number":
        return v
    if t in ("rich_text", "title"):
        return "".join(x.get("plain_text", "") for x in v) or None
    if t == "formula":
        return v.get(v.get("type"))
    if t == "date":
        return (v or {}).get("start")
    return None


def row_values(page: dict) -> dict:
    return {k: prop_value(p) for k, p in page.get("properties", {}).items()}


def to_notion(prop: str, value) -> dict:
    if isinstance(value, str):
        return {"rich_text": [{"text": {"content": value[:1900]}}]}
    return {"number": value}


# ---------- 実行 ----------

def main(argv: list[str]) -> int:
    import requests
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    base = Path(__file__).resolve().parent
    sys.path.insert(0, str(base))
    from config import load_config, load_dotenv
    from connector import strip_connector_suffix

    load_dotenv(base / ".env")
    dry, notify_in_dry = "--dry-run" in argv, "--notify" in argv
    cfg = load_config()
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise SystemExit("NOTION_TOKEN が .env にありません")
    slack = WebClient(token=cfg.slack_bot_token)
    hdr = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}

    def notion(method: str, path: str, body: dict | None = None) -> dict:
        time.sleep(0.4)
        r = requests.request(method, f"{NOTION}{path}", headers=hdr, json=body, timeout=30)
        if r.status_code >= 300:
            msg = ""
            try:
                msg = r.json().get("message", "")
            except ValueError:
                pass
            raise RuntimeError(f"Notion {method} {path} → HTTP {r.status_code} {msg}")
        return r.json()

    def say(text: str) -> None:
        print(text)
        if not dry or notify_in_dry:
            slack.chat_postMessage(channel=cfg.nova_channel_id, text=("【ドライラン】" if dry else "") + text)

    state_path = cfg.state_dir / "diet_written.json"
    written_all = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    # 1. DB の構造を確かめる（足りない・型が違う列があれば、何も書かずに止める）
    db = notion("GET", f"/databases/{DB_ID}")
    props = db.get("properties", {})
    need = {"日": "title", "日付": "date", "食事内容": "rich_text", "メモ": "rich_text", "体重": "number", "体脂肪率": "number"}
    for m in MEALS:
        for n in MACROS:
            need[f"{m}{n}"] = "number"
    bad = [f"{k}（{need[k]}）" for k in need if props.get(k, {}).get("type") != need[k]]
    if bad:
        say("ダイエット記録の集計を止めました。DB に、次の列が無い、または型が違います: " + "、".join(bad))
        return 1
    pct_bodyfat = props["体脂肪率"]["number"].get("format") == "percent"

    # 2. #diet-log の直近の投稿を読む
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")
    oldest = (now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=LOOKBACK_DAYS)).timestamp()
    messages, cursor = [], None
    try:
        while True:
            res = slack.conversations_history(channel=CHANNEL, oldest=str(oldest), limit=200, cursor=cursor)
            messages += res["messages"]
            cursor = (res.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except SlackApiError as e:
        say(f"ダイエット記録の集計を止めました。#diet-log を読めません（{e.response.get('error')}）。Nova を #diet-log に招待してください。")
        return 1

    debug = "--debug" in argv    # 各投稿の先頭を表示して、読めない原因を調べる（食事の記録のみ。鍵は含まない）
    records, skipped = [], 0
    for msg in messages:
        posted = datetime.fromtimestamp(float(msg["ts"]), JST)
        who = "本人" if msg.get("user") == cfg.allowed_user_id else f"他({msg.get('user') or msg.get('bot_id') or '?'})"
        if msg.get("subtype") or msg.get("user") != cfg.allowed_user_id:
            skipped += 1
            if debug:
                print(f"[対象外 {posted:%m-%d %H:%M}] {who} subtype={msg.get('subtype')} 先頭={msg.get('text', '')[:80]!r}")
            continue
        n_before = len(records)
        for line in strip_connector_suffix(msg.get("text", "")).splitlines():
            rec = parse_line(line, posted)
            if rec:
                records.append(rec)
        if debug:
            print(f"[{'読めた' if len(records) > n_before else '読めない'} {posted:%m-%d %H:%M}] 先頭={msg.get('text', '')[:100]!r}")
    days = aggregate(records)
    print(f"読んだ投稿: {len(messages)}件（対象外 {skipped}件）／記録の行: {len(records)}件／日付: {', '.join(sorted(days)) or 'なし'}")

    if today not in days:
        say("本日は #diet-log に記録なし")
    reports = []
    for day in sorted(days):
        if day < (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d"):
            print(f"{day}: 古い日付なので処理しない")
            continue
        agg = days[day]
        if not agg["meals"] and agg["weight"] is None:
            if agg["notes"]:
                print(f"{day}: 読める記録なし／注記: {'、'.join(agg['notes'])}")
            continue
        found = notion("POST", f"/databases/{DB_ID}/query", {"filter": {"property": "日付", "date": {"equals": day}}})["results"]
        if len(found) > 1:
            say(f"{day} の行が{len(found)}件あります。重複のため触っていません（要確認）。")
            continue
        row = row_values(found[0]) if found else None
        written = written_all.get(day, {})
        changes, lines, notes = plan_day(day, agg, row, written, pct_bodyfat)
        memo_new = merge_memo((row or {}).get("メモ") or "", notes)
        memo_changed = memo_new != ((row or {}).get("メモ") or "").strip()
        print(f"{day}: {'更新' if found else '新規作成'}" + ("" if changes or memo_changed or not found else "（変更なし）"))
        for ln in lines:
            print(ln)
        if memo_changed:
            print(f"  メモ: {memo_new[:80]}")
        if dry or not (changes or memo_changed or not found):
            continue
        body = {k: to_notion(k, v) for k, v in changes.items()}
        if memo_changed:
            body["メモ"] = to_notion("メモ", memo_new)
        if found:
            page_id = found[0]["id"]
            if body:
                notion("PATCH", f"/pages/{page_id}", {"properties": body})
        else:
            body["日"] = {"title": [{"text": {"content": day}}]}
            body["日付"] = {"date": {"start": day}}
            page_id = notion("POST", "/pages", {"parent": {"database_id": DB_ID}, "properties": body})["id"]
        written_all.setdefault(day, {}).update(changes)
        state_path.write_text(json.dumps(written_all, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(state_path, 0o600)
        # 数式の結果（合計と目標との差）を読んで、報告に使う
        v = row_values(notion("GET", f"/pages/{page_id}"))
        fmt = lambda k: "-" if v.get(k) is None else f"{v[k]:,.0f}"  # noqa: E731
        reports.append(f"{day} 合計 {fmt('合計kcal')}kcal／P {fmt('合計P')}g／F {fmt('合計F')}g／C {fmt('合計C')}g"
                       f"（目標との差 kcal {fmt('kcal差')}／P {fmt('P差')}／F {fmt('F差')}／C {fmt('C差')}）")
    if reports:
        say("ダイエット記録を更新しました。\n" + "\n".join(reports))
    print("dry-run 終了。Notion には何も書いていません。" if dry else "完了。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:  # 失敗は #nova-hub に知らせる（内容は型名と短い説明だけ）
        print(f"失敗: {type(e).__name__}: {str(e)[:200]}")
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from config import load_config
            from slack_sdk import WebClient
            c = load_config()
            WebClient(token=c.slack_bot_token).chat_postMessage(
                channel=c.nova_channel_id, text=f"ダイエット記録の集計に失敗しました：{type(e).__name__}: {str(e)[:150]}")
        except Exception:
            pass
        sys.exit(1)
