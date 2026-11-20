"""Demo helpers: expose the shipped sample packages for Swagger try-it-out.

Swagger's file input needs a file on the operator's machine, so the service
lists the mounted samples and serves them back over HTTP.  The operator
downloads one, then uploads it in the import form.  Disabled unless
``ENABLE_DEMO=true``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from .config import Settings


def build_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1/demo", tags=["demo"])

    def demo_dir() -> Path:
        if not settings.enable_demo or settings.demo_dir is None:
            raise HTTPException(status_code=404, detail="demo endpoints are disabled")
        if not settings.demo_dir.is_dir():
            raise HTTPException(status_code=404, detail="demo directory is not mounted")
        return settings.demo_dir

    def catalog() -> dict[str, Any]:
        root = demo_dir()
        meta_path = root / "demo.json"
        meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
        items: list[dict[str, Any]] = []
        for path in sorted(root.glob("*.tar")):
            items.append(
                {
                    "name": path.name,
                    "size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "url": f"/api/v1/demo/samples/{path.name}",
                }
            )
        return {"demo": meta, "samples": items}

    @router.get("/samples", summary="List the sample packages mounted for the demo")
    def list_samples() -> dict[str, Any]:
        return catalog()

    @router.get(
        "/samples/{name}",
        summary="Download one sample package (for use with the import form)",
        response_class=FileResponse,
    )
    def get_sample(name: str) -> FileResponse:
        root = demo_dir()
        if "/" in name or "\\" in name or name.startswith(".") or not name.endswith(".tar"):
            raise HTTPException(status_code=400, detail="invalid sample name")
        path = (root / name).resolve()
        if path.parent != root.resolve() or not path.is_file():
            raise HTTPException(status_code=404, detail="sample not found")
        return FileResponse(path, media_type="application/x-tar", filename=name)

    @router.get("/package/{name}", summary="Package metadata (expected digests) for a sample", include_in_schema=False)
    def package_meta(name: str) -> JSONResponse:
        root = demo_dir()
        meta = json.loads((root / "demo.json").read_text())
        return JSONResponse(meta)

    return router
