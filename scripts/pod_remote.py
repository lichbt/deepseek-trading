#!/usr/bin/env python3
"""Run one command on the Zeabur host over SSH WITHOUT a pty.

Same channel as scripts/zeabur_interlock.sh remote(), which uses expect and so needs
a pty. Where ptys cannot be spawned (no more ptys / openpty: Operation not permitted)
that path is dead; this drives the identical k3s commands via paramiko.
Credentials come from .env (USERNAME/PASSWORD/IP), never the command line.
"""
import sys
from pathlib import Path

import paramiko

ENV = Path("/Users/lich/deepseek-oanda-trading/.env")


def env(key):
    for line in ENV.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("missing " + key)


cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(env("IP"), username=env("USERNAME"), password=env("PASSWORD"), timeout=30)
_, out, err = cli.exec_command(sys.argv[1], timeout=300)
stdout = out.read().decode(errors="replace")
stderr = err.read().decode(errors="replace")
rc = out.channel.recv_exit_status()
sys.stdout.write(stdout)
if stderr:
    sys.stderr.write(stderr)
cli.close()
sys.exit(rc)
