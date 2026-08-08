"""Content-addressed local artifact store; S3-compatible storage can mirror it."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


def _sha256_file(source: Path) -> str:
    hasher = hashlib.sha256()
    with source.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk: break
            hasher.update(chunk)
    return hasher.hexdigest()


class ArtifactStore:
    def __init__(self, root: Path):
        self.root = root

    def put_json(self, run_id: str, epoch: int, agent_id: str, name: str, payload: dict[str, Any]) -> dict[str, str]:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        checksum = hashlib.sha256(encoded).hexdigest()
        path = self.root / run_id / f"epoch-{epoch:04d}" / agent_id / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded)
        return {"uri": str(path), "sha256": checksum}

    def put_file(self, run_id: str, epoch: int, agent_id: str, name: str, source: Path) -> dict[str, str]:
        checksum = _sha256_file(source)
        path = self.root / run_id / f"epoch-{epoch:04d}" / agent_id / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as input_stream, path.open("wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
        return {"uri": str(path), "sha256": checksum}

    def delete_run(self, run_id: str) -> None:
        # Local artifacts live below the run directory and are removed with
        # that directory by Kernel.reset.
        return None

    def export_run(self, run_id: str, target: Path) -> None:
        source = self.root / run_id
        if source.exists(): shutil.copytree(source, target, dirs_exist_ok=True)

    def inherit_from(self, source: "ArtifactStore", source_run: str, parent_id: str, target_run: str, child_id: str) -> None:
        source_run_root = source.root / source_run
        if not source_run_root.exists(): return
        destination = self.root / target_run / "inherited" / source_run / child_id
        for epoch in source_run_root.glob("epoch-*"):
            parent = epoch / parent_id
            if parent.exists(): shutil.copytree(parent, destination / epoch.name, dirs_exist_ok=True)


class S3ArtifactStore:
    """S3/MinIO artifact backend used by containerized control runs."""

    def __init__(self, endpoint_url: str, bucket: str, access_key: str, secret_key: str):
        try:
            import boto3
            from botocore.exceptions import ClientError
        except ImportError as exc:
            raise RuntimeError("S3 artifact storage selected but boto3 is not installed") from exc
        self.client = boto3.client("s3", endpoint_url=endpoint_url, aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name="us-east-1")
        self.bucket, self.client_error = bucket, ClientError
        self._bucket_ready = False

    def _ensure_bucket(self) -> None:
        if self._bucket_ready: return
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except self.client_error:
            self.client.create_bucket(Bucket=self.bucket)
        self._bucket_ready = True

    def put_json(self, run_id: str, epoch: int, agent_id: str, name: str, payload: dict[str, Any]) -> dict[str, str]:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        checksum = hashlib.sha256(encoded).hexdigest()
        self._ensure_bucket()
        key = f"{run_id}/epoch-{epoch:04d}/{agent_id}/{name}"
        self.client.put_object(Bucket=self.bucket, Key=key, Body=encoded, ContentType="application/json", Metadata={"sha256": checksum})
        return {"uri": f"s3://{self.bucket}/{key}", "sha256": checksum}

    def put_file(self, run_id: str, epoch: int, agent_id: str, name: str, source: Path) -> dict[str, str]:
        checksum = _sha256_file(source)
        self._ensure_bucket()
        key = f"{run_id}/epoch-{epoch:04d}/{agent_id}/{name}"
        self.client.upload_file(str(source), self.bucket, key, ExtraArgs={"Metadata": {"sha256": checksum}})
        return {"uri": f"s3://{self.bucket}/{key}", "sha256": checksum}

    def delete_run(self, run_id: str) -> None:
        prefix = f"{run_id}/"
        continuation: str | None = None
        while True:
            request: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000}
            if continuation: request["ContinuationToken"] = continuation
            try:
                page = self.client.list_objects_v2(**request)
            except self.client_error as exc:
                if exc.response.get("Error", {}).get("Code") in {"NoSuchBucket", "404"}: return
                raise
            objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            if objects:
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": objects, "Quiet": True})
            if not page.get("IsTruncated"): return
            continuation = page.get("NextContinuationToken")

    def export_run(self, run_id: str, target: Path) -> None:
        prefix = f"{run_id}/"; continuation: str | None = None
        while True:
            request: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000}
            if continuation: request["ContinuationToken"] = continuation
            page = self.client.list_objects_v2(**request)
            for item in page.get("Contents", []):
                relative = item["Key"][len(prefix):]
                destination = target / relative; destination.parent.mkdir(parents=True, exist_ok=True)
                self.client.download_file(self.bucket, item["Key"], str(destination))
            if not page.get("IsTruncated"): return
            continuation = page.get("NextContinuationToken")

    def inherit_from(self, source: "S3ArtifactStore", source_run: str, parent_id: str, target_run: str, child_id: str) -> None:
        prefix = f"{source_run}/"; continuation: str | None = None
        while True:
            request: dict[str, Any] = {"Bucket": source.bucket, "Prefix": prefix, "MaxKeys": 1000}
            if continuation: request["ContinuationToken"] = continuation
            page = source.client.list_objects_v2(**request)
            for item in page.get("Contents", []):
                relative = item["Key"][len(prefix):]
                parts = Path(relative).parts
                if len(parts) < 3 or parts[1] != parent_id: continue
                destination = f"{target_run}/inherited/{source_run}/{child_id}/{parts[0]}/" + "/".join(parts[2:])
                self.client.copy_object(Bucket=self.bucket, Key=destination, CopySource={"Bucket": source.bucket, "Key": item["Key"]})
            if not page.get("IsTruncated"): return
            continuation = page.get("NextContinuationToken")
