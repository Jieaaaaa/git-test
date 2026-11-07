"""Assemble two OCI images from the supplied OCI base without a daemon."""

import gzip
import hashlib
import io
import json
import os
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
BASE = ROOT / "agenthack-base-1.1.1"
DIST = ROOT / "dist"
FILES = ("agent_app.py", "wire.py")
ROLE_IMAGES = {
    "sender": "semantic-agent-sender:dev",
    "receiver": "semantic-agent-receiver:dev",
}


def blob_path(layout, digest):
    algorithm, value = digest.split(":", 1)
    if algorithm != "sha256":
        raise ValueError("only sha256 OCI blobs are supported")
    return layout / "blobs" / algorithm / value


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest_bytes(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def descriptor(media_type, content):
    return {"mediaType": media_type, "digest": digest_bytes(content), "size": len(content)}


def base_image():
    index = json.loads((BASE / "index.json").read_text("utf-8"))
    nested_index = None
    for item in index["manifests"]:
        candidate = json.loads(blob_path(BASE, item["digest"]).read_text("utf-8"))
        if candidate.get("mediaType") == "application/vnd.oci.image.index.v1+json":
            nested_index = candidate
            break
    if nested_index is None:
        raise ValueError("base OCI image index was not found")
    image_desc = next(
        item for item in nested_index["manifests"]
        if item.get("platform", {}).get("os") == "linux"
        and item.get("platform", {}).get("architecture") == "amd64"
    )
    image_manifest = json.loads(blob_path(BASE, image_desc["digest"]).read_text("utf-8"))
    config = json.loads(blob_path(BASE, image_manifest["config"]["digest"]).read_text("utf-8"))
    if config.get("architecture") != "amd64" or config.get("os") != "linux":
        raise ValueError("base image architecture is not linux/amd64")
    if config.get("config", {}).get("User") != "agent":
        raise ValueError("base image does not use the expected non-root agent user")
    return image_manifest, config


def app_layer():
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for filename in FILES:
            source = ROOT / filename
            info = tarfile.TarInfo("app/" + filename)
            info.size = source.stat().st_size
            info.mode = 0o644
            info.uid = info.gid = 1000
            info.uname = info.gname = "agent"
            info.mtime = 0
            with source.open("rb") as stream:
                archive.addfile(info, stream)
    raw_tar = output.getvalue()
    return raw_tar, gzip.compress(raw_tar, compresslevel=9, mtime=0)


def copy_base_blobs(layout, image_manifest, config):
    digests = [image_manifest["config"]["digest"]]
    digests.extend(layer["digest"] for layer in image_manifest["layers"])
    for digest in digests:
        source = blob_path(BASE, digest)
        target = blob_path(layout, digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
        except FileExistsError:
            pass
        except OSError:
            shutil.copyfile(source, target)


def build_one(role, image_name, base_manifest, base_config, raw_layer, compressed_layer):
    layout = DIST / role
    if layout.exists():
        shutil.rmtree(layout)
    layout.mkdir(parents=True)
    (layout / "blobs" / "sha256").mkdir(parents=True)
    (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}\n', "utf-8")
    copy_base_blobs(layout, base_manifest, base_config)

    config = json.loads(json.dumps(base_config))
    runtime_config = config.setdefault("config", {})
    runtime_config["User"] = "agent"
    runtime_config["WorkingDir"] = "/app"
    runtime_config["Cmd"] = ["python", "/app/agent_app.py"]
    runtime_config["Env"] = [entry for entry in runtime_config.get("Env", [])
                              if not entry.startswith("AGENT_ROLE=")]
    runtime_config["Env"].append("AGENT_ROLE=" + role)
    exposed = runtime_config.setdefault("ExposedPorts", {})
    exposed["8080/tcp"] = {}
    exposed["9000/udp"] = {}
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    layer_digest = digest_bytes(compressed_layer)
    diff_id = digest_bytes(raw_layer)
    config.setdefault("rootfs", {}).setdefault("diff_ids", []).append(diff_id)
    config.setdefault("history", []).append({
        "created": now,
        "created_by": "COPY --chown=agent:agent agent_app.py wire.py /app",
    })
    config_blob = canonical_json(config)
    config_desc = descriptor("application/vnd.oci.image.config.v1+json", config_blob)
    config_path = blob_path(layout, config_desc["digest"])
    config_path.write_bytes(config_blob)

    layer_path = blob_path(layout, layer_digest)
    layer_path.write_bytes(compressed_layer)
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": config_desc,
        "layers": list(base_manifest["layers"]) + [{
            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
            "digest": layer_digest,
            "size": len(compressed_layer),
        }],
    }
    manifest_blob = canonical_json(manifest)
    manifest_desc = descriptor("application/vnd.oci.image.manifest.v1+json", manifest_blob)
    blob_path(layout, manifest_desc["digest"]).write_bytes(manifest_blob)
    (layout / "index.json").write_bytes(canonical_json({
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [dict(manifest_desc,
                            platform={"architecture": "amd64", "os": "linux"},
                            annotations={"org.opencontainers.image.ref.name": image_name})],
    }))
    return layout


def validate_layout(layout, expected_role):
    index = json.loads((layout / "index.json").read_text("utf-8"))
    if index.get("schemaVersion") != 2 or len(index.get("manifests", [])) != 1:
        raise ValueError("invalid output OCI index")
    descriptor_item = index["manifests"][0]
    manifest_blob = blob_path(layout, descriptor_item["digest"]).read_bytes()
    if digest_bytes(manifest_blob) != descriptor_item["digest"]:
        raise ValueError("manifest digest mismatch")
    manifest = json.loads(manifest_blob)
    config_blob = blob_path(layout, manifest["config"]["digest"]).read_bytes()
    if digest_bytes(config_blob) != manifest["config"]["digest"]:
        raise ValueError("config digest mismatch")
    config = json.loads(config_blob)
    if config["config"]["User"] != "agent":
        raise ValueError("image is not configured for non-root execution")
    if "AGENT_ROLE=" + expected_role not in config["config"]["Env"]:
        raise ValueError("role configuration mismatch")
    if config["architecture"] != "amd64" or config["os"] != "linux":
        raise ValueError("output image architecture mismatch")
    if len(config["rootfs"]["diff_ids"]) != len(manifest["layers"]):
        raise ValueError("rootfs layer count mismatch")
    for layer, diff_id in zip(manifest["layers"], config["rootfs"]["diff_ids"]):
        content = blob_path(layout, layer["digest"]).read_bytes()
        if digest_bytes(content) != layer["digest"]:
            raise ValueError("layer digest mismatch")
        raw = gzip.decompress(content)
        if digest_bytes(raw) != diff_id:
            raise ValueError("layer diff_id mismatch")
    return descriptor_item["digest"]


def archive_layout(layout, output):
    with tarfile.open(output, "w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(layout.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=str(path.relative_to(layout)), recursive=False)


def validate_archive(path, expected_role):
    with tarfile.open(path, "r") as archive:
        def read_member(name):
            stream = archive.extractfile(name)
            if stream is None:
                raise ValueError("missing OCI archive entry: %s" % name)
            return stream.read()

        index = json.loads(read_member("index.json"))
        if index.get("schemaVersion") != 2 or len(index.get("manifests", [])) != 1:
            raise ValueError("invalid archived OCI index")
        image_desc = index["manifests"][0]
        if image_desc.get("platform") != {"architecture": "amd64", "os": "linux"}:
            raise ValueError("archived image platform mismatch")
        manifest_blob = read_member("blobs/sha256/" + image_desc["digest"].split(":", 1)[1])
        if digest_bytes(manifest_blob) != image_desc["digest"]:
            raise ValueError("archived manifest digest mismatch")
        manifest = json.loads(manifest_blob)
        config_blob = read_member("blobs/sha256/" + manifest["config"]["digest"].split(":", 1)[1])
        if digest_bytes(config_blob) != manifest["config"]["digest"]:
            raise ValueError("archived config digest mismatch")
        config = json.loads(config_blob)
        if config["architecture"] != "amd64" or config["os"] != "linux":
            raise ValueError("archived config platform mismatch")
        if config["config"]["User"] != "agent":
            raise ValueError("archived image is not non-root")
        if "AGENT_ROLE=" + expected_role not in config["config"]["Env"]:
            raise ValueError("archived image role mismatch")
        if len(config["rootfs"]["diff_ids"]) != len(manifest["layers"]):
            raise ValueError("archived rootfs layer count mismatch")
        for layer in manifest["layers"]:
            content = read_member("blobs/sha256/" + layer["digest"].split(":", 1)[1])
            if len(content) != layer["size"] or digest_bytes(content) != layer["digest"]:
                raise ValueError("archived layer digest mismatch")
    return image_desc["digest"]


def main():
    base_manifest, base_config = base_image()
    raw_layer, compressed_layer = app_layer()
    DIST.mkdir(exist_ok=True)
    for role, image_name in ROLE_IMAGES.items():
        layout = build_one(role, image_name, base_manifest, base_config, raw_layer, compressed_layer)
        manifest_digest = validate_layout(layout, role)
        output = DIST / ("semantic-agent-%s.oci.tar" % role)
        if output.exists():
            output.unlink()
        archive_layout(layout, output)
        archived_digest = validate_archive(output, role)
        if archived_digest != manifest_digest:
            raise ValueError("archive manifest differs from its OCI layout")
        print("%s: %s (manifest %s)" % (output, image_name, manifest_digest))


if __name__ == "__main__":
    main()
