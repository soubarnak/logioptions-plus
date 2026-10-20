#!/usr/bin/env python3
"""Dev-only traffic tap: UI <-> /tmp unix socket <-> TCP (pipebridge in Wine) <-> real agent.

Every decoded JSON message is appended to a JSONL capture with its direction.
usage: tap.py <tcp-port> <capture.jsonl>
"""
import asyncio
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from opl_agent.ipc import socket_path  # noqa: E402

PORT = int(sys.argv[1])
OUT = open(sys.argv[2], "a", buffering=1)


class Decoder:
    def __init__(self, direction):
        self.buf = b""
        self.direction = direction

    def feed(self, data):
        self.buf += data
        while len(self.buf) >= 8:
            plen = struct.unpack(">I", self.buf[4:8])[0]
            if len(self.buf) < 8 + plen + 4:
                return
            blen = struct.unpack(">I", self.buf[8 + plen:12 + plen])[0]
            end = 12 + plen + blen
            if len(self.buf) < end:
                return
            body = self.buf[12 + plen:end]
            self.buf = self.buf[end:]
            try:
                msg = json.loads(body)
            except ValueError:
                msg = {"raw": body.decode("latin1")}
            OUT.write(json.dumps({"t": round(time.time(), 3), "dir": self.direction, "msg": msg}) + "\n")


async def pump(reader, writer, dec):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            dec.feed(data)
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()


async def handle(ui_r, ui_w):
    print("ui connected", flush=True)
    ag_r, ag_w = await asyncio.open_connection("127.0.0.1", PORT)
    await asyncio.gather(pump(ui_r, ag_w, Decoder("ui->agent")), pump(ag_r, ui_w, Decoder("agent->ui")))


async def main():
    path = socket_path()
    if os.path.exists(path):
        os.unlink(path)
    srv = await asyncio.start_unix_server(handle, path=path)
    print("tap listening", path, flush=True)
    async with srv:
        await srv.serve_forever()


asyncio.run(main())
