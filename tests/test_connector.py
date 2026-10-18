"""connector.strip_connector_suffix の単体テスト。

実行: ~/nova-bridge で  .venv/bin/python tests/test_connector.py
"""
import importlib.util
import sys
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent


def load(name: str):
    """本番（tests/ の1つ上）か、Vault 内の下書き（接頭辞つき。新しい版を優先）から読み込む。"""
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


connector = load("connector")
strip = connector.strip_connector_suffix


def test_strip():
    # 同じ行・改行のあと・前後の空白
    assert strip("テスト1 *使用して送信されました* Claude") == "テスト1"
    assert strip("本文\n\n*使用して送信されました* Claude\n") == "本文"
    assert strip("本文  *使用して送信されました*   Claude  ") == "本文"
    # リンク記法の Claude
    assert strip("本文 *使用して送信されました* <https://claude.ai|Claude>") == "本文"
    # 英語表記
    assert strip("note *Sent using* Claude") == "note"
    # 複数行の本文は、改行を保つ
    assert strip("1行目\n2行目 *使用して送信されました* Claude") == "1行目\n2行目"
    # 定型文だけなら空
    assert strip("*使用して送信されました* Claude") == ""


def test_keep():
    # 付いていない投稿は変えない
    assert strip("ふつうの投稿") == "ふつうの投稿"
    # 本文の途中にある同じ文字列は変えない
    s = "*使用して送信されました* Claude と書かれた投稿の話"
    assert strip(s) == s
    # 末尾が別の語なら変えない
    s = "*使用して送信されました* Claudeの話"
    assert strip(s) == s, strip(s)
    # 2回付いていても、除くのは1回だけ
    assert strip("a *使用して送信されました* Claude *使用して送信されました* Claude") == "a *使用して送信されました* Claude"


if __name__ == "__main__":
    test_strip()
    test_keep()
    print("OK: all tests passed")
