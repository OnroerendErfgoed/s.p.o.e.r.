"""
File upload signing endpoint.

The dossier API itself never receives file bytes — uploads go directly
to the file_service running on a separate port. This endpoint
mints a signed upload URL the client can POST a file to.

Flow:

1. Client calls `POST /files/upload/request` with a JSON body
   carrying at least `filename`.
2. This endpoint generates a fresh `file_id` (UUID4), signs an
   upload token over `(file_id, action="upload", user_id)`, and
   returns the file service URL with the signature embedded as
   query parameters.
3. Client uploads the file bytes to the returned URL. The file
   service verifies the token and rejects unsigned/expired requests.
4. Client references the `file_id` in subsequent activity content
   (e.g. `bijlage.file_id`). The dossier reader endpoints later
   inject signed *download* URLs for the same file_id, scoped to
   the reader's user + dossier.

The user must be authenticated to mint upload tokens, but tokens
themselves don't carry dossier scope — a freshly uploaded file isn't
yet attached to any dossier. The download tokens (issued by the
dossier read path) are dossier-scoped.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException

from dossier_common.signing import sign_token, token_to_query_string

from ..auth import User


def register(app: FastAPI, *, get_user) -> None:
    """Register file-related endpoints on the FastAPI app."""

    @app.post(
        "/files/upload/request",
        tags=["files"],
        summary="Request a signed upload URL",
    )
    async def request_upload(
        request_body: dict,
        user: User = Depends(get_user),
    ):
        """Request a signed URL for file upload. User must be
        authenticated. Returns a `file_id` and `upload_url` for the
        client to POST the file bytes to.

        The caller must supply ``dossier_id`` — the dossier the file
        is destined for. Because every client flow (including
        dossier-creating activities like ``dienAanvraagIn``) mints
        its dossier_id client-side before uploading attachments,
        this value is always known at token-request time.

        Binding ``dossier_id`` into the signed token, and threading
        it through the file_service's upload .meta, closes Bug 47:
        the file_service's ``/internal/move`` endpoint refuses to
        move a file into any dossier other than the one its upload
        was intended for. An attacker who learns another user's
        file_id cannot graft it onto their own dossier, because the
        move would cross the binding and be rejected.
        """
        file_config = app.state.config.get("file_service", {})
        signing_key = file_config.get(
            "signing_key", "poc-signing-key-change-in-production",
        )
        file_service_url = file_config.get("url", "http://localhost:8001")

        dossier_id = request_body.get("dossier_id", "")
        if not dossier_id:
            raise HTTPException(
                422,
                detail=(
                    "dossier_id is required. Supply the UUID of the "
                    "dossier this file will be attached to. For "
                    "dossier-creating activities (e.g. dienAanvraagIn), "
                    "use the client-generated dossier_id you will "
                    "PUT the activity against."
                ),
            )

        file_id = str(uuid4())
        token = sign_token(
            file_id=file_id,
            action="upload",
            signing_key=signing_key,
            user_id=user.id,
            dossier_id=dossier_id,
        )
        upload_url = (
            f"{file_service_url}/upload/{file_id}"
            f"?{token_to_query_string(token)}"
        )

        return {
            "file_id": file_id,
            "upload_url": upload_url,
            "filename": request_body.get("filename", ""),
            "dossier_id": dossier_id,
        }
