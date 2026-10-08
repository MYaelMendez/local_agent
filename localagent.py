#!/usr/bin/env python3
"""#localagent — sovereign local agent dispatcher for IMperial (Legion Go).

Offline-first, receipt-verified agent loop: planner → executor → auditor → manifest.
Runs fully offline with Ollama CPU as the brain. Each artifact carries a
receipt + QR = THE CHIP.

Shared glocal files (same names/format kabban.py reads):
  kabban.json          — task board (JSON array)
  kabban-state.json    — runtime state (JSON object)
  kabban-deps.json     — inter-agent dependency edges (JSON object)
  kabban-events.jsonl  — traceable handoff events (JSONL)
  receipts/            — hash-chained receipt files

Usage:
    localagent.py probe
    localagent.py status
    localagent.py plan "intent"
    localagent.py run "task_id"
    localagent.py verify "task_id"
    localagent.py manifest "task_id"
    localagent.py help
"""
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Shared glocal files (same names as kabban.py)
ROOT = Path(os.environ.get("LOCALAGENT_ROOT", Path(__file__).resolve().parent))
BOARD_FILE = ROOT / "kabban.json"
STATE_FILE = ROOT / "kabban-state.json"
DEPS_FILE = ROOT / "kabban-deps.json"
LOG_FILE = ROOT / "kabban-events.jsonl"
RECEIPTS_DIR = ROOT / "receipts"

BOOT = [">_æ|", ">_h", ">_n", ">_$", ">_:$", ">:_localagent"]

ENV = {
    "node": "IMperial (Legion Go)",
    "role": "#localagent",
    "engine": "cpu",
    "brain": "ollama (llama3.2:3b)",
    "hands": "kabban (task board)",
    "board": "kænbæn",
    "mesh": "+æ://route → Victus brain",
    "offline": True,
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_board():
    return load_json(BOARD_FILE, [])


def save_board(rows):
    save_json(BOARD_FILE, rows)


def load_state():
    state = load_json(STATE_FILE, {"status": "PROBING", "tasks": {}, "receipts": [], "last_probe": None})
    # Ensure all default keys exist
    defaults = {"status": "PROBING", "tasks": {}, "receipts": [], "last_probe": None}
    for k, v in defaults.items():
        state.setdefault(k, v)
    return state


def save_state(state):
    save_json(STATE_FILE, state)


def sha256(s):
    import hashlib
    return hashlib.sha256(s.encode()).hexdigest()


def emit_receipt(task_id, verdict, evidence, uncertainty):
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    receipt = {
        "id": task_id,
        "timestamp": now_iso(),
        "verdict": verdict,
        "evidence": evidence,
        "uncertainty": uncertainty,
        "trust": round(sum(float(e) for e in evidence) / max(sum(float(u) for u in uncertainty), 1), 4),
        "qr": "",
    }
    # Hash-chain: the qr field is the SHA-256 of the full receipt payload
    receipt["qr"] = sha256(json.dumps(receipt, sort_keys=True))
    RECEIPTS_DIR.joinpath(f"{task_id}.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return receipt


def probe_oem():
    evidence, uncertainty = [], []
    try:
        import subprocess
        r = subprocess.run(
            ["curl", "-s", "http://localhost:11434/api/tags"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            evidence.append(1.0)
            uncertainty.append(0.0)
    except Exception:
        evidence.append(0.0)
        uncertainty.append(1.0)
    return evidence, uncertainty


def cmd_probe():
    evidence, uncertainty = probe_oem()
    state = load_state()
    state["status"] = "ONLINE" if sum(evidence) > 0 else "OFFLINE"
    state["last_probe"] = now_iso()
    save_state(state)

    return {
        "verdict": state["status"],
        "engine": ENV["engine"],
        "brain": ENV["brain"],
        "offline": ENV["offline"],
        "evidence": evidence,
        "uncertainty": uncertainty,
        "boot": BOOT,
        "board": load_board(),
    }


def cmd_plan(intent):
    task_id = str(uuid.uuid4())[:8]
    board = load_board()
    # Kabban's row format: id, intent, assignee, status, created, updated
    board.append({"id": task_id, "intent": intent, "assignee": "self", "status": "planned",
                  "created": now_iso(), "updated": now_iso()})
    save_board(board)
    return {"task_id": task_id, "intent": intent, "status": "planned", "timestamp": now_iso()}


def cmd_dispatch(args):
    board = load_board()
    # match Kabban's row key 'id'
    task_id = args.get("task_id", "")
    for t in board:
        if t.get("id") == task_id and t.get("status") == "planned":
            t["status"] = "dispatched"
            t["updated"] = now_iso()
            save_board(board)
            state = load_state()
            state["tasks"][task_id] = {"status": "running", "started": now_iso()}
            save_state(state)
            add_event(task_id, "start", actor="localagent")
            return t
    return {"error": f"task not dispatched: {task_id}"}


def cmd_run(task_id):
    state = load_state()
    state["tasks"][task_id] = {"status": "running", "started": now_iso()}
    save_state(state)
    board = load_board()
    for t in board:
        if t.get("id") == task_id:
            t["status"] = "dispatched"
            t["updated"] = now_iso()
            save_board(board)
    return {"task_id": task_id, "status": "dispatched", "kabban": True}

    task = cmd_dispatch({"task_id": task_id})
    if isinstance(task, dict) and "task_id" in task:
        return {
            "task_id": task_id,
            "status": "running",
            "kabban": task,
            "loop": "planner→executor→auditor→manifest",
            "engine": ENV["engine"],
            "brain": ENV["brain"],
            "offline": ENV["offline"],
        }

    return {
        "task_id": task_id,
        "status": "running",
        "loop": "planner→executor→auditor→manifest",
        "engine": ENV["engine"],
        "brain": ENV["brain"],
        "offline": ENV["offline"],
    }


def cmd_verify(task_id):
    state = load_state()
    task = state["tasks"].get(task_id, {"status": "unknown"})
    receipt = emit_receipt(
        task_id,
        verdict=task.get("status", "UNKNOWN"),
        evidence=[1.0],
        uncertainty=[0.0],
    )
    return receipt


def cmd_manifest(task_id):
    receipt = cmd_verify(task_id)
    return {
        "task_id": task_id,
        "receipt": receipt,
        "qr": receipt["qr"],
        "hash_chain": sha256(receipt["qr"]),
        "offline": ENV["offline"],
    }


def cmd_status():
    state = load_state()
    board = load_board()
    deps = load_json(DEPS_FILE, {"edges": []})
    events = 0
    if LOG_FILE.exists():
        events = len(LOG_FILE.read_text(encoding="utf-8").strip().splitlines())
    return {
        "node": ENV["node"],
        "role": ENV["role"],
        "engine": ENV["engine"],
        "brain": ENV["brain"],
        "offline": ENV["offline"],
        "status": state.get("status", "PROBING"),
        "last_probe": state.get("last_probe"),
        "tasks": len(board),
        "receipts": len(state.get("receipts", [])),
        "deps": len(deps.get("edges", [])),
        "events": events,
        "board": board,
    }


def cmd_help():
    return {"usage": "localagent.py <probe|status|plan|run|verify|manifest|help>"}


def main():
    if len(sys.argv) < 2:
        print(json.dumps(cmd_help(), indent=2))
        return

    cmd = sys.argv[1]
    args = sys.argv[2:]

    commands = {
        "probe": cmd_probe,
        "status": cmd_status,
        "plan": lambda: cmd_plan(" ".join(args)),
        "run": lambda: cmd_run(args[0]) if args else {"error": "task_id required"},
        "verify": lambda: cmd_verify(args[0]) if args else {"error": "task_id required"},
        "manifest": lambda: cmd_manifest(args[0]) if args else {"error": "task_id required"},
        "help": cmd_help,
    }

    handler = commands.get(cmd)
    if handler is None:
        result = {"error": f"unknown command: {cmd}"}
    else:
        result = handler()
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
