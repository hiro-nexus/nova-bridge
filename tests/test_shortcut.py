"""shortcut（流量制限と確認待ち）と、許可リストの読み込みの単体テスト。Slack には触れない。

実行: ~/nova-bridge で  .venv/bin/python tests/test_shortcut.py
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


shortcut = load("shortcut")
config = load("config")


def test_rate_limiter():
    rl = shortcut.RateLimiter(limit=3, window=600)
    assert [rl.allow(now=t) for t in (0, 1, 2)] == [True, True, True]
    assert rl.allow(now=3) is False            # 4件目は断る
    assert rl.should_notify(now=3) is True     # 知らせるのは1度だけ
    assert rl.should_notify(now=4) is False
    assert rl.allow(now=600) is True           # 0 の分が窓から出て、1件空く
    assert rl.allow(now=600) is False          # 窓内は 1, 2, 600 の3件
    assert rl.should_notify(now=602) is False  # 前回の通知（3）から600秒たっていない
    assert rl.should_notify(now=603) is True


def test_pending_confirmations():
    pc = shortcut.PendingConfirmations(ttl=100)
    pc.add("1.0", "指示A", now=0)
    assert pc.pop("1.0", now=50) == ("ok", "指示A")
    assert pc.pop("1.0", now=51) == ("none", "")    # 取り出したら消える（二重実行しない）
    pc.add("2.0", "指示B", now=0)
    assert pc.pop("2.0", now=101) == ("expired", "")
    pc.add("3.0", "指示C", now=0)
    pc.add("4.0", "指示D", now=200)                 # 期限切れの 3.0 は掃除される
    assert "3.0" not in pc.items and "4.0" in pc.items
    assert pc.pop("9.9", now=1) == ("none", "")     # 登録のない投稿（本人の通常の投稿など）


def test_parse_pairs():
    assert config.parse_pairs("B1:C1,B2:C2") == {"B1": "C1", "B2": "C2"}
    assert config.parse_pairs(" B1 : C1 , , B2:C2 ") == {"B1": "C1", "B2": "C2"}
    assert config.parse_pairs("") == {}
    assert config.parse_pairs("B1") == {}           # チャンネルがない組は無視する
    assert config.parse_pairs(":C1") == {}          # ボットIDがない組も無視する
    # 照合の条件: ボットIDと、そのボットのチャンネルの両方が一致したときだけ許可
    allow = config.parse_pairs("B1:C1")
    assert allow.get("B1") == "C1"                  # 一致 -> 許可
    assert allow.get("B1") != "C2"                  # 別のチャンネルへの投稿 -> 不許可
    assert allow.get("B9") != "C1"                  # 知らないボット -> 不許可
    assert allow.get(None) != "C1"                  # ボットIDがない投稿 -> 不許可


if __name__ == "__main__":
    test_rate_limiter()
    test_pending_confirmations()
    test_parse_pairs()
    print("OK: all tests passed")
