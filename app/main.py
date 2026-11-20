"""FastAPI application exposing an atomic, by-hash enabled APT repository.

Routes
------
``GET  /docs``                      interactive upload form
``POST /api/publish``               upload a batch of ``.deb`` files and publish
``POST /api/rehearse``              dry-run an unpack order of release packages
``GET  /api/status``                repository state and publication history
``GET  /dists/...``, ``GET /pool/...``  the APT document tree (read-only)
``GET  /``                          plain-text instructions / sources.list
``GET  /swagger-ui``                generated OpenAPI documentation

Every upload is written to a private intake directory, validated and hashed,
and only then committed under a lock.  See :mod:`app.storage` for the
transaction and recovery protocol.  Rehearsals are read-only: they open the
selected release's packages and never copy a file or change a publication.
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
from fastapi.encoders import jsonable_encoder
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
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .config import Settings
from .drill import (
    DrillError,
    SCHEMA_NOTES,
    InstalledPackage,
    normalize_sha256_values,
    simulate,
    validate_installed,
)
from .storage import (
    PublicationError,
    ReleaseLookupError,
    Repository,
    StagedUpload,
)

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


# --------------------------------------------------------------- drill models
class InstalledPackageModel(BaseModel):
    """A package already installed on the simulated target machine."""

    name: str = Field(
        ..., description="Debian package name", examples=["libfoo"], min_length=2
    )
    version: str = Field(
        ..., description="Installed Debian version", examples=["1.2.3-1"]
    )
    files: list[str] = Field(
        ...,
        description=(
            "Absolute paths of the regular files this package owns on the "
            "target machine (not directories)"
        ),
        examples=[["/usr/lib/libfoo.so.1", "/usr/share/doc/libfoo/copyright"]],
    )


class RehearsalRequest(BaseModel):
    """Request body of ``POST /api/rehearse``.

    The package list is ordered: it is the order in which the packages would
    be unpacked.  Each entry is the SHA-256 of a ``.deb`` in the selected
    release, and a package name may appear only once.
    """

    release: int = Field(
        ...,
        ge=1,
        description="Publication number of a release that is still downloadable",
        examples=[3],
    )
    packages: list[str] = Field(
        ...,
        min_length=2,
        max_length=8,
        description=(
            "SHA-256 of each .deb in unpack order; 2 to 8 entries, "
            "at most one package per package name"
        ),
        examples=[
            [
                "01754aaca31f14ded926669676cdd65b8c8e40412e5e66fd827f91740d86f11f",
                "58b114f5afdacc20c47fad080564cf8da17fef9ad7a395c78dce9d259ac44cb4",
            ]
        ],
    )
    installed: list[InstalledPackageModel] = Field(
        default_factory=list,
        description=(
            "Packages currently installed on the target machine, with the "
            "absolute paths of the regular files each of them owns"
        ),
    )


_STREAM_CHUNK = 256 * 1024


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
            "successful releases. ``POST /api/rehearse`` dry-runs the unpack "
            "order of a release against a simulated target machine.\n\n"
            + SCHEMA_NOTES
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
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Report a malformed rehearsal manifest in the rehearsal's own shape.

        A schema violation (for example fewer than two packages, or a missing
        ``installed[].files``) is a malformed manifest, not a generic HTTP
        error; mapping it here keeps the three separate error categories
        distinguishable by ``reason.code``.
        """

        if request.url.path != "/api/rehearse":
            return JSONResponse(
                status_code=422, content={"detail": jsonable_encoder(exc.errors())}
            )
        details: list[str] = []
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ()))
            details.append(f"{location}: {error.get('msg', 'invalid value')}")
        return JSONResponse(
            status_code=400,
            content={
                "error": "rehearsal_rejected",
                "reason": {
                    "code": "invalid_manifest",
                    "message": "the rehearsal manifest is malformed",
                    "details": details,
                },
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

    @app.post(
        "/api/rehearse",
        response_model=None,
        summary="Dry-run an unpack order against the target machine",
        responses={
            200: {"description": "Every step passed, or the first conflict"},
            400: {"description": "Malformed manifest"},
            404: {"description": "Release is not downloadable"},
            409: {"description": "Package SHA-256 not in the selected release"},
            422: {"description": "A selected package could not be read"},
        },
    )
    def rehearse(request: RehearsalRequest) -> Response:
        try:
            digests = normalize_sha256_values(request.packages)
            installed = validate_installed(
                InstalledPackage(name=item.name, version=item.version, files=tuple(item.files))
                for item in request.installed
            )
        except DrillError as exc:
            return JSONResponse(
                status_code=exc.status,
                content={
                    "error": "rehearsal_rejected",
                    "release": request.release,
                    "reason": exc.to_dict(),
                },
            )

        snapshot = None
        created_at: str | None = None
        try:
            snapshot, selected = repository.rehearse(
                request.release, digests, installed
            )
            created_at = snapshot.created_at
            outcome = simulate(selected, installed)
        except ReleaseLookupError as exc:
            status = {
                "release_not_available": 404,
                "package_not_in_release": 409,
                "package_unreadable": 422,
            }.get(exc.code, 400)
            return JSONResponse(
                status_code=status,
                content={
                    "error": "rehearsal_rejected",
                    "release": request.release,
                    "reason": {
                        "code": exc.code,
                        "message": exc.message,
                        **({"details": list(exc.details)} if exc.details else {}),
                    },
                },
            )
        except DrillError as exc:
            status = exc.status or 400
            return JSONResponse(
                status_code=status,
                content={
                    "error": "rehearsal_rejected",
                    "release": request.release,
                    "reason": exc.to_dict(),
                },
            )
        finally:
            if snapshot is not None:
                snapshot.close()

        payload: dict[str, object] = {
            "release": request.release,
            "release_created_at": created_at,
            "packages": [
                {
                    "sha256": package.sha256,
                    "package": package.name,
                    "version": package.version,
                    "files": len(package.files),
                }
                for package in selected
            ],
            "installed": [
                {"package": pkg.name, "version": pkg.version, "files": len(pkg.files)}
                for pkg in installed
            ],
            **outcome.to_dict(),
        }
        return JSONResponse(status_code=200, content=payload)

    @app.get("/api/status")
    def status() -> JSONResponse:
        return JSONResponse(repository.status())

    @app.get("/api/publications")
    def publications() -> JSONResponse:
        return JSONResponse(
            {"publications": [p.to_dict() for p in repository.list_publications(50)]}
        )

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
