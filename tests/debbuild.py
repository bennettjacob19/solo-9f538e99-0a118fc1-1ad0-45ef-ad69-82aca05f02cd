"""Build real Debian packages for tests without extra dependencies."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

DEFAULT_CONTROL = {
    "Package": "demo",
    "Version": "1.0.0",
    "Architecture": "amd64",
    "Maintainer": "Test <test@example.com>",
    "Section": "misc",
    "Priority": "optional",
    "Description": "demo package",
}

#: Installed by :func:`build_deb` when no explicit file list is supplied.  It
#: only depends on ``Package`` so existing tests keep their behaviour.
DEFAULT_FILES = ("usr/share/doc/{package}/README",)


def _tar_gz(
    members: list[tuple[str, bytes, str]], mtime: int = 0
) -> bytes:
    """Build a tar.gz from ``(name, data, type)`` tuples.

    ``type`` is ``"file"`` or ``"symlink"``; symlinks are encoded with the
    entry name as their target, which is enough to prove that only regular
    files take part in file ownership.
    """

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.GNU_FORMAT) as tar:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            info.mtime = mtime
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = data.decode()
                info.size = 0
                tar.addfile(info)
                continue
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _ar(members: list[tuple[str, bytes]]) -> bytes:
    out = bytearray(b"!<arch>\n")
    for name, data in members:
        header = (
            f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n".encode()
        )
        assert len(header) == 60, header
        out += header + data
        if len(data) % 2:
            out += b"\n"
    return bytes(out)


def build_deb(
    path: Path,
    *,
    control: dict[str, str] | None = None,
    payload: bytes = b"payload\n",
    arch: str = "amd64",
    files: tuple[str, ...] | list[str] | None = None,
    symlinks: tuple[str, ...] | list[str] | None = None,
) -> Path:
    """Build a minimal but structurally valid ``.deb``.

    *files* is the list of install paths (without a leading slash); every one
    becomes a regular file containing ``payload``.  When it is ``None`` a
    single documentation file is created so the package is never empty.
    *symlinks* names entries that are symlinks and must not participate in
    file ownership.
    """

    fields = dict(DEFAULT_CONTROL)
    fields["Architecture"] = arch
    if control:
        fields.update(control)
    package = fields["Package"]
    control_text = "".join(f"{key}: {value}\n" for key, value in fields.items())
    control_tar = _tar_gz(
        [("control", control_text.encode(), "file"), ("md5sums", b"", "file")]
    )

    if files is None:
        member_names = [name.format(package=package) for name in DEFAULT_FILES]
    else:
        member_names = list(files)
    data_members: list[tuple[str, bytes, str]] = []
    for name in member_names:
        data_members.append(("." + "/" + name.lstrip("/"), payload, "file"))
    for name in symlinks or ():
        data_members.append(("." + "/" + name.lstrip("/"), b"/nonexistent", "symlink"))
    if not data_members:
        data_members.append(("./usr/share/doc/" + package + "/README", payload, "file"))
    data_tar = _tar_gz(data_members)

    blob = _ar(
        [
            ("debian-binary", b"2.0\n"),
            ("control.tar.gz", control_tar),
            ("data.tar.gz", data_tar),
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path
