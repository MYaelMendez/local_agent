#!/usr/bin/env python3
"""kænbæn — Knowledge Breadbasket: sovereign local agent task board + CLI.

Offline-first, traceable handoff + dispatch system for the #localagent
planner→executor→auditor→manifest loop. Runs fully offline with
local-first storage (kabban.json + kabban-state.json).

Usage:
    kabban.py create "task" --assignee imperial|vice|reviewer
    kabban.py list
    kabban.py show <task_id>
    kabban.py dispatch --max 1
    kabban.py start <task_id>
    kabban.py done <task_id> [--evidence "..."]
    kabban.py block <task_id> --reason "..."
    kabban.py block <task_id> --dormant --reroute
    kabban.py unblock <task_id>
    kabban.py dep add <child> --parent <parent>
    kabban.py dep remove <child> --parent <parent>
    kabban.py dep list
    kabban.py log <task_id>
    kabban.py status
    kabban.py stats
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

APP = Path(__file__).resolve().parent
STATE_FILE = APP / "kabban-state.json"
BOARD_FILE = APP / "kabban.json"
DEPS_FILE = APP / "kabban-deps.json"
LOG_FILE = APP / "kabban-events.jsonl"

VALID_ASSIGNEES = {"imperial", "vice", "reviewer", "self"}

SYSTEM = {
    "node": "IMperial (Legion Go)",
    "role": "#localagent",
    "engine": "cpu (Ollama llama3.2:3b)",
    "offline": True,
    "brain": "ollama :11434",
    "board": "kænbæn",
    "mesh": "+æ://route → Victus",
}

DEFAULT_STATE = {
    "status": "PROBING",
    "last_probe": None,
    "last_run": None,
    "dormant_cutoff_hours": 24,
    "logged": 0,
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
    board_path = APP / "kabban.json"
    board_path.parent.mkdir(parents=True, exist_ok=True)
    board_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def new_task(intent, assignee="self"):
    # Unique ID anchored to a persistent counter in the state file
    state = load_state()
    n = int(state.get("task_counter", 0)) + 1
    state["task_counter"] = n
    save_state(state)
    task_id = f"k{n:03d}"
    task = {
        "id": task_id,
        "intent": intent,
        "assignee": assignee,
        "status": "todo",
        "parent": None,
        "children": [],
        "evidence": [],
        "uncertainty": [],
        "created": now_iso(),
        "updated": now_iso(),
        "dormant_since": None,
    }
    board = load_board()
    board.append(task)
    save_board(board)
    return task


def add_event(task_id, event, actor="system"):
    entry = {"timestamp": now_iso(), "task_id": task_id, "event": event, "actor": actor}
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    STATE = load_state()
    STATE["logged"] = STATE.get("logged", 0) + 1
    save_state(STATE)


def load_state():
    return load_json(STATE_FILE, dict(DEFAULT_STATE))


def save_state(state):
    save_json(STATE_FILE, state)


def load_deps():
    return load_json(DEPS_FILE, {"edges": []})


def save_deps(deps):
    save_json(DEPS_FILE, deps)


def cmd_create(args):
    intent = args.get("intent")
    assignee = args.get("assignee", "self")
    if assignee not in VALID_ASSIGNEES:
        assignee = "self"
    task = new_task(intent, assignee)
    add_event(task["id"], "create", actor="kabban")
    return task


def cmd_list(args):
    board = load_board()
    if not board:
        return {"empty": True}
    # Dormant reroute pass
    for t in board:
        if t["status"] == "todo" and t.get("dormant_since"):
            t["status"] = "blocked"
            t["updated"] = now_iso()
            board[board.index(t)] = t
            save_board(board)
            add_event(t["id"], "dormant_reroute", actor="kabban")
    save_board(board)
    return {"board": board}


def cmd_show(args):
    board = load_board()
    task_id = args.get("task_id")
    for t in board:
        if t["id"] == task_id:
            return t
    return {"error": f"task not found: {task_id}"}


def cmd_dispatch(args):
    board = load_board()
    state = load_state()
    deps = load_deps().get("edges", [])
    max_tasks = int(args.get("max", 1))
    if not board:
        return {"empty": True}

    # Count in-progress
    running = [t for t in board if t["status"] in ("running", "dispatched")]
    if len(running) >= max_tasks:
        return {"running": len(running), "at_cap": True}

    # Select the next task to dispatch:
    #  - un-blocked by dependency gating
    #  - ordered by longest dependency chain (critical path first)
    def ready_key(t):
        parents = [e["parent"] for e in deps if e["child"] == t["id"]]
        # return (0, depth) if ready to dispatch, (1, 0) if blocked
        if parents:
            parent_tasks = [p for p in board if p["id"] in parents]
            if any(p.get("status") != "done" for p in parent_tasks if p in board):
                return (1, 0)  # blocked
        # depth = chain length through parents
        depth = 0
        seen = set()
        stack = parents if parents else []
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            p = next((x for x in board if x["id"] == pid), None)
            if p:
                depth += 1
                stack.extend(e["parent"] for e in deps if e["child"] == pid)
        return (0, -depth)

    ready = sorted(board, key=ready_key)
    for t in ready:
        if t["status"] == "todo":
            t["status"] = "dispatched"
            t["updated"] = now_iso()
            save_board(board)
            add_event(t["id"], "dispatch", actor="kabban")
            return t

    return {"at_cap": True, "count": len(running)}


def cmd_start(args):
    task_id = args.get("task_id")
    board = load_board()
    for t in board:
        if t["id"] == task_id:
            t["status"] = "running"
            t["updated"] = now_iso()
            board[board.index(t)] = t
            save_board(board)
            add_event(task_id, "start", actor="kabban")
            return t
    return {"error": f"task not found: {task_id}"}


def cmd_done(args):
    task_id = args.get("task_id")
    evidence = args.get("evidence", "")
    board = load_board()
    state = load_state()
    for t in board:
        if t["id"] == task_id:
            t["status"] = "done"
            if evidence:
                t["evidence"].append({"text": evidence, "at": now_iso()})
            t["updated"] = now_iso()
            board[board.index(t)] = t
            save_board(board)
            add_event(task_id, "done", actor="kabban")
            return t
    return {"error": f"task not found: {task_id}"}


def cmd_block(args):
    task_id = args.get("task_id")
    reason = args.get("reason", "")
    dormant = args.get("dormant", False)
    reroute = args.get("reroute", False)
    board = load_board()
    for t in board:
        if t["id"] == task_id:
            t["status"] = "blocked"
            if reason:
                t["blocked_reason"] = reason
            if dormant:
                t["dormant_since"] = now_iso()
            if reroute:
                t["glocal_tag"] = "task"
            t["updated"] = now_iso()
            board[board.index(t)] = t
            save_board(board)
            add_event(task_id, "blocked", actor="kabban")
            return t
    return {"error": f"task not found: {task_id}"}


def cmd_unblock(args):
    task_id = args.get("task_id")
    board = load_board()
    for t in board:
        if t["id"] == task_id:
            t["status"] = "todo"
            t["dormant_since"] = None
            t["updated"] = now_iso()
            board[board.index(t)] = t
            save_board(board)
            add_event(task_id, "unblock", actor="kabban")
            return t
    return {"error": f"task not found: {task_id}"}


def cmd_add_dep(args):
    child_id = args.get("child")
    parent_id = args.get("parent")
    if not child_id or not parent_id:
        return {"error": "usage: kabban.py add-dep <child_id> --parent <parent_id>"}
    deps = load_deps()
    edge = {"child": child_id, "parent": parent_id, "at": now_iso()}
    deps["edges"].append(edge)
    save_deps(deps)
    # Link in task
    board = load_board()
    child = next((t for t in board if t["id"] == child_id), None)
    parent = next((t for t in board if t["id"] == parent_id), None)
    if child:
        child["parent"] = parent_id
    if parent:
        parent["children"].append(child_id)
    save_board(board)
    return {"edge": edge}


def cmd_dep_list(args):
    deps = load_deps()
    board = load_board()
    by_id = {t["id"]: t for t in board}
    result = []
    for e in deps.get("edges", []):
        child = by_id.get(e["child"])
        parent = by_id.get(e["parent"])
        result.append({
            "child": e["child"],
            "parent": e["parent"],
            "child_name": child["intent"] if child else "UNKNOWN",
            "parent_name": parent["intent"] if parent else "UNKNOWN",
            "at": e.get("at"),
        })
    # Add dependency gating status for each task
    board_by_id = {t["id"]: t for t in board}
    gating = []
    for t in board:
        parents = [e["parent"] for e in deps.get("edges", []) if e["child"] == t["id"]]
        if parents:
            blocked = any(
                board_by_id[p].get("status") != "done" and p in board_by_id
                for p in parents
            )
            gating.append({
                "task": t["id"],
                "intent": t["intent"],
                "parents": parents,
                "blocked": blocked,
                "status": t["status"],
            })
    # Return edges with node names filled in
    edges_with_names = []
    for e in deps.get("edges", []):
        child = by_id.get(e["child"])
        parent = by_id.get(e["parent"])
        edges_with_names.append({
            "child": e["child"],
            "parent": e["parent"],
            "child_name": child["intent"] if child else "UNKNOWN",
            "parent_name": parent["intent"] if parent else "UNKNOWN",
            "at": e.get("at"),
        })
    return {"edges": edges_with_names, "dependency_gating": gating}


def cmd_log(args):
    task_id = args.get("task_id")
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return {"empty": True}
    relevant = []
    for line in lines:
        entry = json.loads(line)
        if task_id is None or entry.get("task_id") == task_id:
            entry["_raw"] = line
            relevant.append(entry)
    return {"entries": relevant}


def cmd_status(args):
    state = load_state()
    return {
        "node": SYSTEM["node"],
        "role": SYSTEM["role"],
        "engine": SYSTEM["engine"],
        "offline": SYSTEM["offline"],
        "status": state.get("status", "PROBING"),
        "last_probe": state.get("last_probe"),
        "tasks": len(load_board()),
        "deps": len(load_deps().get("edges", [])),
        "events": state.get("logged", 0),
    }


def cmd_stats(args):
    board = load_board()
    deps = load_deps()
    return {
        "tasks": {
            "total": len(board),
            "todo": sum(1 for t in board if t["status"] == "todo"),
            "dispatched": sum(1 for t in board if t["status"] == "dispatched"),
            "running": sum(1 for t in board if t["status"] == "running"),
            "blocked": sum(1 for t in board if t["status"] == "blocked"),
            "done": sum(1 for t in board if t["status"] == "done"),
        },
        "deps": len(deps.get("edges", [])),
        "events": load_state().get("logged", 0),
    }


def parse_args(args):
    """Parse CLI args into a dict. Supports --flags and positional values."""
    parsed = {}
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            key = a[2:].replace("-", "_")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                parsed[key] = args[i + 1]
                i += 2
            else:
                parsed[key] = True
                i += 1
        else:
            positional.append(a)
            i += 1
    return parsed, positional


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    args = sys.argv[2:]

    parsed, positional = parse_args(args)

    # Commands that take a positional intent/text
    TEXT_CMDS = {"create", "start", "done", "block"}

    if cmd in TEXT_CMDS:
        parsed["intent"] = positional[0] if positional else "task"
        parsed["reason"] = " ".join(positional[1:]) if len(positional) > 1 else parsed.get("reason", "")
        # Also allow --reason and merge
        if "reason" in parsed:
            parsed["reason"] = " ".join([p for p in positional[1:]] + [parsed["reason"]]).strip() or parsed["reason"]

    # Commands that take task_id
    ID_CMDS = {"show", "dispatch", "start", "done", "block", "unblock", "log", "verify", "manifest"}

    if cmd in ID_CMDS:
        parsed["task_id"] = positional[0] if positional else parsed.get("task_id", "")

    if cmd == "dep":
        # 'dep add <child> --parent <parent>' — the 'dep' command reads args[2:]
        # and args[3] for the edge (after 'add')
        if positional and positional[0] == "add":
            parsed["child"] = positional[1] if len(positional) > 1 else parsed.get("child", "")
            parsed["parent"] = positional[2] if len(positional) > 2 else parsed.get("parent", "")
        else:
            parsed["child"] = positional[0] if positional else parsed.get("child", "")
            parsed["parent"] = positional[1] if len(positional) > 1 else parsed.get("parent", "")

    commands = {
        "create": cmd_create,
        "list": cmd_list,
        "show": cmd_show,
        "dispatch": cmd_dispatch,
        "start": cmd_start,
        "done": cmd_done,
        "block": cmd_block,
        "unblock": cmd_unblock,
        "dep": cmd_add_dep,
        "deplist": cmd_dep_list,
        "log": cmd_log,
        "status": cmd_status,
        "stats": cmd_stats,
    }

    if cmd not in commands:
        print(f"Unknown: {cmd}")
        print("Available: create, list, show, dispatch, start, done, block, unblock, dep, deplist, log, status, stats")
        return

    result = commands[cmd](parsed)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
