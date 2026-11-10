#!/usr/bin/env python3
"""Local WebSocket gateway for ZeroScript Workspace Agent."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import websockets
except ImportError:
    print("[agent] Missing dependency. Run: python -m pip install websockets", file=sys.stderr)
    raise

from zeroscript_agent import __version__
from zeroscript_agent.workspace import ToolError, WorkspaceRuntime


HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
HOST = "127.0.0.1"
DEFAULT_PORT = 17613
PROTOCOL_VERSION = 2


def log(message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def load_config(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            value = json.load(fh)
    except FileNotFoundError:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("config.json must contain a JSON object")
    return value


def resolve_workspace(config: dict[str, Any], override: str | None) -> Path:
    raw = override or os.environ.get("ZS_WORKSPACE") or config.get("workspace", {}).get("root") or "."
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = HERE / path
    return path.resolve(strict=True)


def app_data_dir(workspace: Path) -> Path:
    identity = hashlib.sha256(os.path.normcase(str(workspace)).encode("utf-8")).hexdigest()[:16]
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(HERE / ".agent-data"))) / "ZeroScript-Agent"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "ZeroScript-Agent"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "zeroscript-agent"
    return base / "workspaces" / identity


@dataclass
class PendingApproval:
    token: str
    name: str
    arguments: dict[str, Any]
    fingerprint: str
    created: float
    connection_id: str


class Gateway:
    def __init__(self, runtime: WorkspaceRuntime, config: dict[str, Any]) -> None:
        self.runtime = runtime
        self.config = config
        self.session_id = secrets.token_urlsafe(16)
        self.clients: set[Any] = set()
        self.approvals: dict[str, PendingApproval] = {}
        self.operation_results: dict[str, tuple[float, dict[str, Any]]] = {}
        self._approval_lock = asyncio.Lock()

    def status(self) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "session_id": self.session_id,
            "version": __version__,
            "mcp_alive": True,
            "workspace_ready": True,
            "workspace_name": self.runtime.root.name,
            "runner_ready": self.runtime.runner.get("mode", "host") != "disabled",
            "servers": [{"id": "workspace", "alive": True, "tools": len(self.runtime.tools)}],
            "tools": self.runtime.tools,
            # Compatibility fields for extension builds from before 2.0.
            "studio": True,
            "studio_app": True,
            "studio_proc": True,
        }

    @staticmethod
    def _origin(ws: Any) -> str:
        try:
            return str(ws.request.headers.get("Origin") or "")
        except Exception:
            try:
                return str(ws.request_headers.get("Origin") or "")
            except Exception:
                return ""

    def _origin_allowed(self, ws: Any) -> bool:
        origin = self._origin(ws)
        if not origin:
            return bool(self.config.get("security", {}).get("allow_missing_origin", True))
        return origin.startswith("chrome-extension://") or origin.startswith("moz-extension://")

    @staticmethod
    def _fingerprint(name: str, arguments: dict[str, Any]) -> str:
        payload = json.dumps({"name": name, "arguments": arguments}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def _send(self, ws: Any, payload: dict[str, Any]) -> None:
        payload.setdefault("protocol_version", PROTOCOL_VERSION)
        payload.setdefault("session_id", self.session_id)
        await ws.send(json.dumps(payload, ensure_ascii=False))

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            text = await asyncio.to_thread(self.runtime.call, name, arguments)
            return {"ok": True, "text": text, "images": []}
        except ToolError as exc:
            return {"ok": False, "kind": "tool_error", "error": str(exc)}
        except Exception as exc:
            log(f"unexpected error in {name}: {exc}")
            return {"ok": False, "kind": type(exc).__name__, "error": str(exc)}

    async def _request_approval(
        self,
        ws: Any,
        request_id: Any,
        connection_id: str,
        name: str,
        arguments: dict[str, Any],
        details: dict[str, Any],
    ) -> None:
        token = secrets.token_urlsafe(32)
        pending = PendingApproval(
            token=token,
            name=name,
            arguments=json.loads(json.dumps(arguments)),
            fingerprint=self._fingerprint(name, arguments),
            created=time.monotonic(),
            connection_id=connection_id,
        )
        async with self._approval_lock:
            self._prune_approvals()
            self.approvals[token] = pending
        await self._send(ws, {
            "type": "approval_required",
            "id": request_id,
            "ok": False,
            "kind": "approval_required",
            "approval_token": token,
            "expires_in_ms": 60_000,
            "details": details,
        })

    def _prune_approvals(self) -> None:
        now = time.monotonic()
        for token, pending in list(self.approvals.items()):
            if now - pending.created > 60:
                self.approvals.pop(token, None)
        for key, (created, _result) in list(self.operation_results.items()):
            if now - created > 300:
                self.operation_results.pop(key, None)

    async def _approve(self, ws: Any, message: dict[str, Any], connection_id: str) -> None:
        rid = message.get("id")
        token = str(message.get("approval_token") or "")
        approved = message.get("approved") is True
        async with self._approval_lock:
            self._prune_approvals()
            pending = self.approvals.pop(token, None)
        if not pending:
            await self._send(ws, {"type": "tool_result", "id": rid, "ok": False, "kind": "approval_expired", "error": "approval expired or was already used"})
            return
        if pending.connection_id != connection_id:
            await self._send(ws, {"type": "tool_result", "id": rid, "ok": False, "kind": "approval_invalid", "error": "approval belongs to another connection"})
            return
        if not approved:
            await asyncio.to_thread(self.runtime.audit_approval, pending.name, pending.arguments, False)
            await self._send(ws, {"type": "tool_result", "id": rid, "ok": False, "kind": "denied", "error": "operation denied by the user"})
            return
        if self._fingerprint(pending.name, pending.arguments) != pending.fingerprint:
            await self._send(ws, {"type": "tool_result", "id": rid, "ok": False, "kind": "approval_invalid", "error": "operation changed after approval was requested"})
            return
        await asyncio.to_thread(self.runtime.audit_approval, pending.name, pending.arguments, True)
        result = await self._call(pending.name, pending.arguments)
        await self._send(ws, {"type": "tool_result", "id": rid, **result})

    async def handler(self, ws: Any) -> None:
        if not self._origin_allowed(ws):
            log(f"rejected WebSocket origin: {self._origin(ws)!r}")
            await ws.close(code=1008, reason="browser extension origin required")
            return
        connection_id = secrets.token_urlsafe(12)
        self.clients.add(ws)
        log(f"extension connected; workspace={self.runtime.root.name}")
        try:
            await self._send(ws, {"type": "connected", **self.status()})
            async for raw in ws:
                try:
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise ValueError("message must be an object")
                except Exception as exc:
                    await self._send(ws, {"type": "error", "error": f"invalid JSON message: {exc}"})
                    continue
                mtype = message.get("type")
                rid = message.get("id")
                if mtype == "ping":
                    await self._send(ws, {"type": "pong", "id": rid})
                elif mtype in {"workspace_status", "studio_status"}:
                    await self._send(ws, {"type": "workspace_status", "id": rid, "ok": True, **self.status()})
                elif mtype == "list_tools":
                    await self._send(ws, {"type": "tools", "id": rid, **self.status()})
                elif mtype == "call_tool":
                    name = str(message.get("name") or "")
                    arguments = message.get("arguments") or {}
                    if not isinstance(arguments, dict):
                        await self._send(ws, {"type": "tool_result", "id": rid, "ok": False, "kind": "invalid_arguments", "error": "arguments must be an object"})
                        continue
                    details = self.runtime.approval_details(name, arguments)
                    if details:
                        await self._request_approval(ws, rid, connection_id, name, arguments, details)
                    else:
                        operation_id = str(message.get("operation_id") or "")
                        self._prune_approvals()
                        if operation_id and operation_id in self.operation_results:
                            result = self.operation_results[operation_id][1]
                        else:
                            result = await self._call(name, arguments)
                            if operation_id:
                                self.operation_results[operation_id] = (time.monotonic(), result)
                        await self._send(ws, {"type": "tool_result", "id": rid, **result})
                elif mtype == "approve_operation":
                    await self._approve(ws, message, connection_id)
                elif mtype == "restart_mcp":
                    await self._send(ws, {"type": "mcp_status", "id": rid, "ok": True, "alive": True, **self.status()})
                elif mtype in {"add_server", "remove_server"}:
                    await self._send(ws, {"type": "server_changed", "id": rid, "ok": False, "error": "external MCP server management is not available in Workspace Agent 2.0"})
                else:
                    await self._send(ws, {"type": "error", "id": rid, "error": f"unknown message type: {mtype}"})
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)
            async with self._approval_lock:
                for token, pending in list(self.approvals.items()):
                    if pending.connection_id == connection_id:
                        self.approvals.pop(token, None)
            log("extension disconnected")


async def async_main(args: argparse.Namespace) -> None:
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    workspace = resolve_workspace(config, args.workspace)
    runtime = WorkspaceRuntime(workspace, config, app_data_dir(workspace))
    gateway = Gateway(runtime, config)
    port = int(args.port or os.environ.get("ZS_BRIDGE_PORT", DEFAULT_PORT))
    log(f"ZeroScript Agent {__version__}")
    log(f"workspace: {workspace}")
    log(f"runner: {runtime.runner.get('mode', 'host')} (every command requires approval)")
    log(f"listening: ws://{HOST}:{port}")
    stop = asyncio.Future()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.cancel)
        except (NotImplementedError, RuntimeError):
            pass
    async with websockets.serve(gateway.handler, HOST, port, max_size=2_000_000, ping_interval=20, ping_timeout=20):
        try:
            await stop
        except asyncio.CancelledError:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ZeroScript Workspace Agent bridge")
    parser.add_argument("--workspace", help="workspace directory (overrides config and ZS_WORKSPACE)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config.json")
    parser.add_argument("--port", type=int, help="WebSocket port")
    return parser.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(async_main(parse_args()))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"[agent] FATAL: {exc}", file=sys.stderr)
        raise

