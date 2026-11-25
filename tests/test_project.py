import os
import subprocess
import sys
from pathlib import Path
from time import monotonic

import pytest

from conftest import git


def call(project, name, **kwargs):
    return project.invoke(name, **kwargs)


def test_project_layout_and_metadata(project, reference):
    assert project.repository_root == reference
    assert project.unity_project_root == reference / "client"
    assert not project.service_root.is_relative_to(reference)
    result = call(project, "get_project_info")
    assert result["success"] and result["commit"] == git(reference, "rev-parse", "HEAD")
    info, = result["data"]
    assert info["unity_version"] == "2022.3.62f3"
    assert info["branch"] == "main"
    assert info["unity_project_path"] == "client"
    assert info["package_count"] == 2
    assert info["last_commit_time"]
    assert not info["tracked_dirty"]


def test_startup_validates_repository_and_external_state(settings, reference):
    from unity_project_mcp.errors import ProjectError
    from unity_project_mcp.project import Project

    invalid_root = settings.project.model_copy(update={"repository_root": reference / "client"})
    with pytest.raises(ProjectError, match="Git repository root"):
        Project(settings.model_copy(update={"project": invalid_root}))
    invalid_runtime = settings.runtime.model_copy(update={"state_dir": reference / "cache"})
    with pytest.raises(ProjectError, match="outside"):
        Project(settings.model_copy(update={"runtime": invalid_runtime}))
    assert not (reference / "cache").exists()


def test_worktree_config_is_checked_without_running_filters(settings, reference):
    from unity_project_mcp.project import Project

    git(reference, "config", "extensions.worktreeConfig", "true")
    git(reference, "config", "--worktree", "filter.fixture.clean", "missing-filter-must-not-run")
    git(reference, "config", "--worktree", "filter.fixture.required", "true")
    (reference / "client/Assets/.gitattributes").write_text("*.cs filter=fixture\n")
    modified_times = reference / "client/Assets/Scripts/InventoryManager.cs"
    os.utime(modified_times, (10, 10))
    assert Project(settings).invoke("get_project_info")["success"]


def test_toml_and_environment_config(reference, tmp_path, monkeypatch):
    from unity_project_mcp.config import load_settings

    path = tmp_path / "config.toml"
    path.write_text(
        "[project]\nname='fixture'\nrepository_root='placeholder'\nunity_project_path='client'\n"
        "[search]\ninclude_paths=['client/Assets', 'shared']\n", encoding="utf-8",
    )
    monkeypatch.setenv("UNITY_MCP_REPOSITORY_ROOT", str(reference))
    monkeypatch.setenv("UNITY_MCP_PORT", "9001")
    settings = load_settings(path)
    assert settings.project.repository_root == reference and settings.server.port == 9001
    assert settings.search.include_paths == ["client/Assets", "shared"]


def test_directory_only_lists_allowed_paths_and_ancestors(project):
    roots = call(project, "list_directory", path=".", depth=3)["data"]
    paths = {item["path"] for item in roots}
    assert {"client", "client/Assets", "client/Assets/Scripts", "client/Docs/Inventory.md"} <= paths
    assert not any("Library" in path or "Logs" in path or path.startswith(("server", "shared")) for path in paths)
    assert call(project, "list_directory", path="server")["error"]["code"] == "PATH_NOT_ALLOWED"
    assert call(project, "read_file", path="shared/Shared.md")["error"]["code"] == "PATH_NOT_ALLOWED"
    assert call(project, "read_file", path="client/Assets/ignored-untracked.cs")["error"]["code"] == "FILE_NOT_FOUND"


def test_other_modules_need_explicit_permission(settings):
    from unity_project_mcp.project import Project

    search = settings.search.model_copy(update={"include_paths": ["client/Assets", "server"]})
    project = Project(settings.model_copy(update={"search": search}))
    assert call(project, "search_files", query="Remote")["data"][0]["path"] == "server/Remote.cs"
    assert call(project, "search_code", query="Sell", path="server")["data"][0]["path"] == "server/Remote.cs"
    assert call(project, "read_file", path="shared/Shared.md")["error"]["code"] == "PATH_NOT_ALLOWED"


@pytest.mark.parametrize("path, code", [
    ("/client/Assets/a.cs", "INVALID_PATH"),
    (r"C:client\a.cs", "INVALID_PATH"),
    (r"C:\client\a.cs", "INVALID_PATH"),
    (r"\\host\share\a.cs", "INVALID_PATH"),
    (r"\\?\D:\foo", "INVALID_PATH"),
    (r"\\.\NUL", "INVALID_PATH"),
    ("client/Assets/a.cs:secret", "INVALID_PATH"),
    ("client/Assets/CON.txt", "INVALID_PATH"),
    ("client/Assets/COM¹.txt", "INVALID_PATH"),
    ("client/Assets/a.cs.", "INVALID_PATH"),
    ("client/Assets/a.cs\x00", "INVALID_PATH"),
    (r"client\Assets/../../private.txt", "PATH_OUTSIDE_PROJECT"),
    ("../client/Assets/x.cs", "PATH_OUTSIDE_PROJECT"),
    ("client/Assets/.env.local", "PATH_NOT_ALLOWED"),
    ("CLIENT/ASSETS/SELFSIGNED.pem", "PATH_NOT_ALLOWED"),
    ("client/Library/filter.cs", "PATH_NOT_ALLOWED"),
    (".git/config", "PATH_NOT_ALLOWED"),
])
def test_unsafe_paths(project, path, code):
    assert call(project, "read_file", path=path)["error"]["code"] == code


def test_canonical_windows_path_and_line_range(project):
    result = call(project, "read_file", path=r"CLIENT\assets\Scripts\INVENTORYmanager.cs", start_line=2, end_line=3)
    assert result["meta"]["path"] == "client/Assets/Scripts/InventoryManager.cs"
    assert [(row["line"], row["text"]) for row in result["data"]] == [
        (2, "class InventoryManager {"), (3, "  void Sell() {}"),
    ]
    assert result["meta"]["truncated"] and result["meta"]["next_line"] == 4


def test_listing_and_search_pagination(project):
    for name, args in [("list_directory", {"depth": 3}), ("search_files", {"query": "inventory"})]:
        first = call(project, name, **args, limit=1)
        cursor = first["meta"]["next_cursor"]
        second = call(project, name, **args, limit=1, cursor=cursor)
        assert first["meta"]["truncated"] and cursor
        assert first["data"][0]["path"] != second["data"][0]["path"]
        assert call(project, name, **args, cursor=cursor + "x")["error"]["code"] == "INVALID_CURSOR"
    assert call(project, "search_files", query="does-not-match")["data"] == []


def test_code_search_is_literal_and_scoped(project):
    result = call(project, "search_code", query="-literal[1]", context_lines=1)
    assert {(row["path"], row["line"]) for row in result["data"]} == {
        ("client/Assets/Scripts/-Other.cs", 1), ("client/Assets/Scripts/InventoryManager.cs", 5),
    }
    assert result["data"][-1]["context"][0]["line"] == 4
    assert call(project, "search_code", query="--help")["data"] == []
    assert call(project, "search_code", query="Sell", limit=1)["meta"]["truncated"]
    prefab = call(project, "search_code", query="Sell", extensions=["prefab"])
    assert prefab["data"][0]["path"] == "client/Assets/Prefabs/Store.prefab"
    assert call(project, "search_code", query="Hidden")["data"] == []
    assert call(project, "search_code", query="café", path="client/Assets/direct.cs")["data"][0]["line"] == 1
    assert call(project, "search_code", query="café", path="client/Assets")["data"][0]["path"] == "client/Assets/direct.cs"


def test_references_are_candidates(project):
    result = call(project, "find_references", symbol="Sell")
    assert {row["line"] for row in result["data"]} == {3, 4}
    assert all(row["confidence"] == "text_candidate" for row in result["data"])
    assert call(project, "find_references", symbol="ell")["data"] == []


def test_packages_do_not_expose_url_credentials(project):
    result = call(project, "get_packages")
    packages = {item["name"]: item for item in result["data"]}
    assert packages["com.test.ui"]["dependencies"] == {"com.test.core": "2.0.0"}
    assert packages["com.test.remote"]["source"] == "git"
    assert packages["com.test.remote"]["source_url"] == "https://example.invalid"
    assert all(secret not in str(result) for secret in ("password", "token-secret", "?token", "user:"))


def test_package_file_redacts_json_escaped_url_credentials(project):
    result = call(project, "read_file", path="client/Packages/manifest.json")
    assert result["success"]
    content = result["data"][0]["text"]
    assert "https://example.invalid" in content
    assert not any(value in content for value in ("password", "token-secret", "user:"))


@pytest.mark.parametrize("name, code", [("raw.asset", "BINARY_FILE"), ("lfs.asset", "GIT_LFS_POINTER")])
def test_read_rejects_non_text(project, name, code):
    assert call(project, "read_file", path=f"client/Assets/{name}")["error"]["code"] == code


def test_file_and_response_budgets(settings):
    import json

    from unity_project_mcp.project import Project

    limits = settings.limits.model_copy(update={"max_text_file_bytes": 1024, "max_response_bytes": 1024})
    small = Project(settings.model_copy(update={"limits": limits}))
    assert call(small, "read_file", path="client/Assets/huge.txt")["error"]["code"] == "FILE_TOO_LARGE"
    larger = Project(settings.model_copy(update={"limits": limits.model_copy(update={"max_text_file_bytes": 30_000})}))
    long_line = call(larger, "read_file", path="client/Assets/huge.txt")
    assert long_line["success"] and long_line["meta"]["truncated"]
    assert len(json.dumps(long_line, ensure_ascii=False).encode()) <= 1024


def test_dirty_and_changed_baselines(project, settings, reference):
    from unity_project_mcp.errors import ProjectError
    from unity_project_mcp.project import Project

    assert call(project, "search_code", query="Sell", expected_commit="0" * 40)["error"]["code"] == "COMMIT_MISMATCH"
    tracked = reference / "server/Remote.cs"
    tracked.write_text("dirty in denied sibling\n")
    assert call(project, "get_project_info")["error"]["code"] == "PROJECT_CHANGED"
    with pytest.raises(ProjectError, match="tracked"):
        Project(settings)
    git(reference, "restore", "--", "server/Remote.cs")
    assert call(project, "get_project_info")["error"]["code"] == "PROJECT_CHANGED"


def test_branch_change_at_same_commit_is_detected(project, reference):
    git(reference, "switch", "-c", "fixture-branch")
    assert call(project, "get_project_info")["error"]["code"] == "PROJECT_CHANGED"


def test_queries_leave_worktree_and_git_index_unchanged(project, reference):
    def snapshot():
        return {
            path.relative_to(reference).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in reference.rglob("*") if path.is_file()
        }

    before = snapshot()
    assert call(project, "get_project_info")["success"]
    assert call(project, "search_code", query="Inventory")["success"]
    assert call(project, "read_file", path="client/Docs/Inventory.md")["success"]
    assert snapshot() == before


def test_runner_kills_slow_process_and_caps_output(tmp_path):
    from unity_project_mcp.errors import ProjectError
    from unity_project_mcp.process import run_bounded

    started = monotonic()
    launcher = (
        "import subprocess, sys; "
        "subprocess.run([sys.executable, '-c', 'import time; time.sleep(10)'])"
    )
    with pytest.raises(ProjectError) as error:
        run_bounded([sys.executable, "-c", launcher], tmp_path, os.environ, timeout=0.5, cap=64)
    assert error.value.code == "SEARCH_TIMEOUT"
    assert monotonic() - started < 4, "a surviving child held the output pipe open"
    output = run_bounded([sys.executable, "-c", "print('x' * 1000000)"], tmp_path, os.environ, timeout=5, cap=64)
    assert output.truncated and output.stdout == b"x" * 64


def test_project_query_deadline_covers_git_baseline_checks(settings):
    from unity_project_mcp.project import Project

    limits = settings.limits.model_copy(update={"search_timeout_seconds": 0.00001})
    project = Project(settings.model_copy(update={"limits": limits}))
    result = call(project, "search_code", query="Inventory")
    assert result["error"]["code"] == "SEARCH_TIMEOUT"
    assert call(project, "get_project_info")["success"]


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_windows_links_are_never_followed(project, reference, tmp_path, kind):
    from unity_project_mcp.errors import ProjectError
    from unity_project_mcp.security import ensure_no_links

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "external.cs").write_text("outside data")
    link = reference / "client" / "Assets" / "Escape"
    if kind == "junction":
        if os.name != "nt":
            pytest.skip("NTFS junction requires Windows")
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        if result.returncode:
            pytest.skip("This Windows account or filesystem cannot create junctions")
    else:
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("Symlink creation requires privileges or Windows Developer Mode")
    rel = "client/Assets/Escape/external.cs"
    with pytest.raises(ProjectError, match="link|reparse"):
        ensure_no_links(Path(reference), rel)
    result = call(project, "read_file", path=rel)
    assert result["error"]["code"] == "PATH_NOT_ALLOWED"


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction requires Windows")
def test_tracked_parent_replaced_with_junction_invalidates_project(project, reference, tmp_path):
    parent = reference / "client/Assets/Scripts"
    outside = tmp_path / "moved-tracked"
    parent.rename(outside)
    result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(parent), str(outside)], capture_output=True)
    if result.returncode:
        pytest.skip("This Windows account or filesystem cannot create junctions")
    assert call(project, "get_project_info")["error"]["code"] == "PROJECT_CHANGED"


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction requires Windows")
def test_git_object_store_cannot_redirect_outside_reference(settings, reference, tmp_path):
    from unity_project_mcp.errors import ProjectError
    from unity_project_mcp.project import Project

    object_store = reference / ".git/objects"
    outside = tmp_path / "git-objects"
    object_store.rename(outside)
    result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(object_store), str(outside)], capture_output=True)
    if result.returncode:
        pytest.skip("This Windows account or filesystem cannot create junctions")
    with pytest.raises(ProjectError, match="reparse"):
        Project(settings)
