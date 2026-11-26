from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from conftest import git


SCRIPT = "client/Assets/Scripts/InventoryManager.cs"


def advance(writer, content="// new remote content\n"):
    (writer / SCRIPT).write_text(content, encoding="utf-8")
    git(writer, "commit", "-qam", "Change script remotely")
    git(writer, "push", "origin", "main")
    return git(writer, "rev-parse", "HEAD")


def test_first_unpinned_info_fetches_and_cooldown_reuses_result(refresh_settings, reference):
    from unity_project_mcp.refresh import RefreshingProject

    settings, writer, _ = refresh_settings
    initial = git(reference, "rev-parse", "HEAD")
    original_file = (reference / SCRIPT).read_bytes()
    service = RefreshingProject(settings)
    updated = advance(writer)

    assert service.invoke("read_file", path=SCRIPT)["commit"] == initial
    assert service.invoke("get_project_info", expected_commit=initial)["commit"] == initial
    first = service.invoke("get_project_info")
    assert first["success"] and first["commit"] == updated
    assert first["meta"]["refresh"]["status"] == "updated"
    assert first["meta"]["refresh"]["checked_at"]
    assert service.invoke("read_file", path=SCRIPT, expected_commit=updated)["data"][0]["text"] == "// new remote content"
    assert service.invoke("read_file", path=SCRIPT, expected_commit=initial)["error"]["code"] == "COMMIT_MISMATCH"

    newest = advance(writer, "// newest remote content\n")
    assert service.invoke("get_project_info")["commit"] == updated
    service.next_refresh_at = 0
    assert service.invoke("get_project_info")["commit"] == newest
    assert git(reference, "rev-parse", "HEAD") == initial
    assert (reference / SCRIPT).read_bytes() == original_file


def test_failed_refresh_keeps_the_last_usable_commit_across_restart(refresh_settings):
    from unity_project_mcp.refresh import RefreshingProject

    settings, writer, remote = refresh_settings
    service = RefreshingProject(settings)
    updated = advance(writer)
    assert service.invoke("get_project_info")["commit"] == updated

    git(writer, "rm", "client/ProjectSettings/ProjectVersion.txt")
    git(writer, "commit", "-qm", "Remove Unity version")
    git(writer, "push", "origin", "main")
    service.next_refresh_at = 0
    invalid = service.invoke("get_project_info")
    assert invalid["success"] and invalid["commit"] == updated
    assert invalid["meta"]["refresh"]["status"] == "failed"
    assert service.invoke("get_project_info")["meta"]["refresh"] == invalid["meta"]["refresh"]

    remote.rename(remote.with_name("offline.git"))
    restarted = RefreshingProject(settings)
    offline = restarted.invoke("get_project_info")
    assert offline["success"] and offline["commit"] == updated
    assert offline["meta"]["refresh"]["status"] == "failed"
    assert restarted.invoke("read_file", path=SCRIPT, expected_commit=updated)["success"]


def test_concurrent_info_calls_share_one_fetch(refresh_settings, monkeypatch):
    from unity_project_mcp.refresh import RefreshingProject

    settings, writer, _ = refresh_settings
    service = RefreshingProject(settings)
    updated = advance(writer)
    entered, finish = Event(), Event()
    fetch = service.fetch
    attempts = []

    def wait_for_test(deadline):
        attempts.append(True)
        entered.set()
        assert finish.wait(timeout=5)
        return fetch(deadline)

    monkeypatch.setattr(service, "fetch", wait_for_test)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.invoke, "get_project_info")
        assert entered.wait(timeout=5)
        second = executor.submit(service.invoke, "get_project_info")
        finish.set()
        results = [first.result(timeout=10), second.result(timeout=10)]
    assert len(attempts) == 1
    assert [result["commit"] for result in results] == [updated, updated]
    assert results[0]["meta"]["refresh"] == results[1]["meta"]["refresh"]


def test_cache_seed_rejects_hidden_reference_changes(refresh_settings, reference):
    from unity_project_mcp.errors import ProjectError
    from unity_project_mcp.refresh import RefreshingProject

    settings, _, _ = refresh_settings
    git(reference, "update-index", "--assume-unchanged", "--", SCRIPT)
    (reference / SCRIPT).write_text("// concealed change\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="must not hide tracked changes"):
        RefreshingProject(settings)


def test_refreshed_project_rejects_executables_inside_reference(refresh_settings, reference):
    from unity_project_mcp.errors import ProjectError
    from unity_project_mcp.refresh import RefreshingProject

    settings, _, _ = refresh_settings
    fixture_rg = reference / "rg.exe"
    fixture_rg.touch()
    settings.runtime.rg = fixture_rg
    with pytest.raises(ProjectError, match="must be installed outside the reference repository"):
        RefreshingProject(settings)
