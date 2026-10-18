"""Claude アプリ（Slack コネクタ）経由の投稿の末尾に付く定型の文言を取り除く。

例: 「テストです *使用して送信されました* Claude」→「テストです」
末尾にあるときだけ除く。本文の途中にある同じ文字列は変えない。
"""
import re

# 「Claude」は、そのまま書かれる場合と、Slack のリンク記法（<https://…|Claude>）の場合がある
_NAME = r"(?:Claude|<[^>]*\|Claude>|<[^>]*claude[^>]*>)"
_SUFFIX = re.compile(
    r"[ \t\r\n]*[*_]?(?:使用して送信されました|Sent using)[*_]?[ \t]*" + _NAME + r"[ \t\r\n]*\Z",
    re.IGNORECASE,
)


def strip_connector_suffix(text: str) -> str:
    """末尾の定型文を1回だけ除く。除いた結果が空になるときは、空文字を返す。"""
    return _SUFFIX.sub("", text, count=1)
