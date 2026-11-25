import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import git


@pytest.mark.skipif(os.name != "nt", reason="The administrator script uses Windows file locking")
def test_sync_reference_only_fast_forwards_unlocked_clean_fixture(reference, tmp_path):
    from unity_project_mcp.activity import activity_lock

    workspace = Path(__file__).resolve().parents[1]
    state = tmp_path / "state"
    config = tmp_path / "sync.toml"
    config.write_text(
        f"[project]\nname='fixture'\nrepository_root='{reference.as_posix()}'\nunity_project_path='client'\n"
        f"[runtime]\nstate_dir='{state.as_posix()}'\n", encoding="utf-8",
    )
    remote = tmp_path / "local-remote.git"
    remote.mkdir()
    git(remote, "init", "-q", "--bare", "-b", "main")
    git(reference, "remote", "add", "origin", remote.as_posix())
    git(reference, "push", "-q", "origin", "main")
    before = git(reference, "rev-parse", "HEAD")
    git(reference, "switch", "-q", "-c", "writer")
    source = "client/Docs/Inventory.md"
    (reference / source).write_text("# Updated inventory\n", encoding="utf-8", newline="\n")
    git(reference, "add", "--", source)
    git(reference, "commit", "-qm", "Fast-forward test fixture")
    git(reference, "push", "-q", "origin", "writer:main")
    expected = git(reference, "rev-parse", "HEAD")
    git(reference, "switch", "-q", "main")

    def sync():
        return subprocess.run(
            [shutil.which("powershell.exe"), "-NoProfile", "-NonInteractive", "-File",
             str(workspace / "scripts/sync-reference.ps1"), "-Config", str(config), "-ExpectedBranch", "main"],
            env={**os.environ, "PYTHONPATH": str(workspace / "src")}, capture_output=True, text=True, timeout=30,
        )

    with activity_lock(reference, tmp_path / "different-state"):
        busy = sync()
        assert busy.returncode and "Stop the MCP service" in busy.stderr
    assert git(reference, "rev-parse", "HEAD") == before
    assert not (reference / ".git/FETCH_HEAD").is_file()

    dirty = sync()
    assert dirty.returncode and "untracked changes" in dirty.stderr
    assert git(reference, "rev-parse", "HEAD") == before

    (reference / ".git/info/exclude").write_text("client/Assets/ignored-untracked.cs\n", encoding="utf-8")
    result = sync()
    assert result.returncode == 0, result.stderr
    assert before in result.stdout and expected in result.stdout
    assert git(reference, "rev-parse", "HEAD") == expected
    assert (reference / "client/Assets/ignored-untracked.cs").exists()

    (reference / source).write_text("# Divergent local change\n", encoding="utf-8", newline="\n")
    git(reference, "add", "--", source)
    git(reference, "commit", "-qm", "Local-only commit")
    divergent = git(reference, "rev-parse", "HEAD")
    assert sync().returncode
    assert git(reference, "rev-parse", "HEAD") == divergent
