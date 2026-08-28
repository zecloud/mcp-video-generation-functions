from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Sequence
from urllib.parse import quote, urlsplit

from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.blob import BlobSasPermissions, generate_blob_sas
from azure.storage.blob.aio import BlobServiceClient

from video_workflow import VIDEO_BLOB_PATH_PREFIX


USER_DELEGATION_KEY_CLOCK_SKEW = timedelta(minutes=5)
DEFAULT_VIDEO_SAS_TTL_SECONDS = 60 * 60
MAX_VIDEO_SAS_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class VideoBlobLocation:
    account_url: str
    account_name: str
    container_name: str
    blob_name: str
    unsigned_url: str


def video_blob_location(blob_path: str, base_url: str) -> VideoBlobLocation:
    if not blob_path.startswith(VIDEO_BLOB_PATH_PREFIX):
        raise ValueError(f"Chemin blob vidéo inattendu : {blob_path!r}.")

    parsed = urlsplit(base_url.rstrip("/"))
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("VIDEO_BLOB_BASE_URL doit être une URL HTTPS Azure Blob.")
    if parsed.query or parsed.fragment:
        raise ValueError("VIDEO_BLOB_BASE_URL ne doit contenir ni query ni fragment.")

    base_path = parsed.path.strip("/")
    if not base_path or not blob_path.startswith(f"{base_path}/"):
        raise ValueError(
            "VIDEO_BLOB_BASE_URL et le chemin blob vidéo ne correspondent pas."
        )

    container_name, separator, blob_name = blob_path.partition("/")
    if not separator or not blob_name:
        raise ValueError(f"Chemin blob vidéo incomplet : {blob_path!r}.")

    relative_path = blob_path.removeprefix(f"{base_path}/")
    unsigned_url = (
        f"{parsed.scheme}://{parsed.netloc}/{quote(base_path, safe='/')}/"
        f"{quote(relative_path, safe='/')}"
    )
    return VideoBlobLocation(
        account_url=f"{parsed.scheme}://{parsed.netloc}",
        account_name=parsed.netloc.split(".", 1)[0],
        container_name=container_name,
        blob_name=blob_name,
        unsigned_url=unsigned_url,
    )


def _storage_credential():
    if os.environ.get("WEBSITE_HOSTNAME"):
        client_id = os.environ.get("AZURE_CLIENT_ID")
        if not client_id:
            raise RuntimeError(
                "AZURE_CLIENT_ID est requis pour signer les vidéos avec "
                "l’identité managée de la Function App."
            )
        return ManagedIdentityCredential(client_id=client_id)
    return DefaultAzureCredential()


async def generate_video_sas_uris(
    blob_paths: Sequence[str],
    *,
    base_url: str,
    ttl_seconds: int,
    now: datetime | None = None,
    credential_factory: Callable[[], object] = _storage_credential,
    service_client_factory: Callable[..., object] = BlobServiceClient,
) -> dict[str, str]:
    if not 1 <= ttl_seconds <= MAX_VIDEO_SAS_TTL_SECONDS:
        raise ValueError(
            "La durée du SAS vidéo doit être comprise entre 1 et "
            f"{MAX_VIDEO_SAS_TTL_SECONDS} secondes."
        )
    if not blob_paths:
        return {}

    locations = [video_blob_location(path, base_url) for path in blob_paths]
    account_url = locations[0].account_url
    if any(location.account_url != account_url for location in locations):
        raise ValueError("Toutes les vidéos doivent appartenir au même compte Blob.")

    issued_at = now or datetime.now(timezone.utc)
    if issued_at.tzinfo is None:
        raise ValueError("L’horodatage SAS doit inclure un fuseau horaire.")
    start = issued_at - USER_DELEGATION_KEY_CLOCK_SKEW
    expiry = issued_at + timedelta(seconds=ttl_seconds)

    credential = credential_factory()
    try:
        async with service_client_factory(
            account_url=account_url,
            credential=credential,
        ) as blob_service_client:
            delegation_key = await blob_service_client.get_user_delegation_key(
                key_start_time=start,
                key_expiry_time=expiry,
            )
    finally:
        close = getattr(credential, "close", None)
        if close is not None:
            closed = close()
            if isinstance(closed, Awaitable):
                await closed

    return {
        blob_path: (
            f"{location.unsigned_url}?"
            f"{generate_blob_sas(
                account_name=location.account_name,
                container_name=location.container_name,
                blob_name=location.blob_name,
                user_delegation_key=delegation_key,
                permission=BlobSasPermissions(read=True),
                start=start,
                expiry=expiry,
                protocol='https',
            )}"
        )
        for blob_path, location in zip(blob_paths, locations, strict=True)
    }
