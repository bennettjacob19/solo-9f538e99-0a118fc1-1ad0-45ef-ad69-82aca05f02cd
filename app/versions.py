"""Debian version ordering and dependency relation parsing.

The rehearsal must decide whether a package's ``Replaces`` clause matches the
*current* owner of a contested path, so the version algorithms follow
Debian Policy 5.6.3 (``dpkg --compare-versions``) rather than lexical order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Canonical dpkg relation operators, plus the tolerated single-char forms.
_OPERATORS = ("<<", "<=", "=", ">=", ">>", "<", ">")

_RELATION_RE = re.compile(
    r"^(?P<name>[a-z0-9][a-z0-9+.-]+)"
    r"(?::(?P<arch>[a-z0-9][a-z0-9-]*))?"
    r"(?:\s*\((?P<op><<=|<<|<=|=|>=|>>|<|>)\s*(?P<ver>[^()\s]+)\s*\))?"
    r"(?:\s*<[^>]*>)?\s*$"
)

_EPOCH_RE = re.compile(r"^[0-9]+$")


class VersionError(ValueError):
    """A string is not a valid Debian version or relation clause."""


@dataclass(frozen=True)
class ReplaceClause:
    """One comma-separated entry of a ``Replaces`` control field."""

    package: str
    operator: str | None
    version: str | None
    architecture: str | None

    def matches(self, owner: str, owner_version: str, local_arch: str) -> bool:
        """Return whether this clause covers *owner* at *owner_version*.

        A clause without an architecture qualifier applies on every
        architecture; a qualified one only applies to the local one.
        """

        if self.package != owner:
            return False
        if self.architecture is not None and self.architecture != local_arch:
            return False
        if self.operator is None or self.version is None:
            return True
        return _relation_holds(owner_version, self.operator, self.version)


def parse_replaces(text: str | None) -> tuple[ReplaceClause, ...]:
    """Parse a deb822 ``Replaces`` value into structured clauses."""

    if not text:
        return ()
    clauses: list[ReplaceClause] = []
    for raw in text.split(","):
        clause = raw.strip()
        if not clause:
            continue
        match = _RELATION_RE.match(clause)
        if match is None:
            raise VersionError(f"malformed Replaces clause: {clause!r}")
        clauses.append(
            ReplaceClause(
                package=match.group("name"),
                operator=match.group("op"),
                version=match.group("ver"),
                architecture=match.group("arch"),
            )
        )
    return tuple(clauses)


def parse_version(version: str) -> tuple[int, str, str]:
    """Split *version* into ``(epoch, upstream_version, debian_revision)``."""

    if ":" in version:
        epoch_text, rest = version.split(":", 1)
        if not _EPOCH_RE.match(epoch_text):
            raise VersionError(f"invalid epoch in version: {version!r}")
        epoch = int(epoch_text)
    else:
        rest = version
        epoch = 0
    if "-" in rest:
        upstream, revision = rest.rsplit("-", 1)
    else:
        upstream, revision = rest, "0"
    if not upstream:
        raise VersionError(f"empty upstream version: {version!r}")
    return epoch, upstream, revision


def _token_order(char: str) -> int:
    """Ordering of a single non-digit character per dpkg's verrevcomp."""

    if char == "":
        # The end of the string sorts after '~' but before real characters.
        return 0
    if char == "~":
        return -1
    if char.isalpha():
        return ord(char)
    # Every other non-letter (".", "-", "+", ":", ...) sorts after letters.
    return ord(char) + 256


def _compare_parts(left: str, right: str) -> int:
    """Compare an upstream version or a Debian revision, dpkg style."""

    i = j = 0
    while i < len(left) or j < len(right):
        # Leading non-digit runs, one character token at a time.
        ni = i
        while ni < len(left) and not left[ni].isdigit():
            ni += 1
        nj = j
        while nj < len(right) and not right[nj].isdigit():
            nj += 1
        run_left, run_right = left[i:ni], right[j:nj]
        for k in range(max(len(run_left), len(run_right))):
            char_left = run_left[k] if k < len(run_left) else ""
            char_right = run_right[k] if k < len(run_right) else ""
            order_left, order_right = _token_order(char_left), _token_order(char_right)
            if order_left != order_right:
                return -1 if order_left < order_right else 1
        i, j = ni, nj

        # Following digit runs compare numerically (empty run means zero).
        ni = i
        while ni < len(left) and left[ni].isdigit():
            ni += 1
        nj = j
        while nj < len(right) and right[nj].isdigit():
            nj += 1
        num_left = int(left[i:ni] or "0")
        num_right = int(right[j:nj] or "0")
        if num_left != num_right:
            return -1 if num_left < num_right else 1
        i, j = ni, nj
    return 0


def compare_versions(left: str, right: str) -> int:
    """Return -1/0/1 for the Debian ordering of *left* and *right*."""

    epoch_left, upstream_left, revision_left = parse_version(left)
    epoch_right, upstream_right, revision_right = parse_version(right)
    if epoch_left != epoch_right:
        return -1 if epoch_left < epoch_right else 1
    result = _compare_parts(upstream_left, upstream_right)
    if result != 0:
        return result
    return _compare_parts(revision_left, revision_right)


def _relation_holds(installed_version: str, operator: str, required: str) -> bool:
    comparison = compare_versions(installed_version, required)
    if operator in ("<<", "<"):
        return comparison < 0
    if operator == "<=":
        return comparison <= 0
    if operator == "=":
        return comparison == 0
    if operator == ">=":
        return comparison >= 0
    if operator in (">>", ">"):
        return comparison > 0
    raise VersionError(f"unsupported relation operator: {operator!r}")
