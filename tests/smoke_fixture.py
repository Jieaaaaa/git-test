"""Development-only model stub and lossy UDP relay; never included in submission images."""

import json
import os
import random
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


stats = {"model_calls": 0, "sender_model_calls": 0, "guide_packets": 0, "udp_bytes": 0, "max_datagram": 0, "dropped": 0,
         "data_dropped": 0, "ack_dropped": 0}
lock = threading.Lock()


def encode_facts(text):
    facts = re.findall(r"Agency(\d+) received (\d+) units in (\d+)", text)
    if not facts:
        facts = re.findall(r"Agency(\d+): (\d+) units in (\d+)", text)
    return "; ".join("Agency%s: %s units in %s; alternative rejected" % fact for fact in facts) or "No additional facts."


def encode_packet(text):
    facts = re.findall(r"Agency(\d+) received (\d+) units in (\d+)", text)
    if not facts:
        facts = re.findall(r"Agency(\d+): (\d+) units in (\d+)", text)
    groups = [{"subject": "Agency" + agency, "facts": [units + " units in " + year,
               "alternative rejected"]} for agency, units, year in facts]
    return json.dumps({"groups": groups or [{"subject": "Report", "facts": ["No additional facts"]}]})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond(stats)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        system, user = [message["content"] for message in body["messages"][:2]]
        if "entity/layout planner" in system:
            content = '{"layout":"ledger","entities":[]}'
        elif "fragile-fact sentinel" in system:
            content = json.dumps({"anchors": ["Agency0 received 100 units in 2025."]})
        elif "You verify a semantic" in system:
            if os.getenv("SMOKE_SLOW_REVIEW") == "1":
                time.sleep(60)
            verdict = "reject" if os.getenv("SMOKE_CRITIC_REJECT") == "1" else "accept"
            name = re.search(r"Agency\d+", user).group()
            checks = [{"source_quote": name, "candidate_quote": name}]
            if "TOOL OBSERVATION:\n" in user:
                observation = json.loads(user.split("TOOL OBSERVATION:\n", 1)[1])
                checks += [{**item, "candidate_quotes": ["alternative rejected"]}
                           for item in observation.get("required_checks", [])]
            content = json.dumps({"verdict": verdict, "repairs": [], "risk": "local fixture decision",
                                  "checks": checks})
        elif "sending semantic communication agent" in system:
            target = user.split("TARGET SECTION:\n", 1)[1].split("\n\nTOOL OBSERVATION", 1)[0]
            content = json.dumps({"strategy": "entity ledger", "content": encode_facts(target), "risk": "none"})
        elif "FACT_PACKET_V1" in system:
            if user.startswith("SOURCE:\n"):
                source = user.split("SOURCE:\n", 1)[1].split("\n\nOPTIONAL SOURCE GUIDE", 1)[0]
            else:
                source = user.split("TARGET SECTION:\n", 1)[1].split("\nFOLLOWING CONTEXT", 1)[0]
            content = encode_packet(source)
        elif "Act as a semantic communication agent" in system:
            source = user.split("SOURCE:\n", 1)[1].split("\n\nCURRENT RECORD", 1)[0]
            content = json.dumps({"action": "propose", "strategy": "entity ledger", "content": encode_facts(source)})
        elif "Audit the factual" in system:
            content = '{"patches":[]}'
        elif "independent, concise coverage" in system:
            pieces = re.split(r"SECTION \d+:\n", user)[1:]
            content = json.dumps({"records": [encode_facts(piece) for piece in pieces]})
        elif "receiving semantic compiler" in system:
            source = user.split("SOURCE (", 1)[1].split("\n\nSOURCE-ALIGNED", 1)[0]
            source = source.split("bytes):\n", 1)[1]
            content = encode_facts(source)
        else:
            target = user.split("TARGET SECTION:\n", 1)[1].split("\nFOLLOWING CONTEXT", 1)[0]
            content = encode_facts(target)
            if "compact factual record" not in system:
                content += " The report records those agency decisions and their rejected alternatives."
        time.sleep(0.1)
        with lock:
            stats["model_calls"] += 1
            if ("entity/layout planner" in system or "fragile-fact sentinel" in system
                    or "sending semantic communication agent" in system):
                stats["sender_model_calls"] += 1
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
    packets = 0
    forced_data_drop = False
    forced_ack_drop = False
    while True:
        data, address = transport.recvfrom(65536)
        packets += 1
        with lock:
            stats["udp_bytes"] += len(data)
            stats["max_datagram"] = max(stats["max_datagram"], len(data))
        kind = data[5] if len(data) >= 6 else 0
        if kind == 9:
            with lock:
                stats["guide_packets"] += 1
        force_data = os.getenv("SMOKE_DROP_DATA") == "1" and kind in (2, 5, 7) and not forced_data_drop
        force_ack = os.getenv("SMOKE_DROP_ACK") == "1" and kind in (4, 8) and not forced_ack_drop
        if force_data:
            forced_data_drop = True
        if force_ack:
            forced_ack_drop = True
        if force_data or force_ack or packets == 2 or rng.random() < float(os.getenv("SMOKE_LOSS", "0.12")):
            with lock:
                stats["dropped"] += 1
                if kind in (2, 5, 7):
                    stats["data_dropped"] += 1
                if kind in (4, 8):
                    stats["ack_dropped"] += 1
            continue
        delay = rng.uniform(0.01, 0.07)
        threading.Timer(delay, transport.sendto, args=(data, (target, 9000))).start()
        if rng.random() < 0.03:
            threading.Timer(delay + 0.02, transport.sendto, args=(data, (target, 9000))).start()


if __name__ == "__main__":
    threading.Thread(target=relay, args=(9001, "smoke-receiver", 19), daemon=True).start()
    threading.Thread(target=relay, args=(9002, "smoke-sender", 37), daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
