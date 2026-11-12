"""Unit tests for ClaudeRuntime with mock Claude CLI."""

from pathlib import Path

import pytest

from nekro_cc_sandbox.claude.runtime import ClaudeRuntime
from nekro_cc_sandbox.errors import ClaudeCliError, ErrorCode
from nekro_cc_sandbox.workspace.manager import WorkspaceManager


async def _collect_async(gen):
    chunks: list[str] = []
    async for chunk in gen:
        chunks.append(chunk)
    return chunks


@pytest.mark.asyncio
async def test_send_message_success_updates_session(tmp_path: Path, mock_claude_cli, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MOCK_CLAUDE_SCENARIO", "success")
    monkeypatch.setenv("MOCK_CLAUDE_SESSION_ID", "22222222-2222-2222-2222-222222222222")

    wm = WorkspaceManager(tmp_path / "workspaces")
    runtime = ClaudeRuntime(workspace_manager=wm, skip_permissions=True)

    chunks = await _collect_async(runtime.send_message_in_workspace("default", "hello"))
    assert "".join(chunks) == "Hello world"

    ws = await wm.get_workspace("default")
    assert ws is not None
    assert ws.session_id == "22222222-2222-2222-2222-222222222222"
    assert runtime.get_last_tools("default") == ["Read", "Write", "Bash"]


@pytest.mark.asyncio
async def test_send_message_resume_retry_on_missing(tmp_path: Path, mock_claude_cli, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MOCK_CLAUDE_SCENARIO", "resume-miss-once")
    monkeypatch.setenv("MOCK_CLAUDE_SESSION_ID", "33333333-3333-3333-3333-333333333333")

    wm = WorkspaceManager(tmp_path / "workspaces")
    await wm.create_default_workspace("default")
    await wm.update_session("default", "00000000-0000-0000-0000-000000000001")

    runtime = ClaudeRuntime(workspace_manager=wm, skip_permissions=True)
    chunks = await _collect_async(runtime.send_message_in_workspace("default", "hello"))

    assert "".join(chunks) == "Hello world"
    ws = await wm.get_workspace("default")
    assert ws is not None
    assert ws.session_id == "33333333-3333-3333-3333-333333333333"


def _calls(log: Path) -> list[str | None]:
    import json

    if not log.exists():
        return []
    return [json.loads(line)["resume"] for line in log.read_text(encoding="utf-8").splitlines() if line]


async def _workspace_with_session(tmp_path: Path, session_id: str) -> WorkspaceManager:
    wm = WorkspaceManager(tmp_path / "workspaces")
    await wm.create_default_workspace("default")
    await wm.update_session("default", session_id)
    return wm


@pytest.mark.asyncio
async def test_empty_result_then_real_answer_is_read_in_the_same_run(
    tmp_path: Path, mock_claude_cli, monkeypatch: pytest.MonkeyPatch
):
    """What real Claude Code does after a force-cancel cut off a model request: an empty "success"
    result for the interrupted turn first, then the real answer from the same process. Read past
    the empty one; do not start the CLI again (that would make the model answer twice)."""
    old = "00000000-0000-0000-0000-0000000000a0"
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("MOCK_CLAUDE_SCENARIO", "empty-then-answer")
    monkeypatch.setenv("MOCK_CLAUDE_CALL_LOG", str(log))
    monkeypatch.setenv("MOCK_CLAUDE_SESSION_ID", "77777777-7777-7777-7777-777777777777")

    wm = await _workspace_with_session(tmp_path, old)
    runtime = ClaudeRuntime(workspace_manager=wm, skip_permissions=True)
    chunks = await _collect_async(runtime.send_message_in_workspace("default", "after cancel"))

    assert "".join(chunks) == "Hello world"
    assert _calls(log) == [old]
    ws = await wm.get_workspace("default")
    assert ws is not None
    assert ws.session_id == "77777777-7777-7777-7777-777777777777"


@pytest.mark.asyncio
async def test_reading_on_after_an_empty_result_gives_up_when_idle(
    tmp_path: Path, mock_claude_cli, monkeypatch: pytest.MonkeyPatch
):
    """If nothing follows the empty result for too long, stop reading and fall back to the retry."""
    import nekro_cc_sandbox.claude.runtime as runtime_module

    old = "00000000-0000-0000-0000-0000000000a4"
    log = tmp_path / "calls.jsonl"
    monkeypatch.setattr(runtime_module, "EMPTY_RESULT_READ_ON_IDLE_SECONDS", 0.5)
    monkeypatch.setenv("MOCK_CLAUDE_SCENARIO", "empty-then-silence")
    monkeypatch.setenv("MOCK_CLAUDE_SILENCE", "2")
    monkeypatch.setenv("MOCK_CLAUDE_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("MOCK_CLAUDE_CALL_LOG", str(log))

    wm = await _workspace_with_session(tmp_path, old)
    runtime = ClaudeRuntime(workspace_manager=wm, skip_permissions=True)
    chunks = await _collect_async(runtime.send_message_in_workspace("default", "after cancel"))

    assert "".join(chunks) == "Hello world"
    assert _calls(log) == [old, old]


@pytest.mark.asyncio
async def test_empty_resume_retries_once_with_the_same_session(
    tmp_path: Path, mock_claude_cli, monkeypatch: pytest.MonkeyPatch
):
    """Fallback: the CLI prints only the empty result and exits. The next resume of the same
    session works, so retry once with it (the context is kept)."""
    old = "00000000-0000-0000-0000-0000000000a1"
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("MOCK_CLAUDE_SCENARIO", "empty-on-resume-once")
    monkeypatch.setenv("MOCK_CLAUDE_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("MOCK_CLAUDE_CALL_LOG", str(log))
    monkeypatch.setenv("MOCK_CLAUDE_SESSION_ID", "55555555-5555-5555-5555-555555555555")

    wm = await _workspace_with_session(tmp_path, old)
    runtime = ClaudeRuntime(workspace_manager=wm, skip_permissions=True)
    chunks = await _collect_async(runtime.send_message_in_workspace("default", "after cancel"))

    assert "".join(chunks) == "Hello world"
    assert _calls(log) == [old, old]
    ws = await wm.get_workspace("default")
    assert ws is not None
    assert ws.session_id == "55555555-5555-5555-5555-555555555555"


@pytest.mark.asyncio
async def test_empty_resume_twice_falls_back_to_a_new_session(
    tmp_path: Path, mock_claude_cli, monkeypatch: pytest.MonkeyPatch
):
    old = "00000000-0000-0000-0000-0000000000a2"
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("MOCK_CLAUDE_SCENARIO", "empty-on-resume-always")
    monkeypatch.setenv("MOCK_CLAUDE_CALL_LOG", str(log))
    monkeypatch.setenv("MOCK_CLAUDE_SESSION_ID", "66666666-6666-6666-6666-666666666666")

    wm = await _workspace_with_session(tmp_path, old)
    runtime = ClaudeRuntime(workspace_manager=wm, skip_permissions=True)
    chunks = await _collect_async(runtime.send_message_in_workspace("default", "after cancel"))

    assert "".join(chunks) == "Hello world"
    assert _calls(log) == [old, old, None]
    ws = await wm.get_workspace("default")
    assert ws is not None
    assert ws.session_id == "66666666-6666-6666-6666-666666666666"


@pytest.mark.asyncio
async def test_empty_result_without_resume_is_not_retried(
    tmp_path: Path, mock_claude_cli, monkeypatch: pytest.MonkeyPatch
):
    """Without a session to resume there is nothing to repair: report the error, run the CLI once."""
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("MOCK_CLAUDE_SCENARIO", "empty-always")
    monkeypatch.setenv("MOCK_CLAUDE_CALL_LOG", str(log))

    wm = WorkspaceManager(tmp_path / "workspaces")
    runtime = ClaudeRuntime(workspace_manager=wm, skip_permissions=True)
    with pytest.raises(ClaudeCliError) as exc:
        await _collect_async(runtime.send_message_in_workspace("default", "hello"))

    assert exc.value.code == ErrorCode.CLAUDE_CLI_NO_PARSEABLE_OUTPUT
    assert _calls(log) == [None]


@pytest.mark.asyncio
async def test_empty_result_after_tool_activity_is_not_retried(
    tmp_path: Path, mock_claude_cli, monkeypatch: pytest.MonkeyPatch
):
    """Tool events already reached the caller: a retry would replay them, so do not retry."""
    old = "00000000-0000-0000-0000-0000000000a3"
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("MOCK_CLAUDE_SCENARIO", "empty-after-tool")
    monkeypatch.setenv("MOCK_CLAUDE_CALL_LOG", str(log))

    wm = await _workspace_with_session(tmp_path, old)
    runtime = ClaudeRuntime(workspace_manager=wm, skip_permissions=True)
    with pytest.raises(ClaudeCliError) as exc:
        await _collect_async(runtime.send_message_in_workspace("default", "hello"))

    assert exc.value.code == ErrorCode.CLAUDE_CLI_NO_PARSEABLE_OUTPUT
    assert _calls(log) == [old]


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, True),
        ({"output_tokens": None}, True),
        ({"output_tokens": 3}, False),
        ({"result_seen_ok": False}, False),
        ({"final_result_text": "hi"}, False),
        ({"last_assistant_text": "hi"}, False),
        ({"cli_error_summary": "Claude CLI returned error result"}, False),
        ({"tool_activity": True}, False),
    ],
)
def test_is_empty_success_result(kwargs: dict, expected: bool):
    from nekro_cc_sandbox.claude.runtime import _is_empty_success_result

    base = {
        "result_seen_ok": True,
        "output_tokens": 0,
        "final_result_text": "",
        "last_assistant_text": None,
        "cli_error_summary": None,
        "tool_activity": False,
    }
    assert _is_empty_success_result(**{**base, **kwargs}) is expected


@pytest.mark.asyncio
async def test_send_message_error_result_raises(tmp_path: Path, mock_claude_cli, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MOCK_CLAUDE_SCENARIO", "error-result")

    wm = WorkspaceManager(tmp_path / "workspaces")
    runtime = ClaudeRuntime(workspace_manager=wm, skip_permissions=True)

    with pytest.raises(ClaudeCliError) as exc:
        await _collect_async(runtime.send_message_in_workspace("default", "hello"))

    assert exc.value.code == ErrorCode.CLAUDE_CLI_ERROR_RESULT


@pytest.mark.asyncio
async def test_send_message_no_parseable_output(tmp_path: Path, mock_claude_cli, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MOCK_CLAUDE_SCENARIO", "no-output")

    wm = WorkspaceManager(tmp_path / "workspaces")
    runtime = ClaudeRuntime(workspace_manager=wm, skip_permissions=True)

    with pytest.raises(ClaudeCliError) as exc:
        await _collect_async(runtime.send_message_in_workspace("default", "hello"))

    assert exc.value.code == ErrorCode.CLAUDE_CLI_NO_PARSEABLE_OUTPUT


@pytest.mark.asyncio
async def test_probe_tools_success(tmp_path: Path, mock_claude_cli, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MOCK_CLAUDE_SCENARIO", "probe-tools")

    wm = WorkspaceManager(tmp_path / "workspaces")
    await wm.create_default_workspace("default")
    runtime = ClaudeRuntime(workspace_manager=wm, skip_permissions=True)

    tools = await runtime.probe_tools(workspace_id="default")
    assert tools == ["Read", "Write", "Bash"]
