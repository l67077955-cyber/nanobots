import asyncio
import os
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from nanobot.groupchat.engine import GroupChatEngine


@pytest.fixture
def temp_collab_sessions(tmp_path):
    """Mock the home directory so collab-sessions are in tmp_path."""
    mock_home = tmp_path / "home"
    mock_home.mkdir()
    collab_dir = mock_home / ".nanobot" / "collab-sessions"
    collab_dir.mkdir(parents=True)
    
    with patch("pathlib.Path.home", return_value=mock_home):
        yield collab_dir


def create_mock_session(base_dir: Path, session_id: str, status: str, mtime: float):
    """Create a mock session directory with a state.yaml."""
    session_dir = base_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    state_file = session_dir / "state.yaml"
    data = {
        "session": {
            "id": session_id,
            "status": status,
        }
    }
    with open(state_file, "w", encoding="utf-8") as f:
        yaml.dump(data, f)
    # Set mtime to simulate older/newer sessions
    os.utime(state_file, (mtime, mtime))
    return session_dir


@pytest.fixture
def mock_engine(temp_collab_sessions):
    """Provide a minimal GroupChatEngine instance."""
    # We pass minimal dummy args for initialization
    config = type("DummyConfig", (), {"max_rounds": 50, "max_history": 50, "excluded_agents": [], "agents": {}, "mode": "broadcast"})()
    provider = type("DummyProvider", (), {})()
    
    with patch("nanobot.groupchat.persistence.GroupChatState.load_active", return_value=[]), \
         patch("nanobot.groupchat.persistence.GroupChatState.save_active"), \
         patch("nanobot.groupchat.engine.load_agents", return_value={}):
        engine = GroupChatEngine(config=config, provider=provider, workspace=Path("/tmp"))
        # Mock actual loop tasks to avoid real background execution
        engine._run_loop = AsyncMock() 
        return engine


@pytest.mark.asyncio
class TestSchedulerRouting:
    """Test group chat session routing and cleanup logic."""

    async def test_resume_most_recent_running_session(self, temp_collab_sessions, mock_engine):
        """Should resume the most recently modified session with status='running'."""
        # Create an older running session
        create_mock_session(temp_collab_sessions, "gc-20260401-100000", "running", 1000.0)
        # Create a newer running session
        expected_dir = create_mock_session(temp_collab_sessions, "gc-20260402-100000", "running", 2000.0)
        # Create an even newer but STOPPED session
        create_mock_session(temp_collab_sessions, "gc-20260403-100000", "stopped", 3000.0)

        # Trigger group loop start
        mock_engine._start_group_loop()

        # Should latch onto the latest *running* session
        assert mock_engine._session_dir == expected_dir

    async def test_create_new_session_when_all_stopped(self, temp_collab_sessions, mock_engine):
        """Should create a new session if no running sessions exist."""
        create_mock_session(temp_collab_sessions, "gc-20260401-100000", "stopped", 1000.0)
        create_mock_session(temp_collab_sessions, "gc-20260402-100000", "done", 2000.0)

        mock_engine._start_group_loop()

        # Should create a new one, skipping the stopped ones
        assert mock_engine._session_dir is not None
        assert mock_engine._session_dir.parent == temp_collab_sessions
        assert mock_engine._session_dir.name not in ("gc-20260401-100000", "gc-20260402-100000")
        assert mock_engine._session_dir.name.startswith("gc-")

    async def test_corrypt_yaml_skipped_gracefully(self, temp_collab_sessions, mock_engine):
        """Corrupt YAML files should be skipped without crashing."""
        # Valid older running session
        expected_dir = create_mock_session(temp_collab_sessions, "gc-20260401-100000", "running", 1000.0)
        
        # Corrupt newer session
        corrupt_dir = temp_collab_sessions / "gc-20260402-999999"
        corrupt_dir.mkdir()
        (corrupt_dir / "state.yaml").write_text("{ invalid yaml: [", encoding="utf-8")
        os.utime(corrupt_dir / "state.yaml", (2000.0, 2000.0))

        mock_engine._start_group_loop()

        # Should skip the corrupt one and fall back to the older running one
        assert mock_engine._session_dir == expected_dir

    async def test_engine_stop_marks_session_stopped(self, temp_collab_sessions, mock_engine):
        """Calling engine.stop() should mutate the active session's status to stopped."""
        # 1. Start a group loop to establish state_bus
        mock_engine._start_group_loop()
        session_dir = mock_engine._session_dir
        state_file = session_dir / "state.yaml"
        
        # Manually set it to running using raw dict structure
        data = {"session": {"status": "running"}}
        with open(state_file, "w", encoding="utf-8") as f:
            yaml.dump(data, f)

        # Confirm it's running
        with open(state_file, "r", encoding="utf-8") as f:
            assert yaml.safe_load(f)["session"]["status"] == "running"

        # 2. Stop the engine
        mock_engine.stop()

        # 3. Confirm it was marked as stopped
        with open(state_file, "r", encoding="utf-8") as f:
            updated_data = yaml.safe_load(f)
            assert updated_data["session"]["status"] == "stopped"


    async def test_missing_status_key_not_assumed_running(self, temp_collab_sessions, mock_engine):
        """Session with missing 'status' key should be skipped, not assumed running.

        Regression: engine.py uses get("status", "running") which silently
        treats a missing status as active — this test ensures such sessions
        are NOT resumed.
        """
        # Create a session with NO status field at all
        session_dir = temp_collab_sessions / "gc-20260401-ghost"
        session_dir.mkdir()
        data = {"session": {"id": "gc-20260401-ghost"}}  # no "status" key
        with open(session_dir / "state.yaml", "w", encoding="utf-8") as f:
            yaml.dump(data, f)
        os.utime(session_dir / "state.yaml", (3000.0, 3000.0))

        # Create an older but explicitly running session
        expected_dir = create_mock_session(temp_collab_sessions, "gc-20260331-090000", "running", 1000.0)

        mock_engine._start_group_loop()

        # Should NOT pick up the ghost session (no status), fall back to the real running one
        assert mock_engine._session_dir == expected_dir
