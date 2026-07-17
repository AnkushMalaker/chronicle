"""Run a shell command on the Jarvis VM via the Jupyter kernel API (SSH-free).

SSH to the instance is currently throttled/blocked, but the Jupyter server is
reachable. This opens a kernel, executes a python snippet that shells out, streams
stdout back, and tears the kernel down.

Usage:
    JLTOKEN=... python vm_exec.py "<shell command>"
It resolves the instance URL+token via jlclient automatically.
"""

import json
import os
import ssl
import sys
import time
import uuid

import requests
from jlclient import jarvisclient
from jlclient.jarvisclient import *
from websocket import create_connection

jarvisclient.token = os.environ["JLTOKEN"]
url = User.get_instances()[0].url  # https://<host>/lab?token=<tok>
host = url.split("//")[1].split("/")[0]
tok = url.split("token=")[1]
base = f"https://{host}"

cmd = sys.argv[1]
code = (
    "import subprocess;"
    f"r=subprocess.run({cmd!r}, shell=True, capture_output=True, text=True);"
    "print(r.stdout);"
    "print(r.stderr) if r.stderr else None"
)

r = requests.post(f"{base}/api/kernels", params={"token": tok}, timeout=30)
kid = r.json()["id"]
try:
    ws = create_connection(
        f"wss://{host}/api/kernels/{kid}/channels?token={tok}",
        sslopt={"cert_reqs": ssl.CERT_NONE},
        timeout=30,
    )
    msg_id = uuid.uuid4().hex
    hdr = {
        "msg_id": msg_id,
        "username": "u",
        "session": uuid.uuid4().hex,
        "msg_type": "execute_request",
        "version": "5.3",
    }
    ws.send(
        json.dumps(
            {
                "header": hdr,
                "parent_header": {},
                "metadata": {},
                "content": {
                    "code": code,
                    "silent": False,
                    "store_history": False,
                    "user_expressions": {},
                    "allow_stdin": False,
                    "stop_on_error": True,
                },
                "channel": "shell",
            }
        )
    )
    out = []
    t0 = time.time()
    while time.time() - t0 < 600:
        m = json.loads(ws.recv())
        if m.get("parent_header", {}).get("msg_id") != msg_id:
            continue
        mt = m.get("msg_type")
        if mt == "stream":
            out.append(m["content"]["text"])
        elif mt in ("execute_result", "display_data"):
            out.append(str(m["content"]["data"].get("text/plain", "")))
        elif mt == "error":
            out.append("\n".join(m["content"]["traceback"]))
        elif mt == "status" and m["content"]["execution_state"] == "idle":
            break
    ws.close()
    print("".join(out))
finally:
    requests.delete(f"{base}/api/kernels/{kid}", params={"token": tok}, timeout=30)
