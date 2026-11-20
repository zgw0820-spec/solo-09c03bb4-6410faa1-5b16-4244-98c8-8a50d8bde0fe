"""FastAPI application: single-instance OCI layout import / download API."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import oci, tarball
from .config import Settings
from .demo import build_router
from .store import (
    NotReady,
    SingleInstanceViolation,
    StateConflict,
    Store,
    StoreError,
    VersionNotFound,
    normalize_expected,
)

log = logging.getLogger("importer")

STATIC_DIR = Path(__file__).parent / "static"


def error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    payload: dict[str, Any] = {"error": {"code": code, "message": message, **extra}}
    return JSONResponse(payload, status_code=status)


def oci_error_response(exc: oci.OciError) -> JSONResponse:
    body = exc.as_dict()
    code = body.pop("code")
    message = body.pop("message")
    return error(exc.http_status, code, message, **body)


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = store or Store(settings)
        try:
            app.state.store.acquire_single_instance_lock()
        except SingleInstanceViolation:
            raise
        _seed(app.state.store, settings)
        try:
            yield
        finally:
            app.state.store.release_single_instance_lock()

    app = FastAPI(
        title="Offline OCI Image Importer",
        version="1.0.0",
        description=(
            "Single-instance import/activation API for air-gapped environments. "
            "Imports one OCI Image Layout tar (linux/amd64 + linux/arm64), completes "
            "missing blobs from the local store, refuses missing or mismatched blobs, "
            "activates only against the expected current index digest, and serves the "
            "active layout as a complete OCI tar."
        ),
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    if settings.enable_demo:
        app.include_router(build_router(settings))

    def get_store(request: Request) -> Store:
        return request.app.state.store

    # ------------------------------------------------------------------ meta
    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        return _swagger_page(settings)

    @app.get("/healthz", summary="Liveness and current active version")
    def healthz(s: Store = Depends(get_store)) -> dict[str, Any]:
        try:
            state = s.state_json()
        except StoreError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"status": "ok", "active": state["active"], "generation": state["generation"]}

    @app.get("/api/v1/status", summary="Active version, generation and platform summary")
    def status(s: Store = Depends(get_store)) -> dict[str, Any]:
        return s.state_json()

    @app.get("/api/v1/versions", summary="All imported (staged or active) versions")
    def versions(s: Store = Depends(get_store)) -> dict[str, Any]:
        active = s.active_digest()
        items = [{**record.as_json(), "active": record.digest == active} for record in s.list_versions()]
        return {"active": active, "versions": items}

    # ---------------------------------------------------------------- import
    def _import(
        s: Store,
        upload: UploadFile,
        expected_current_digest: str,
        enable: bool,
        source_name: str | None,
    ) -> JSONResponse:
        try:
            expected = normalize_expected(expected_current_digest)
        except oci.OciError as exc:
            return error(400, "bad_expected_digest", str(exc))
        try:
            tar_path, package_sha, package_size = s.stage_upload(
                upload.file, max_bytes=s.settings.max_import_bytes
            )
        except oci.OciError as exc:
            return oci_error_response(exc)
        try:
            result = s.stage(
                tar_path,
                expected_current_digest=expected,
                source_name=source_name,
                enable=enable,
            )
        except StateConflict as exc:
            s.event("import.conflict", expected=exc.expected, actual=exc.actual)
            return error(
                409,
                "version_conflict",
                exc.message,
                expected=exc.expected,
                actual=exc.actual,
                stagedDigest=exc.staged_digest,
            )
        except oci.OciError as exc:
            s.event("import.rejected", error=exc.code, digest=exc.digest)
            return oci_error_response(exc)
        except StoreError as exc:
            s.event("import.failed", error=str(exc))
            return error(500, "store_error", str(exc))
        body = {
            "status": "activated" if result["activated"] else "staged",
            "digest": result["digest"],
            "changed": result.get("changed"),
            "generation": result.get("generation"),
            "version": result["version"],
            "blobs": result["blobs"],
            "package": {"sha256": package_sha, "size": package_size},
            "download": f"/api/v1/images/{result['digest']}/layout.tar",
        }
        if result["activated"]:
            body["previous"] = None if result.get("changed") is False else expected
            body["active"] = result["active"]
        else:
            body["activationHint"] = (
                "staged only; POST /api/v1/images/{digest}/enable with expected_current_digest "
                "to activate"
            )
        return JSONResponse(body, status_code=201, headers={"Location": f"/api/v1/images/{result['digest']}"})

    @app.post(
        "/api/v1/images/import",
        status_code=201,
        summary="Import one multi-platform image version (staged, optionally activated)",
        responses={
            201: {"description": "Version validated; activated when enable=true (default)"},
            400: {"description": "Malformed package or parameters"},
            409: {"description": "Expected current digest does not match the active version"},
            422: {"description": "Missing/mismatched blobs or unsupported platforms (digest listed)"},
        },
    )
    def import_image(
        file: Annotated[UploadFile, File(description="OCI Image Layout tar (single image, amd64+arm64)")],
        s: Store = Depends(get_store),
        expected_current_digest: Annotated[
            str,
            Form(
                description=(
                    "Digest of the index.json currently in use. Send the exact value from "
                    "GET /api/v1/status. Use the literal 'none' when the store is empty."
                )
            ),
        ] = "none",
        enable: Annotated[
            bool,
            Form(
                description=(
                    "true: validate and activate in one step. false: validate and stage only; "
                    "activate later with POST /api/v1/images/{digest}/enable."
                )
            ),
        ] = True,
        source_name: Annotated[str | None, Form(description="Free-form label recorded in the version metadata")] = None,
    ) -> JSONResponse:
        return _import(s, file, expected_current_digest, enable, source_name)

    @app.post(
        "/api/v1/images/{digest}/enable",
        summary="Activate a previously staged (validated) version against the expected current digest",
        responses={
            200: {"description": "Version activated"},
            409: {"description": "Expected current digest does not match the active version"},
        },
    )
    def enable_version(
        digest: str,
        s: Store = Depends(get_store),
        expected_current_digest: Annotated[str, Form(description="Digest of the currently active version, or 'none'")] = "none",
    ) -> JSONResponse:
        try:
            expected = normalize_expected(expected_current_digest)
            oci.parse_digest(digest)
        except oci.OciError as exc:
            return error(400, "bad_request", str(exc))
        try:
            result = s.activate(digest, expected_current_digest=expected)
        except StateConflict as exc:
            return error(
                409,
                "version_conflict",
                exc.message,
                expected=exc.expected,
                actual=exc.actual,
                stagedDigest=digest,
            )
        except VersionNotFound as exc:
            return error(404, "version_not_found", str(exc))
        except oci.OciError as exc:
            return oci_error_response(exc)
        return JSONResponse(
            {
                "status": "activated",
                "digest": result["active"],
                "previous": None if result.get("changed") is False else expected,
                "changed": result.get("changed"),
                "generation": result["generation"],
                "version": result["version"],
                "download": f"/api/v1/images/{result['active']}/layout.tar",
            }
        )

    # -------------------------------------------------------------- download
    def _layout_plan(s: Store, digest: str | None) -> tuple[Any, Path, tarball.TarPlan]:
        record, version_dir = s.resolve_download(digest)
        layout_marker = oci.layout_document(
            oci.Descriptor(media_type=record.media_type, digest=record.digest, size=record.size)
        )
        layout_index = (version_dir / "index.json").read_bytes()
        entries = [
            tarball.TarEntry("oci-layout", len(layout_marker), data=layout_marker),
            tarball.TarEntry("index.json", len(layout_index), data=layout_index),
        ]
        # index.json is served at the layout root AND remains available under its
        # blob path, so clients that follow the layout index descriptor resolve
        # the version index without a second request.
        seen: set[str] = set()
        for blob in record.blobs:
            if blob in seen:
                continue
            seen.add(blob)
            path = s.blobs.path(blob)
            entries.append(
                tarball.TarEntry(f"blobs/sha256/{blob[len('sha256:'):]}", path.stat().st_size, path=path)
            )
        return record, version_dir, tarball.TarPlan(entries)

    def _download_headers(record: Any, plan: tarball.TarPlan) -> dict[str, str]:
        return {
            "Content-Type": "application/x-tar",
            "Content-Length": str(plan.content_length()),
            "Content-Disposition": f'attachment; filename="{record.digest.replace(":", "-")}.tar"',
            "X-OCI-Image-Digest": record.digest,
            "X-OCI-Image-Size": str(record.size),
            # The version is pinned at request start; activation changes cannot
            # affect this response.  Clients should compare the digest header.
            "X-OCI-Image-Pinned": "true",
            "Cache-Control": "no-store",
        }

    @app.get(
        "/api/v1/images/current/layout.tar",
        summary="Download the currently active image as a complete OCI layout tar",
        response_class=StreamingResponse,
    )
    def download_current(s: Store = Depends(get_store)) -> StreamingResponse:
        try:
            record, _, plan = _layout_plan(s, None)
        except NotReady as exc:
            return error(503, "no_active_version", str(exc))
        except VersionNotFound as exc:
            return error(404, "version_not_found", str(exc))
        except oci.OciError as exc:
            return oci_error_response(exc)
        return StreamingResponse(plan.chunks(), headers=_download_headers(record, plan))

    @app.get(
        "/api/v1/images/{digest}/layout.tar",
        summary="Download one pinned image version as a complete OCI layout tar",
        response_class=StreamingResponse,
    )
    def download_version(digest: str, s: Store = Depends(get_store)) -> StreamingResponse:
        try:
            oci.parse_digest(digest)
        except oci.OciError as exc:
            return oci_error_response(exc)
        try:
            record, _, plan = _layout_plan(s, digest)
        except VersionNotFound as exc:
            return error(404, "version_not_found", str(exc))
        except NotReady as exc:
            return error(404, "version_not_found", str(exc))
        except oci.OciError as exc:
            return oci_error_response(exc)
        return StreamingResponse(plan.chunks(), headers=_download_headers(record, plan))

    return app


def _seed(store: Store, settings: Settings) -> None:
    """Load the initial image only when the store has no active version yet."""
    seed = settings.seed_initial_image
    if seed is None:
        log.info("no SEED_INITIAL_IMAGE configured; active=%s", store.active_digest())
        return
    if not seed.is_file():
        if settings.seed_required:
            raise RuntimeError(f"SEED_INITIAL_IMAGE={seed} does not exist")
        log.warning("seed image %s missing; starting empty", seed)
        return
    if store.active_digest() is not None:
        log.info("store already has an active version; seed package ignored")
        return
    try:
        record = store.seed_if_empty(seed, source_name=seed.name)
    except oci.OciError as exc:
        raise RuntimeError(
            f"initial image {seed} failed validation: {exc.code} {exc.message} "
            f"(digest={exc.digest})"
        ) from exc
    log.info("seeded initial image: %s", record["digest"] if record else "already active")


def _swagger_page(settings: Settings) -> HTMLResponse:
    # Swagger UI assets are served locally so the closed network needs no CDN.
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Offline OCI Image Importer</title>
  <link rel="stylesheet" href="/static/swagger-ui.css" />
  <link rel="icon" href="/static/favicon-32x32.png" />
  <style>body {{ margin: 0; }} .topbar {{ background: #1f2937; }} </style>
</head>
<body>
<div id="swagger-ui"></div>
<script src="/static/swagger-ui-bundle.js"></script>
<script>
window.onload = () => {{
  window.ui = SwaggerUIBundle({{
    url: "/openapi.json",
    dom_id: "#swagger-ui",
    deepLinking: true,
    persistAuthorization: true,
    displayRequestDuration: true,
    tryItOutEnabled: true,
    filter: true,
  }});
}};
</script>
</body>
</html>
"""
    return HTMLResponse(html)


app = create_app()
