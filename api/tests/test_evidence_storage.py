import hashlib
import io

import pytest

from services.evidence_storage import (
    EvidenceUnavailableError,
    LocalEvidenceStorage,
    S3EvidenceStorage,
    evidence_storage,
)


class _MissingObject(Exception):
    response = {
        "Error": {"Code": "NoSuchKey"},
        "ResponseMetadata": {"HTTPStatusCode": 404},
    }


class _FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, **kwargs):
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = dict(kwargs)

    def get_object(self, *, Bucket, Key):
        try:
            value = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise _MissingObject() from exc
        return {"Body": io.BytesIO(value["Body"])}

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


def test_local_storage_round_trip_and_path_guard(tmp_path):
    store = LocalEvidenceStorage(tmp_path / "evidence")
    store.put("photo.jpg", b"photo", "image/jpeg")
    assert store.get("photo.jpg") == b"photo"

    with pytest.raises(ValueError):
        store.get("../escape.jpg")

    store.delete("photo.jpg")
    with pytest.raises(FileNotFoundError):
        store.get("photo.jpg")


def test_s3_storage_round_trip_metadata_and_delete():
    client = _FakeS3()
    store = S3EvidenceStorage(client=client, bucket="warehouse", prefix="evidence")
    store.put("arrival.jpg", b"photo", "image/jpeg")

    uploaded = client.objects[("warehouse", "evidence/arrival.jpg")]
    assert uploaded["ContentType"] == "image/jpeg"
    assert uploaded["Metadata"]["sha256"] == hashlib.sha256(b"photo").hexdigest()
    assert store.get("arrival.jpg") == b"photo"

    store.delete("arrival.jpg")
    with pytest.raises(FileNotFoundError):
        store.get("arrival.jpg")


def test_s3_backend_requires_bucket_configuration(monkeypatch):
    monkeypatch.setenv("EVIDENCE_STORAGE_BACKEND", "s3")
    for name in ("EVIDENCE_S3_BUCKET", "AWS_S3_BUCKET_NAME", "BUCKET"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(EvidenceUnavailableError, match="bucket variable"):
        evidence_storage()
