"""capture / gitsync の単体テスト。一時フォルダだけを使い、本番の vault や Slack には触れない。

実行: ~/nova-bridge で  .venv/bin/python tests/test_capture.py
"""
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent


def load(name: str):
    """本番（tests/ の1つ上）か、Vault 内の下書き（接頭辞つき）から読み込む。"""
    for p in (HERE.parent / f"{name}.py", HERE / f"nova-bridge_phase2B_{name}.py"):
        if p.exists():
            spec = importlib.util.spec_from_file_location(name, p)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError(name)


capture = load("capture")
gitsync = load("gitsync")

# 2026-10-08 01:06:53 JST と、その60秒後
TS_A = "1791389213.000100"
TS_B = "1791389273.000200"


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(cwd), "-c", "core.quotepath=false", *args],
                       capture_output=True, text=True, check=True)
    return r.stdout


def test_slug():
    assert capture.slug("  Hello World!  ") == "Hello_World!"
    assert capture.slug("") == "memo"
    assert capture.slug("a/b:c") == "abc"
    assert capture.slug("#[]") == "memo"
    assert capture.slug("あ" * 30) == "あ" * 15
    assert capture.slug("1行目\n2行目") == "1行目"


def test_plain():
    got = capture.plain("<https://x.com/a|label> &amp; &lt;b&gt; <https://y.com>")
    assert got == "[label](https://x.com/a) & <b> https://y.com", got


def test_context(tmp: Path):
    p1 = capture.save_context(tmp, "C1", TS_A, "最初の発言\n2行目")
    p2 = capture.save_context(tmp, "C1", TS_B, "次の発言")
    assert p1 == p2
    assert p1.name == "2026-10-08_context.md", p1.name
    assert p1.parent == tmp / "70_AI_Lab/75_Context/10_self/slack"
    body = p1.read_text(encoding="utf-8")
    assert body.startswith("---\ntitle: 2026-10-08 context\n")
    assert "tags: [context]" in body and "status: raw" in body
    assert "origin_id: slack:C1:2026-10-08" in body
    assert body.count("updated: 2026-10-08") == 1
    assert body.index("## 01:06:53") < body.index("最初の発言") < body.index("## 01:07:53") < body.index("次の発言")
    assert not list(p1.parent.glob("*.tmp"))


def test_inbox(tmp: Path):
    a = capture.save_inbox(tmp, "C2", TS_A, '"テスト" 最初の一行\n本文')
    b = capture.save_inbox(tmp, "C2", TS_A, '"テスト" 最初の一行\n本文')
    c = capture.save_inbox(tmp, "C2", TS_A, '"テスト" 最初の一行\n本文')
    assert a.name == '2026-10-08_0106_テスト_最初の一行.md', a.name
    assert b.name == '2026-10-08_0106_テスト_最初の一行_53.md', b.name
    assert c.name == '2026-10-08_0106_テスト_最初の一行_53_2.md', c.name
    body = a.read_text(encoding="utf-8")
    assert 'title: "テスト 最初の一行"' in body
    assert "type: fleeting" in body and "tags: [inbox]" in body and "status: active" in body
    assert body.rstrip().endswith("本文")


def make_repos(tmp: Path):
    bare, vault = tmp / "bare.git", tmp / "vault"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True)
    subprocess.run(["git", "clone", "-q", str(bare), str(vault)], check=True, capture_output=True)
    git(vault, "config", "user.email", "t@example.com")
    git(vault, "config", "user.name", "t")
    (vault / "README.md").write_text("x", encoding="utf-8")
    git(vault, "add", "README.md")
    git(vault, "commit", "-q", "-m", "init")
    git(vault, "branch", "-M", "main")
    git(vault, "push", "-q", "-u", "origin", "main")
    return bare, vault


def test_gitsync(tmp: Path):
    bare, vault = make_repos(tmp)
    msgs = []
    sync = gitsync.GitSync(vault, notify=msgs.append)

    # 記録したファイルだけが push される
    p1 = capture.save_inbox(vault, "C2", TS_A, "テスト one")
    (vault / "other.md").write_text("関係ないファイル", encoding="utf-8")
    sync.mark(p1)
    sync.flush()
    names = git(bare, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert any(n.startswith("00_Inbox/2026-10-08_0106_") for n in names), names
    assert "other.md" not in names, names
    assert "capture:" in git(bare, "log", "--format=%s", "main")
    assert not sync.pending and not msgs

    # 再起動後の拾い直しは、ブリッジの保存形式のファイルだけ
    p2 = capture.save_context(vault, "C1", TS_A, "未送信")
    sync2 = gitsync.GitSync(vault, notify=msgs.append)
    sync2.recover()
    assert sync2.pending == {str(p2.relative_to(vault))}, sync2.pending

    # 手元から消えたファイルは対象から外れる（失敗扱いにしない）
    sync3 = gitsync.GitSync(vault, notify=msgs.append)
    sync3.mark(p2)
    p2.unlink()
    sync3.flush()
    assert not sync3.pending and sync3.failures == 0 and not msgs

    # push 先に届かないときは、3回目に1度だけ知らせ、ファイルは残す
    p3 = capture.save_inbox(vault, "C2", TS_B, "テスト two")
    git(vault, "remote", "set-url", "origin", str(tmp / "missing.git"))
    sync4 = gitsync.GitSync(vault, notify=msgs.append)
    sync4.mark(p3)
    for _ in range(4):
        sync4.flush()
    assert len(msgs) == 1, msgs
    assert p3.exists() and sync4.pending == {str(p3.relative_to(vault))}


if __name__ == "__main__":
    test_slug()
    test_plain()
    for t in (test_context, test_inbox, test_gitsync):
        with tempfile.TemporaryDirectory() as d:
            t(Path(d))
    print("OK: all tests passed")
