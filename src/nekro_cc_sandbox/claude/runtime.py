"""Claude Code runtime management"""

import asyncio
import json
import os
import re
import shlex
import signal
import time
from collections import Counter, deque
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from ..errors import ClaudeCliError, ErrorCode, new_err_id
from ..settings import Settings
from ..workspace import WorkspaceManager
from .policy import RuntimePolicy


@dataclass
class WorkspaceTaskInfo:
    """队列中或运行中的工作区任务信息"""
    source_chat_key: str        # 发起任务的 NA 频道标识
    prompt_preview: str         # 任务描述预览（前 80 字符）
    enqueued_at: datetime       # 进入队列时间（含 tz）

    started_at: datetime | None = None  # 开始执行时间（等待时为 None）

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at:
            return (datetime.now(UTC) - self.started_at).total_seconds()
        return 0.0

    @property
    def wait_seconds(self) -> float:
        return (datetime.now(UTC) - self.enqueued_at).total_seconds()

    def to_dict(self) -> dict:
        return {
            "source_chat_key": self.source_chat_key,
            "prompt_preview": self.prompt_preview,
            "enqueued_at": self.enqueued_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "wait_seconds": round(self.wait_seconds, 1),
        }


@dataclass
class QueueWaitEvent:
    """排队等待事件（send_message_in_workspace 在等待锁期间 yield 此对象）"""
    position: int                        # 排队位置（1 = 下一个执行）
    current_task: WorkspaceTaskInfo      # 当前正在执行的任务
    queued_count: int                    # 总等待数（含自身）


@dataclass
class ToolCallEvent:
    """Claude Code 工具调用事件"""
    tool_use_id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResultEvent:
    """Claude Code 工具结果事件"""
    tool_use_id: str
    content: str
    is_error: bool


@dataclass
class ClaudeSession:
    """Represents a Claude Code session"""

    session_id: str
    workspace_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_active: datetime = field(default_factory=lambda: datetime.now(UTC))
    is_active: bool = True


# After an empty "success" result at the start of a resumed run, keep reading stdout for the real
# result, but give up if the CLI prints nothing at all for this long (it may still be waiting for
# the model, so this is generous; a force-cancel stays available to the caller).
EMPTY_RESULT_READ_ON_IDLE_SECONDS = 300.0


def _is_empty_success_result(
    *,
    result_seen_ok: bool,
    output_tokens: int | None,
    final_result_text: str | None,
    last_assistant_text: str | None,
    cli_error_summary: str | None,
    tool_activity: bool,
) -> bool:
    """A success `result` that carries nothing at all (0 output tokens, no text, no tool activity).

    Claude Code prints exactly this when it resumes a session cut off by a force-cancel (SIGKILL of
    the whole process group) while a background Bash task was running: it first handles the
    "background task stopped" notification with a synthetic turn ("No response requested.", model
    "<synthetic>", no model call) and reports that as a separate, empty "success" result, and only
    then works on the new prompt and prints a second, real result in the same process. Nothing was
    shown to the caller before it, so reading on (or retrying) does not duplicate anything.
    A missing token count is accepted; a positive one is not (the model really answered).
    """
    if not result_seen_ok or cli_error_summary or tool_activity:
        return False
    if final_result_text or last_assistant_text:
        return False
    return output_tokens is None or output_tokens == 0


class ClaudeRuntime:
    """
    Manages Claude Code as a Workspace Agent.

    Key principles:
    - Session persistence across requests (by storing `session_id` in workspace state)
    - Prefer *one-shot* Agent SDK CLI invocations (`claude -p`) over a long-lived REPL subprocess
      because:
        - Claude Code `-p` output and permissions are designed for programmatic use
        - Running `claude` under pipes (no TTY) can hang in some environments; we use a pseudo-tty
          wrapper to make behavior reliable (see `.cursor/docs/knowledge.md`)
    """

    def __init__(
        self,
        workspace_manager: WorkspaceManager,
        skip_permissions: bool = True,
        env_overrides: dict[str, str] | None = None,
        policy: RuntimePolicy | None = None,
        settings_path: Path | None = None,
    ) -> None:
        self.workspace_manager: WorkspaceManager = workspace_manager
        self.skip_permissions: bool = skip_permissions
        self._static_env_overrides: dict[str, str] = env_overrides or {}
        self._settings_path: Path | None = settings_path
        self.policy: RuntimePolicy = policy or RuntimePolicy.relaxed()
        self._lock: asyncio.Lock = asyncio.Lock()
        self._sessions: dict[str, ClaudeSession] = {}
        self._active_workspace_id: str = "default"
        self._last_init_tools: dict[str, list[str]] = {}

        # 工作区任务队列（per workspace）
        self._workspace_locks: dict[str, asyncio.Lock] = {}
        self._workspace_current_task: dict[str, WorkspaceTaskInfo | None] = {}
        self._workspace_queued_tasks: dict[str, list[WorkspaceTaskInfo]] = {}
        self._workspace_procs: dict[str, asyncio.subprocess.Process | None] = {}
        self._workspace_force_cancelled: dict[str, bool] = {}

        # ANSI stripping (pseudo-tty wrapper may emit terminal controls)
        self._ansi_csi = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
        self._ansi_osc = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")

    def _get_env_overrides(self) -> dict[str, str]:
        """每次 spawn 时动态读取 settings.json，支持 NA 侧热更新模型预设而无需重启容器。"""
        if self._settings_path is not None:
            try:
                return Settings.load(self._settings_path).get_env_vars()
            except Exception as e:
                logger.warning(f"[ClaudeRuntime] 读取 settings.json 失败，使用静态配置: {e}")
        return self._static_env_overrides

    def _strip_ansi_and_controls(self, text: str) -> str:
        stripped = self._ansi_csi.sub("", text)
        stripped = self._ansi_osc.sub("", stripped)
        # Keep printable chars + common whitespace
        return "".join(ch for ch in stripped if ch in ("\n", "\r", "\t") or ord(ch) >= 32)

    def _is_uuid(self, value: str) -> bool:
        # Claude Code session_id is a UUID string in practice.
        return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value))

    async def start(self, workspace_id: str = "default") -> ClaudeSession:
        """Ensure workspace exists and load session state."""
        async with self._lock:
            self._active_workspace_id = workspace_id
            workspace = await self.workspace_manager.get_workspace(workspace_id)
            if not workspace:
                logger.debug(f"[ClaudeRuntime] Creating new workspace: {workspace_id}")
                workspace = await self.workspace_manager.create_default_workspace(workspace_id)

            existing = self._sessions.get(workspace_id)
            if existing:
                existing.last_active = datetime.now(UTC)
                # Keep in sync with persisted workspace state
                if workspace.session_id and existing.session_id != workspace.session_id:
                    existing.session_id = workspace.session_id
                return existing

            session = ClaudeSession(
                session_id=workspace.session_id or "",
                workspace_id=workspace_id,
            )
            self._sessions[workspace_id] = session
            return session

    def get_workspace_queue_status(self, workspace_id: str) -> dict:
        """返回工作区当前任务队列状态。"""
        current = self._workspace_current_task.get(workspace_id)
        queued = self._workspace_queued_tasks.get(workspace_id, [])
        return {
            "workspace_id": workspace_id,
            "current_task": current.to_dict() if current else None,
            "queued_tasks": [t.to_dict() for t in queued],
            "queue_length": len(queued),
        }

    async def force_cancel_workspace_task(self, workspace_id: str) -> bool:
        """强制终止工作区当前正在运行的任务（杀整个进程组）。

        由于子进程通过 start_new_session=True 启动，script(1) 及其子进程
        （claude、bash 等）同属一个进程组。使用 os.killpg 向整个进程组发送
        SIGKILL，确保 pty 主端 EOF，避免 stdout.readline() 永久阻塞导致锁无法释放。
        """
        proc = self._workspace_procs.get(workspace_id)
        current_task = self._workspace_current_task.get(workspace_id)
        if proc is not None and proc.returncode is None:
            self._workspace_force_cancelled[workspace_id] = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                # 进程组不存在或权限问题，回退到单进程 kill
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            task_desc = ""
            if current_task:
                task_desc = (
                    f" source_chat_key={current_task.source_chat_key!r}"
                    f" elapsed={current_task.elapsed_seconds:.1f}s"
                )
            logger.warning(
                f"[ClaudeRuntime] ✂ Force cancelled task (process group SIGKILL):"
                f" workspace={workspace_id} pid={proc.pid}{task_desc}"
            )
            return True
        logger.debug(
            f"[ClaudeRuntime] Force cancel requested but no running task:"
            f" workspace={workspace_id} has_proc={proc is not None}"
        )
        return False

    def _build_claude_cmd(self, prompt: str, session_id: str | None) -> list[str]:
        disallowed: set[str] = set(self.policy.blocked_tools)
        if not self.policy.allow_command_execution:
            disallowed.add("Bash")
        if not self.policy.allow_file_modification:
            disallowed.update({"Write", "Edit"})
        if not self.policy.allow_network:
            disallowed.update({"WebFetch", "WebSearch"})

        cmd = [
            "claude",
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
        ]
        if self.skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        if self.policy.allowed_tools:
            # 约束：--tools 才是真正“限制可用工具”的开关，避免把 --allowedTools 误当成白名单
            cmd.extend(["--tools", ",".join(sorted(self.policy.allowed_tools))])
        if disallowed:
            cmd.append("--disallowedTools")
            cmd.extend(sorted(disallowed))
        if session_id:
            cmd.extend(["--resume", session_id])
        # Prevent prompt from being parsed as an option
        cmd.append("--")
        cmd.append(prompt)
        return cmd

    def _build_pseudotty_wrapper_cmd(self, claude_cmd: list[str]) -> list[str]:
        """
        Wrap `claude` invocation with a pseudo-tty to avoid hangs under pipes.

        On Linux, `script(1)` is widely available (util-linux).
        """
        shell_cmd = shlex.join(claude_cmd)
        return ["script", "-q", "-c", shell_cmd, "/dev/null"]

    async def _iter_stream_json_objects(
        self,
        stdout: asyncio.StreamReader,
        *,
        on_line: Callable[[str, str], None] | None = None,
        idle_timeout: Callable[[], float | None] | None = None,
    ) -> AsyncGenerator[dict[str, Any]]:
        while True:
            timeout = idle_timeout() if idle_timeout is not None else None
            if timeout is None:
                raw = await stdout.readline()
            else:
                try:
                    raw = await asyncio.wait_for(stdout.readline(), timeout=timeout)
                except TimeoutError:
                    if on_line is not None:
                        on_line("", "idle_timeout")
                    break
            if not raw:
                break
            decoded = raw.decode("utf-8", errors="replace")
            decoded = self._strip_ansi_and_controls(decoded)
            line = decoded.strip()
            if not line:
                if on_line is not None:
                    on_line("", "skip_empty")
                continue
            # Ignore common pty residue (e.g. "[<u")
            if line.startswith("["):
                if on_line is not None:
                    on_line(line, "skip_bracket_prefix")
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                if on_line is not None:
                    on_line(line, "skip_json_decode_error")
                continue
            if isinstance(obj, dict):
                if on_line is not None:
                    obj_type = obj.get("type")
                    on_line(line, f"yield:{obj_type}" if isinstance(obj_type, str) else "yield:<no-type>")
                yield obj

    async def send_message(self, message: str) -> AsyncGenerator[str]:
        """
        Send one message to Claude Code using Agent SDK CLI (`claude -p`).

        Output parsing rules are based on real `stream-json` behavior:
        - `stream_event.content_block_delta` with `text_delta` contains incremental text
        - final `result.result` contains the full text
        - tool calls/results appear as `assistant.message.content[].type=tool_use`
          and `user.message.content[].type=tool_result` (see knowledge doc)
        """
        raise RuntimeError("send_message(message) must be called with workspace context")

    async def send_message_in_workspace(self, workspace_id: str, message: str, source_chat_key: str = "", extra_env: dict[str, str] | None = None) -> AsyncGenerator[str | QueueWaitEvent | ToolCallEvent | ToolResultEvent]:
        # ── 队列管理 ──────────────────────────────────────────────────────────
        task_info = WorkspaceTaskInfo(
            source_chat_key=source_chat_key,
            prompt_preview=message[:80].replace("\n", " "),
            enqueued_at=datetime.now(UTC),
        )
        queued_list = self._workspace_queued_tasks.setdefault(workspace_id, [])
        queued_list.append(task_info)
        lock = self._workspace_locks.setdefault(workspace_id, asyncio.Lock())

        try:
            # 若工作区已有任务在执行，yield 排队等待事件（每 3 秒一次）
            while lock.locked():
                current = self._workspace_current_task.get(workspace_id)
                if current:
                    try:
                        pos = queued_list.index(task_info) + 1
                    except ValueError:
                        pos = 1
                    yield QueueWaitEvent(
                        position=pos,
                        current_task=current,
                        queued_count=len(queued_list),
                    )
                await asyncio.sleep(3)

            # 取得锁后移入"运行中"
            async with lock:
                if task_info in queued_list:
                    queued_list.remove(task_info)
                task_info.started_at = datetime.now(UTC)
                self._workspace_current_task[workspace_id] = task_info
                self._workspace_force_cancelled[workspace_id] = False

                try:
                    # ── 原有执行逻辑 ─────────────────────────────────────────
                    msg_preview = message[:100].replace("\n", " ")
                    logger.info(f"[ClaudeRuntime] ▶ Message recv (workspace={workspace_id}): {msg_preview!r}")
                    start_time = time.monotonic()
                    session = await self.start(workspace_id)
                    workspace = await self.workspace_manager.get_workspace(workspace_id)
                    if not workspace:
                        raise ClaudeCliError(
                            code=ErrorCode.WORKSPACE_NOT_FOUND,
                            message=f"Workspace not found: {workspace_id}",
                            retryable=False,
                            details={"workspace_id": workspace_id},
                            err_id=new_err_id(),
                        )

                    resume_id = session.session_id if (session.session_id and self._is_uuid(session.session_id)) else None
                    env = os.environ.copy()
                    env_overrides = self._get_env_overrides()
                    env.update(env_overrides)
                    if extra_env:
                        env.update(extra_env)
                    # 记录本次执行使用的关键配置（脱敏）
                    _model = env_overrides.get("ANTHROPIC_MODEL", "<default>")
                    _base_url = env_overrides.get("ANTHROPIC_BASE_URL", "<default>")
                    _timeout = env_overrides.get("API_TIMEOUT_MS", "<default>")
                    _has_token = bool(env_overrides.get("ANTHROPIC_AUTH_TOKEN"))
                    logger.info(
                        f"[ClaudeRuntime] ⚑ Config: model={_model!r} base_url={_base_url!r} "
                        f"timeout_ms={_timeout} has_token={_has_token} "
                        f"resume_id={resume_id!r} workspace={workspace_id}"
                    )
                    attempt_resume_id = resume_id
                    # Resuming a session cut off by a force-cancel: the CLI first prints an empty
                    # "success" result for the interrupted turn, then the real one. Within a run we
                    # read past the empty result (see the result handler). If a resumed run still
                    # produces nothing at all, retry once with the same session, then once without
                    # --resume. "No conversation found" on --resume retries once without --resume.
                    # At most 3 attempts in total.
                    empty_resume_retried = False

                    for attempt in range(3):
                        claude_cmd = self._build_claude_cmd(prompt=message, session_id=attempt_resume_id)
                        cmd = self._build_pseudotty_wrapper_cmd(claude_cmd)
                        # 记录实际执行的命令（脱敏 prompt 内容）
                        _cmd_preview = [c if c != message else f"<prompt:{len(message)}chars>" for c in claude_cmd]
                        logger.info(f"[ClaudeRuntime] ⚡ Spawning CLI (attempt={attempt}): {_cmd_preview!r}")

                        proc = await asyncio.create_subprocess_exec(
                            *cmd,
                            cwd=str(workspace.path),
                            stdin=asyncio.subprocess.DEVNULL,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            env=env,
                            # 将 script(1) 及其所有子进程（claude、bash 等）置于独立进程组，
                            # 使 force_cancel 时可通过 os.killpg 一次性杀死整个进程组，
                            # 确保 pty 主端 EOF，避免 stdout.readline() 永久阻塞。
                            start_new_session=True,
                            # 默认 64KB limit 在 Read 大文件时会触发 LimitOverrunError，扩大到 10MB
                            limit=10 * 1024 * 1024,
                        )
                        # 保存进程引用（供 force_cancel_workspace_task 使用）
                        self._workspace_procs[workspace_id] = proc

                        assert proc.stdout is not None
                        assert proc.stderr is not None

                        yielded_any_text = False
                        final_result_text: str | None = None
                        new_session_id: str | None = None
                        last_assistant_text: str | None = None
                        parsed_objects = 0
                        cli_error_summary: str | None = None
                        cli_errors: list[str] | None = None
                        cli_error_details: dict[str, Any] | None = None
                        tool_calls_seen: list[str] = []
                        yielded_tool_use_ids: set[str] = set()
                        yielded_tool_result_ids: set[str] = set()
                        result_seen_ok = False
                        result_output_tokens: int | None = None
                        read_on_after_empty = False

                        stdout_tail: deque[str] = deque(maxlen=30)
                        stdout_skipped_tail: deque[str] = deque(maxlen=30)
                        line_stats: Counter[str] = Counter()
                        seen_types: Counter[str] = Counter()

                        def _on_line(line: str, status: str) -> None:
                            line_stats[status] += 1
                            if status.startswith("yield:"):
                                if line:
                                    stdout_tail.append(line[:500])
                            elif status.startswith("skip"):
                                if line:
                                    stdout_skipped_tail.append(line[:500])

                        def _idle_timeout() -> float | None:
                            return EMPTY_RESULT_READ_ON_IDLE_SECONDS if read_on_after_empty else None

                        async for obj in self._iter_stream_json_objects(
                            proc.stdout, on_line=_on_line, idle_timeout=_idle_timeout
                        ):
                            parsed_objects += 1
                            obj_type = obj.get("type")
                            if isinstance(obj_type, str):
                                seen_types[obj_type] += 1
                            if obj_type == "stream_event":
                                event = obj.get("event", {})
                                if isinstance(event, dict) and event.get("type") == "content_block_delta":
                                    delta = event.get("delta", {})
                                    if isinstance(delta, dict) and delta.get("type") == "text_delta":
                                        text = delta.get("text", "")
                                        if isinstance(text, str) and text:
                                            if not yielded_any_text:
                                                elapsed_first = time.monotonic() - start_time
                                                logger.info(
                                                    f"[ClaudeRuntime] ◉ First chunk"
                                                    f" (ttfb={elapsed_first:.1f}s workspace={workspace_id})"
                                                )
                                            yielded_any_text = True
                                            yield text
                            elif obj_type == "system" and obj.get("subtype") == "init":
                                tools = obj.get("tools")
                                if isinstance(tools, list) and all(isinstance(x, str) for x in tools):
                                    self._last_init_tools[workspace_id] = tools
                            elif obj_type == "assistant":
                                msg = obj.get("message", {})
                                # Turns the CLI makes up itself (model "<synthetic>", e.g. the
                                # "No response requested." it writes for a background-task
                                # notification after a force-cancel) are not the model's answer.
                                if isinstance(msg, dict) and msg.get("model") == "<synthetic>":
                                    continue
                                if isinstance(msg, dict):
                                    content = msg.get("content", [])
                                    if isinstance(content, list):
                                        text_parts: list[str] = []
                                        for block in content:
                                            if not isinstance(block, dict):
                                                continue
                                            btype = block.get("type")
                                            if btype == "text":
                                                t = block.get("text", "")
                                                if isinstance(t, str) and t:
                                                    text_parts.append(t)
                                            elif btype == "tool_use":
                                                tool_name = block.get("name", "?")
                                                tool_use_id = block.get("id", "")
                                                tool_input = block.get("input", {})
                                                if tool_name not in tool_calls_seen:
                                                    tool_calls_seen.append(tool_name)
                                                if tool_use_id not in yielded_tool_use_ids:
                                                    yielded_tool_use_ids.add(tool_use_id)
                                                    _tool_elapsed = time.monotonic() - start_time
                                                    logger.info(
                                                        f"[ClaudeRuntime] ⚙ Tool call: {tool_name}"
                                                        f" (t={_tool_elapsed:.1f}s workspace={workspace_id})"
                                                    )
                                                    yield ToolCallEvent(
                                                        tool_use_id=tool_use_id,
                                                        name=tool_name,
                                                        input=tool_input if isinstance(tool_input, dict) else {},
                                                    )
                                        if text_parts:
                                            last_assistant_text = "".join(text_parts)
                            elif obj_type == "user":
                                msg = obj.get("message", {})
                                if isinstance(msg, dict):
                                    content = msg.get("content", [])
                                    if isinstance(content, list):
                                        for block in content:
                                            if not isinstance(block, dict):
                                                continue
                                            if block.get("type") == "tool_result":
                                                tool_use_id = block.get("tool_use_id", "")
                                                result_content = block.get("content", "")
                                                is_error = bool(block.get("is_error", False))
                                                if not isinstance(result_content, str):
                                                    result_content = str(result_content)
                                                if tool_use_id not in yielded_tool_result_ids:
                                                    yielded_tool_result_ids.add(tool_use_id)
                                                    yield ToolResultEvent(
                                                        tool_use_id=tool_use_id,
                                                        content=result_content,
                                                        is_error=is_error,
                                                    )
                            elif obj_type == "result":
                                subtype = obj.get("subtype")
                                is_error = bool(obj.get("is_error")) or (
                                    isinstance(subtype, str) and subtype.startswith("error")
                                )
                                result_text = obj.get("result")
                                if isinstance(result_text, str):
                                    final_result_text = result_text
                                sid = obj.get("session_id")
                                if not is_error and isinstance(sid, str) and sid:
                                    new_session_id = sid
                                usage = obj.get("usage") or {}
                                in_tok = usage.get("input_tokens", "?")
                                out_tok = usage.get("output_tokens", "?")
                                if not is_error:
                                    result_seen_ok = True
                                    if isinstance(out_tok, int):
                                        result_output_tokens = out_tok
                                _result_len = len(result_text) if isinstance(result_text, str) else 0
                                _elapsed_result = time.monotonic() - start_time
                                if not is_error:
                                    logger.info(
                                        f"[ClaudeRuntime] ✦ Result: subtype={subtype!r} "
                                        f"tokens(in={in_tok} out={out_tok}) "
                                        f"result_len={_result_len} elapsed={_elapsed_result:.1f}s "
                                        f"workspace={workspace_id}"
                                    )
                                else:
                                    result_preview = (result_text[:200] + "...") if isinstance(result_text, str) and len(result_text) > 200 else result_text
                                    logger.warning(
                                        f"[ClaudeRuntime] ✗ Result error: subtype={subtype!r} "
                                        f"tokens(in={in_tok} out={out_tok}) "
                                        f"result_len={_result_len} elapsed={_elapsed_result:.1f}s "
                                        f"workspace={workspace_id} "
                                        f"result_preview={result_preview!r}"
                                    )
                                errors_raw = obj.get("errors")
                                if isinstance(errors_raw, list) and all(isinstance(x, str) for x in errors_raw):
                                    cli_errors = list(errors_raw)
                                if is_error:
                                    permission_denials = obj.get("permission_denials")
                                    pd_preview: list[Any] | None = None
                                    if isinstance(permission_denials, list):
                                        pd_preview = permission_denials[:3]
                                    preview: list[str] | None = None
                                    if cli_errors:
                                        preview = cli_errors[:3]
                                    cli_error_summary = "Claude CLI returned error result"
                                    cli_error_details = {
                                        "subtype": subtype,
                                        "permission_denials_preview": pd_preview if isinstance(permission_denials, list) else permission_denials,
                                        "errors_preview": preview if cli_errors else None,
                                    }
                                # Resuming a session whose last turn was cut off mid model request:
                                # the CLI first reports the interrupted turn as an empty "success"
                                # result, then answers the new prompt and prints the real result.
                                # Read on (once) instead of stopping at the empty one.
                                if (
                                    attempt_resume_id
                                    and not read_on_after_empty
                                    and not yielded_any_text
                                    and _is_empty_success_result(
                                        result_seen_ok=result_seen_ok,
                                        output_tokens=result_output_tokens,
                                        final_result_text=final_result_text,
                                        last_assistant_text=last_assistant_text,
                                        cli_error_summary=cli_error_summary,
                                        tool_activity=bool(tool_calls_seen or yielded_tool_use_ids or yielded_tool_result_ids),
                                    )
                                ):
                                    read_on_after_empty = True
                                    result_seen_ok = False
                                    result_output_tokens = None
                                    final_result_text = None
                                    logger.warning(
                                        "[ClaudeRuntime] Empty result at the start of a resumed run "
                                        "(the CLI closed a turn cut off by a force-cancel first); "
                                        "reading on for the real result. "
                                        f"workspace_id={workspace_id} resume_id={attempt_resume_id!r}"
                                    )
                                    continue
                                # result 是 CLI 的最终输出，后面不会有任何有意义的数据。
                                # 必须立即 break 跳出 stdout 读取循环，否则 script(1) 伪终端
                                # wrapper 可能迟迟不关闭 stdout，导致 readline() 无限阻塞，
                                # 进而让整条 SSE 数据流停滞、NA 侧超时。
                                break

                        # 收到 result 后主动 break，进程可能仍在运行（会话持久化等清理工作）。
                        # 分级等待策略：给 CLI 足够的时间完成会话写入，避免下次 --resume 时丢失上下文。
                        #   阶段1: 等 60s 正常退出（CLI 持久化大型会话可能需要较长时间）
                        #   阶段2: SIGTERM 优雅终止 + 5s 等待
                        #   阶段3: SIGKILL 强制杀进程
                        _wait_start = time.monotonic()
                        try:
                            stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=10.0)
                        except (TimeoutError, Exception):
                            stderr_bytes = b""
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=60.0)
                            logger.debug(
                                f"[ClaudeRuntime] Process exited normally after result: "
                                f"wait={time.monotonic() - _wait_start:.1f}s "
                                f"exit={proc.returncode} workspace={workspace_id}"
                            )
                        except TimeoutError:
                            logger.warning(
                                f"[ClaudeRuntime] Process did not exit within 60s after result, "
                                f"sending SIGTERM to process group. workspace={workspace_id} pid={proc.pid}"
                            )
                            try:
                                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                            except (ProcessLookupError, OSError):
                                try:
                                    proc.terminate()
                                except ProcessLookupError:
                                    pass
                            try:
                                await asyncio.wait_for(proc.wait(), timeout=5.0)
                            except TimeoutError:
                                logger.warning(
                                    f"[ClaudeRuntime] SIGTERM ignored, sending SIGKILL to process group. "
                                    f"workspace={workspace_id} pid={proc.pid}"
                                )
                                try:
                                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                                except (ProcessLookupError, OSError):
                                    try:
                                        proc.kill()
                                    except ProcessLookupError:
                                        pass
                                try:
                                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                                except TimeoutError:
                                    logger.error(
                                        f"[ClaudeRuntime] Process group unkillable! "
                                        f"workspace={workspace_id} pid={proc.pid}"
                                    )
                        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
                        if proc.returncode not in (0, None):
                            logger.warning(
                                f"[ClaudeRuntime] Non-zero exit={proc.returncode} "
                                f"workspace={workspace_id} "
                                f"stderr={stderr_text[:2000]!r}"
                            )
                        elif stderr_text:
                            logger.debug(
                                f"[ClaudeRuntime] Process exit=0 stderr={stderr_text[:500]!r} "
                                f"workspace={workspace_id}"
                            )

                        if new_session_id and new_session_id != session.session_id:
                            session.session_id = new_session_id
                            await self.workspace_manager.update_session(workspace_id, new_session_id)
                            logger.info(f"[ClaudeRuntime] Updated session_id: {new_session_id}")

                        elapsed = time.monotonic() - start_time
                        tools_str = ", ".join(tool_calls_seen) if tool_calls_seen else "none"
                        if yielded_any_text:
                            chars: int | str = "streaming"
                        elif final_result_text:
                            chars = len(final_result_text)
                        elif last_assistant_text:
                            chars = len(last_assistant_text)
                        else:
                            chars = 0
                        _error_flag = f" ERROR={cli_error_summary!r}" if cli_error_summary else ""
                        logger.info(
                            f"[ClaudeRuntime] ✓ Exec done: elapsed={elapsed:.1f}s "
                            f"exit={proc.returncode} "
                            f"tools=[{tools_str}] chars={chars} "
                            f"parsed_objs={parsed_objects} "
                            f"seen_types={dict(seen_types)} "
                            f"workspace={workspace_id}{_error_flag}"
                        )

                        # 检查是否被强制取消
                        if self._workspace_force_cancelled.get(workspace_id):
                            raise ClaudeCliError(
                                code=ErrorCode.TASK_CANCELLED,
                                message="任务被强制取消",
                                retryable=False,
                                details={"workspace_id": workspace_id, "source_chat_key": source_chat_key},
                                err_id=new_err_id(),
                            )

                        if not yielded_any_text and final_result_text and not cli_error_summary:
                            yield final_result_text
                            return

                        if not yielded_any_text and last_assistant_text:
                            yield last_assistant_text
                            return

                        if yielded_any_text:
                            return

                        if not yielded_any_text:
                            tool_activity = bool(tool_calls_seen or yielded_tool_use_ids or yielded_tool_result_ids)
                            # Nothing at all came out of this run: either the empty result was the only
                            # one (the CLI stopped after closing the interrupted turn), or we read on
                            # after it and the real result never came.
                            ended_empty = not (
                                final_result_text or last_assistant_text or cli_error_summary or tool_activity
                            ) and (
                                read_on_after_empty
                                or _is_empty_success_result(
                                    result_seen_ok=result_seen_ok,
                                    output_tokens=result_output_tokens,
                                    final_result_text=final_result_text,
                                    last_assistant_text=last_assistant_text,
                                    cli_error_summary=cli_error_summary,
                                    tool_activity=tool_activity,
                                )
                            )
                            if attempt < 2 and attempt_resume_id and ended_empty:
                                if not empty_resume_retried:
                                    empty_resume_retried = True
                                    # Resume whatever session the CLI reported (it may hand out a new id),
                                    # exactly like the next message would.
                                    if session.session_id and self._is_uuid(session.session_id):
                                        attempt_resume_id = session.session_id
                                    logger.warning(
                                        "[ClaudeRuntime] Resumed run produced nothing "
                                        "(its session was cut off by a force-cancel); "
                                        "retrying once with the same session. "
                                        f"workspace_id={workspace_id} resume_id={attempt_resume_id!r}"
                                    )
                                    continue
                                logger.warning(
                                    "[ClaudeRuntime] Resumed session returned an empty result again; "
                                    "retrying without --resume. "
                                    f"workspace_id={workspace_id} resume_id={attempt_resume_id!r}"
                                )
                                attempt_resume_id = None
                                session.session_id = ""
                                await self.workspace_manager.update_session(workspace_id, "")
                                continue

                            if (
                                attempt < 2
                                and attempt_resume_id
                                and cli_errors
                                and any("No conversation found with session ID" in e for e in cli_errors)
                            ):
                                logger.warning(
                                    "[ClaudeRuntime] Resume session not found; retrying without --resume. "
                                    f"workspace_id={workspace_id} resume_id={attempt_resume_id!r}"
                                )
                                attempt_resume_id = None
                                session.session_id = ""
                                await self.workspace_manager.update_session(workspace_id, "")
                                continue

                            if cli_error_summary:
                                err_id = new_err_id()
                                logger.error(f"[ClaudeRuntime] {cli_error_summary} err_id={err_id} details={cli_error_details!r}")
                                raise ClaudeCliError(
                                    code=ErrorCode.CLAUDE_CLI_ERROR_RESULT,
                                    message=cli_error_summary,
                                    retryable=True,
                                    details=cli_error_details,
                                    err_id=err_id,
                                )

                            logger.error(
                                "[ClaudeRuntime] No parseable text output. "
                                f"workspace_id={workspace_id} "
                                f"resume_id={attempt_resume_id!r} "
                                f"parsed_objects={parsed_objects} "
                                f"seen_types={dict(seen_types)} "
                                f"line_stats={dict(line_stats)} "
                                f"exit={proc.returncode} "
                                f"stderr={stderr_text[:500]!r} "
                                f"stdout_tail={list(stdout_tail)!r} "
                                f"stdout_skipped_tail={list(stdout_skipped_tail)!r}"
                            )
                            err_id = new_err_id()
                            logger.error(f"[ClaudeRuntime] err_id={err_id} code=CLAUDE_CLI_NO_PARSEABLE_OUTPUT workspace_id={workspace_id}")
                            raise ClaudeCliError(
                                code=ErrorCode.CLAUDE_CLI_NO_PARSEABLE_OUTPUT,
                                message="Claude produced no parseable text output",
                                retryable=True,
                                details={
                                    "workspace_id": workspace_id,
                                    "resume_id": attempt_resume_id,
                                    "parsed_objects": parsed_objects,
                                    "seen_types": dict(seen_types),
                                    "line_stats": dict(line_stats),
                                    "exit": proc.returncode,
                                    "stderr_preview": stderr_text[:500],
                                },
                                err_id=err_id,
                            )
                    # ── 原有执行逻辑结束 ──────────────────────────────────────
                finally:
                    self._workspace_current_task[workspace_id] = None
                    self._workspace_procs.pop(workspace_id, None)
        finally:
            if task_info in queued_list:
                queued_list.remove(task_info)

    async def shutdown(self) -> None:
        """Shutdown runtime: kill all running processes, then clear sessions."""
        logger.info("[ClaudeRuntime] Shutting down...")

        # 终止所有仍在运行的工作区进程
        for ws_id, proc in list(self._workspace_procs.items()):
            if proc is not None and proc.returncode is None:
                logger.warning(
                    f"[ClaudeRuntime] Shutdown: killing running process "
                    f"workspace={ws_id} pid={proc.pid}"
                )
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except TimeoutError:
                    logger.error(
                        f"[ClaudeRuntime] Shutdown: process unkillable "
                        f"workspace={ws_id} pid={proc.pid}"
                    )

        self._workspace_procs.clear()
        self._workspace_current_task.clear()
        self._workspace_queued_tasks.clear()
        self._workspace_force_cancelled.clear()
        self._sessions.clear()
        logger.info("[ClaudeRuntime] Shutdown complete")

    async def reset_workspace_session(self, workspace_id: str) -> None:
        """重置指定工作区的会话（清理内存缓存；持久化由 WorkspaceManager 负责）。"""
        async with self._lock:
            if workspace_id in self._sessions:
                del self._sessions[workspace_id]
            if workspace_id == self._active_workspace_id:
                self._active_workspace_id = "default"

    async def get_session(self) -> ClaudeSession | None:
        """Deprecated: runtime keeps sessions per workspace."""
        return None

    async def get_workspace_session(self, workspace_id: str) -> ClaudeSession | None:
        async with self._lock:
            return self._sessions.get(workspace_id)

    def get_last_tools(self, workspace_id: str) -> list[str] | None:
        return self._last_init_tools.get(workspace_id)

    async def probe_tools(self, *, workspace_id: str = "default") -> list[str]:
        """独立探测 Claude Code 的工具列表（不与会话/聊天绑定）。"""
        workspace = await self.workspace_manager.get_workspace(workspace_id)
        if not workspace:
            raise ClaudeCliError(
                code=ErrorCode.WORKSPACE_NOT_FOUND,
                message=f"Workspace not found: {workspace_id}",
                retryable=False,
                details={"workspace_id": workspace_id},
                err_id=new_err_id(),
            )

        # 不使用 --resume，且禁用会话持久化，确保与“聊天会话”完全解耦
        disallowed: set[str] = set(self.policy.blocked_tools)
        if not self.policy.allow_command_execution:
            disallowed.add("Bash")
        if not self.policy.allow_file_modification:
            disallowed.update({"Write", "Edit"})
        if not self.policy.allow_network:
            disallowed.update({"WebFetch", "WebSearch"})

        # 关键点：不能依赖 --init-only。
        # 实测该模式只输出终端控制序列，不会输出 stream-json 的 system.init（含 tools）。
        # 因此必须使用 -p/--print 用一个极小 prompt 触发初始化，再从 system.init 提取工具列表。
        claude_cmd: list[str] = [
            "claude",
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--no-session-persistence",
        ]
        if self.skip_permissions:
            claude_cmd.append("--dangerously-skip-permissions")
        if self.policy.allowed_tools:
            claude_cmd.extend(["--tools", ",".join(sorted(self.policy.allowed_tools))])
        if disallowed:
            claude_cmd.append("--disallowedTools")
            claude_cmd.extend(sorted(disallowed))
        # Prevent prompt from being parsed as an option
        claude_cmd.append("--")
        claude_cmd.append("ping")

        cmd = self._build_pseudotty_wrapper_cmd(claude_cmd)
        logger.debug(f"[ClaudeRuntime] Probe tools: {cmd!r} (cwd={workspace.path})")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(workspace.path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **self._get_env_overrides()},
            start_new_session=True,
            limit=10 * 1024 * 1024,
        )

        assert proc.stdout is not None
        assert proc.stderr is not None

        tools: list[str] | None = None
        parsed_objects = 0
        stdout_tail: deque[str] = deque(maxlen=30)
        stdout_skipped_tail: deque[str] = deque(maxlen=30)
        line_stats: Counter[str] = Counter()

        def _on_line(line: str, status: str) -> None:
            line_stats[status] += 1
            if status.startswith("yield:"):
                if line:
                    stdout_tail.append(line[:500])
            elif status.startswith("skip"):
                if line:
                    stdout_skipped_tail.append(line[:500])

        async for obj in self._iter_stream_json_objects(proc.stdout, on_line=_on_line):
            parsed_objects += 1
            if obj.get("type") == "system" and obj.get("subtype") == "init":
                t = obj.get("tools")
                if isinstance(t, list) and all(isinstance(x, str) for x in t):
                    tools = list(t)
                    # 拿到 tools 后立即结束：避免为了“探测工具”继续消耗 token
                    break

        # 若已获取 tools，尽快终止进程组（print 模式后续会继续生成响应文本）
        if tools is not None and proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except TimeoutError:
                    logger.error(
                        f"[ClaudeRuntime] Probe: process unkillable after SIGKILL "
                        f"workspace={workspace_id} pid={proc.pid}"
                    )

        stderr_bytes = await proc.stderr.read()
        _ = await proc.wait()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if tools is None:
            err_id = new_err_id()
            logger.error(
                "[ClaudeRuntime] Probe tools failed. "
                f"err_id={err_id} parsed_objects={parsed_objects} exit={proc.returncode} "
                f"stderr={stderr_text[:500]!r} line_stats={dict(line_stats)!r}"
            )
            raise ClaudeCliError(
                code=ErrorCode.CLAUDE_CLI_NO_PARSEABLE_OUTPUT,
                message="Claude produced no parseable init tools output",
                retryable=True,
                details={
                    "parsed_objects": parsed_objects,
                    "exit": proc.returncode,
                    "stderr_preview": stderr_text[:500],
                    "stdout_tail": list(stdout_tail),
                    "stdout_skipped_tail": list(stdout_skipped_tail),
                    "line_stats": dict(line_stats),
                },
                err_id=err_id,
            )

        self._last_init_tools[workspace_id] = tools
        return tools
