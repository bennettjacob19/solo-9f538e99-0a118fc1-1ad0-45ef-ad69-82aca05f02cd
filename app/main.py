"""FastAPI application exposing an atomic, by-hash enabled APT repository.

Routes
------
``GET  /docs``                      interactive upload form
``POST /api/publish``               upload a batch of ``.deb`` files and publish
``GET  /api/status``                repository state and publication history
``GET  /dists/...``, ``GET /pool/...``  the APT document tree (read-only)
``GET  /``                          plain-text instructions / sources.list
``GET  /swagger-ui``                generated OpenAPI documentation

Every upload is written to a private intake directory, validated and hashed,
and only then committed under a lock.  See :mod:`app.storage` for the
transaction and recovery protocol.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from email.utils import formatdate
from pathlib import Path

import anyio

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, Field, field_validator
from starlette.background import BackgroundTask

from .config import Settings
from .rehearsal import (
    InstalledPackage,
    RehearsalError,
    RehearsalEngine,
    validate_manifest,
)
from .storage import PublicationError, Repository, StagedUpload

#: Content types Debian clients expect for the repository documents.
_MEDIA_TYPES = {
    "Release": "text/plain; charset=utf-8",
    "Packages": "text/plain; charset=utf-8",
    "Packages.gz": "application/gzip",
    "InRelease": "text/plain; charset=utf-8",
    "Release.gpg": "application/pgp-signature",
}

UPLOAD_FORM = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>APT repository upload</title>
<style>
 body {{ font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 52rem; }}
 code, pre {{ background: #f4f4f4; padding: .1rem .3rem; border-radius: 3px; }}
 pre {{ padding: .8rem; overflow-x: auto; }}
 form {{ border: 1px solid #ccc; padding: 1rem; border-radius: 6px; }}
 button {{ margin-top: .6rem; padding: .4rem 1rem; }}
 .hint {{ color: #555; }}
</style></head><body>
<h1>APT repository upload</h1>
<p>Suite <code>{suite}</code>, component <code>{component}</code>,
 architecture <code>{architecture}</code>.</p>
<p class="hint">Upload a complete batch of <code>.deb</code> files. The new
Release becomes visible only after every package has been validated and all
indexes are on disk; the two most recent publications stay downloadable.</p>
<form method="post" action="/api/publish" enctype="multipart/form-data">
  <label>Package files<br>
  <input type="file" name="files" multiple accept=".deb" required></label><br>
  <button type="submit">Publish</button>
</form>
<h2>Client configuration</h2>
<pre>echo 'deb [trusted=yes arch={architecture}] {base_url} {suite} {component}' \\
  | sudo tee /etc/apt/sources.list.d/internal.list
sudo apt-get update
sudo apt-get download &lt;package&gt;</pre>
</body></html>
"""


def _media_type(relpath: str) -> str:
    name = relpath.rsplit("/", 1)[-1]
    return _MEDIA_TYPES.get(name, "application/octet-stream")


_STREAM_CHUNK = 256 * 1024


class InstalledPackageModel(BaseModel):
    """One package already installed on the target machine."""

    name: str = Field(..., description="installed package name")
    version: str = Field(..., description="installed Debian version")
    files: list[str] = Field(
        default_factory=list,
        description="absolute paths of the regular files owned by the package",
    )

    @field_validator("name", "version")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("files")
    @classmethod
    def _absolute_paths(cls, value: list[str]) -> list[str]:
        for path in value:
            if not path.startswith("/"):
                raise ValueError(f"not an absolute path: {path!r}")
        return value


class RehearsalRequest(BaseModel):
    """A conflict-check rehearsal over one retained publication."""

    publication_id: int = Field(
        ..., gt=0, description="id of a still-downloadable publication"
    )
    package_sha256: list[str] = Field(
        ...,
        min_length=2,
        max_length=8,
        description="package SHA-256 digests in the planned unpack order; "
        "same-named packages may appear only once",
    )
    installed: list[InstalledPackageModel] = Field(
        default_factory=list,
        description="packages already installed on the target machine, with "
        "the absolute regular-file paths each one owns",
    )

    @field_validator("package_sha256")
    @classmethod
    def _sha256_shape(cls, value: list[str]) -> list[str]:
        import re

        pattern = re.compile(r"^[0-9a-f]{64}$")
        seen: set[str] = set()
        for digest in value:
            if not pattern.match(digest):
                raise ValueError(f"not a lowercase SHA-256 digest: {digest!r}")
            if digest in seen:
                raise ValueError(f"duplicate package digest: {digest}")
            seen.add(digest)
        return value


async def _stream(lease) -> AsyncIterator[bytes]:  # type: ignore[no-untyped-def]
    """Yield the leased file in chunks without blocking the event loop."""

    while True:
        chunk = await anyio.to_thread.run_sync(lease.fh.read, _STREAM_CHUNK)
        if not chunk:
            return
        yield chunk


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    repository = Repository(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Recover from an interrupted publication before serving anything.
        repository.reconcile()
        yield

    app = FastAPI(
        title="Internal APT repository",
        version="1.0.0",
        description=(
            "Single-suite, single-component APT repository with atomic "
            "publications and Acquire-By-Hash retention of the last two "
            "successful releases."
        ),
        lifespan=lifespan,
        # /docs is the upload form; the generated OpenAPI UI lives at /swagger.
        docs_url=None,
        redoc_url=None,
        swagger_ui_oauth2_redirect_url="/swagger/oauth2-redirect",
    )
    app.state.settings = settings
    app.state.repository = repository

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # A malformed rehearsal manifest is a client error, not a framework 422.
        issues = [
            {
                "location": list(error.get("loc", ())),
                "message": error.get("msg", ""),
                "type": error.get("type", ""),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=400,
            content={
                "detail": {
                    "error": "invalid_manifest",
                    "detail": "request body does not match the rehearsal schema",
                    "issues": issues,
                }
            },
        )

    @app.get("/", response_class=PlainTextResponse)
    def root() -> str:
        return (
            "Internal APT repository\n"
            "=======================\n\n"
            f"Suite:        {settings.distribution}\n"
            f"Component:    {settings.component}\n"
            f"Architecture: {settings.architecture}\n\n"
            "Client configuration:\n"
            f"  {settings.sources_line}\n\n"
            "Then run: apt-get update && apt-get download <package>\n\n"
            "Upload packages at /docs or POST them to /api/publish.\n"
        )

    @app.get("/docs", response_class=HTMLResponse, include_in_schema=False)
    def upload_docs(request: Request) -> HTMLResponse:
        base_url = str(request.base_url).rstrip("/")
        return HTMLResponse(
            UPLOAD_FORM.format(
                suite=settings.distribution,
                component=settings.component,
                architecture=settings.architecture,
                base_url=base_url,
            )
        )

    @app.get("/swagger", include_in_schema=False)
    def swagger_redirect(request: Request) -> Response:
        return RedirectResponse(url=str(request.url_for("swagger_ui_html")))

    @app.get("/swagger-ui", response_class=HTMLResponse, include_in_schema=False)
    def swagger_ui_html(request: Request) -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=app.openapi_url or "/openapi.json",
            title=f"{app.title} - OpenAPI",
            oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        )

    @app.post("/api/publish", response_model=None)
    def publish(files: list[UploadFile]) -> Response:
        if not files:
            raise HTTPException(status_code=400, detail="no files were uploaded")
        if len(files) > settings.max_batch_files:
            raise HTTPException(
                status_code=413,
                detail=f"at most {settings.max_batch_files} packages per publication",
            )

        token = uuid.uuid4().hex
        intake = repository.incoming / token
        intake.mkdir(parents=True, exist_ok=False)
        staged: list[StagedUpload] = []
        total = 0
        try:
            for index, upload in enumerate(files):
                name = Path(upload.filename or f"upload-{index}.deb").name
                if not name.endswith(".deb"):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{name}: only .deb files are accepted",
                    )
                target = intake / f"{index:04d}.deb"
                size = 0
                with target.open("wb") as out:
                    while chunk := upload.file.read(1024 * 1024):
                        size += len(chunk)
                        total += len(chunk)
                        if size > settings.max_file_bytes:
                            raise HTTPException(
                                status_code=413,
                                detail=f"{name}: package exceeds the size limit",
                            )
                        if total > settings.max_batch_bytes:
                            raise HTTPException(
                                status_code=413,
                                detail="uploaded batch exceeds the size limit",
                            )
                        out.write(chunk)
                    out.flush()
                if size == 0:
                    raise HTTPException(
                        status_code=400, detail=f"{name}: uploaded file is empty"
                    )
                staged.append(StagedUpload(path=target, original_name=name))

            try:
                publication = repository.publish(staged)
            except PublicationError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            shutil.rmtree(intake, ignore_errors=True)

        return JSONResponse(
            status_code=201,
            content={
                "published": publication.to_dict(),
                "release_url": f"/{settings.release_path}",
            },
        )

    @app.get("/api/status")
    def status() -> JSONResponse:
        return JSONResponse(repository.status())

    @app.get("/api/publications")
    def publications() -> JSONResponse:
        return JSONResponse(
            {"publications": [p.to_dict() for p in repository.list_publications(50)]}
        )

    @app.post("/api/rehearsals", response_model=None)
    def rehearse(body: RehearsalRequest) -> JSONResponse:
        """Simulate an unpack order without touching the repository.

        The rehearsal reads the selected pool packages through open
        descriptors, so an accepted run completes even if the selected
        publication is evicted concurrently.  No ``.deb`` is copied and no
        publication state changes.
        """

        installed = [
            InstalledPackage(
                name=item.name, version=item.version, files=tuple(item.files)
            )
            for item in body.installed
        ]
        # The engine re-validates the manifest (defence in depth) and then
        # resolves the publication and parses every selected package.
        try:
            validate_manifest(
                body.publication_id, body.package_sha256, installed
            )
            result = RehearsalEngine(repository).run(
                body.publication_id, list(body.package_sha256), installed
            )
        except RehearsalError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"error": exc.code, "detail": str(exc)},
            ) from exc
        return JSONResponse(result.to_dict())

    def _lease_headers(lease) -> dict[str, str]:  # type: ignore[no-untyped-def]
        return {
            "Cache-Control": "public, max-age=300",
            "Content-Length": str(lease.size),
            "ETag": lease.etag,
            "Last-Modified": formatdate(lease.last_modified, usegmt=True),
        }

    @app.get("/{relpath:path}", include_in_schema=False)
    def serve(relpath: str) -> Response:
        lease = repository.open_file(relpath)
        if lease is None:
            raise HTTPException(status_code=404, detail="not found")
        # Stream through the already-open descriptor: garbage collection may
        # unlink the name concurrently, but the inode stays readable.
        return StreamingResponse(
            _stream(lease),
            media_type=_media_type(relpath),
            headers=_lease_headers(lease),
            background=BackgroundTask(lease.close),
        )

    @app.head("/{relpath:path}", include_in_schema=False)
    def serve_head(relpath: str) -> Response:
        # FastAPI's router does not route HEAD to a GET catch-all path
        # operation, so HEAD is registered explicitly.
        lease = repository.open_file(relpath)
        if lease is None:
            raise HTTPException(status_code=404, detail="not found")
        headers = _lease_headers(lease)
        lease.close()
        return Response(status_code=200, media_type=_media_type(relpath), headers=headers)

    return app


app = create_app()
