import asyncio
from datetime import datetime, timezone

import pytest

import video_access
from video_access import generate_video_sas_uris, video_blob_location


class FakeCredential:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeBlobServiceClient:
    def __init__(self, *, account_url, credential):
        self.account_url = account_url
        self.credential = credential
        self.key_request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return None

    async def get_user_delegation_key(self, *, key_start_time, key_expiry_time):
        self.key_request = (key_start_time, key_expiry_time)
        return object()


def test_video_blob_location_encodes_multibyte_path():
    location = video_blob_location(
        "video/vidéo 42/fichier final.mp4",
        "https://storage.example.invalid/"
        "video",
    )

    assert location.account_name == "storage"
    assert location.container_name == "video"
    assert     location.blob_name == "vidéo 42/fichier final.mp4"
    assert location.unsigned_url.endswith(
        "vid%C3%A9o%2042/fichier%20final.mp4"
    )


def test_video_blob_location_rejects_non_https_base_url():
    with pytest.raises(ValueError, match="HTTPS"):
        video_blob_location(
            "video/video/file.mp4",
            "http://storage.example.invalid/"
            "video",
        )


@pytest.mark.parametrize("ttl_seconds", [0, 86401])
def test_generate_video_sas_uris_rejects_invalid_ttl(ttl_seconds):
    with pytest.raises(ValueError, match="comprise entre 1 et 86400"):
        asyncio.run(
            generate_video_sas_uris(
                [],
                base_url=(
                    "https://storage.example.invalid/"
                    "video"
                ),
                ttl_seconds=ttl_seconds,
            )
        )


def test_generate_video_sas_uris_uses_one_delegation_key(monkeypatch):
    credential = FakeCredential()
    clients = []
    sas_calls = []

    def service_client_factory(**kwargs):
        client = FakeBlobServiceClient(**kwargs)
        clients.append(client)
        return client

    def fake_generate_blob_sas(**kwargs):
        sas_calls.append(kwargs)
        return f"sp=r&sig=signature-{len(sas_calls)}"

    monkeypatch.setattr(video_access, "generate_blob_sas", fake_generate_blob_sas)
    issued_at = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)
    blob_paths = [
        "video/video-42/first.mp4",
        "video/video-42/second.mp4",
    ]

    uris = asyncio.run(
        generate_video_sas_uris(
            blob_paths,
            base_url=(
                "https://storage.example.invalid/"
                "video"
            ),
            ttl_seconds=3600,
            now=issued_at,
            credential_factory=lambda: credential,
            service_client_factory=service_client_factory,
        )
    )

    assert len(clients) == 1
    assert clients[0].account_url == (
        "https://storage.example.invalid"
    )
    assert clients[0].key_request == (
        datetime(2026, 8, 28, 8, 55, tzinfo=timezone.utc),
        datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
    )
    assert credential.closed
    assert uris[blob_paths[0]].endswith("?sp=r&sig=signature-1")
    assert uris[blob_paths[1]].endswith("?sp=r&sig=signature-2")
    assert [call["blob_name"] for call in sas_calls] == [
        "video-42/first.mp4",
        "video-42/second.mp4",
    ]
    assert all(str(call["permission"]) == "r" for call in sas_calls)
    assert all(call["protocol"] == "https" for call in sas_calls)
