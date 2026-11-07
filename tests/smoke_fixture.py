"""Development-only model stub and lossy UDP relay; never included in submission images."""

import json
import os
import random
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


stats = {"model_calls": 0, "udp_bytes": 0, "max_datagram": 0, "dropped": 0}
lock = threading.Lock()


def encode_facts(text):
    facts = re.findall(r"Agency(\d+) received (\d+) units in (\d+)", text)
    return "; ".join("Agency%s: %s units in %s; alternative rejected" % fact for fact in facts) or "No additional facts."


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond(stats)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        system, user = [message["content"] for message in body["messages"]]
        if "Audit the factual" in system:
            content = '{"patches":[]}'
        elif "independent, concise coverage" in system:
            pieces = re.split(r"SECTION \d+:\n", user)[1:]
            content = json.dumps({"records": [encode_facts(piece) for piece in pieces]})
        else:
            target = user.split("TARGET SECTION:\n", 1)[1].split("\nFOLLOWING CONTEXT", 1)[0]
            content = encode_facts(target)
        time.sleep(0.1)
        with lock:
            stats["model_calls"] += 1
        self.respond({"id": "local-mock", "object": "chat.completion", "created": int(time.time()),
                      "model": body["model"], "choices": [{"index": 0, "finish_reason": "stop",
                      "message": {"role": "assistant", "content": content}}],
                      "usage": {"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200}})

    def respond(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def relay(port, target, seed):
    rng = random.Random(seed)
    transport = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    transport.bind(("0.0.0.0", port))
    while True:
        data, address = transport.recvfrom(65536)
        with lock:
            stats["udp_bytes"] += len(data)
            stats["max_datagram"] = max(stats["max_datagram"], len(data))
        if rng.random() < float(os.getenv("SMOKE_LOSS", "0.12")):
            with lock:
                stats["dropped"] += 1
            continue
        delay = rng.uniform(0.01, 0.07)
        threading.Timer(delay, transport.sendto, args=(data, (target, 9000))).start()
        if rng.random() < 0.03:
            threading.Timer(delay + 0.02, transport.sendto, args=(data, (target, 9000))).start()


if __name__ == "__main__":
    threading.Thread(target=relay, args=(9001, "smoke-receiver", 19), daemon=True).start()
    threading.Thread(target=relay, args=(9002, "smoke-sender", 37), daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
