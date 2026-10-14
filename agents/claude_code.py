"""Claude Code（ヘッドレスモード）用アダプター。"""
import json
import logging
import os
import shutil
import subprocess

from .base import AgentAdapter, AgentAuthError, AgentResult

log = logging.getLogger(__name__)

# ヘッドレスでは許可確認ができないため、許可する操作をここで固定する。
# ファイル編集は --permission-mode acceptEdits で許可。外部への送信系は許可しない。
ALLOWED_TOOLS = [
    "Read", "Glob", "Grep", "WebSearch", "WebFetch",
    "Bash(git *)", "Bash(ls *)",
]

AUTH_ERROR_HINTS = ("authenticat", "/login", "oauth", "unauthorized", "401", "credentials")


class ClaudeCodeAdapter(AgentAdapter):
    def __init__(self, workdir: str, model: str | None = None):
        self.workdir = workdir
        self.model = model
        self.claude = shutil.which("claude")
        if not self.claude:
            raise RuntimeError("claude コマンドが PATH にありません")

    def _env(self) -> dict:
        env = dict(os.environ)
        # サブスクリプションではなくAPI従量課金に流れるのを防ぐ
        env.pop("ANTHROPIC_API_KEY", None)
        return env

    def run(self, prompt: str, session_id: str | None) -> AgentResult:
        cmd = [
            self.claude, "-p", "--output-format", "json",
            "--permission-mode", "acceptEdits",
            "--allowedTools", *ALLOWED_TOOLS,
        ]
        if self.model:
            cmd += ["--model", self.model]
        if session_id:
            cmd += ["--resume", session_id]

        # プロンプトは標準入力で渡す（"-" で始まる文をオプションと誤解させない）
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            cwd=self.workdir, env=self._env(),
        )

        data = None
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            pass

        if data is None:
            detail = (proc.stderr or proc.stdout or "").strip()[-500:]
            if any(h in detail.lower() for h in AUTH_ERROR_HINTS):
                raise AgentAuthError(detail)
            return AgentResult(text=f"Claude Code の実行に失敗しました（終了コード {proc.returncode}）\n{detail}",
                               session_id=session_id, is_error=True)

        text = data.get("result") or ""
        if data.get("is_error") and any(h in text.lower() for h in AUTH_ERROR_HINTS):
            raise AgentAuthError(text)

        denials = []
        for d in data.get("permission_denials") or []:
            name = d.get("tool_name", "?")
            arg = d.get("tool_input", {})
            hint = arg.get("command") or arg.get("file_path") or arg.get("url") or ""
            denials.append(f"{name} {hint}".strip())

        log.info("claude done: turns=%s cost_usd=%s denials=%d",
                 data.get("num_turns"), data.get("total_cost_usd"), len(denials))
        return AgentResult(text=text, session_id=data.get("session_id") or session_id,
                           denials=denials, is_error=bool(data.get("is_error")))
