"""Build and export role images tied to a clean Git revision."""

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKER = shutil.which("docker") or "/Applications/Docker.app/Contents/Resources/bin/docker"


def run(*args):
    return subprocess.check_output(args, cwd=ROOT, text=True).strip()


def main():
    if run("git", "status", "--porcelain"):
        raise RuntimeError("Commit source changes before exporting submission images")
    revision = run("git", "rev-parse", "HEAD")
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    manifest = {"git_commit": revision, "built_at": datetime.now(timezone.utc).isoformat(), "images": {}}
    for role in ("sender", "receiver"):
        tag = "semantic-agent-%s:%s" % (role, revision[:12])
        subprocess.run([DOCKER, "buildx", "build", "--platform", "linux/amd64",
                        "--build-arg", "AGENT_ROLE=" + role, "--build-arg", "SOURCE_REVISION=" + revision,
                        "--load", "-t", tag, "-t", "semantic-agent-%s:dev" % role, "."], cwd=ROOT, check=True)
        info = json.loads(run(DOCKER, "image", "inspect", tag))[0]
        assert info["Architecture"] == "amd64" and info["Os"] == "linux"
        assert info["Config"]["User"] == "agent"
        archive = output / (role + "-image.tar")
        subprocess.run([DOCKER, "save", "-o", str(archive), tag], check=True)
        digest = hashlib.sha256()
        with archive.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        manifest["images"][role] = {"tag": tag, "image_id": info["Id"], "file": archive.name,
                                    "bytes": archive.stat().st_size, "sha256": digest.hexdigest()}
    path = output / "submission-manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
