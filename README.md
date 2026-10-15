# Nova Slack Bridge

Slack `#nova-hub` と Nova（エージェント）を Socket Mode でつなぐ中継。公開URL・開放ポートは使わない。

## 構成
- `bridge.py` — Slack 入出力。エージェントの種類を知らない
- `agents/base.py` — アダプターの約束 `run(prompt, session_id) -> AgentResult`
- `agents/claude_code.py` — Claude Code（`claude -p`）用アダプター
- `session_store.py` — 会話のセッションID（`state/sessions.json`）
- `config.py` — `.env` 読み込みとアダプター選択

## Slack での使い方
- `#nova-hub` に書くと Nova が返事をする（Hiro 本人の投稿だけに反応）
- 👀 受付 → ✅ 完了 / ❌ エラー
- `新規` または `!new` で会話をリセット
- 権限がなく実行できなかった操作は、返答の末尾に一覧で表示される

## 手動起動（テスト用）
```bash
cd ~/nova-bridge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # 初回のみ
.venv/bin/python bridge.py      # Ctrl+C で停止
```

## 常駐（systemd）
`/etc/systemd/system/nova-bridge.service`（原本は `./nova-bridge.service`）。落ちたら5秒後に自動再起動、サーバー起動時に自動起動。

```bash
systemctl status nova-bridge            # 状態
sudo systemctl restart nova-bridge      # 再起動（.env やコードを変えたとき）
sudo systemctl disable --now nova-bridge  # 停止（Telegram へ戻すとき）
sudo journalctl -u nova-bridge -f       # ログ（hiro は systemd-journal グループ外のため sudo が必要）
```
手動起動（下記）とサービスを同時に動かさないこと（同じメッセージを二重に処理する）。

## 権限
`agents/claude_code.py` の `ALLOWED_TOOLS` と `--permission-mode acceptEdits` で固定。
外部への送信（メール・カレンダー登録など）は許可していない。

## エージェントの差し替え
1. `agents/<name>.py` に `AgentAdapter` を継承したクラスを作る
2. `config.py` の `build_agent()` に分岐を1つ追加する
3. `.env` の `AGENT=<name>` を変えて再起動する

## 注意
- `.env` は chmod 600。トークンをログやチャットに出さない
- `ANTHROPIC_API_KEY` はアダプター内で必ず環境から外す（API従量課金の防止）

## #context / #inbox の保存（Phase 2-B）
- `#context` に書く → `70_AI_Lab/75_Context/10_self/slack/YYYY-MM-DD_context.md` に時刻つきで追記
- `#inbox` に書く → `00_Inbox/YYYY-MM-DD_HHMM_<先頭15文字>.md` に1件1ファイルで保存
- Claude は呼ばない。コードが直接書く（`capture.py`）。保存できたら ✅、失敗したら ❌ とチャンネルに理由
- Hiro 本人の通常投稿だけが対象（ボット・Webhook・他ユーザー・編集・削除・スレッド返信・空の投稿は無視）
- 最後の保存から5分後に、保存したファイルだけをまとめて `git pull --rebase --autostash` → `add` → `commit` → `push`（`gitsync.py`）
- 有効にするには `.env` に `CONTEXT_CHANNEL_ID` / `INBOX_CHANNEL_ID` を書いて再起動（空なら無効）
- テスト: `.venv/bin/python tests/test_capture.py`

## Siri ショートカット（Webhook）の投稿（Phase 2-D）
- 専用の Slack アプリ「Nova Shortcut」の Webhook だけが対象。`.env` の `SHORTCUT_BOT_ID` と一致するボットの投稿（`subtype=bot_message`）だけを受け付ける。空なら無効
- `#context` / `#inbox` に届いたら、キーボード投稿と同じ保存（`#context` は見出しに `(音声)`、`#inbox` は frontmatter に `via: 音声`）
- `#nova-hub` に届いたら、すぐには実行しない。👀 を付けて「実行しますか？」とスレッドに返信し、**Hiro 本人が元の投稿にチェック（✅）を付けたときだけ**実行する。24時間で失効。ブリッジを再起動すると確認待ちは消える
- 流量制限: 10分に20件まで。超えた分は保存せず、`#nova-hub` に1度だけ知らせる
- Webhook の URL が漏れたら: Slack アプリの設定で URL を無効にし、新しい URL を発行してショートカットを更新する
- Nova アプリの設定に、`reactions:read` の権限と `reaction_added` のイベント購読が必要
- テスト: `.venv/bin/python tests/test_shortcut.py`

### 訂正（Phase 2-F）: Webhook のボットIDは、Webhook ごとに違う
- 実機で確認したところ、Webhook の投稿のボットIDは、Webhook（＝チャンネル）ごとに異なった（`#inbox` と `#context` で別のID）
- `.env` の `SHORTCUT_BOT_ID`（単一）は廃止し、`SHORTCUT_BOTS=ボットID:チャンネルID,ボットID:チャンネルID` に置き換えた
- 許可する条件は「ボットIDが一致し、かつ、そのボットに結びつけたチャンネルに投稿されたこと」
- 新しい Webhook を足したとき: テスト投稿を1件送ると、ログ（`state/bridge.log`）に `ignored bot/subtype message: channel=… bot_id=…` が出る。そのボットIDとチャンネルIDを `SHORTCUT_BOTS` に足して再起動する
