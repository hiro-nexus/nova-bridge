"""Nova Slack Bridge — Slack（Socket Mode）とエージェントの中継。

このファイルはエージェントの種類を知らない。agents/base.py の約束（run）だけを使う。
"""
import logging
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("nova-bridge")

CHUNK_SIZE = 3500          # Slack の1投稿あたりの上限より小さく
PROGRESS_INTERVAL = 600    # 10分ごとに「処理中です」
RESET_WORDS = {"新規", "!new"}

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


def capture(saver, channel: str, ts: str, text: str) -> None:
    """#context / #inbox の投稿を保存する（Claude は呼ばない）。"""
    try:
        path = saver(vault, channel, ts, plain(text))
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

@app.event("message")
def on_message(event, logger):
    # ボットの投稿・編集・削除などは無視（無限ループ防止）
    if event.get("subtype") or event.get("bot_id"):
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


@app.event("app_mention")
def on_mention(event):
    pass  # 反応は message イベント側で行う（二重処理を防ぐ）


if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    gitsync.recover()
    threading.Thread(target=gitsync.run, daemon=True).start()
    me = app.client.auth_test()
    log.info("connected as %s; agent=%s workdir=%s model=%s",
             me.get("user"), cfg.agent, cfg.agent_workdir, cfg.agent_model)
    SocketModeHandler(app, cfg.slack_app_token).start()
