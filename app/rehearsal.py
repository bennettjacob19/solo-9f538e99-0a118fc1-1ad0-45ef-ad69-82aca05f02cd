"""Unpack-order rehearsal: simulate ``dpkg`` file-ownership transitions.

A rehearsal never mutates the repository: it does not copy ``.deb`` files,
does not create or change publications, and reads every package through an
already-open descriptor so that a concurrent eviction cannot interrupt an
accepted run.

Rules implemented (from the task statement):

* Regular files listed by the target machine are seeded as owned by the
  installed package whose list contains them; every owned path is an
  absolute path.  Package archive paths are converted the same way.
* A package may always overwrite files it already owns.
* Overwriting another package's file is only allowed when the acting
  package's ``Replaces`` field matches the *current* owner package name and
  its current Debian version.
* After every accepted step the simulated package versions and file
  ownership move forward, and paths the acting package still owns but no
  longer ships are released.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field

from .deb import DebError, inspect_deb_stream, list_regular_files
from .storage import FileLease, Repository
from .versions import VersionError, parse_replaces


class RehearsalError(Exception):
    """Base class for rehearsal request failures (never a conflict)."""

    status_code = 400
    code = "invalid_rehearsal"


class RehearsalManifestError(RehearsalError):
    """The submitted manifest is malformed."""

    status_code = 400
    code = "invalid_manifest"


class RehearsalPublicationError(RehearsalError):
    """The publication is unknown or no longer downloadable."""

    status_code = 404
    code = "publication_unavailable"


class RehearsalPackageError(RehearsalError):
    """A requested package is not part of the selected publication."""

    status_code = 404
    code = "package_not_in_publication"


class RehearsalReadError(RehearsalError):
    """A pool package could not be read or parsed."""

    status_code = 422
    code = "package_read_failed"


@dataclass(frozen=True)
class InstalledPackage:
    name: str
    version: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class PackagePayload:
    """Everything learned from one pool ``.deb`` through its open lease."""

    name: str
    version: str
    replaces: tuple
    files: tuple[str, ...]
    lease: FileLease


@dataclass(frozen=True)
class Conflict:
    step: int
    package: str
    version: str
    sha256: str
    path: str
    owner_package: str
    owner_version: str


@dataclass
class RehearsalResult:
    publication_id: int
    steps: list[dict[str, object]] = field(default_factory=list)
    conflict: Conflict | None = None

    @property
    def accepted(self) -> bool:
        return self.conflict is None

    def to_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "publication_id": self.publication_id,
            "result": "no_conflict" if self.accepted else "conflict",
            "steps": self.steps,
        }
        if self.conflict is not None:
            body["conflict"] = {
                "step": self.conflict.step,
                "package": self.conflict.package,
                "version": self.conflict.version,
                "sha256": self.conflict.sha256,
                "path": self.conflict.path,
                "owner_package": self.conflict.owner_package,
                "owner_version": self.conflict.owner_version,
            }
        return body


_ABS_PATH_RE = re.compile(r"^/[^\x00]*$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
_VERSION_RE = re.compile(r"^[0-9][A-Za-z0-9.+~:-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _validate_installed(installed: list[InstalledPackage]) -> None:
    names: set[str] = set()
    owners: dict[str, str] = {}
    for item in installed:
        if not _NAME_RE.match(item.name):
            raise RehearsalManifestError(
                f"installed package has an invalid name: {item.name!r}"
            )
        if not _VERSION_RE.match(item.version):
            raise RehearsalManifestError(
                f"installed package {item.name!r} has an invalid version: "
                f"{item.version!r}"
            )
        if item.name in names:
            raise RehearsalManifestError(
                f"installed package listed more than once: {item.name}"
            )
        names.add(item.name)
        for path in item.files:
            if not _ABS_PATH_RE.match(path):
                raise RehearsalManifestError(
                    f"installed package {item.name!r} owns a path that is not "
                    f"an absolute path: {path!r}"
                )
            if path == "/":
                raise RehearsalManifestError(
                    f"installed package {item.name!r} lists the root directory"
                )
            previous = owners.get(path)
            if previous is not None and previous != item.name:
                raise RehearsalManifestError(
                    f"path {path!r} is owned by both {previous!r} and "
                    f"{item.name!r} in the submitted machine state"
                )
            owners[path] = item.name


def validate_manifest(
    publication_id: int,
    package_sha256: list[str],
    installed: list[InstalledPackage],
) -> None:
    """Validate the shape of a rehearsal request before touching the tree."""

    if not isinstance(publication_id, int) or publication_id <= 0:
        raise RehearsalManifestError("publication_id must be a positive integer")
    if not 2 <= len(package_sha256) <= 8:
        raise RehearsalManifestError(
            "between 2 and 8 package SHA-256 digests are required, in unpack order"
        )
    seen: set[str] = set()
    for digest in package_sha256:
        if not _SHA256_RE.match(digest):
            raise RehearsalManifestError(f"not a SHA-256 digest: {digest!r}")
        if digest in seen:
            raise RehearsalManifestError(
                "each package may appear only once in the unpack order; "
                f"duplicate digest {digest}"
            )
        seen.add(digest)
    _validate_installed(installed)


class RehearsalEngine:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.settings = repository.settings

    # ----------------------------------------------------------- intake phase
    def _resolve_publication(self, publication_id: int):
        retained = self.repository.retained_publication_ids()
        if publication_id not in retained:
            publication = self.repository.publication_by_id(publication_id)
            if publication is None:
                raise RehearsalPublicationError(
                    f"publication {publication_id} does not exist"
                )
            raise RehearsalPublicationError(
                f"publication {publication_id} is no longer downloadable; only "
                "the retained publications are accepted for a rehearsal"
            )
        publication = self.repository.publication_by_id(publication_id)
        assert publication is not None
        return publication

    def _load_payloads(
        self, publication_id: int, digests: list[str]
    ) -> list[PackagePayload]:
        """Open and parse every selected package before the simulation starts.

        Every descriptor is acquired before any payload is parsed: once
        accepted, a rehearsal can finish even if a concurrent publication
        evicts the selected release and unlinks its pool files (the open
        inodes stay readable until the descriptors close).
        """

        leases: list[tuple[str, FileLease]] = []
        try:
            # Phase 1: resolve every digest and hold every descriptor.
            for digest in digests:
                entry = self.repository.find_pool_entry(publication_id, digest)
                if entry is None:
                    raise RehearsalPackageError(
                        f"package with SHA-256 {digest} is not part of "
                        f"publication {publication_id}"
                    )
                lease = self.repository.open_pool_file(str(entry["filename"]))
                if lease is None:
                    raise RehearsalReadError(
                        f"pool file for SHA-256 {digest} cannot be opened"
                    )
                leases.append((digest, lease))

            # Phase 2: parse the packages through the held descriptors.
            payloads: list[PackagePayload] = []
            for digest, lease in leases:
                try:
                    info, ar_entries, data_member = inspect_deb_stream(
                        lease.fh, lease.size, self.settings.architecture
                    )
                    files = tuple(
                        list_regular_files(lease.fh, ar_entries, data_member)
                    )
                    replaces_text = info.field_map().get("Replaces")
                    try:
                        replaces = parse_replaces(replaces_text)
                    except VersionError as exc:
                        raise RehearsalReadError(
                            f"package {info.package} {info.version}: {exc}"
                        ) from exc
                except RehearsalError:
                    raise
                except DebError as exc:
                    raise RehearsalReadError(
                        f"package with SHA-256 {digest} cannot be read: {exc}"
                    ) from exc
                payloads.append(
                    PackagePayload(
                        name=info.package,
                        version=info.version,
                        replaces=replaces,
                        files=files,
                        lease=lease,
                    )
                )
            return payloads
        except BaseException:
            for _digest, lease in leases:
                with contextlib.suppress(Exception):
                    lease.close()
            raise

    # --------------------------------------------------------- simulation run
    def run(
        self,
        publication_id: int,
        digests: list[str],
        installed: list[InstalledPackage],
    ) -> RehearsalResult:
        validate_manifest(publication_id, digests, installed)
        self._resolve_publication(publication_id)
        payloads = self._load_payloads(publication_id, digests)
        try:
            return self._simulate(publication_id, digests, payloads, installed)
        finally:
            for payload in payloads:
                with contextlib.suppress(Exception):
                    payload.lease.close()

    def _simulate(
        self,
        publication_id: int,
        digests: list[str],
        payloads: list[PackagePayload],
        installed: list[InstalledPackage],
    ) -> RehearsalResult:
        # Seed the simulated machine state from the submitted packages.
        versions: dict[str, str] = {item.name: item.version for item in installed}
        owners: dict[str, str] = {}
        for item in installed:
            for path in item.files:
                owners[path] = item.name

        result = RehearsalResult(publication_id=publication_id)
        local_arch = self.settings.architecture

        for step, (digest, payload) in enumerate(zip(digests, payloads), start=1):
            new_files = set(payload.files)
            contested: list[tuple[str, str, str]] = []
            for path in sorted(new_files):
                owner = owners.get(path)
                if owner is None or owner == payload.name:
                    continue
                contested.append((path, owner, versions[owner]))

            for path, owner, owner_version in contested:
                allowed = any(
                    clause.matches(owner, owner_version, local_arch)
                    for clause in payload.replaces
                )
                if not allowed:
                    result.conflict = Conflict(
                        step=step,
                        package=payload.name,
                        version=payload.version,
                        sha256=digest,
                        path=path,
                        owner_package=owner,
                        owner_version=owner_version,
                    )
                    result.steps.append(
                        {
                            "step": step,
                            "package": payload.name,
                            "version": payload.version,
                            "sha256": digest,
                            "status": "blocked",
                        }
                    )
                    return result

            # All checks for this step passed: release paths the package still
            # owns but the new version no longer ships, then take ownership of
            # every path the new package provides.
            for path, owner in tuple(owners.items()):
                if owner == payload.name and path not in new_files:
                    del owners[path]
            for path in new_files:
                owners[path] = payload.name
            versions[payload.name] = payload.version

            result.steps.append(
                {
                    "step": step,
                    "package": payload.name,
                    "version": payload.version,
                    "sha256": digest,
                    "status": "applied",
                    "files": len(new_files),
                }
            )
        return result
