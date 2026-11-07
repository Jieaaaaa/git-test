"""Check real containers and OpenAI SDK against a local stub, through a lossy UDP relay."""

import argparse
import base64
import json
import shutil
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKER = shutil.which("docker") or "/Applications/Docker.app/Contents/Resources/bin/docker"


def docker(*args):
    return subprocess.check_output([DOCKER, *args], text=True).strip()


def get(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="dev")
    parser.add_argument("--semantic-mode", choices=("aggressive", "lossless"), default="aggressive")
    parser.add_argument("--critic-reject", action="store_true")
    parser.add_argument("--drop-data-ack", action="store_true")
    args = parser.parse_args()
    network = "semantic-smoke-" + uuid.uuid4().hex[:12]
    containers = []
    docker("network", "create", network)
    try:
        for role in ("sender", "receiver", "fixture"):
            image_role = "receiver" if role == "fixture" else role
            command = ["run", "-d", "--platform", "linux/amd64", "--network", network,
                       "--network-alias", "smoke-" + role, "--cap-drop=ALL"]
            if role == "fixture":
                command += ["-v", str(ROOT / "tests/smoke_fixture.py") + ":/fixture.py:ro",
                            "-p", "127.0.0.1::8000", "--entrypoint", "python"]
                if args.critic_reject:
                    command += ["-e", "SMOKE_CRITIC_REJECT=1"]
                if args.drop_data_ack:
                    command += ["-e", "SMOKE_DROP_DATA=1", "-e", "SMOKE_DROP_ACK=1"]
            else:
                command += ["-e", "UDP_PEER_HOST=smoke-fixture", "-e",
                            "UDP_PEER_PORT=" + ("9001" if role == "sender" else "9002"),
                            "-p", "127.0.0.1::8080"]
                command += ["-e", "PLATFORM_LLM_URL=http://smoke-fixture:8000/v1",
                            "-e", "PLATFORM_API_KEY=local-mock-only", "-e", "SEMANTIC_MODE=" + args.semantic_mode]
            command += ["semantic-agent-" + image_role + ":" + args.tag]
            if role == "fixture":
                command += ["/fixture.py"]
            containers.append(docker(*command))
        addresses = []
        for container, port in zip(containers, (8080, 8080, 8000)):
            address = docker("port", container, str(port) + "/tcp")
            addresses.append("http://" + address)
        for address, role in zip(addresses[:2], ("sender", "receiver")):
            for attempt in range(60):
                try:
                    health = get(address + "/health")
                    assert health["role"] == role and health["status"] == "ok", health
                    break
                except (OSError, ValueError):
                    time.sleep(0.2)
            else:
                raise RuntimeError("health failed: " + role)
        assert get(addresses[1] + "/result/never-seen")["status"] == "pending"
        article = "Grant decisions in 2025.\n" + "\n".join(
            "Agency%d received %d units in 2025. The alternative proposal was rejected. "
            "The following narrative repeats the context without changing the preceding factual decision. "
            "The committee published the described decision in its ordinary report." % (i, 100 + i)
            for i in range(18))
        task_id = "local-lossy-sdk-smoke"
        started = time.monotonic()
        get(addresses[0] + "/send", {"task_id": task_id,
            "payload_b64": base64.b64encode(article.encode()).decode(), "chunk_size": 512, "deadline_ms": 45000})
        while time.monotonic() - started < 45:
            result = get(addresses[1] + "/result/" + task_id)
            if result["status"] in ("success", "error"):
                break
            time.sleep(0.1)
        assert result["status"] == "success", result
        content = result["reconstructed"]["content"]
        assert "SCNSEM1" not in content, content
        for i in range(18):
            assert "Agency%d: %d units in 2025" % (i, 100 + i) in content, content
        stats = get(addresses[2] + "/stats")
        assert stats["model_calls"] >= 2, stats
        assert stats["max_datagram"] <= 1400 and stats["udp_bytes"] <= 16 * 1024 * 1024, stats
        assert stats["dropped"] > 0, stats
        if args.semantic_mode == "aggressive":
            assert stats["sender_model_calls"] > 0, stats
        if args.drop_data_ack:
            assert stats["data_dropped"] > 0 and stats["ack_dropped"] > 0, stats
        print(json.dumps({"status": "passed", "model": "local mock, not official Gateway",
                          "semantic_mode": args.semantic_mode, "critic_reject": args.critic_reject,
                          "elapsed_seconds": round(time.monotonic() - started, 2),
                          "source_bytes": len(article.encode()), "result_bytes": len(content.encode()),
                          **stats}, ensure_ascii=False, indent=2))
    except Exception:
        for container in containers:
            subprocess.run([DOCKER, "logs", "--tail", "25", container], check=False)
        raise
    finally:
        for container in containers:
            subprocess.run([DOCKER, "rm", "-f", container], stdout=subprocess.DEVNULL, check=False)
        subprocess.run([DOCKER, "network", "rm", network], stdout=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    main()
