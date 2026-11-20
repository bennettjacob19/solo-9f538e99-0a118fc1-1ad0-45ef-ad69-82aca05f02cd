"""Dry-run analysis of an unpack order against a simulated target machine.

The rehearsal answers a single question without installing anything: given the
packages of one published release, in the order they would be unpacked, and
the packages already installed on the target machine, which unpack step would
first overwrite a file owned by a *different* package without the right
``Replaces`` permission?

Rules implemented (they mirror the dpkg policy the task describes):

* A package may always overwrite files it already owns itself, regardless of
  the version being replaced.
* Taking over a file owned by another package is only allowed when the
  incoming package's ``Replaces`` names that owner *and* its optional version
  restriction matches the owner's version **at that moment** in the
  simulation.  Because earlier steps update the simulated versions, a later
  step must satisfy ``Replaces`` against the version that is current then,
  not the version the machine started with.
* Every accepted step updates the simulated version of the unpacked package
  and its file ownership.  Paths the package owned before the step but that
  the new version no longer ships are released to "no owner".

Nothing here touches the repository: callers pass an already-open snapshot of
the ``.deb`` files and this module only reads from them.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass
from typing import BinaryIO, Iterable, Sequence

from .deb import PACKAGE_RE as _PACKAGE_RE
from .deb import DebError, DebInfo, read_deb_contents
from .versioning import (
    Relation,
    RelationError,
    VersionError,
    parse_relations,
    parse_version,
    relation_allows,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: The rehearsal accepts between two and eight packages, as specified.
MIN_PACKAGES = 2
MAX_PACKAGES = 8

#: HTTP status codes used for the separate error categories.
_STATUS_INVALID_MANIFEST = 400
_STATUS_RELEASE_NOT_AVAILABLE = 404
_STATUS_PACKAGE_NOT_IN_RELEASE = 409
_STATUS_PACKAGE_UNREADABLE = 422

class DrillError(Exception):
    """A rehearsal request that must be rejected.

    ``code`` is a stable machine-readable category:

    ``invalid_manifest``
        The request itself is malformed (wrong package count, duplicate
        package names/SHA-256 values, malformed version or non-absolute or
        duplicated file path, ...).
    ``release_not_available``
        The requested release number is unknown, not committed, or no longer
        downloadable after retention garbage collection.
    ``package_not_in_release``
        A requested package SHA-256 is not part of the selected release.
    ``package_unreadable``
        A package was listed by the release but its ``.deb`` could not be read
        or no longer matches the release index.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        release: int | None = None,
        details: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.release = release
        self.details = tuple(details)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = list(self.details)
        return payload


# --------------------------------------------------------------------- models
@dataclass(frozen=True)
class InstalledPackage:
    """One package already present on the simulated target machine."""

    name: str
    version: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class SelectedPackage:
    """A ``.deb`` of the selected release after it has been read."""

    sha256: str
    name: str
    version: str
    files: tuple[str, ...]
    replaces: tuple[Relation, ...]


@dataclass(frozen=True)
class Conflict:
    """The first blocked unpack step."""

    step: int
    package: str
    version: str
    path: str
    owner_package: str
    owner_version: str

    def to_dict(self) -> dict[str, object]:
        return {
            "step": self.step,
            "package": self.package,
            "version": self.version,
            "path": self.path,
            "owner_package": self.owner_package,
            "owner_version": self.owner_version,
        }


@dataclass(frozen=True)
class StepResult:
    """A step that passed."""

    step: int
    package: str
    version: str
    provided: int
    overwritten: int
    released: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "step": self.step,
            "package": self.package,
            "version": self.version,
            "provided": self.provided,
            "overwritten": self.overwritten,
            "released": list(self.released),
        }


@dataclass(frozen=True)
class DrillOutcome:
    steps: tuple[StepResult, ...]
    conflict: Conflict | None

    @property
    def ok(self) -> bool:
        return self.conflict is None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "result": "ok" if self.conflict is None else "conflict",
            "conflict": self.conflict.to_dict() if self.conflict else None,
            "steps": [step.to_dict() for step in self.steps],
        }
        if self.conflict is not None:
            # Convenience mirror of the fields callers most often need to
            # report; the nested ``conflict`` carries the same information.
            payload.update(
                {
                    "first_blocked_step": self.conflict.step,
                    "conflict_path": self.conflict.path,
                    "owner_package": self.conflict.owner_package,
                    "owner_version": self.conflict.owner_version,
                }
            )
        return payload


# ---------------------------------------------------------------------- result
SCHEMA_NOTES = """\
Rehearsal semantics
-------------------
* The package list order is the unpack order.
* A package may overwrite its own files, including after its simulated
  version changed in an earlier step.
* Overwriting a file owned by another package requires the incoming package's
  ``Replaces`` to name that owner, with its optional version restriction
  matching the owner's version *at that point in the simulation*.
* Each passing step adopts the new version and re-assigns file ownership;
  paths the package owned before but no longer provides become unowned.
* The first blocked step ends the run.  A request that lists an unknown
  release, a package digest outside that release, or an unreadable package is
  rejected as its own error and does not attempt a simulation.
* Rehearsals never copy a package and never change the repository: packages
  are read through descriptors opened when the request starts, so a release
  that is evicted while the rehearsal runs still completes with the original
  files.
"""


# ---------------------------------------------------------------- validation
def normalize_sha256_values(values: Iterable[str]) -> tuple[str, ...]:
    """Validate the selected package digest list.

    Digests are case-insensitive on input and normalised to lower case; they
    identify packages in the release index, so duplicates are meaningless and
    rejected as a malformed manifest.
    """

    items = list(values)
    if not MIN_PACKAGES <= len(items) <= MAX_PACKAGES:
        raise DrillError(
            "invalid_manifest",
            f"select between {MIN_PACKAGES} and {MAX_PACKAGES} packages, "
            f"got {len(items)}",
        )
    digests: list[str] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, str):
            raise DrillError(
                "invalid_manifest", "each package must be a SHA-256 hex string"
            )
        digest = raw.strip().lower()
        if not _SHA256_RE.match(digest):
            raise DrillError(
                "invalid_manifest",
                f"invalid SHA-256 value: {raw!r}",
                details=["a package digest must be 64 hexadecimal characters"],
            )
        if digest in seen:
            raise DrillError(
                "invalid_manifest",
                f"duplicate package SHA-256 in the batch: {digest}",
            )
        seen.add(digest)
        digests.append(digest)
    return tuple(digests)


def normalize_install_path(path: str) -> str:
    """Validate and return one absolute, normalised install-time file path."""

    if not isinstance(path, str):
        raise DrillError("invalid_manifest", "file paths must be strings")
    if "\x00" in path:
        raise DrillError("invalid_manifest", "file paths must not contain NUL")
    if not path.startswith("/"):
        raise DrillError(
            "invalid_manifest",
            f"installed file path must be absolute: {path!r}",
        )
    if path == "/" or path.endswith("/"):
        raise DrillError(
            "invalid_manifest",
            f"installed file path must name a regular file, not a directory: {path!r}",
        )
    if path.startswith("//"):
        raise DrillError(
            "invalid_manifest", f"installed file path must be normalised: {path!r}"
        )
    if posixpath.normpath(path) != path:
        raise DrillError(
            "invalid_manifest", f"installed file path must be normalised: {path!r}"
        )
    return path


def validate_installed(values: Iterable[object]) -> tuple[InstalledPackage, ...]:
    """Validate the target-machine inventory.

    Every installed package must have a valid Debian name and version and a
    list of absolute regular-file paths.  A path may have exactly one owner on
    a real machine, so a path claimed by two different installed packages is
    a malformed manifest rather than a rehearsal conflict.
    """

    packages: list[InstalledPackage] = []
    names: set[str] = set()
    claims: dict[str, str] = {}
    for item in values:
        if not isinstance(item, InstalledPackage):  # pragma: no cover - typing guard
            raise DrillError("invalid_manifest", "invalid installed package entry")
        name = item.name
        if not isinstance(name, str) or not _PACKAGE_RE.match(name):
            raise DrillError(
                "invalid_manifest", f"invalid installed package name: {name!r}"
            )
        if name in names:
            raise DrillError(
                "invalid_manifest", f"installed package listed twice: {name}"
            )
        names.add(name)
        try:
            parse_version(item.version)
        except VersionError as exc:
            raise DrillError(
                "invalid_manifest",
                f"installed package {name} has an invalid version: {exc}",
            ) from exc
        seen: set[str] = set()
        files: list[str] = []
        for path in item.files:
            normalised = normalize_install_path(path)
            if normalised in seen:
                raise DrillError(
                    "invalid_manifest",
                    f"installed package {name} lists {normalised} twice",
                )
            seen.add(normalised)
            previous = claims.get(normalised)
            if previous is not None and previous != name:
                raise DrillError(
                    "invalid_manifest",
                    f"installed file {normalised} is claimed by both "
                    f"{previous} and {name}",
                )
            claims[normalised] = name
            files.append(normalised)
        packages.append(InstalledPackage(name=name, version=item.version, files=tuple(files)))
    return tuple(packages)


# ------------------------------------------------------------------ reading
def _sha256_fd(fh: BinaryIO) -> str:
    digest = hashlib.sha256()
    fh.seek(0)
    while chunk := fh.read(1024 * 1024):
        digest.update(chunk)
    fh.seek(0)
    return digest.hexdigest()


def _replaces(info: DebInfo) -> tuple[Relation, ...]:
    value = None
    for key, candidate in info.fields:
        if key.casefold() == "replaces":
            value = candidate
            break
    try:
        return parse_relations(value)
    except RelationError as exc:
        raise DrillError(
            "package_unreadable",
            f"{info.package}: invalid Replaces field: {exc}",
            status=_STATUS_PACKAGE_UNREADABLE,
        ) from exc


def read_selected_packages(
    opened: Iterable[object], architecture: str, release: int
) -> tuple[SelectedPackage, ...]:
    """Hash and parse every opened package, enforcing the "one per name" rule.

    ``opened`` items must expose ``sha256``, ``filename`` and ``fh`` (see
    :class:`app.storage.OpenedPackage`).  The descriptor is read directly and
    never copied, so a release evicted after acceptance is still usable: the
    inode stays alive as long as it is open.
    """

    selected: list[SelectedPackage] = []
    by_name: dict[str, str] = {}
    for item in opened:
        sha256 = item.sha256  # type: ignore[attr-defined]
        filename = item.filename  # type: ignore[attr-defined]
        try:
            if _sha256_fd(item.fh) != sha256:  # type: ignore[attr-defined]
                raise DrillError(
                    "package_unreadable",
                    f"{filename}: content no longer matches the release index",
                    status=_STATUS_PACKAGE_UNREADABLE,
                    release=release,
                )
            contents = read_deb_contents(item.fh, architecture)  # type: ignore[attr-defined]
        except DrillError:
            raise
        except DebError as exc:
            raise DrillError(
                "package_unreadable",
                f"{filename}: {exc}",
                status=_STATUS_PACKAGE_UNREADABLE,
                release=release,
            ) from exc
        except (OSError, ValueError) as exc:
            raise DrillError(
                "package_unreadable",
                f"{filename}: cannot read package: {exc}",
                status=_STATUS_PACKAGE_UNREADABLE,
                release=release,
            ) from exc

        info = contents.info
        if info.package in by_name:
            raise DrillError(
                "invalid_manifest",
                f"only one package per name may be selected: {info.package} "
                f"appears as {by_name[info.package]} and {sha256}",
                release=release,
            )
        by_name[info.package] = sha256
        selected.append(
            SelectedPackage(
                sha256=sha256,
                name=info.package,
                version=info.version,
                files=contents.files,
                replaces=_replaces(info),
            )
        )
    return tuple(selected)


# --------------------------------------------------------------- simulation
def simulate(
    selected: Sequence[SelectedPackage], installed: Sequence[InstalledPackage]
) -> DrillOutcome:
    """Run the unpack steps against the simulated machine state."""

    versions: dict[str, str] = {pkg.name: pkg.version for pkg in installed}
    owners: dict[str, str] = {}
    for pkg in installed:
        for path in pkg.files:
            owners[path] = pkg.name

    steps: list[StepResult] = []
    for index, pkg in enumerate(selected, start=1):
        overwritten = 0
        for path in pkg.files:
            owner = owners.get(path)
            if owner is None:
                owners[path] = pkg.name
                continue
            if owner == pkg.name:
                # A package may always overwrite its own files.
                continue
            owner_version = versions.get(owner)
            if owner_version is None:  # pragma: no cover - invariant
                raise AssertionError(f"owner {owner} has no simulated version")
            if not relation_allows(pkg.replaces, owner, owner_version):
                # First blocked step: stop the simulation here.
                return DrillOutcome(
                    steps=tuple(steps),
                    conflict=Conflict(
                        step=index,
                        package=pkg.name,
                        version=pkg.version,
                        path=path,
                        owner_package=owner,
                        owner_version=owner_version,
                    ),
                )
            owners[path] = pkg.name
            overwritten += 1

        # The step passed: adopt the new version and drop files this package
        # used to own but the new version no longer ships.
        versions[pkg.name] = pkg.version
        provided = set(pkg.files)
        released = sorted(
            path for path, owner in owners.items() if owner == pkg.name and path not in provided
        )
        for path in released:
            del owners[path]
        steps.append(
            StepResult(
                step=index,
                package=pkg.name,
                version=pkg.version,
                provided=len(pkg.files),
                overwritten=overwritten,
                released=tuple(released),
            )
        )
    return DrillOutcome(steps=tuple(steps), conflict=None)
