import os
import shutil
import subprocess

import pytest


def git(root, *args, check=True):
    return subprocess.run(
        [shutil.which("git"), "-C", str(root), *args],
        check=check, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    ).stdout.strip()


@pytest.fixture
def reference(tmp_path):
    if not shutil.which("git") or not shutil.which("rg"):
        pytest.fail("The tests require git and ripgrep on PATH")
    root = tmp_path / "reference"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Fixture Author")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "core.autocrlf", "false")
    files = {
        "client/ProjectSettings/ProjectVersion.txt": "m_EditorVersion: 2022.3.62f3\n",
        "client/Packages/manifest.json": (
            '{"dependencies":{"com.test.ui":"1.2.3",'
            r'"com.test.remote":"https:\/\/user:password@example.invalid/token-secret/ui.git?token=secret#secret"}}' + "\n"
        ),
        "client/Packages/packages-lock.json": (
            '{"dependencies":{"com.test.ui":{"version":"1.2.3","source":"registry",'
            '"dependencies":{"com.test.core":"2.0.0"}},'
            '"com.test.remote":{"version":"https://user:password@example.invalid/ui.git",'
            '"source":"git","dependencies":{}}}}\n'
        ),
        "client/Assets/Scripts/InventoryManager.cs": (
            "// inventory C#\nclass InventoryManager {\n  void Sell() {}\n"
            "  void Resell() { Sell(); }\n  // -literal[1]\n}\n"
        ),
        "client/Assets/Scripts/-Other.cs": "// -literal[1]\nclass Other { InventoryManager inventory; }\n",
        "client/Assets/Prefabs/Store.prefab": "%YAML 1.1\n--- !u!1 &1\nm_Name: Sell\n",
        "client/Docs/Inventory.md": "# Inventory\nInventory business rules.\n",
        "client/LocalTests/cases.txt": "Case 1\n",
        "client/Library/filter.cs": "Sell",
        "client/Logs/editor.log": "private log",
        "client/Assets/.env.local": "PROTECTED_SECRET=never_return_this\n",
        "client/Assets/selfsigned.PEM": "private material\n",
        "client/Assets/raw.asset": b"UnityFS\0\xff\x03",
        "client/Assets/bad.cs": b"class Hidden {}\n" + b"a" * 9000 + b"\x00",
        "client/Assets/lfs.asset": (
            "version https://git-lfs.github.com/spec/v1\n" + "oid sha256:" + "0" * 64 + "\nsize 2000000\n"
        ),
        "client/Assets/huge.txt": "x" * 20_000,
        "client/Assets/direct.cs": "class Café { }\r\n".encode("utf-16"),
        "server/Remote.cs": "class Remote { void Sell() {} }\n",
        "shared/Shared.md": "private sibling\n",
    }
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
    git(root, "add", "--all")
    git(root, "commit", "-qm", "Initial Unity fixture")
    (root / "client/Assets/ignored-untracked.cs").write_text("class Untracked { void Sell() {} }")
    return root


@pytest.fixture
def settings(reference, tmp_path):
    from unity_project_mcp.config import Settings

    return Settings.model_validate({
        "project": {"name": "fixture", "repository_root": reference, "unity_project_path": "client"},
        "runtime": {"state_dir": tmp_path / "state"},
    })


@pytest.fixture
def project(settings):
    from unity_project_mcp.project import Project

    return Project(settings)


@pytest.fixture
def refresh_settings(settings, reference, tmp_path):
    remote = tmp_path / "remote.git"
    writer = tmp_path / "writer"
    git(tmp_path, "clone", "--bare", str(reference), str(remote))
    git(tmp_path, "clone", str(remote), str(writer))
    git(writer, "config", "user.name", "Remote Writer")
    git(writer, "config", "user.email", "writer@example.invalid")
    git(reference, "remote", "add", "origin", str(remote))
    settings = type(settings).model_validate({**settings.model_dump(), "refresh": {"enabled": True}})
    return settings, writer, remote
