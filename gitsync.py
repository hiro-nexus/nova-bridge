"""保存したファイルを、最後の保存から一定時間たったらまとめて commit & push する。"""
import logging
import re
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger("nova-bridge.gitsync")

QUIET_SECONDS = 300   # 最後の保存から5分
POLL_SECONDS = 30
NOTIFY_AFTER = 3      # 連続失敗がこの回数に達したら知らせる
OURS = re.compile(
    r"^(00_Inbox/\d{4}-\d{2}-\d{2}_\d{4}_.+\.md"
    r"|70_AI_Lab/75_Context/10_self/slack/\d{4}-\d{2}-\d{2}_(context|journal_summary)\.md)$"
)


class GitSync:
    def __init__(self, vault: Path, notify, quiet_seconds: int = QUIET_SECONDS):
        self.vault = vault
        self.notify = notify  # (str) -> None。失敗を #nova-hub に知らせる
        self.quiet_seconds = quiet_seconds
        self.pending: set[str] = set()
        self.last_write = 0.0
        self.failures = 0
        self.lock = threading.Lock()

    def mark(self, path: Path) -> None:
        with self.lock:
            self.pending.add(str(path.relative_to(self.vault)))
            self.last_write = time.time()

    def recover(self) -> None:
        """再起動前に送れなかったファイルを拾い直す。"""
        # 日本語のファイル名が八進数にならないよう、quotepath を切る
        r = self._git("-c", "core.quotepath=false", "status", "--porcelain", "--untracked-files=all")
        for line in r.stdout.splitlines():
            name = line[3:].strip().strip('"')
            if OURS.match(name):
                with self.lock:
                    self.pending.add(name)
                    self.last_write = time.time()

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.vault), *args],
                              capture_output=True, text=True, timeout=120)

    def flush(self) -> None:
        with self.lock:
            files = sorted(self.pending)
        # 手元から消えたファイルは対象から外す（残すと add が失敗し続ける）
        gone = [f for f in files if not (self.vault / f).exists()]
        if gone:
            with self.lock:
                self.pending -= set(gone)
            files = [f for f in files if f not in gone]
        if not files:
            return
        steps = [
            ("pull", ["pull", "--rebase", "--autostash"]),
            ("add", ["add", "--", *files]),
            ("commit", ["commit", "-m", f"capture: {len(files)} file(s) from Slack", "--", *files]),
            ("push", ["push"]),
        ]
        for name, args in steps:
            r = self._git(*args)
            out = (r.stdout + r.stderr).strip()
            if r.returncode != 0 and not (name == "commit" and "nothing to commit" in out):
                self._git("rebase", "--abort")
                self.failures += 1
                log.error("gitsync %s failed (%d): %s", name, self.failures, out[-300:])
                if self.failures == NOTIFY_AFTER:
                    self.notify(f"vault への保存内容の push に{NOTIFY_AFTER}回続けて失敗しました（{name}）。ファイルは手元に残っています。")
                return
        with self.lock:
            self.pending -= set(files)
        self.failures = 0
        log.info("gitsync pushed %d file(s)", len(files))

    def run(self) -> None:
        while True:
            time.sleep(POLL_SECONDS)
            with self.lock:
                due = bool(self.pending) and time.time() - self.last_write >= self.quiet_seconds
            if due:
                try:
                    self.flush()
                except Exception:
                    log.exception("gitsync crashed")
