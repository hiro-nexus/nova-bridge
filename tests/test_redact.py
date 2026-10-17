"""redact（秘密情報の伏せ字）の単体テスト。テストに使う文字列は、実在しないダミーを、分割して組み立てる。

実行: ~/nova-bridge で  .venv/bin/python tests/test_redact.py
"""
import importlib.util
import sys
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent


def load(name: str):
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


redact_mod = load("redact")
MASK = redact_mod.MASK
redact = redact_mod.redact

# ダミー（実在しない値。ソースにそのままの形で書かない）
WEBHOOK = "https://hooks." + "slack.com/services/" + "T0AAAAAAAAA/B0BBBBBBBBB/" + "abcdefghijklmnopqrstuvwx"
SLACK_TOKEN = "xo" + "xb-" + "1234567890-abcdefghijkl"
APP_TOKEN = "xa" + "pp-" + "1-A0AAAAAAAAA-1234567890-abcdef"
ANTHROPIC = "sk-" + "ant-" + "api03-abcdefghijklmnop"
OPENAI = "sk-" + "abcdefghijklmnopqrstuvwx"
GITHUB = "gh" + "p_" + "abcdefghijklmnopqrstuvwxyz0123"
AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"


def test_no_secret():
    text = "今日の日付を教えてください。hooks と slack の話ではありません。 https://example.com/a"
    assert redact(text) == (text, [])


def test_each_kind():
    for value, kind in [
        (WEBHOOK, "Slack Webhook URL"),
        (SLACK_TOKEN, "Slack トークン"),
        (APP_TOKEN, "Slack アプリトークン"),
        (ANTHROPIC, "Anthropic API キー"),
        (OPENAI, "OpenAI API キー"),
        (GITHUB, "GitHub トークン"),
        (AWS, "AWS アクセスキー"),
    ]:
        clean, found = redact(f"前 {value} 後")
        assert clean == f"前 {MASK} 後", (kind, clean)
        assert found == [kind], (kind, found)


def test_slack_link_wrapping():
    # Slack は URL を <URL|表示名> に変えて送ってくる
    text = f"<{WEBHOOK}|hooks.slack.com/services/…>"
    clean, found = redact(text)
    assert WEBHOOK not in clean and "abcdefghijklmnopqrstuvwx" not in clean
    assert found == ["Slack Webhook URL"]
    assert clean == f"<{MASK}|hooks.slack.com/services/…>", clean


def test_multiple_and_private_key():
    text = f"{SLACK_TOKEN}\n{GITHUB}\n-----BEGIN RSA PRIVATE KEY-----\nAAAABBBB\n-----END RSA PRIVATE KEY-----\n以上"
    clean, found = redact(text)
    assert clean == f"{MASK}\n{MASK}\n{MASK}\n以上", clean
    assert sorted(found) == sorted(["Slack トークン", "GitHub トークン", "秘密鍵"]), found
    # 終わりの行がない鍵も、残りを伏せる
    clean2, found2 = redact("-----BEGIN PRIVATE KEY-----\nAAAA\nBBBB")
    assert clean2 == MASK and found2 == ["秘密鍵"], (clean2, found2)


def test_short_look_alikes_are_kept():
    # 短すぎる・形が違うものは対象外（誤検出を減らす）
    text = "xoxb-short sk-short ghp_short AKIA123"
    assert redact(text) == (text, [])


if __name__ == "__main__":
    test_no_secret()
    test_each_kind()
    test_slack_link_wrapping()
    test_multiple_and_private_key()
    test_short_look_alikes_are_kept()
    print("OK: all tests passed")
