"""Ensure SSH jump tunnel: local SOCKS port -> relay-15 -> 103.227.166.183:9498."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_TUNNEL = {
    "enabled": True,
    "ssh_host": "relay-15",
    "local_host": "127.0.0.1",
    "local_port": 19498,
    "remote_host": "103.227.166.183",
    "remote_port": 9498,
    "identity_file": os.path.expanduser("~/.ssh/vps_154"),
    "pid_path": "logs/socks-jump-tunnel.pid",
    "status_path": "logs/socks-jump-tunnel.json",
}


def load_tunnel_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(DEFAULT_TUNNEL)
    if raw:
        cfg.update(raw)
    return cfg


def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def read_pid(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                check=False,
            )
            return str(pid) in result.stdout and "ssh.exe" in result.stdout.lower()
        except OSError:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_tunnel(pid_path: Path) -> dict[str, Any]:
    pid = read_pid(pid_path)
    if pid is None:
        pid_path.unlink(missing_ok=True)
        return {"stopped": False, "reason": "no_pid_file"}
    if not pid_alive(pid):
        pid_path.unlink(missing_ok=True)
        return {"stopped": False, "reason": "stale_pid", "pid": pid}
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False, capture_output=True)
    else:
        os.kill(pid, 15)
    pid_path.unlink(missing_ok=True)
    return {"stopped": True, "pid": pid}


def start_tunnel(cfg: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    root = Path.cwd()
    pid_path = root / str(cfg["pid_path"])
    status_path = root / str(cfg["status_path"])
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    local_host = str(cfg["local_host"])
    local_port = int(cfg["local_port"])
    if not force and tcp_open(local_host, local_port):
        existing_pid = read_pid(pid_path)
        status = {
            "running": True,
            "alreadyUp": True,
            "localEndpoint": f"{local_host}:{local_port}",
            "remoteEndpoint": f"{cfg['remote_host']}:{cfg['remote_port']}",
            "sshHost": cfg["ssh_host"],
            "pid": existing_pid,
        }
        status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        return status

    if force:
        stop_tunnel(pid_path)
        time.sleep(0.5)

    forward = f"{local_host}:{local_port}:{cfg['remote_host']}:{int(cfg['remote_port'])}"
    ssh_cmd = [
        "ssh",
        "-N",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "BatchMode=yes",
        "-L",
        forward,
        str(cfg["ssh_host"]),
    ]
    identity = str(cfg.get("identity_file") or "").strip()
    if identity:
        ssh_cmd[1:1] = ["-i", os.path.expanduser(identity)]

    creationflags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(
        ssh_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    pid_path.write_text(str(proc.pid), encoding="ascii")

    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            pid_path.unlink(missing_ok=True)
            raise RuntimeError(f"SSH tunnel exited early with code {proc.returncode}")
        if tcp_open(local_host, local_port):
            status = {
                "running": True,
                "alreadyUp": False,
                "localEndpoint": f"{local_host}:{local_port}",
                "remoteEndpoint": f"{cfg['remote_host']}:{cfg['remote_port']}",
                "sshHost": cfg["ssh_host"],
                "pid": proc.pid,
                "forward": forward,
            }
            status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
            return status
        time.sleep(0.4)

    proc.kill()
    pid_path.unlink(missing_ok=True)
    raise RuntimeError(f"SSH tunnel did not open {local_host}:{local_port} within 15s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure SOCKS jump SSH tunnel is running.")
    parser.add_argument("--config", default="market_autotrader.growth.example.json")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()

    cfg_raw: dict[str, Any] | None = None
    config_path = Path(args.config)
    if config_path.exists():
        import json as json_mod

        with config_path.open("r", encoding="utf-8") as fh:
            full = json_mod.load(fh)
        cfg_raw = full.get("execution", {}).get("ssh_jump_tunnel")

    cfg = load_tunnel_config(cfg_raw)
    pid_path = Path(str(cfg["pid_path"]))

    if args.stop:
        print(json.dumps(stop_tunnel(pid_path), ensure_ascii=False))
        return 0

    if not cfg.get("enabled", True):
        print(json.dumps({"running": False, "enabled": False}, ensure_ascii=False))
        return 0

    try:
        result = start_tunnel(cfg, force=args.restart)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"running": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
