import json

import pytest

from conftest import git


SCRIPT = "a1" * 16
PREFAB = "b2" * 16
ASSEMBLY = "c3" * 16
UNKNOWN = "d4" * 16
BUILTIN = "0000000000000000f000000000000000"
STORE = "client/Assets/Prefabs/Store.prefab"
SCRIPT_PATH = "client/Assets/Scripts/InventoryManager.cs"
ASSEMBLY_PATH = "client/Assets/Scripts/Shared.asmdef"


@pytest.fixture
def asset_project(settings, reference):
    from unity_project_mcp.project import Project

    paths = {
        STORE: (
            "%YAML 1.1\n--- !u!114 &8123456789012345\nMonoBehaviour:\n"
            f"  m_Script: {{fileID : 11500000, guid : {SCRIPT}, type : 3}}\n"
            f"  m_Unknown: {{fileID: -400,\n    guid: {UNKNOWN}, type: 3}}\n"
            f"  m_Material: {{fileID: 123, guid: {BUILTIN}, type: 0}}\n"
            "  m_Local: {fileID: 8123456789012345}\n  m_Unbound: {fileID: 17}\n  m_Empty: {fileID: 0}\n"
            f"  # m_Comment: {{fileID: 9, guid: {SCRIPT}, type: 3}}\n"
            f"  m_String: \"{{fileID: 8, guid: {SCRIPT}, type: 3}}\"\n"
        ),
        STORE + ".meta": f"fileFormatVersion: 2\nguid: {PREFAB}\n",
        SCRIPT_PATH + ".meta": f"fileFormatVersion: 2\nguid: {SCRIPT}\nMonoImporter:\n",
        ASSEMBLY_PATH: json.dumps({"name": "Game.Shared"}),
        ASSEMBLY_PATH + ".meta": f"fileFormatVersion: 2\nguid: {ASSEMBLY}\n",
        "client/Assets/Scripts/Main.asmdef": json.dumps({
            "name": "Game.Main", "references": ["Game.Shared", f"GUID:{ASSEMBLY}", "Missing.Assembly"],
            "includePlatforms": ["Editor"], "excludePlatforms": [], "defineConstraints": ["UNITY_EDITOR"],
        }),
        "client/Packages/com.test.local/UseShared.asmref": json.dumps({"reference": f"GUID:{ASSEMBLY}"}),
        "client/Packages/com.test.local/Sample.asset": "%YAML 1.1\n--- !u!1 &1\n",
        "client/Packages/com.test.local/Sample.asset.meta": f"fileFormatVersion: 2\nguid: {'e5' * 16}\n",
        "server/Other.asset": f"%YAML 1.1\n--- !u!1 &1\n  m_Script: {{fileID: 1, guid: {SCRIPT}}}\n",
        "server/Other.asset.meta": f"fileFormatVersion: 2\nguid: {UNKNOWN}\n",
    }
    for rel, value in paths.items():
        target = reference / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8", newline="\n")
    git(reference, "add", "--", *paths)
    git(reference, "commit", "-qm", "Add Unity asset references")
    return Project(settings)


def invoke(project, name, **arguments):
    result = project.invoke(name, **arguments)
    assert result["success"], result
    return result


def test_guid_index_uses_baseline_and_external_cache(asset_project, settings, reference):
    from unity_project_mcp.project import Project

    result = invoke(asset_project, "find_asset_by_guid", guid=SCRIPT.upper(), expected_commit=asset_project.commit)
    found, = result["data"]
    assert found["asset_path"] == SCRIPT_PATH and found["meta_path"] == SCRIPT_PATH + ".meta"
    cache, = list(asset_project.state_dir.glob("guid-*.json"))
    assert not cache.is_relative_to(reference)
    assert json.loads(cache.read_text(encoding="utf-8"))["commit"] == asset_project.commit
    assert invoke(Project(settings), "find_asset_by_guid", guid=SCRIPT)["data"] == result["data"]
    assert invoke(asset_project, "find_asset_by_guid", guid=UNKNOWN)["data"][0]["status"] == "unresolved_guid"
    local = invoke(asset_project, "find_asset_by_guid", guid="e5" * 16)["data"][0]
    assert local["asset_path"].startswith("client/Packages/")
    assert asset_project.invoke("find_asset_by_guid", guid="--help")["error"]["code"] == "INVALID_ARGUMENT"


def test_forward_asset_references_include_file_ids_and_uncertainty(asset_project):
    rows = invoke(asset_project, "get_asset_references", path=STORE)["data"]
    assert len(rows) == 6
    by_status = {row["status"]: row for row in rows}
    script = by_status["resolved_asset_guid"]
    assert (script["path"], script["line"], script["file_id"], script["asset_path"]) == (STORE, 4, 11500000, SCRIPT_PATH)
    assert not script["file_id_verified"]
    assert by_status["unresolved_guid"]["file_id"] == -400
    assert "package_unavailable" in by_status["unresolved_guid"]["possible_reasons"]
    assert by_status["builtin_resource"]["guid"] == BUILTIN
    assert by_status["local_file_id"]["file_id_verified"]
    assert not by_status["unresolved_file_id"]["file_id_verified"]


def test_reverse_asset_references_use_serialization_candidates(asset_project):
    rows = invoke(asset_project, "get_asset_references", path=SCRIPT_PATH, direction="reverse")["data"]
    hit, = rows
    assert (hit["path"], hit["line"], hit["confidence"]) == (STORE, 4, "serialized_candidate")


@pytest.mark.parametrize("filename, error_code", [("raw.asset", "BINARY_FILE"), ("lfs.asset", "GIT_LFS_POINTER")])
def test_asset_reference_errors_are_explicit(project, filename, error_code):
    result = project.invoke("get_asset_references", path=f"client/Assets/{filename}")
    assert result["error"]["code"] == error_code


def test_assembly_metadata_resolves_name_and_guid(asset_project):
    result = invoke(asset_project, "get_assembly_info", path="client/Assets/Scripts/Main.asmdef")
    main, = result["data"]
    assert main["name"] == "Game.Main" and main["include_platforms"] == ["Editor"]
    assert main["define_constraints"] == ["UNITY_EDITOR"]
    by_reference = {item["reference"]: item for item in main["references"]}
    assert by_reference["Game.Shared"]["path"] == ASSEMBLY_PATH
    assert by_reference[f"GUID:{ASSEMBLY}"]["path"] == ASSEMBLY_PATH
    assert by_reference["Missing.Assembly"]["status"] == "unresolved"
    ref, = invoke(asset_project, "get_assembly_info", path="client/Packages/com.test.local/UseShared.asmref")["data"]
    assert ref["kind"] == "asmref" and ref["references"][0]["path"] == ASSEMBLY_PATH


def test_git_history_and_diff_do_not_expose_denied_paths(settings, reference):
    from unity_project_mcp.project import Project

    initial = git(reference, "rev-parse", "HEAD")
    for rel, content, title in (
        ("server/Remote.cs", "denied sibling change\n", "Denied sibling change"),
        ("client/Assets/.env.local", "SECRET=new-secret\n", "Denied credentials change"),
        ("client/Docs/Inventory.md", "# Inventory\nBusiness rules updated.\n", "Update Inventory docs"),
    ):
        (reference / rel).write_text(content, encoding="utf-8", newline="\n")
        git(reference, "add", "--", rel)
        git(reference, "commit", "-qm", title)
    project = Project(settings)
    history = invoke(project, "get_git_log")["data"]
    assert [item["subject"] for item in history] == ["Update Inventory docs", "Initial Unity fixture"]
    assert history[0]["commit"] == project.commit
    assert invoke(project, "get_git_log", path=r"CLIENT\DOCS", limit=1)["meta"]["truncated"]
    patch = invoke(project, "get_git_diff", from_commit=initial, to_commit=project.commit)
    entry, = patch["data"]
    assert entry["path"] == "client/Docs/Inventory.md" and "+Business rules updated." in entry["patch"]
    assert "new-secret" not in str(patch) and "Remote.cs" not in str(patch)
    assert invoke(project, "get_git_diff", from_commit=initial, to_commit=project.commit, max_lines=2)["meta"]["truncated"]
    assert project.invoke("get_git_diff", from_commit="HEAD~1", to_commit=project.commit)["error"]["code"] == "INVALID_COMMIT"
    assert project.invoke("get_git_log", path="client/Assets/.env.local")["error"]["code"] == "PATH_NOT_ALLOWED"


def test_history_supports_deleted_and_dash_prefixed_file(settings, reference):
    from unity_project_mcp.project import Project

    initial = git(reference, "rev-parse", "HEAD")
    path = "client/Assets/Scripts/-Other.cs"
    git(reference, "rm", "--", path)
    git(reference, "commit", "-qm", "Remove example script")
    project = Project(settings)
    assert invoke(project, "get_git_log", path=path)["data"][0]["subject"] == "Remove example script"
    diff, = invoke(project, "get_git_diff", from_commit=initial, to_commit=project.commit, path=path)["data"]
    assert diff["path"] == path and diff["status"] == "D"


def test_diff_file_to_directory_does_not_include_denied_descendants(settings, reference):
    from unity_project_mcp.project import Project

    rel = "client/Assets/Switch[1].txt"
    path = reference / rel
    path.write_text("former file\n", encoding="utf-8")
    git(reference, "--literal-pathspecs", "add", "--", rel)
    git(reference, "commit", "-qm", "Add fixture file")
    before = git(reference, "rev-parse", "HEAD")
    git(reference, "--literal-pathspecs", "rm", "--", rel)
    path.mkdir()
    (path / ".env.local").write_text("test-credential-must-not-appear\n", encoding="utf-8")
    (path / "visible.txt").write_text("visible child\n", encoding="utf-8")
    git(reference, "--literal-pathspecs", "add", "--", rel)
    git(reference, "commit", "-qm", "Replace fixture file with directory")
    project = Project(settings)
    diff = invoke(project, "get_git_diff", from_commit=before, to_commit=project.commit)
    rows = {row["path"]: row for row in diff["data"]}
    assert set(rows) == {rel, rel + "/visible.txt"}
    assert "former file" in rows[rel]["patch"] and "visible child" not in rows[rel]["patch"]
    assert "test-credential-must-not-appear" not in str(diff) and ".env.local" not in str(diff)
