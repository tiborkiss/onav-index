# /// script
# requires-python = ">=3.11"
# ///
"""Apply an onav-index manifest to the vault via turbovault's stdio MCP.

Bypasses the opencode MCP tool layer to avoid batch_execute's response echo
(the full content of every operation echoed back in records[] — ~40 KB per
15-op batch). Reads a manifest JSON (from gen_index.py --emit-mode manifest),
chunks into batches, applies each via turbovault over stdio MCP JSON-RPC,
and prints only one summary line per batch.

Canvas entries (kind: "canvas") are written directly to the filesystem —
turbovault manages notes, not JSON assets.

Usage:
    uv run scripts/apply_manifest.py <manifest.json>
    uv run scripts/apply_manifest.py -                         # from stdin
    uv run scripts/apply_manifest.py <manifest.json> --vault-root /path/to/vault
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

MCP_VERSION = "2025-06-18"
CHUNK_SIZE = 15
MAX_RETRIES = 3
RETRY_DELAY = 2.0


def _find_turbovault_bin() -> str:
    for candidate in (
        os.environ.get("TURBOVAULT_BIN"),
        shutil.which("turbovault"),
        str(Path.home() / ".cargo" / "bin" / "turbovault"),
    ):
        if candidate and Path(candidate).exists():
            return candidate
    sys.exit(
        "onav-index: turbovault binary not found. Set TURBOVAULT_BIN or install turbovault."
    )


class TurbovaultMCP:
    """Minimal MCP JSON-RPC client over stdio for batch_execute calls."""

    def __init__(self, vault_root: str, profile: str | None = None):
        bin_path = _find_turbovault_bin()
        cmd = [bin_path, "--vault", vault_root]
        if profile:
            cmd += ["--profile", profile]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._id = 0
        self._initialize()

    def _send(self, method: str, params: dict | None = None, *, is_notification: bool = False) -> dict | None:
        self._id += 1
        msg = {"jsonrpc": "2.0", "method": method}
        if not is_notification:
            msg["id"] = self._id
        if params:
            msg["params"] = params
        line = json.dumps(msg) + "\n"
        self.proc.stdin.write(line)
        self.proc.stdin.flush()
        if is_notification:
            return None
        return self._read_response(msg["id"])

    def _read_response(self, expected_id: int) -> dict:
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise ConnectionError("turbovault closed stdout")
            data = json.loads(line)
            if data.get("id") == expected_id:
                return data

    def _initialize(self) -> None:
        resp = self._send("initialize", {
            "protocolVersion": MCP_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "onav-apply-manifest", "version": "1.0"},
        })
        if "error" in resp:
            raise RuntimeError(f"MCP initialize failed: {resp['error']}")
        self._send("notifications/initialized", is_notification=True)

    def batch_execute(self, operations: list[dict]) -> dict:
        """Call turbovault batch_execute. Returns the inner data dict."""
        resp = self._send("tools/call", {
            "name": "batch_execute",
            "arguments": {"operations": operations},
        })
        if "error" in resp:
            raise RuntimeError(f"batch_execute error: {resp['error']}")
        content = resp.get("result", {}).get("content", [])
        for part in content:
            if part.get("type") == "text":
                outer = json.loads(part["text"])
                # turbovault wraps the result: {vault, operation, success, data: {executed, errors, ...}}
                if outer.get("isError"):
                    raise RuntimeError(outer.get("content", [{}])[0].get("text", "unknown error"))
                return outer.get("data", outer)
        return {}

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)


def _resolve_vault_root(arg: str | None) -> str:
    if arg:
        return arg
    for toml_path in ("_bmad/custom/config.user.toml", "_bmad/config.user.toml"):
        p = Path(toml_path)
        if p.exists():
            for line in p.read_text().splitlines():
                if "onav_vault_root" in line and "=" in line:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("onav-index: no --vault-root given and no onav_vault_root in config.")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Apply an onav-index manifest via turbovault stdio MCP.")
    parser.add_argument("manifest", help="Path to manifest JSON, or - for stdin.")
    parser.add_argument("--vault-root", default=None, help="Obsidian vault root (default: from onav config).")
    parser.add_argument("--profile", default=os.environ.get("TURBOVAULT_PROFILE"), help="turbovault profile.")
    args = parser.parse_args()

    source = sys.stdin if args.manifest == "-" else open(args.manifest)
    manifest = json.load(source)
    source.close()

    vault_root = args.vault_root or manifest.get("vault_root") or _resolve_vault_root(None)
    writes = [w for w in manifest.get("writes", [])]
    deletes = manifest.get("deletes", [])

    direct_writes: list[dict] = []
    mcp_writes: list[dict] = []
    for w in writes:
        if w.get("kind") == "canvas":
            direct_writes.append(w)
        else:
            mcp_writes.append(w)

    # Write canvas entries directly to the filesystem.
    for w in direct_writes:
        target = Path(vault_root) / w["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(w["content"], encoding="utf-8")
        print(f"  wrote (direct): {w['path']}")

    if not mcp_writes and not deletes:
        print("onav-index: nothing to apply.")
        return 0

    tv = TurbovaultMCP(vault_root, args.profile)
    applied = 0
    failed = 0

    # Apply write batches.
    for i in range(0, len(mcp_writes), CHUNK_SIZE):
        chunk = mcp_writes[i : i + CHUNK_SIZE]
        ops = [{"type": "WriteNote", "path": w["path"], "content": w["content"]} for w in chunk]
        for attempt in range(MAX_RETRIES):
            try:
                result = tv.batch_execute(ops)
                applied += result.get("executed", 0)
                errors = result.get("errors", [])
                if errors:
                    failed += len(errors)
                    print(f"  batch {i // CHUNK_SIZE + 1}: {result.get('executed', 0)}/{len(ops)} ok, {len(errors)} errors")
                else:
                    print(f"  batch {i // CHUNK_SIZE + 1}: {result.get('executed', 0)}/{len(ops)} ok")
                break
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
                else:
                    failed += len(ops)
                    print(f"  batch {i // CHUNK_SIZE + 1}: FAILED ({e})")

    # Apply delete batches.
    for i in range(0, len(deletes), CHUNK_SIZE):
        chunk = deletes[i : i + CHUNK_SIZE]
        ops = [{"type": "DeleteNote", "path": d, "confirm_path": d} for d in chunk]
        for attempt in range(MAX_RETRIES):
            try:
                result = tv.batch_execute(ops)
                applied += result.get("executed", 0)
                print(f"  delete batch {i // CHUNK_SIZE + 1}: {result.get('executed', 0)}/{len(ops)} ok")
                break
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
                else:
                    failed += len(ops)
                    print(f"  delete batch {i // CHUNK_SIZE + 1}: FAILED ({e})")

    tv.close()
    print(f"onav-index: applied={applied}, failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
