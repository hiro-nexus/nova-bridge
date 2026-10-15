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
from gitsync import GitSync
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


def post(text: str) -> None:
    for part in chunks(to_slack(text) or "（空の返答でした）"):
        app.client.chat_postMessage(channel=cfg.nova_channel_id, text=part)


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


def capture(saver, channel: str, ts: str, text: str, via: str | None = None) -> None:
    """#context / #inbox の投稿を保存する（Claude は呼ばない）。"""
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

def handle(job: dict) -> None:
    ts, text = job["ts"], job["text"]

    if text.strip() in RESET_WORDS:
        sessions.reset()
        post("新しい会話を始めます。")
        react(ts, add="white_check_mark", remove="eyes")
        return

    done = threading.Event()

    def progress():
        while not done.wait(PROGRESS_INTERVAL):
            post("処理中です。もう少しお待ちください。")

    threading.Thread(target=progress, daemon=True).start()
    try:
        result = agent.run(text, sessions.get())
    except AgentAuthError:
        log.error("agent auth error")
        post("認証が切れました。SSHで再ログインが必要です。")
        react(ts, add="x", remove="eyes")
        return
    except Exception as e:
        log.exception("agent failed")
        post(f"エラーが発生しました：{type(e).__name__}: {str(e)[:300]}")
        react(ts, add="x", remove="eyes")
        return
    finally:
        done.set()

    if result.session_id:
        sessions.set(result.session_id)

    body = result.text
    if result.denials:
        body += "\n\n---\n権限がないため、次の操作は実行しませんでした：\n" + "\n".join(f"- {d}" for d in result.denials)
    post(body)
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
    pending_confirm.add(ts, plain(text))
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
        jobs.put({"ts": item["ts"], "text": text})


@app.event("app_mention")
def on_mention(event):
    pass  # 反応は message イベント側で行う（二重処理を防ぐ）


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    gitsync.recover()
    threading.Thread(target=gitsync.run, daemon=True).start()
    me = app.client.auth_test()
    log.info("connected as %s; agent=%s workdir=%s model=%s shortcut_bots=%d",
             me.get("user"), cfg.agent, cfg.agent_workdir, cfg.agent_model, len(cfg.shortcut_bots))
    SocketModeHandler(app, cfg.slack_app_token).start()
