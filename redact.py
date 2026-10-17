"""秘密情報らしき文字列を伏せ字にする。Slack には触れない。

AI に渡す前、また Vault に保存する前に使う。検出した種類は返すが、文字列そのものは返さない・記録しない。
"""
import re

MASK = "[機密情報を除外]"

# (種類の名前, 正規表現)。順番に適用する
PATTERNS = [
    ("Slack Webhook URL", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+")),
    ("Slack トークン", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Slack アプリトークン", re.compile(r"\bxapp-[A-Za-z0-9-]{10,}")),
    ("Anthropic API キー", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}")),
    ("OpenAI API キー", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("GitHub トークン", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("AWS アクセスキー", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("秘密鍵", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)")),
]


def redact(text: str) -> tuple[str, list[str]]:
    """(伏せ字にした本文, 見つかった種類の一覧) を返す。見つからなければ、本文はそのまま、一覧は空。"""
    found: list[str] = []
    for name, rx in PATTERNS:
        text, n = rx.subn(MASK, text)
        found.extend([name] * n)
    return text, found
