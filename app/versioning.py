"""Debian version comparison and relationship parsing.

The unpack rehearsal has to reproduce the two pieces of dpkg policy that
decide whether one package may take over a file owned by another package:

* the exact Debian version ordering (epoch, upstream version, revision);
* ``Replaces`` relationships, whose optional version restriction is compared
  against the version of the *current* owner of the file.

Both rules are implemented here without external dependencies so the service
behaves the same inside the slim Docker image (which has no ``dpkg`` binary).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A Debian version as accepted in control files.  The checks are deliberately
#: close to dpkg's own parser: a leading digit, then alphanumerics and the
#: punctuation allowed by policy.
_VERSION_RE = re.compile(r"^[0-9][A-Za-z0-9.+~:-]*$")
_UPSTREAM_RE = re.compile(r"^[A-Za-z0-9.+~:-]+$")
_REVISION_RE = re.compile(r"^[A-Za-z0-9.+~]+$")

#: ``package[:arch] [(op version)]``.  Architecture qualifiers are accepted
#: and ignored; the rehearsal works on package names only.
_RELATION_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9+.-]*)"
    r"(?::[A-Za-z0-9-]+)?"
    r"(?:\s*\(\s*(?P<op><<|<=|>=|>>|=|<|>)\s*(?P<version>[^()\s]+)\s*\))?$"
)

_REPLACES_OPS = {
    "<<": lambda c: c < 0,
    "<=": lambda c: c <= 0,
    "=": lambda c: c == 0,
    ">=": lambda c: c >= 0,
    ">>": lambda c: c > 0,
    # The single-character forms are historical aliases for <= and >=.
    "<": lambda c: c <= 0,
    ">": lambda c: c >= 0,
}


class VersionError(ValueError):
    """The text is not a syntactically valid Debian version."""


class RelationError(ValueError):
    """A ``Replaces`` field could not be parsed."""


@dataclass(frozen=True)
class DebianVersion:
    epoch: int
    upstream: str
    revision: str


def parse_version(text: str) -> DebianVersion:
    """Split and validate a Debian version string."""

    if not isinstance(text, str) or not text:
        raise VersionError("version is empty")
    if not _VERSION_RE.match(text):
        raise VersionError(f"invalid Debian version: {text!r}")
    epoch = 0
    rest = text
    if ":" in rest:
        epoch_text, rest = rest.split(":", 1)
        if not epoch_text.isdigit():
            raise VersionError(f"invalid Debian version epoch: {text!r}")
        epoch = int(epoch_text)
    if not rest:
        raise VersionError(f"invalid Debian version: {text!r}")
    if "-" in rest:
        upstream, revision = rest.rsplit("-", 1)
        if not revision or not _REVISION_RE.match(revision):
            raise VersionError(f"invalid Debian revision: {text!r}")
    else:
        upstream, revision = rest, ""
    if not upstream or not _UPSTREAM_RE.match(upstream):
        raise VersionError(f"invalid Debian upstream version: {text!r}")
    return DebianVersion(epoch=epoch, upstream=upstream, revision=revision)


def _order(character: str) -> int:
    """dpkg's ``order`` function for a single character.

    ``~`` sorts before everything (including the end of the string), digits
    are handled separately by :func:`_verrevcmp`, letters sort by ASCII value
    and all other characters after letters.
    """

    if not character:
        return 0
    if character.isdigit():
        return 0
    if character.isalpha():
        return ord(character)
    if character == "~":
        return -1
    return ord(character) + 256


def _verrevcmp(left: str, right: str) -> int:
    """The dpkg ``verrevcmp`` algorithm for upstream/revision strings."""

    i = j = 0
    left_len, right_len = len(left), len(right)
    while i < left_len or j < right_len:
        # Compare the non-digit runs first.
        while (
            (i < left_len and not left[i].isdigit())
            or (j < right_len and not right[j].isdigit())
        ):
            left_order = _order(left[i]) if i < left_len else 0
            right_order = _order(right[j]) if j < right_len else 0
            if left_order != right_order:
                return left_order - right_order
            i += 1
            j += 1

        # Leading zeroes are not significant.
        while i < left_len and left[i] == "0":
            i += 1
        while j < right_len and right[j] == "0":
            j += 1

        first_difference = 0
        while (
            i < left_len
            and j < right_len
            and left[i].isdigit()
            and right[j].isdigit()
        ):
            if first_difference == 0:
                first_difference = ord(left[i]) - ord(right[j])
            i += 1
            j += 1
        if i < left_len and left[i].isdigit():
            return 1
        if j < right_len and right[j].isdigit():
            return -1
        if first_difference:
            return first_difference
    return 0


def compare_versions(left: str, right: str) -> int:
    """Return ``-1``, ``0`` or ``1`` using Debian version ordering."""

    left_version = parse_version(left)
    right_version = parse_version(right)
    if left_version.epoch != right_version.epoch:
        return -1 if left_version.epoch < right_version.epoch else 1
    result = _verrevcmp(left_version.upstream, right_version.upstream)
    if result:
        return -1 if result < 0 else 1
    result = _verrevcmp(left_version.revision, right_version.revision)
    if result:
        return -1 if result < 0 else 1
    return 0


@dataclass(frozen=True)
class Relation:
    """One entry of a relationship field such as ``Replaces``."""

    package: str
    operator: str | None = None
    version: str | None = None

    def matches_version(self, version: str) -> bool:
        if self.operator is None or self.version is None:
            return True
        comparison = compare_versions(version, self.version)
        return _REPLACES_OPS[self.operator](comparison)


def parse_relations(value: str | None) -> tuple[Relation, ...]:
    """Parse a ``Replaces`` field.

    Continuation lines are folded to spaces; entries may be separated by
    commas.  ``|`` alternatives are accepted as separate allowed relations
    even though Debian policy does not normally use them in ``Replaces``.
    """

    if not value:
        return ()
    relations: list[Relation] = []
    text = " ".join(line.strip() for line in value.splitlines())
    for chunk in text.split(","):
        for alternative in chunk.split("|"):
            item = alternative.strip()
            if not item:
                continue
            match = _RELATION_RE.match(item)
            if match is None:
                raise RelationError(f"invalid relationship entry: {item!r}")
            operator = match.group("op")
            version = match.group("version")
            if operator is not None and version is not None:
                # Validate eagerly so a malformed package is reported as a
                # read failure rather than failing halfway through a step.
                parse_version(version)
            relations.append(
                Relation(
                    package=match.group("name"),
                    operator=operator,
                    version=version,
                )
            )
    return tuple(relations)


def relation_allows(
    relations: tuple[Relation, ...], owner_package: str, owner_version: str
) -> bool:
    """Whether *relations* permit overwriting a file of the named owner."""

    for relation in relations:
        if relation.package != owner_package:
            continue
        try:
            if relation.matches_version(owner_version):
                return True
        except VersionError:
            # The owner's version came from the submitted manifest and has
            # already been validated; be conservative if it somehow is not.
            continue
    return False
