"""Nova Slack Bridge — Slack（Socket Mode）とエージェントの中継。

このファイルはエージェントの種類を知らない。agents/base.py の約束（run）だけを使う。
"""
import logging
import logging.handlers
import queue
import re
import threading
from pathlib import Path

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from agents.base import AgentAuthError
from capture import plain, save_context, save_inbox
from config import build_agent, load_config
from connector import strip_connector_suffix
from gitsync import GitSync
from journal import DoneStore, build_prompt, day_of, is_morning_post, save_summary
from redact import redact
from session_store import SessionStore
from shortcut import PendingConfirmations, RateLimiter

# ログは journal に加えて、state/bridge.log にも書く（sudo なしで読めるように。本文は記録しない）
_state_dir = Path(__file__).resolve().parent / "state"
_state_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(_state_dir / "bridge.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
    ],
)
log = logging.getLogger("nova-bridge")

CHUNK_SIZE = 3500          # Slack の1投稿あたりの上限より小さく
PROGRESS_INTERVAL = 600    # 10分ごとに「処理中です」
RESET_WORDS = {"新規", "!new"}
CONFIRM_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check"}

cfg = load_config()
agent = build_agent(cfg)
sessions = SessionStore(cfg.state_dir / "sessions.json", key=cfg.nova_channel_id)
app = App(token=cfg.slack_bot_token)
jobs: "queue.Queue[dict]" = queue.Queue()

# #context / #inbox は Claude を呼ばず、コードが直接 vault に保存する
vault = Path(cfg.agent_workdir)
capture_by_channel = {}
if cfg.context_channel_id:
    capture_by_channel[cfg.context_channel_id] = save_context
if cfg.inbox_channel_id:
    capture_by_channel[cfg.inbox_channel_id] = save_inbox

# Siri ショートカット（Webhook）の投稿。許可するのは、SHORTCUT_BOTS に書いた「ボットID:チャンネルID」の組と一致するものだけ
shortcut_limiter = RateLimiter()      # 10分に20件まで
pending_confirm = PendingConfirmations()  # #nova-hub への音声の指示は、本人の確認を待つ

# #journal：朝の投稿へのスレッド返信（Claude アプリの回答のまとめ）。処理済みの返信を覚えて、二重処理を防ぐ
journal_done = DoneStore(cfg.state_dir / "journal_done.json")
_bot_user_id: str | None = None


# ---- Slack 出力 ----

def to_slack(text: str) -> str:
    """Markdown を Slack の mrkdwn に最低限寄せる（見出し・太字のみ）。コードブロック内は変えない。"""
    parts = re.split(r"(```.*?```)", text, flags=re.S)
    for i in range(0, len(parts), 2):
        p = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", parts[i], flags=re.M)
        parts[i] = re.sub(r"\*\*(.+?)\*\*", r"*\1*", p)
    return "".join(parts)


def chunks(text: str, size: int = CHUNK_SIZE):
    """改行の位置で切り、size を超えないように分割する。"""
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        yield text[:cut]
        text = text[cut:].lstrip("\n")
    if text:
        yield text


def post(text: str, thread_ts: str | None = None, broadcast: bool = False) -> None:
    """#nova-hub に投稿する。thread_ts があればそのスレッドへ。broadcast なら、スレッドの返信を本流にも出す（最初の1通だけ）。"""
    for i, part in enumerate(chunks(to_slack(text) or "（空の返答でした）")):
        extra = {}
        if thread_ts:
            extra["thread_ts"] = thread_ts
            if broadcast and i == 0:
                extra["reply_broadcast"] = True
        app.client.chat_postMessage(channel=cfg.nova_channel_id, text=part, **extra)


def react(ts: str, add: str | None = None, remove: str | None = None, channel: str | None = None) -> None:
    channel = channel or cfg.nova_channel_id
    try:
        if remove:
            app.client.reactions_remove(channel=channel, timestamp=ts, name=remove)
        if add:
            app.client.reactions_add(channel=channel, timestamp=ts, name=add)
    except Exception as e:  # リアクションの失敗で本処理を止めない
        log.warning("reaction failed: %s", e)


gitsync = GitSync(vault, notify=post)


def scrub(text: str, channel: str) -> str:
    """Claude アプリ経由の定型文を除き、秘密情報らしき文字列を伏せ字にする。見つけたら、そのチャンネルに警告を出す（文字列そのものは書かない・記録しない）。"""
    text = strip_connector_suffix(text)
    clean, found = redact(text)
    if found:
        kinds = "、".join(sorted(set(found)))
        log.warning("secret-like strings redacted: channel=%s kinds=%s count=%d", channel, kinds, len(found))
        try:
            app.client.chat_postMessage(
                channel=channel,
                text=f"秘密情報らしき文字列（{kinds}）を検出したため、伏せ字にして処理します。元の投稿は Slack に残っています。投稿を削除し、該当の URL や鍵は無効にして、作り直してください。",
            )
        except Exception:
            log.exception("could not post secret warning")
    return clean


def capture(saver, channel: str, ts: str, text: str, via: str | None = None) -> None:
    """#context / #inbox の投稿を保存する（Claude は呼ばない）。保存の前に、定型文を除き、秘密情報を伏せ字にする。"""
    text = scrub(text, channel)
    if not text.strip():  # 定型文だけの投稿は保存しない
        log.info("skipped empty capture after cleanup: channel=%s", channel)
        return
    try:
        path = saver(vault, channel, ts, plain(text), via=via)
        gitsync.mark(path)
        log.info("captured %d chars -> %s", len(text), path.name)
        react(ts, add="white_check_mark", channel=channel)
    except Exception as e:
        log.exception("capture failed")
        react(ts, add="x", channel=channel)
        try:
            app.client.chat_postMessage(channel=channel, text=f"保存に失敗しました：{type(e).__name__}")
        except Exception:
            log.exception("could not report capture failure")


# ---- 処理（1件ずつ直列） ----

def handle_journal(job: dict) -> None:
    """#journal のまとめの清書。#nova-hub の会話とは別の、新しい会話で実行する。結果は #nova-hub に短く知らせる。"""
    channel, ts = cfg.journal_channel_id, job["ts"]
    try:
        result = agent.run(job["prompt"], None)
    except AgentAuthError:
        log.error("agent auth error (journal)")
        post("ジャーナルの清書ができませんでした。認証が切れました。SSHで再ログインが必要です。原文は保存済みです。")
        react(ts, add="x", channel=channel)
        return
    except Exception as e:
        log.exception("journal agent failed")
        post(f"ジャーナルの清書に失敗しました：{type(e).__name__}。原文は保存済みです。")
        react(ts, add="x", channel=channel)
        return
    body = f"ジャーナルの清書：{result.text}"
    if result.denials:
        body += "\n\n権限がないため、次の操作は実行しませんでした：\n" + "\n".join(f"- {d}" for d in result.denials)
    post(body)
    if result.is_error:
        react(ts, add="x", channel=channel)
    log.info("journal job done: error=%s", result.is_error)


def handle(job: dict) -> None:
    if job.get("journal"):
        handle_journal(job)
        return
    ts, text = job["ts"], job["text"]
    # 確認ずみの音声の指示は、元の投稿のスレッドに返事をする。最初の1通は本流にも出して、見つけやすくする
    thread_ts = job.get("thread_ts")

    def say(message: str) -> None:
        post(message, thread_ts=thread_ts, broadcast=bool(thread_ts))

    if text.strip() in RESET_WORDS:
        sessions.reset()
        say("新しい会話を始めます。")
        react(ts, add="white_check_mark", remove="eyes")
        return

    done = threading.Event()

    def progress():
        while not done.wait(PROGRESS_INTERVAL):
            post("処理中です。もう少しお待ちください。", thread_ts=thread_ts)

    threading.Thread(target=progress, daemon=True).start()
    try:
        result = agent.run(text, sessions.get())
    except AgentAuthError:
        log.error("agent auth error")
        say("認証が切れました。SSHで再ログインが必要です。")
        react(ts, add="x", remove="eyes")
        return
    except Exception as e:
        log.exception("agent failed")
        say(f"エラーが発生しました：{type(e).__name__}: {str(e)[:300]}")
        react(ts, add="x", remove="eyes")
        return
    finally:
        done.set()

    if result.session_id:
        sessions.set(result.session_id)

    body = result.text
    if result.denials:
        body += "\n\n---\n権限がないため、次の操作は実行しませんでした：\n" + "\n".join(f"- {d}" for d in result.denials)
    if thread_ts:
        # どの指示への返事かが分かるように、冒頭に指示を1行で添える
        one_line = " ".join(text.split())
        body = f"音声の指示：「{one_line[:80]}{'…' if len(one_line) > 80 else ''}」への返事です。\n\n{body}"
    say(body)
    react(ts, add="x" if result.is_error else "white_check_mark", remove="eyes")


def worker() -> None:
    while True:
        job = jobs.get()
        try:
            handle(job)
        except Exception:
            log.exception("job failed")
        finally:
            jobs.task_done()


# ---- Slack 入力 ----

def bot_user_id() -> str:
    global _bot_user_id
    if _bot_user_id is None:
        _bot_user_id = app.client.auth_test()["user_id"]
    return _bot_user_id


def on_journal_message(event) -> None:
    """#journal：Nova の朝の投稿へのスレッド返信（Hiroさん本人）だけを、回答のまとめとして受け取る。"""
    channel, ts = event.get("channel"), event.get("ts")
    if event.get("user") != cfg.allowed_user_id:
        log.info("ignored journal message from other user")
        return
    thread_ts = event.get("thread_ts")
    if not thread_ts or thread_ts == ts:
        log.info("ignored top-level post in journal channel")
        return
    text = event.get("text") or ""
    if not text.strip() or journal_done.seen(ts):
        return
    try:
        replies = app.client.conversations_replies(channel=channel, ts=thread_ts, limit=1, inclusive=True)
        parent = (replies.get("messages") or [None])[0]
        is_post = is_morning_post(parent, bot_user_id())
    except Exception as e:
        log.exception("journal parent check failed")
        post(f"#journal の返信の確認に失敗しました：{type(e).__name__}。Nova のアプリに #journal の履歴を読む権限があるか、確認が必要です。")
        return
    if not is_post:
        log.info("ignored journal reply: parent is not a Nova post")
        return
    journal_done.add(ts)  # 先に記録する（再配信や再起動で二重に処理しない）
    text = scrub(text, channel)
    if not text.strip():  # 定型文だけの返信は保存しない
        log.info("skipped empty journal reply after cleanup")
        return
    try:
        path = save_summary(vault, channel, thread_ts, ts, plain(text))
    except Exception as e:
        log.exception("journal summary save failed")
        react(ts, add="x", channel=channel)
        post(f"ジャーナルのまとめの保存に失敗しました：{type(e).__name__}")
        return
    gitsync.mark(path)
    react(ts, add="white_check_mark", channel=channel)
    log.info("journal summary saved: %d chars -> %s", len(text), path.name)
    jobs.put({"journal": True, "ts": ts,
              "prompt": build_prompt(day_of(thread_ts), str(path.relative_to(vault)), plain(text))})


def on_shortcut_message(event) -> None:
    """専用アプリ「Nova Shortcut」の Webhook 投稿。保存用チャンネルには保存、#nova-hub は確認待ち。"""
    channel, ts = event.get("channel"), event.get("ts")
    text = event.get("text") or ""
    if not text.strip():
        return
    saver = capture_by_channel.get(channel)
    if saver is None and channel != cfg.nova_channel_id:
        log.info("ignored shortcut message in other channel %s", channel)
        return
    if not shortcut_limiter.allow():
        log.warning("shortcut rate limit exceeded")
        if shortcut_limiter.should_notify():
            post("音声（Webhook）の投稿が短時間に多すぎるため、一部を受け付けませんでした。心当たりがなければ、Webhook の URL を無効にしてください。")
        return
    if saver:
        capture(saver, channel, ts, text, via="音声")
        return
    # #nova-hub：すぐには実行せず、Hiroさん本人の確認（チェックのリアクション）を待つ
    pending_confirm.add(ts, plain(scrub(text, cfg.nova_channel_id)))
    log.info("voice instruction waiting for confirmation: %d chars", len(text))
    react(ts, add="eyes")
    app.client.chat_postMessage(
        channel=cfg.nova_channel_id, thread_ts=ts,
        text="この音声の指示を実行しますか？ 実行する場合は、元の投稿にチェック（✅）のリアクションを付けてください。24時間で失効します。",
    )


@app.event("message")
def on_message(event, logger):
    # 受け取った出来事の形だけを記録する（本文は記録しない）。原因調査のため
    log.info("event: channel=%s subtype=%s bot_id=%s user_is_hiro=%s thread=%s",
             event.get("channel"), event.get("subtype"), event.get("bot_id"),
             event.get("user") == cfg.allowed_user_id, bool(event.get("thread_ts")))
    # 許可リスト（ボットIDと、そのボットのチャンネル）に一致する Webhook 投稿だけは、別の経路で扱う
    if (event.get("subtype") == "bot_message"
            and cfg.shortcut_bots.get(event.get("bot_id")) == event.get("channel")):
        on_shortcut_message(event)
        return
    # それ以外のボットの投稿・編集・削除などは無視（無限ループ防止）
    if event.get("subtype") or event.get("bot_id"):
        log.info("ignored bot/subtype message: channel=%s subtype=%s bot_id=%s",
                 event.get("channel"), event.get("subtype"), event.get("bot_id"))
        return
    channel, user = event.get("channel"), event.get("user")
    if cfg.journal_channel_id and channel == cfg.journal_channel_id:
        on_journal_message(event)
        return
    saver = capture_by_channel.get(channel)
    if channel != cfg.nova_channel_id and not saver:
        log.info("ignored message in other channel %s", channel)
        return
    if user != cfg.allowed_user_id:
        log.info("ignored message from other user")
        return
    text = event.get("text") or ""
    if saver:
        # スレッド返信と空の投稿は保存しない
        if (event.get("thread_ts") and event["thread_ts"] != event["ts"]) or not text.strip():
            return
        capture(saver, channel, event["ts"], text)
        return
    text = scrub(text, cfg.nova_channel_id)  # AI に渡す前に、定型文を除き、秘密情報を伏せ字にする
    if not text.strip():  # 定型文だけの投稿は実行しない
        log.info("ignored empty message after cleanup")
        return
    log.info("accepted message: %d chars (queue=%d)", len(text), jobs.qsize())
    react(event["ts"], add="eyes")
    jobs.put({"ts": event["ts"], "text": text})


@app.event("reaction_added")
def on_reaction(event):
    """Hiroさん本人が、確認待ちの音声の指示にチェックを付けたら実行する。"""
    log.info("reaction_added: user_is_hiro=%s reaction=%s", event.get("user") == cfg.allowed_user_id, event.get("reaction"))
    if event.get("user") != cfg.allowed_user_id or event.get("reaction") not in CONFIRM_REACTIONS:
        return
    item = event.get("item") or {}
    if item.get("type") != "message" or item.get("channel") != cfg.nova_channel_id:
        return
    status, text = pending_confirm.pop(item.get("ts"))
    if status == "expired":
        post("確認の期限（24時間）が切れた音声の指示です。もう一度送ってください。")
    elif status == "ok":
        log.info("voice instruction confirmed: %d chars", len(text))
        jobs.put({"ts": item["ts"], "text": text, "thread_ts": item["ts"]})


@app.event("app_mention")
def on_mention(event):
    pass  # 反応は message イベント側で行う（二重処理を防ぐ）


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    gitsync.recover()
    threading.Thread(target=gitsync.run, daemon=True).start()
    me = app.client.auth_test()
    _bot_user_id = me.get("user_id")
    log.info("connected as %s; agent=%s workdir=%s model=%s shortcut_bots=%d journal=%s",
             me.get("user"), cfg.agent, cfg.agent_workdir, cfg.agent_model, len(cfg.shortcut_bots),
             "on" if cfg.journal_channel_id else "off")
    SocketModeHandler(app, cfg.slack_app_token).start()
