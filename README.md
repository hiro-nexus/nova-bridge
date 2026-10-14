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
