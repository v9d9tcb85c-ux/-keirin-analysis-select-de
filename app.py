from flask import Flask, jsonify, request, send_from_directory
from pathlib import Path
from threading import Lock
import os, time, uuid

app = Flask(__name__)
BASE = Path(__file__).resolve().parent
lock = Lock()

AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "").strip()
CONTROL_KEY = os.environ.get("CONTROL_KEY", "").strip()

state = {
    "agent_online": False,
    "agent_last_seen": 0.0,
    "phase": "idle",
    "running": False,
    "current": "",
    "detail": "",
    "venues_info": [],
    "matches": [],
    "skipped": [],
    "errors": [],
    "counters": {},
    "pending_command": None,
    "active_command_id": None,
    "stop_requested": False,
    "updated_at": time.time(),
    "last_control": "",
    "last_control_at": 0.0,
    "retry_preserve": False,
}



def now():
    return time.time()

def mark_control(name):
    state["last_control"] = str(name)
    state["last_control_at"] = now()
    state["updated_at"] = now()

def control_ok():
    if not CONTROL_KEY:
        return True
    got = request.headers.get("X-Control-Key", "") or request.args.get("key", "")
    return got == CONTROL_KEY

def agent_ok():
    if not AGENT_TOKEN:
        return False
    return request.headers.get("X-Agent-Token", "") == AGENT_TOKEN

def public_state():
    with lock:
        s = dict(state)
        s["agent_online"] = bool(s["agent_last_seen"] and now() - s["agent_last_seen"] < 12)
        s.pop("pending_command", None)
        return s

def enqueue(kind, payload=None):
    with lock:
        if state["pending_command"] is not None:
            return None
        mark_control("途中停止")
        cid = uuid.uuid4().hex
        state["pending_command"] = {
            "id": cid,
            "kind": kind,
            "payload": payload or {},
            "created_at": now(),
        }
        state["active_command_id"] = cid
        state["updated_at"] = now()
        return cid

@app.get("/")
def index():
    return send_from_directory(BASE, "index.html")

@app.get("/api/status")
def status():
    return jsonify(public_state())

@app.post("/api/control/load-board")
def load_board():
    if not control_ok():
        return jsonify(ok=False, reason="unauthorized"), 401
    s = public_state()
    if not s["agent_online"]:
        return jsonify(ok=False, reason="pc_offline"), 409
    if s["running"]:
        return jsonify(ok=False, reason="running"), 409
    cid = enqueue("load_board")
    if not cid:
        return jsonify(ok=False, reason="command_pending"), 409
    with lock:
        mark_control("今日の開催場を取得")
        state.update({
            "running": True,
            "phase": "queued",
            "current": "PCへ開催場取得を依頼中",
            "detail": "PC側の取得エンジンが開始するのを待っています。",
            "venues_info": [],
            "matches": [],
            "skipped": [],
            "errors": [],
            "counters": {},
            "stop_requested": False,
            "retry_preserve": False,
        })
    return jsonify(ok=True, command_id=cid)

@app.post("/api/control/auto-search")
def auto_search():
    """取得済み開催場からF2だけを自動選択して検索する。"""
    if not control_ok():
        return jsonify(ok=False, reason="unauthorized"), 401
    s = public_state()
    if not s["agent_online"]:
        return jsonify(ok=False, reason="pc_offline"), 409
    if s["running"]:
        return jsonify(ok=False, reason="running"), 409

    selected = [
        x.get("slug") for x in s.get("venues_info", [])
        if x.get("grade") == "F2" and x.get("slug")
    ]
    if not selected:
        return jsonify(ok=False, reason="no_f2_venues"), 400

    cid = enqueue("search_selected", {"selected": selected})
    if not cid:
        return jsonify(ok=False, reason="command_pending"), 409
    with lock:
        mark_control("自動検索")
        state.update({
            "running": True,
            "phase": "queued",
            "current": "F2開催を自動検索中",
            "detail": f"今日のF2 {len(selected)}場を自動選択してPC側で検索します。",
            "matches": [],
            "skipped": [],
            "errors": [],
            "counters": {},
            "stop_requested": False,
            "retry_preserve": False,
        })
    return jsonify(ok=True, command_id=cid, selected=selected)

@app.post("/api/control/search")
def search():
    if not control_ok():
        return jsonify(ok=False, reason="unauthorized"), 401
    s = public_state()
    if not s["agent_online"]:
        return jsonify(ok=False, reason="pc_offline"), 409
    if s["running"]:
        return jsonify(ok=False, reason="running"), 409
    body = request.get_json(silent=True) or {}
    selected = body.get("selected") or []
    preserve_display = bool(body.get("preserve_display"))
    valid = {x.get("slug") for x in s.get("venues_info", [])}
    selected = [x for x in selected if x in valid]
    if not selected:
        return jsonify(ok=False, reason="no_selection"), 400
    cid = enqueue("search_selected", {"selected": selected})
    if not cid:
        return jsonify(ok=False, reason="command_pending"), 409
    with lock:
        mark_control("再検索" if preserve_display else "検索")
        next_state = {
            "running": True,
            "phase": "queued",
            "current": "PCへ再検索を依頼中" if preserve_display else "PCへ検索を依頼中",
            "detail": (
                f"前回結果を表示したまま、選択した{len(selected)}場を再検索します。"
                if preserve_display else
                f"選択した{len(selected)}場をPC側で検索します。"
            ),
            "stop_requested": False,
            "retry_preserve": preserve_display,
        }
        # 通常検索は新規なのでリセット。
        # 再検索は「押した瞬間」に前回表示を消さない。
        if not preserve_display:
            next_state.update({"matches": [], "skipped": [], "errors": [], "counters": {}})
        state.update(next_state)
    return jsonify(ok=True, command_id=cid)

@app.post("/api/control/stop")
def stop():
    if not control_ok():
        return jsonify(ok=False, reason="unauthorized"), 401

    with lock:
        # すでに止まっているなら、stopコマンドを増やさずそのまま成功扱い。
        if not state["running"]:
            state["stop_requested"] = False
            return jsonify(ok=True, already_stopped=True)

        # 1回stopを送った後は、同じstopを何度押しても二重送信しない。
        if state.get("stop_requested"):
            return jsonify(ok=True, already_requested=True)

        cid = uuid.uuid4().hex
        state["pending_command"] = {
            "id": cid,
            "kind": "stop",
            "payload": {},
            "created_at": now(),
        }
        state["active_command_id"] = cid
        state["stop_requested"] = True
        state["phase"] = "stopping"
        state["current"] = "停止処理中"
        state["detail"] = "現在のレース処理が安全に止まるのを待っています。"
        state["updated_at"] = now()

    return jsonify(ok=True, command_id=cid)

@app.post("/api/control/reset")
def reset():
    if not control_ok():
        return jsonify(ok=False, reason="unauthorized"), 401
    with lock:
        if state["running"]:
            return jsonify(ok=False, reason="running"), 409

        has_venues = bool(state.get("venues_info"))
        mark_control("終了")
        state.update({
            # 終了後も開催場一覧は保持し、同じチェックで再検索できるようにする。
            "phase": "select" if has_venues else "idle",
            "running": False,
            "current": "終了しました",
            "detail": "検索結果をリセットしました。F2チェックはそのまま残しています。" if has_venues else "検索状態をリセットしました。",
            "matches": [],
            "skipped": [],
            "errors": [],
            "counters": {},
            "pending_command": None,
            "active_command_id": None,
            "stop_requested": False,
            "retry_preserve": False,
            "updated_at": now(),
        })
    return jsonify(ok=True)


@app.post("/api/control/hard-reset")
def hard_reset():
    if not control_ok():
        return jsonify(ok=False, reason="unauthorized"), 401

    with lock:
        if state["running"]:
            return jsonify(ok=False, reason="running"), 409

        mark_control("完全リセット")
        state.update({
            "phase": "idle",
            "running": False,
            "current": "",
            "detail": "",
            "venues_info": [],
            "matches": [],
            "skipped": [],
            "errors": [],
            "counters": {},
            "pending_command": None,
            "active_command_id": None,
            "stop_requested": False,
            "retry_preserve": False,
            "updated_at": now(),
        })
    return jsonify(ok=True)

@app.get("/api/agent/next")
def agent_next():
    if not agent_ok():
        return jsonify(ok=False), 401
    with lock:
        state["agent_last_seen"] = now()
        cmd = state["pending_command"]
        state["pending_command"] = None
        state["updated_at"] = now()
    return jsonify(ok=True, command=cmd)

@app.post("/api/agent/progress")
def agent_progress():
    if not agent_ok():
        return jsonify(ok=False), 401
    data = request.get_json(silent=True) or {}
    with lock:
        state["agent_last_seen"] = now()

        preserve = bool(state.get("retry_preserve"))
        incoming_counters = data.get("counters") if isinstance(data.get("counters"), dict) else {}
        fresh_checked = int(incoming_counters.get("checked_races") or 0)
        fresh_matches = data.get("matches") if isinstance(data.get("matches"), list) else []

        # 再検索開始直後（まだ0R確認）は、前回の件数・該当表示を消さない。
        # 新しい検索で1R以上確認した瞬間から新しい進捗へ切り替える。
        fresh_started = fresh_checked > 0 or bool(fresh_matches)
        if preserve and fresh_started:
            state["retry_preserve"] = False
            preserve = False

        for k in ("phase","running","current","detail","venues_info","matches","skipped","errors","counters"):
            if k not in data:
                continue
            if preserve and k in ("matches","skipped","errors","counters"):
                continue
            state[k] = data[k]

        state["updated_at"] = now()
    return jsonify(ok=True)

@app.post("/api/agent/finish")
def agent_finish():
    if not agent_ok():
        return jsonify(ok=False), 401
    data = request.get_json(silent=True) or {}
    with lock:
        state["agent_last_seen"] = now()
        for k in ("phase","current","detail","venues_info","matches","skipped","errors","counters"):
            if k in data:
                state[k] = data[k]
        state["running"] = False
        state["stop_requested"] = False
        state["retry_preserve"] = False
        # 停止完了後に古いstop命令が残っていた場合は破棄する。
        if state.get("pending_command") and state["pending_command"].get("kind") == "stop":
            state["pending_command"] = None
        state["updated_at"] = now()
    return jsonify(ok=True)

@app.get("/health")
def health():
    return jsonify(ok=True, role="relay-only", browser="none")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
