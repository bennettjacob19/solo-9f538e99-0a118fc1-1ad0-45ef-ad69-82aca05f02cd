"""Tests for the unpack-order rehearsal (dry run) endpoint."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from debbuild import build_deb  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.storage import Repository  # noqa: E402
from app.versioning import (  # noqa: E402
    RelationError,
    VersionError,
    compare_versions,
    parse_relations,
)


@pytest.fixture()
def client(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", retention=2)
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client, settings


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def publish(client: TestClient, paths: list[Path]) -> dict:
    files = [
        ("files", (path.name, path.read_bytes(), "application/vnd.debian.binary-package"))
        for path in paths
    ]
    response = client.post("/api/publish", files=files)
    assert response.status_code == 201, response.text
    return response.json()["published"]


def pack(
    directory: Path,
    name: str,
    version: str = "1.0.0",
    *,
    files: list[str] | None = None,
    replaces: str | None = None,
    symlinks: list[str] | None = None,
    payload: bytes | None = None,
) -> Path:
    control = {"Package": name, "Version": version}
    if replaces is not None:
        control["Replaces"] = replaces
    path = directory / f"{name}_{version}_amd64.deb"
    return build_deb(
        path,
        control=control,
        files=files,
        symlinks=symlinks,
        payload=payload if payload is not None else f"{name}-{version}".encode(),
    )


def rehearse(client: TestClient, **body) -> tuple[int, dict]:
    response = client.post("/api/rehearse", json=body)
    return response.status_code, response.json()


# ------------------------------------------------------------------ scenarios
def test_clean_unpack_reports_no_conflict(client, tmp_path):
    test_client, settings = client
    src = tmp_path / "src"
    a = pack(src, "alpha", files=["usr/bin/alpha", "usr/share/alpha/data"])
    b = pack(src, "beta", files=["usr/bin/beta"])
    published = publish(test_client, [a, b])

    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(b), sha256(a)],
        installed=[],
    )
    assert status == 200, body
    assert body["release"] == published["id"]
    assert body["result"] == "ok"
    assert body["conflict"] is None
    assert [step["package"] for step in body["steps"]] == ["beta", "alpha"]
    assert body["packages"][0]["sha256"] == sha256(b)
    assert body["packages"][0]["package"] == "beta"
    assert body["packages"][0]["version"] == "1.0.0"


def test_first_blocked_step_reports_owner_and_stops(client, tmp_path):
    test_client, _settings = client
    src = tmp_path / "src"
    shared = "usr/share/common/config"
    a = pack(src, "alpha", files=[shared])
    b = pack(src, "beta", files=["usr/bin/beta"])
    c = pack(src, "gamma", files=[shared])
    published = publish(test_client, [a, b, c])

    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(a), sha256(b), sha256(c)],
        installed=[],
    )
    assert status == 200, body
    assert body["result"] == "conflict"
    assert body["first_blocked_step"] == 3
    assert body["conflict_path"] == "/" + shared
    assert body["owner_package"] == "alpha"
    assert body["owner_version"] == "1.0.0"
    conflict = body["conflict"]
    assert conflict == {
        "step": 3,
        "package": "gamma",
        "version": "1.0.0",
        "path": "/" + shared,
        "owner_package": "alpha",
        "owner_version": "1.0.0",
    }
    # Only the steps before the blocked one are reported as passed.
    assert [step["step"] for step in body["steps"]] == [1, 2]


def test_replaces_matching_installed_owner_allows_overwrite(client, tmp_path):
    test_client, _settings = client
    src = tmp_path / "src"
    shared = "usr/lib/libshared.so"
    new = pack(src, "newlib", version="2.0.0", files=[shared], replaces="oldlib (<< 2.0)")
    other = pack(src, "other", files=["usr/bin/other"])
    published = publish(test_client, [new, other])

    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(new), sha256(other)],
        installed=[
            {"name": "oldlib", "version": "1.5", "files": ["/" + shared]},
        ],
    )
    assert status == 200, body
    assert body["result"] == "ok", body
    assert body["steps"][0]["overwritten"] == 1


def test_replaces_version_mismatch_is_a_conflict(client, tmp_path):
    test_client, _settings = client
    src = tmp_path / "src"
    shared = "usr/lib/libshared.so"
    new = pack(src, "newlib", version="2.0.0", files=[shared], replaces="oldlib (<< 2.0)")
    other = pack(src, "other", files=["usr/bin/other"])
    published = publish(test_client, [new, other])

    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(new), sha256(other)],
        installed=[
            {"name": "oldlib", "version": "2.1", "files": ["/" + shared]},
        ],
    )
    assert status == 200, body
    assert body["result"] == "conflict"
    assert body["conflict"]["package"] == "newlib"
    assert body["conflict"]["owner_package"] == "oldlib"
    assert body["conflict"]["owner_version"] == "2.1"


def test_replaces_must_not_name_a_third_package(client, tmp_path):
    test_client, _settings = client
    src = tmp_path / "src"
    shared = "usr/share/thing/file"
    new = pack(src, "newlib", files=[shared], replaces="somebodyelse")
    other = pack(src, "other", files=["usr/bin/other"])
    published = publish(test_client, [new, other])

    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(new), sha256(other)],
        installed=[{"name": "owner", "version": "1.0", "files": ["/" + shared]}],
    )
    assert status == 200, body
    assert body["result"] == "conflict"
    assert body["conflict"]["owner_package"] == "owner"


def test_same_package_overwrites_its_own_files_regardless_of_version(client, tmp_path):
    test_client, _settings = client
    src = tmp_path / "src"
    shared = "usr/bin/tool"
    upgraded = pack(src, "tool", version="3.0.0", files=[shared])
    other = pack(src, "other", files=["usr/bin/other"])
    published = publish(test_client, [upgraded, other])

    # The installed version is newer than the packaged one and there is no
    # Replaces relationship: own files may still be overwritten.
    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(upgraded), sha256(other)],
        installed=[{"name": "tool", "version": "9.9", "files": ["/" + shared]}],
    )
    assert status == 200, body
    assert body["result"] == "ok", body
    assert body["steps"][0]["overwritten"] == 0


def test_released_files_stop_being_owned_after_upgrade(client, tmp_path):
    test_client, _settings = client
    src = tmp_path / "src"
    removed = "usr/share/tool/legacy"
    fresh = pack(src, "tool", version="2.0.0", files=["usr/bin/tool"])
    # The new package takes over the legacy path only if it is still owned by
    # the old version at that step; here it is not, because step 1 released it.
    successor = pack(src, "successor", files=[removed])
    published = publish(test_client, [fresh, successor])

    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(fresh), sha256(successor)],
        installed=[
            {
                "name": "tool",
                "version": "1.0.0",
                "files": ["/" + removed, "/usr/bin/tool"],
            }
        ],
    )
    assert status == 200, body
    assert body["result"] == "ok", body
    assert body["steps"][0]["released"] == ["/" + removed]
    # Step 2 installs the path again; nobody owns it, so no Replaces needed.
    assert body["steps"][1]["overwritten"] == 0


def test_release_no_longer_providing_a_file_releases_it(client, tmp_path):
    test_client, _settings = client
    src = tmp_path / "src"
    dropped = "usr/share/tool/dropped"
    upgraded = pack(src, "tool", version="2.0", files=["usr/bin/tool"])
    other = pack(src, "other", files=["usr/bin/other"])
    # The machine owns the dropped path, but through the *same* package.
    published = publish(test_client, [upgraded, other])

    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(upgraded), sha256(other)],
        installed=[{"name": "tool", "version": "1.0", "files": ["/" + dropped]}],
    )
    assert status == 200, body
    assert body["steps"][0]["released"] == ["/" + dropped]
    assert body["result"] == "ok"


def test_simulation_uses_version_installed_by_an_earlier_step(client, tmp_path):
    """Replaces is evaluated against the version current at that step."""

    test_client, _settings = client
    src = tmp_path / "src"
    shared = "usr/lib/libthing.so"
    # Step 2 installs libthing 5.0 over libthing 1.0 (its own package).
    lib = pack(src, "libthing", version="5.0", files=[shared])
    # Step 3 replaces libthing but only up to 2.0.  On the real machine the
    # owner would be 1.0 (matching), but after step 2 it is 5.0.
    takeover = pack(src, "takeover", version="1.0", files=[shared], replaces="libthing (<< 2.0)")
    filler = pack(src, "filler", files=["usr/bin/filler"])
    published = publish(test_client, [lib, takeover, filler])

    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(lib), sha256(filler), sha256(takeover)],
        installed=[{"name": "libthing", "version": "1.0", "files": ["/" + shared]}],
    )
    assert status == 200, body
    assert body["result"] == "conflict"
    assert body["conflict"]["package"] == "takeover"
    assert body["conflict"]["owner_package"] == "libthing"
    assert body["conflict"]["owner_version"] == "5.0"


def test_unversioned_replaces_matches_any_owner_version(client, tmp_path):
    test_client, _settings = client
    src = tmp_path / "src"
    shared = "usr/share/old/conf"
    takeover = pack(src, "newcomer", files=[shared], replaces="oldpkg")
    other = pack(src, "other", files=["usr/bin/other"])
    published = publish(test_client, [takeover, other])

    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(takeover), sha256(other)],
        installed=[{"name": "oldpkg", "version": "42:9.9~x", "files": ["/" + shared]}],
    )
    assert status == 200, body
    assert body["result"] == "ok", body


def test_symlinks_and_directories_are_not_owned_files(client, tmp_path):
    test_client, _settings = client
    src = tmp_path / "src"
    link = "usr/bin/link"
    owner = pack(src, "owner", files=["usr/bin/real"])
    claimant = pack(src, "claimant", files=["usr/bin/claimant"], symlinks=[link])
    published = publish(test_client, [owner, claimant])

    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(owner), sha256(claimant)],
        installed=[{"name": "linkpkg", "version": "1.0", "files": ["/" + link]}],
    )
    assert status == 200, body
    assert body["result"] == "ok", body
    # The symlink is not a regular file, so the package provides only one.
    assert body["packages"][1]["files"] == 1


# ------------------------------------------------------------ rejected requests
def test_package_count_must_be_between_two_and_eight(client, tmp_path):
    test_client, _settings = client
    src = tmp_path / "src"
    a = pack(src, "alpha")
    published = publish(test_client, [a])

    status, body = rehearse(
        test_client, release=published["id"], packages=[sha256(a)], installed=[]
    )
    assert status == 400
    assert body["reason"]["code"] == "invalid_manifest"

    response = test_client.post(
        "/api/rehearse",
        json={"release": published["id"], "packages": [sha256(a)] * 9, "installed": []},
    )
    assert response.status_code in (400, 422)


def test_duplicate_sha256_is_a_malformed_manifest(client, tmp_path):
    test_client, _settings = client
    src = tmp_path / "src"
    a = pack(src, "alpha")
    b = pack(src, "beta")
    published = publish(test_client, [a, b])

    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(a), sha256(a)],
        installed=[],
    )
    assert status == 400
    assert body["reason"]["code"] == "invalid_manifest"


def test_two_packages_with_the_same_name_are_rejected(client, tmp_path):
    test_client, _settings = client
    src = tmp_path / "src"
    # Different versions of one package name may coexist in an index.
    one = pack(src, "same", version="1.0", files=["usr/bin/same"])
    two = pack(src, "same", version="2.0", files=["usr/bin/same"])
    published = publish(test_client, [one, two])

    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(one), sha256(two)],
        installed=[],
    )
    assert status == 400
    assert body["reason"]["code"] == "invalid_manifest"
    assert "same" in body["reason"]["message"]


def test_bad_sha256_and_bad_installed_path_are_manifest_errors(client, tmp_path):
    test_client, _settings = client
    src = tmp_path / "src"
    a = pack(src, "alpha")
    b = pack(src, "beta")
    published = publish(test_client, [a, b])

    status, body = rehearse(
        test_client, release=published["id"], packages=["nothex", sha256(b)], installed=[]
    )
    assert status == 400
    assert body["reason"]["code"] == "invalid_manifest"

    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(a), sha256(b)],
        installed=[{"name": "x", "version": "1.0", "files": ["relative/path"]}],
    )
    assert status == 400
    assert body["reason"]["code"] == "invalid_manifest"

    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(a), sha256(b)],
        installed=[
            {"name": "x", "version": "1.0", "files": ["/etc/x"]},
            {"name": "y", "version": "1.0", "files": ["/etc/x"]},
        ],
    )
    assert status == 400
    assert body["reason"]["code"] == "invalid_manifest"


def test_unknown_or_uncommitted_release_is_its_own_error(client, tmp_path):
    test_client, _settings = client
    src = tmp_path / "src"
    a = pack(src, "alpha")
    b = pack(src, "beta")
    published = publish(test_client, [a, b])

    status, body = rehearse(
        test_client, release=999, packages=[sha256(a), sha256(b)], installed=[]
    )
    assert status == 404
    assert body["reason"]["code"] == "release_not_available"
    assert body["release"] == 999

    # An aborted publication is never usable for a rehearsal.
    repository: Repository = test_client.app.state.repository
    with repository._connect() as db:
        cursor = db.execute(
            "INSERT INTO publications (created_at, state, release_sha256, "
            "index_sha256, plain_index_sha256, index_size, package_count, "
            "packages_json) VALUES ('now', 'aborted', 'x', 'y', 'z', 0, 0, '[]')"
        )
        aborted = int(cursor.lastrowid)
    status, body = rehearse(
        test_client, release=aborted, packages=[sha256(a), sha256(b)], installed=[]
    )
    assert status == 404
    assert body["reason"]["code"] == "release_not_available"


def test_package_not_in_selected_release_is_its_own_error(client, tmp_path):
    test_client, _settings = client
    src = tmp_path / "src"
    a = pack(src, "alpha")
    b = pack(src, "beta")
    published = publish(test_client, [a, b])
    outside = pack(src / "outside", "outside")

    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(a), sha256(outside)],
        installed=[],
    )
    assert status == 409
    assert body["reason"]["code"] == "package_not_in_release"
    assert sha256(outside) in body["reason"]["details"]


def test_unreadable_package_is_its_own_error(client, tmp_path):
    test_client, settings = client
    src = tmp_path / "src"
    a = pack(src, "alpha")
    b = pack(src, "beta")
    published = publish(test_client, [a, b])

    repository: Repository = test_client.app.state.repository
    for path in (repository.export / "pool").rglob("*.deb"):
        if sha256(path) == sha256(a):
            path.write_bytes(b"garbage")
    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(a), sha256(b)],
        installed=[],
    )
    assert status == 422
    assert body["reason"]["code"] == "package_unreadable"


def test_evicted_release_is_rejected(client, tmp_path):
    test_client, _settings = client
    src = tmp_path / "src"
    first = publish(test_client, [pack(src / "r1", "alpha"), pack(src / "r1", "beta")])
    publish(test_client, [pack(src / "r2", "alpha", "2.0"), pack(src / "r2", "beta", "2.0")])
    publish(test_client, [pack(src / "r3", "alpha", "3.0"), pack(src / "r3", "beta", "3.0")])

    digests = [sha256(p) for p in sorted((src / "r1").glob("*.deb"))]
    status, body = rehearse(
        test_client, release=first["id"], packages=digests, installed=[]
    )
    assert status == 404
    assert body["reason"]["code"] == "release_not_available"


def test_rehearsal_does_not_copy_or_change_the_repository(client, tmp_path):
    test_client, _settings = client
    src = tmp_path / "src"
    a = pack(src, "alpha")
    b = pack(src, "beta")
    published = publish(test_client, [a, b])
    repository: Repository = test_client.app.state.repository

    release_before = (repository.export / "dists/stable/Release").read_bytes()
    pool_before = sorted(p.relative_to(repository.export) for p in (repository.export / "pool").rglob("*"))
    db_before = published["id"]

    status, _body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(a), sha256(b)],
        installed=[],
    )
    assert status == 200
    assert (repository.export / "dists/stable/Release").read_bytes() == release_before
    assert sorted(
        p.relative_to(repository.export) for p in (repository.export / "pool").rglob("*")
    ) == pool_before
    assert repository.current_publication().id == db_before
    assert not any(repository.incoming.iterdir())


def test_rehearsal_started_before_eviction_still_completes(client, tmp_path, monkeypatch):
    """An accepted drill keeps using the original packages after eviction."""

    test_client, settings = client
    src = tmp_path / "src"
    shared = "usr/lib/libold.so"
    a = pack(src, "alpha", files=[shared])
    b = pack(src, "beta", files=["usr/bin/beta"])
    published = publish(test_client, [a, b])

    from app import drill as drill_module

    original = drill_module.read_selected_packages
    captured = {}

    def read_then_evict(opened, architecture, release):
        result = original(opened, architecture, release)
        captured["result"] = result
        # Simulate a concurrent third publication evicting the release: the
        # paths vanish, but the rehearsal's open descriptors must still work.
        repository: Repository = test_client.app.state.repository
        for path in (repository.export / "pool").rglob("*.deb"):
            path.unlink()
        return result

    monkeypatch.setattr(
        "app.storage.read_selected_packages", read_then_evict
    )
    status, body = rehearse(
        test_client,
        release=published["id"],
        packages=[sha256(a), sha256(b)],
        installed=[],
    )
    assert status == 200, body
    assert body["result"] == "ok"
    assert [pkg.name for pkg in captured["result"]] == ["alpha", "beta"]


# ------------------------------------------------------------ version ordering
@pytest.mark.parametrize(
    "left,right,expected",
    [
        ("1.0", "1.0-0", 0),
        ("1.0~rc1", "1.0", -1),
        ("1:1.0", "2.0", 1),
        ("2.0", "2.0-0ubuntu1", -1),
        ("1.0+b1", "1.0", 1),
        ("1.0-1", "1.0-1+b1", -1),
        ("1.0~~", "1.0~", -1),
        ("1.0a", "1.0", 1),
    ],
)
def test_compare_versions(left, right, expected):
    assert compare_versions(left, right) == expected


def test_compare_versions_rejects_invalid_input():
    with pytest.raises(VersionError):
        compare_versions("not a version", "1.0")
    with pytest.raises(VersionError):
        compare_versions("1.0", "1.0-")  # empty revision
    with pytest.raises(VersionError):
        compare_versions(":1.0", "1.0")  # empty epoch


def test_parse_relations_handles_folding_and_versions():
    relations = parse_relations(
        "oldlib (<< 2.0),\n otherpkg (= 1.0~rc1), thirdpkg:any"
    )
    assert [(r.package, r.operator, r.version) for r in relations] == [
        ("oldlib", "<<", "2.0"),
        ("otherpkg", "=", "1.0~rc1"),
        ("thirdpkg", None, None),
    ]
    with pytest.raises(RelationError):
        parse_relations("broken (")
