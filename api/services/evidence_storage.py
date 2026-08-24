"""Private evidence-photo storage for warehouse work control.

Local disk remains the safe development default.  Production can select an
S3-compatible Railway Storage Bucket without exposing bucket credentials or
public object URLs to employee devices.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class EvidenceUnavailableError(RuntimeError):
    """Raised when configured evidence storage cannot serve a request."""


class LocalEvidenceStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _target(self, key: str) -> Path:
        target = (self.root / key).resolve()
        if target.parent != self.root:
            raise ValueError("Invalid evidence storage key")
        return target

    def put(self, key: str, data: bytes, content_type: str) -> None:
        del content_type
        self.root.mkdir(parents=True, exist_ok=True)
        self._target(key).write_bytes(data)

    def get(self, key: str) -> bytes:
        target = self._target(key)
        if not target.is_file():
            raise FileNotFoundError(key)
        return target.read_bytes()

    def delete(self, key: str) -> None:
        self._target(key).unlink(missing_ok=True)


class S3EvidenceStorage:
    def __init__(self, *, client, bucket: str, prefix: str = "evidence"):
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    def _object_key(self, key: str) -> str:
        if not key or "/" in key or "\\" in key or key in {".", ".."}:
            raise ValueError("Invalid evidence storage key")
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, data: bytes, content_type: str) -> None:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._object_key(key),
                Body=data,
                ContentType=content_type,
                Metadata={"sha256": hashlib.sha256(data).hexdigest()},
            )
        except Exception as exc:
            raise EvidenceUnavailableError("Evidence bucket upload failed") from exc

    def get(self, key: str) -> bytes:
        try:
            response = self.client.get_object(
                Bucket=self.bucket,
                Key=self._object_key(key),
            )
            return response["Body"].read()
        except Exception as exc:
            response = getattr(exc, "response", {}) or {}
            code = str((response.get("Error") or {}).get("Code", ""))
            status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
            if code in {"NoSuchKey", "404", "NotFound"} or status == 404:
                raise FileNotFoundError(key) from exc
            raise EvidenceUnavailableError("Evidence bucket is unavailable") from exc

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(
                Bucket=self.bucket,
                Key=self._object_key(key),
            )
        except Exception as exc:
            raise EvidenceUnavailableError("Evidence bucket cleanup failed") from exc


def _required_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    raise EvidenceUnavailableError(
        f"Evidence bucket variable is missing: {' or '.join(names)}"
    )


def _s3_client():
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - production dependency guard
        raise EvidenceUnavailableError("boto3 is required for S3 evidence storage") from exc

    endpoint = _required_env("EVIDENCE_S3_ENDPOINT", "AWS_ENDPOINT_URL", "ENDPOINT")
    access_key = _required_env(
        "EVIDENCE_S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID", "ACCESS_KEY_ID"
    )
    secret_key = _required_env(
        "EVIDENCE_S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY", "SECRET_ACCESS_KEY"
    )
    region = (
        os.getenv("EVIDENCE_S3_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or os.getenv("REGION")
        or "auto"
    )
    url_style = (
        os.getenv("EVIDENCE_S3_URL_STYLE")
        or os.getenv("AWS_S3_URL_STYLE")
        or "virtual"
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": url_style}),
    )


def evidence_storage():
    backend = (os.getenv("EVIDENCE_STORAGE_BACKEND") or "local").strip().lower()
    if backend == "local":
        root = os.getenv("EVIDENCE_STORAGE_DIR") or str(
            Path.cwd() / "data" / "evidence"
        )
        return LocalEvidenceStorage(Path(root))
    if backend == "s3":
        bucket = _required_env("EVIDENCE_S3_BUCKET", "AWS_S3_BUCKET_NAME", "BUCKET")
        return S3EvidenceStorage(
            client=_s3_client(),
            bucket=bucket,
            prefix=os.getenv("EVIDENCE_S3_PREFIX", "evidence"),
        )
    raise EvidenceUnavailableError(f"Unsupported evidence storage backend: {backend}")
