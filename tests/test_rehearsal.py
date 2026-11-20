"""Tests for the unpack-order rehearsal endpoint and its simulation rules."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from debbuild import build_deb  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent))
from app import rehearsal as rehearsal_mod  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.versions import compare_versions, parse_replaces  # noqa: E402


def make_settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", retention=2)


@pytest.fixture()
def context(tmp_path):
    settings = make_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app.state.repository, settings, tmp_path


def upload(client: TestClient, paths: list[Path]):
    files = [
        ("files", (p.name, p.read_bytes(), "application/vnd.debian.binary-package"))
        for p in paths
    ]
    return client.post("/api/publish", files=files)


def build(
    tmp: Path,
    name: str,
    version: str,
    *,
    files: dict[str, bytes] | None = None,
    replaces: str | None = None,
    symlinks: dict[str, str] | None = None,
) -> Path:
    control = {"Package": name, "Version": version}
    if replaces:
        control["Replaces"] = replaces
    path = tmp / f"{name}_{version}.deb"
    build_deb(
        path,
        control=control,
        files=files,
        symlinks=symlinks,
        payload=None if files is not None else b"payload\n",
    )
    return path


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current_publication_id(client: TestClient) -> int:
    return client.get("/api/status").json()["current"]["id"]


def rehearse(client: TestClient, payload: dict):
    return client.post("/api/rehearsals", json=payload)


# --------------------------------------------------------------- pure helpers
def test_version_ordering_matches_dpolicy():
    assert compare_versions("1.0~rc1", "1.0") < 0
    assert compare_versions("1:2.0", "2.0") > 0
    assert compare_versions("1.0", "1.00") == 0
    assert compare_versions("1.0-2", "1.0-10") < 0
    assert compare_versions("1.0~~", "1.0~") < 0


def test_replaces_clause_arch_and_versions():
    clauses = parse_replaces("libfoo (<< 2.0), libfoo:amd64 (>= 1:1.0)")
    assert clauses[0].matches("libfoo", "1.9", "amd64") is True
    assert clauses[0].matches("libfoo", "2.0", "amd64") is False
    assert clauses[0].matches("libbar", "1.0", "amd64") is False
    assert clauses[1].matches("libfoo", "1:1.0", "arm64") is False
    assert clauses[1].matches("libfoo", "1:1.0", "amd64") is True


# -------------------------------------------------------------------- happy path
def test_no_conflict_when_files_do_not_overlap(context):
    client, _repo, _settings, tmp = context
    src = tmp / "src"
    a = build(src, "alpha", "1.0", files={"usr/bin/alpha": b"a\n"})
    b = build(src, "beta", "1.0", files={"usr/bin/beta": b"b\n"})
    assert upload(client, [a, b]).status_code == 201
    pub = current_publication_id(client)

    resp = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(a), sha256_of(b)],
            "installed": [],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["publication_id"] == pub
    assert body["result"] == "no_conflict"
    assert [step["status"] for step in body["steps"]] == ["applied", "applied"]
    assert "conflict" not in body


def test_response_always_echoes_publication_id(context):
    client, _repo, _settings, tmp = context
    src = tmp / "src"
    a = build(src, "alpha", "1.0")
    b = build(src, "beta", "1.0")
    upload(client, [a, b])
    pub = current_publication_id(client)
    resp = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(a), sha256_of(b)],
            "installed": [],
        },
    )
    assert resp.json()["publication_id"] == pub


# ------------------------------------------------------------- same-package upgrade
def test_same_package_may_overwrite_its_own_files_and_releases_old_paths(context):
    client, _repo, _settings, tmp = context
    src = tmp / "src"
    old = build(
        src,
        "app",
        "1.0",
        files={
            "usr/bin/app": b"1\n",
            "usr/share/app/old.txt": b"gone\n",
        },
    )
    upload(client, [old])
    pub = current_publication_id(client)

    # A second publication carries the upgrade and an unrelated package.
    new = build(
        src,
        "app",
        "2.0",
        files={"usr/bin/app": b"2\n", "usr/share/app/new.txt": b"new\n"},
    )
    other = build(src, "other", "1.0", files={"usr/bin/other": b"x\n"})
    upload(client, [new, other])
    pub = current_publication_id(client)

    resp = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(new), sha256_of(other)],
            "installed": [
                {
                    "name": "app",
                    "version": "1.0",
                    "files": [
                        "/usr/bin/app",
                        "/usr/share/app/old.txt",
                    ],
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "no_conflict"


def test_released_path_can_be_taken_by_another_package_later(context):
    client, _repo, _settings, tmp = context
    src = tmp / "src"
    old = build(src, "app", "1.0", files={"usr/share/x": b"old\n"})
    upload(client, [old])
    new = build(src, "app", "2.0", files={"usr/share/keep": b"k\n"})
    taker = build(src, "taker", "1.0", files={"usr/share/x": b"new\n"})
    upload(client, [new, taker])
    pub = current_publication_id(client)

    # Unpack the upgrade first: it releases /usr/share/x. Only then may the
    # other package claim the path without any Replaces clause.
    resp = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(new), sha256_of(taker)],
            "installed": [
                {"name": "app", "version": "1.0", "files": ["/usr/share/x"]}
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "no_conflict"

    # Reversing the order must block: the path is still app's when taker runs.
    resp = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(taker), sha256_of(new)],
            "installed": [
                {"name": "app", "version": "1.0", "files": ["/usr/share/x"]}
            ],
        },
    )
    body = resp.json()
    assert body["result"] == "conflict"
    conflict = body["conflict"]
    assert conflict["step"] == 1
    assert conflict["package"] == "taker"
    assert conflict["version"] == "1.0"
    assert conflict["path"] == "/usr/share/x"
    assert conflict["owner_package"] == "app"
    assert conflict["owner_version"] == "1.0"


# --------------------------------------------------------------- Replaces rules
def test_replaces_accepts_owner_then_blocks_newer_owner(context):
    client, _repo, _settings, tmp = context
    src = tmp / "src"
    # Publication containing the replacing package plus a filler.
    newlib = build(
        src,
        "libnew",
        "2.0",
        files={"usr/lib/lib.so": b"2\n"},
        replaces="libold (<< 2.0)",
    )
    filler = build(src, "filler", "1.0", files={"usr/bin/filler": b"f\n"})
    upload(client, [newlib, filler])
    pub = current_publication_id(client)

    def run(owner_version: str):
        return rehearse(
            client,
            {
                "publication_id": pub,
                "package_sha256": [sha256_of(newlib), sha256_of(filler)],
                "installed": [
                    {
                        "name": "libold",
                        "version": owner_version,
                        "files": ["/usr/lib/lib.so"],
                    }
                ],
            },
        )

    ok = run("1.9")
    assert ok.status_code == 200
    assert ok.json()["result"] == "no_conflict"

    blocked = run("2.0")
    body = blocked.json()
    assert body["result"] == "conflict"
    conflict = body["conflict"]
    assert conflict["owner_package"] == "libold"
    assert conflict["owner_version"] == "2.0"
    assert conflict["path"] == "/usr/lib/lib.so"
    assert conflict["step"] == 1
    # The run stops at the first blocked step; nothing after it is simulated.
    assert [step["step"] for step in blocked.json()["steps"]] == [1]
    assert blocked.json()["steps"][0]["status"] == "blocked"


def test_unversioned_replaces_matches_any_owner_version(context):
    client, _repo, _settings, tmp = context
    src = tmp / "src"
    successor = build(
        src,
        "successor",
        "1.0",
        files={"etc/conf": b"n\n"},
        replaces="predecessor",
    )
    filler = build(src, "filler", "1.0", files={"usr/bin/filler": b"f\n"})
    upload(client, [successor, filler])
    pub = current_publication_id(client)

    resp = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(successor), sha256_of(filler)],
            "installed": [
                {
                    "name": "predecessor",
                    "version": "99:99.99",
                    "files": ["/etc/conf"],
                }
            ],
        },
    )
    assert resp.json()["result"] == "no_conflict"


def test_replaces_wrong_owner_name_does_not_help(context):
    client, _repo, _settings, tmp = context
    src = tmp / "src"
    a = build(
        src,
        "alpha",
        "1.0",
        files={"usr/shared": b"a\n"},
        replaces="someoneelse",
    )
    b = build(src, "beta", "1.0", files={"usr/bin/beta": b"b\n"})
    upload(client, [a, b])
    pub = current_publication_id(client)

    resp = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(a), sha256_of(b)],
            "installed": [
                {"name": "victim", "version": "1.0", "files": ["/usr/shared"]}
            ],
        },
    )
    conflict = resp.json()["conflict"]
    assert conflict["owner_package"] == "victim"
    assert conflict["package"] == "alpha"


def test_owner_version_is_the_simulated_current_one(context):
    """A prior step upgrades the owner; the conflict check sees the new version."""

    client, _repo, _settings, tmp = context
    src = tmp / "src"
    lib_upgrade = build(src, "libx", "2.0", files={"usr/lib/x": b"2\n"})
    app = build(
        src,
        "app",
        "1.0",
        files={"usr/lib/x": b"app\n"},
        replaces="libx (<< 2.0)",
    )
    filler = build(src, "filler", "1.0", files={"usr/bin/filler": b"f\n"})
    upload(client, [lib_upgrade, app, filler])
    pub = current_publication_id(client)

    resp = rehearse(
        client,
        {
            "publication_id": pub,
            # libx upgrades to 2.0 first; app's Replaces (<< 2.0) then fails.
            "package_sha256": [
                sha256_of(lib_upgrade),
                sha256_of(app),
                sha256_of(filler),
            ],
            "installed": [
                {"name": "libx", "version": "1.0", "files": ["/usr/lib/x"]}
            ],
        },
    )
    body = resp.json()
    assert body["result"] == "conflict"
    conflict = body["conflict"]
    assert conflict["step"] == 2
    assert conflict["owner_package"] == "libx"
    assert conflict["owner_version"] == "2.0"


# ------------------------------------------------------------- archive reading
def test_archive_paths_become_absolute_and_symlinks_ignored(context):
    client, _repo, _settings, tmp = context
    src = tmp / "src"
    pkg = build(
        src,
        "links",
        "1.0",
        files={"./usr/bin/real": b"x\n", "opt/data/file": b"y\n"},
        symlinks={"usr/bin/alink": "real"},
    )
    filler = build(src, "filler", "1.0", files={"usr/bin/filler": b"f\n"})
    upload(client, [pkg, filler])
    pub = current_publication_id(client)

    # Victim owns the real file; the symlink must not appear as a regular file.
    resp = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(pkg), sha256_of(filler)],
            "installed": [
                {
                    "name": "victim",
                    "version": "1.0",
                    "files": ["/usr/bin/real", "/opt/data/file"],
                }
            ],
        },
    )
    conflict = resp.json()["conflict"]
    assert conflict["path"] == "/opt/data/file"
    assert conflict["step"] == 1


# ---------------------------------------------------------------- manifest errors
@pytest.mark.parametrize(
    "mutate",
    [
        # Only one digest: at least two are required.
        lambda p: {
            "publication_id": p,
            "package_sha256": ["a" * 64],
        },
        # Nine digests: at most eight are accepted.
        lambda p: {
            "publication_id": p,
            "package_sha256": [f"{i:0{64}x}" for i in range(9)],
        },
        # Non-hexadecimal character in an otherwise 64-char digest.
        lambda p: {
            "publication_id": p,
            "package_sha256": ["z" * 64, "b" * 64],
        },
        # The same digest appears twice (same-named package only once).
        lambda p: {
            "publication_id": p,
            "package_sha256": ["a" * 64, "a" * 64],
        },
        # Publication ids must be positive integers.
        lambda p: {
            "publication_id": 0,
            "package_sha256": ["a" * 64, "b" * 64],
        },
    ],
)
def test_invalid_manifest_returns_400(context, mutate):
    client, _repo, _settings, tmp = context
    src = tmp / "src"
    a = build(src, "alpha", "1.0")
    b = build(src, "beta", "1.0")
    upload(client, [a, b])
    pub = current_publication_id(client)
    payload = mutate(pub)
    payload.setdefault("installed", [])
    resp = rehearse(client, payload)
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["error"] == "invalid_manifest"


def test_relative_installed_path_rejected(context):
    client, _repo, _settings, tmp = context
    src = tmp / "src"
    a = build(src, "alpha", "1.0")
    b = build(src, "beta", "1.0")
    upload(client, [a, b])
    pub = current_publication_id(client)
    resp = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(a), sha256_of(b)],
            "installed": [
                {"name": "px", "version": "1.0", "files": ["usr/bin/x"]}
            ],
        },
    )
    assert resp.status_code == 400


def test_installed_file_owned_by_two_packages_rejected(context):
    client, _repo, _settings, tmp = context
    src = tmp / "src"
    a = build(src, "alpha", "1.0")
    b = build(src, "beta", "1.0")
    upload(client, [a, b])
    pub = current_publication_id(client)
    resp = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(a), sha256_of(b)],
            "installed": [
                {"name": "px", "version": "1.0", "files": ["/shared"]},
                {"name": "py", "version": "1.0", "files": ["/shared"]},
            ],
        },
    )
    assert resp.status_code == 400
    assert "owned by both" in resp.text


def test_duplicate_installed_package_name_rejected(context):
    client, _repo, _settings, tmp = context
    src = tmp / "src"
    a = build(src, "alpha", "1.0")
    b = build(src, "beta", "1.0")
    upload(client, [a, b])
    pub = current_publication_id(client)
    resp = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(a), sha256_of(b)],
            "installed": [
                {"name": "px", "version": "1.0", "files": ["/a"]},
                {"name": "px", "version": "2.0", "files": ["/b"]},
            ],
        },
    )
    assert resp.status_code == 400


# ------------------------------------------------------------ publication errors
def test_unknown_publication_returns_404(context):
    client, _repo, _settings, tmp = context
    src = tmp / "src"
    a = build(src, "alpha", "1.0")
    b = build(src, "beta", "1.0")
    upload(client, [a, b])
    resp = rehearse(
        client,
        {
            "publication_id": 9999,
            "package_sha256": [sha256_of(a), sha256_of(b)],
            "installed": [],
        },
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "publication_unavailable"


def test_evicted_publication_is_rejected(context):
    client, repository, _settings, tmp = context
    src = tmp / "src"
    first_a = build(src / "v1", "alpha", "1.0", files={"usr/bin/a": b"1\n"})
    first_b = build(src / "v1", "beta", "1.0")
    upload(client, [first_a, first_b])
    evicted = current_publication_id(client)

    # Two more publications push the first one out of the retention window.
    upload(
        client,
        [
            build(src / "v2", "alpha", "2.0"),
            build(src / "v2", "beta", "2.0"),
        ],
    )
    upload(
        client,
        [
            build(src / "v3", "alpha", "3.0"),
            build(src / "v3", "beta", "3.0"),
        ],
    )

    assert evicted not in repository.retained_publication_ids()

    resp = rehearse(
        client,
        {
            "publication_id": evicted,
            "package_sha256": [sha256_of(first_a), sha256_of(first_b)],
            "installed": [],
        },
    )
    assert resp.status_code == 404
    assert "no longer downloadable" in resp.text


def test_digest_not_in_publication_returns_404(context):
    client, _repo, _settings, tmp = context
    src = tmp / "src"
    a = build(src, "alpha", "1.0")
    b = build(src, "beta", "1.0")
    upload(client, [a, b])
    pub = current_publication_id(client)
    foreign = build(src / "foreign", "ghost", "1.0")

    resp = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(a), sha256_of(foreign)],
            "installed": [],
        },
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "package_not_in_publication"


def test_unreadable_pool_file_returns_422(context, monkeypatch):
    client, repository, _settings, tmp = context
    src = tmp / "src"
    a = build(src, "alpha", "1.0")
    b = build(src, "beta", "1.0")
    upload(client, [a, b])
    pub = current_publication_id(client)

    def broken_inspect(fh, size, expected_arch):
        raise rehearsal_mod.DebError("simulated corrupt package")

    monkeypatch.setattr(rehearsal_mod, "inspect_deb_stream", broken_inspect)
    resp = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(a), sha256_of(b)],
            "installed": [],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "package_read_failed"


# ------------------------------------------------------------- eviction immunity
def test_accepted_rehearsal_completes_after_publication_evicted(context, monkeypatch):
    client, repository, settings, tmp = context
    src = tmp / "src"
    a = build(src, "alpha", "1.0", files={"usr/bin/a": b"a\n"})
    b = build(src, "beta", "1.0", files={"usr/bin/b": b"b\n"})
    upload(client, [a, b])
    pub = current_publication_id(client)

    # While the rehearsal is reading its first package, two more publications
    # evict the selected release and garbage-collect its pool files.
    original_list = rehearsal_mod.list_regular_files
    triggered = {"done": False}

    def evicting_list(fh, entries, data_member):
        if not triggered["done"]:
            triggered["done"] = True
            upload(
                client,
                [
                    build(src / "v2", "alpha", "2.0"),
                    build(src / "v2", "beta", "2.0"),
                ],
            )
            upload(
                client,
                [
                    build(src / "v3", "alpha", "3.0"),
                    build(src / "v3", "beta", "3.0"),
                ],
            )
            assert pub not in repository.retained_publication_ids()
        return original_list(fh, entries, data_member)

    monkeypatch.setattr(rehearsal_mod, "list_regular_files", evicting_list)
    resp = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(a), sha256_of(b)],
            "installed": [],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "no_conflict"


def test_rehearsal_does_not_change_publication_state(context):
    client, repository, settings, tmp = context
    src = tmp / "src"
    a = build(src, "alpha", "1.0", files={"usr/bin/a": b"a\n"})
    b = build(src, "beta", "1.0", files={"usr/bin/b": b"b\n"})
    upload(client, [a, b])
    before = client.get("/api/status").json()

    for order in ([a, b], [b, a]):
        resp = rehearse(
            client,
            {
                "publication_id": before["current"]["id"],
                "package_sha256": [sha256_of(p) for p in order],
                "installed": [],
            },
        )
        assert resp.status_code == 200

    after = client.get("/api/status").json()
    assert after == before
    # No copied deb files appear outside the read-only export tree.
    assert not (settings.data_dir / "rehearsals").exists()


def test_epoch_replaces_clause_uses_debian_ordering(context):
    client, _repo, _settings, tmp = context
    src = tmp / "src"
    successor = build(
        src,
        "successor",
        "1.0",
        files={"srv/data": b"n\n"},
        replaces="legacy (>= 1:0.9)",
    )
    filler = build(src, "filler", "1.0", files={"usr/bin/filler": b"f\n"})
    upload(client, [successor, filler])
    pub = current_publication_id(client)

    allowed = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(successor), sha256_of(filler)],
            "installed": [
                {"name": "legacy", "version": "2.0", "files": ["/srv/data"]}
            ],
        },
    )
    # 2.0 without epoch sorts below 1:0.9, so the clause does not match.
    assert allowed.json()["result"] == "conflict"
    assert allowed.json()["conflict"]["owner_version"] == "2.0"

    ok = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(successor), sha256_of(filler)],
            "installed": [
                {"name": "legacy", "version": "1:1.0", "files": ["/srv/data"]}
            ],
        },
    )
    assert ok.json()["result"] == "no_conflict"


def test_arch_qualified_replaces_only_applies_locally(context):
    client, _repo, settings, tmp = context
    src = tmp / "src"
    pkg = build(
        src,
        "multiarch",
        "1.0",
        files={"usr/lib/multi": b"m\n"},
        replaces="victim:arm64",
    )
    filler = build(src, "filler", "1.0", files={"usr/bin/filler": b"f\n"})
    upload(client, [pkg, filler])
    pub = current_publication_id(client)
    assert settings.architecture == "amd64"

    resp = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(pkg), sha256_of(filler)],
            "installed": [
                {"name": "victim", "version": "1.0", "files": ["/usr/lib/multi"]}
            ],
        },
    )
    # The :arm64 clause never applies on an amd64 repository.
    assert resp.json()["result"] == "conflict"
    assert resp.json()["conflict"]["owner_package"] == "victim"


def test_malformed_replaces_field_is_a_read_failure(context):
    client, _repo, _settings, tmp = context
    src = tmp / "src"
    bad = build(
        src,
        "badfield",
        "1.0",
        files={"usr/bin/bad": b"b\n"},
        replaces="not a valid relation (~~ 1.0)",
    )
    filler = build(src, "filler", "1.0", files={"usr/bin/filler": b"f\n"})
    upload(client, [bad, filler])
    pub = current_publication_id(client)

    resp = rehearse(
        client,
        {
            "publication_id": pub,
            "package_sha256": [sha256_of(bad), sha256_of(filler)],
            "installed": [],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "package_read_failed"


def test_path_traversal_member_is_rejected_at_read(context, monkeypatch):
    from app.deb import archive_member_to_abs_path

    with pytest.raises(Exception):
        archive_member_to_abs_path("./usr/../etc/passwd")
    assert archive_member_to_abs_path("./usr/bin/tool") == "/usr/bin/tool"
    assert archive_member_to_abs_path("usr/bin/tool") == "/usr/bin/tool"
