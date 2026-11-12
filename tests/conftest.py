import asyncio
import os
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--enable-provider-tests",
        action="store_true",
        default=False,
        help="Enable real provider tests (marked with: @pytest.mark.provider).",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip real provider tests unless explicitly enabled.

    Rationale:
    - Provider tests require real API credentials and may make network calls.
    - `poe check` must be deterministic and pass without secrets by default.
    """
    if config.getoption("--enable-provider-tests"):
        return

    skip_marker = pytest.mark.skip(reason="Provider tests disabled (pass --enable-provider-tests to run).")
    for item in items:
        if "provider" in item.keywords:
            item.add_marker(skip_marker)


@pytest.fixture(scope="session")
def event_loop() -> Generator[asyncio.AbstractEventLoop]:
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def temp_workspace() -> Generator[Path]:
    """Create a temporary workspace directory"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_claude_session():
    """Mock Claude Code session"""
    return {
        "session_id": "test-session-123",
        "workspace_id": "default",
        "is_active": True,
    }


@pytest.fixture
def sample_message():
    """Sample message for testing"""
    return {
        "role": "user",
        "content": "Analyze the codebase and suggest improvements",
        "workspace_id": "default",
    }


@pytest.fixture
def env_vars():
    """Test environment variables"""
    return {
        "ANTHROPIC_AUTH_TOKEN": "test-token",
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
        "WORKSPACE_ROOT": "./workspaces",
        "SKIP_PERMISSIONS": "true",
        "DEBUG": "true",
    }


@pytest.fixture
def mock_policy():
    """Create a mock runtime policy"""
    from nekro_cc_sandbox.claude.policy import RuntimePolicy

    return RuntimePolicy.relaxed()


@pytest.fixture
def mock_claude_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install a mock 'claude' CLI on PATH for runtime tests."""
    from nekro_cc_sandbox.claude.runtime import ClaudeRuntime

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script_path = bin_dir / "claude"

    script_path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
scenario = os.getenv("MOCK_CLAUDE_SCENARIO", "success")
session_id = os.getenv("MOCK_CLAUDE_SESSION_ID", "11111111-1111-1111-1111-111111111111")

def emit(obj):
    print(json.dumps(obj), flush=True)

resume_id = None
if "--resume" in args:
    idx = args.index("--resume")
    if idx + 1 < len(args):
        resume_id = args[idx + 1]

prompt = ""
if "--" in args:
    idx = args.index("--")
    if idx + 1 < len(args):
        prompt = args[idx + 1]

call_log = os.getenv("MOCK_CLAUDE_CALL_LOG")
if call_log:
    with open(call_log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"resume": resume_id}) + "\\n")

def bump_counter():
    state = os.getenv("MOCK_CLAUDE_STATE")
    if not state:
        return 0
    try:
        with open(state, encoding="utf-8") as fh:
            count = int(fh.read() or "0")
    except FileNotFoundError:
        count = 0
    with open(state, "w", encoding="utf-8") as fh:
        fh.write(str(count + 1))
    return count

def emit_empty_success(sid):
    # What Claude Code 2.1.x prints when it resumes a session cut off mid tool call:
    # a "success" result with 0 tokens and no text, without calling the model.
    emit({"type": "system", "subtype": "init", "tools": ["Read", "Write", "Bash"]})
    emit({"type": "system", "subtype": "status"})
    emit({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "session_id": sid,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    })

empty_now = (
    (scenario == "empty-on-resume-once" and resume_id and bump_counter() == 0)
    or (scenario == "empty-on-resume-always" and resume_id)
    or scenario == "empty-always"
)
if empty_now:
    emit_empty_success(resume_id or session_id)
    sys.exit(0)

if scenario == "empty-then-answer" and resume_id:
    # Real Claude Code after a force-cancel killed a background task: it answers the "task stopped"
    # notification with a synthetic turn and an empty "success" result, then answers the new prompt
    # in the same process.
    emit({"type": "system", "subtype": "init", "tools": ["Read", "Write", "Bash"]})
    emit({"type": "assistant", "message": {
        "model": "<synthetic>",
        "content": [{"type": "text", "text": "No response requested."}],
    }})
    emit({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "session_id": resume_id,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    })
    emit({
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello world"}},
    })
    emit({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Hello world",
        "session_id": session_id,
        "usage": {"input_tokens": 10, "output_tokens": 2},
    })
    sys.exit(0)

if scenario == "empty-then-silence" and resume_id and bump_counter() == 0:
    # Empty result, then the process stays quiet for a while before exiting without more output.
    import time
    emit_empty_success(resume_id)
    time.sleep(float(os.getenv("MOCK_CLAUDE_SILENCE", "3")))
    sys.exit(0)

if scenario == "empty-after-tool" and resume_id:
    emit({"type": "system", "subtype": "init", "tools": ["Read", "Write", "Bash"]})
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "true"}},
    ]}})
    emit({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "", "is_error": False},
    ]}})
    emit({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "session_id": resume_id,
        "usage": {"input_tokens": 5, "output_tokens": 0},
    })
    sys.exit(0)

if scenario == "no-output":
    sys.exit(0)

if scenario == "error-result":
    emit({
        "type": "result",
        "subtype": "error",
        "is_error": True,
        "errors": ["mock error"],
        "result": "",
    })
    sys.exit(0)

if scenario == "resume-miss-once" and resume_id:
    emit({
        "type": "result",
        "subtype": "error",
        "is_error": True,
        "errors": [f"No conversation found with session ID: {resume_id}"],
        "result": "",
    })
    sys.exit(0)

if "--no-session-persistence" in args or prompt == "ping" or scenario == "probe-tools":
    emit({"type": "system", "subtype": "init", "tools": ["Read", "Write", "Bash"]})
    sys.exit(0)

# Default: successful streaming response
emit({"type": "system", "subtype": "init", "tools": ["Read", "Write", "Bash"]})

if scenario == "result-only":
    emit({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Hello world",
        "session_id": session_id,
    })
    sys.exit(0)

emit({
    "type": "stream_event",
    "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello "}},
})
emit({
    "type": "stream_event",
    "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "world"}},
})
emit({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "Hello world",
    "session_id": session_id,
})
""",
        encoding="utf-8",
    )
    script_path.chmod(0o755)

    existing_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{existing_path}")

    # Avoid pseudo-tty wrapper in tests (some environments deny PTY allocation).
    monkeypatch.setattr(ClaudeRuntime, "_build_pseudotty_wrapper_cmd", lambda _self, cmd: cmd)
    return script_path
