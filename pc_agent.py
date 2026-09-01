import hashlib
import json
import tempfile
import os, sys, time, threading, traceback, socket
from pathlib import Path
import requests

from pc_engine import load_today_board, scan_selected
from email_notifier import mail_enabled, validate_mail_config, send_search_result
from datetime import datetime, timezone, timedelta
import sys
import traceback



LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_KEEP = 10

def _cleanup_old_logs():
    try:
        files = sorted(
            [p for p in LOG_DIR.glob("*.log") if p.name != "latest.log"],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in files[LOG_KEEP:]:
            old.unlink(missing_ok=True)
    except Exception:
        pass

def _open_session_log():
    _cleanup_old_logs()
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    session_path = LOG_DIR / f"{stamp}.log"
    latest_path = LOG_DIR / "latest.log"
    return session_path, latest_path

_SESSION_LOG, _LATEST_LOG = _open_session_log()

def _append_log(line):
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = f"[{ts}] {line}"
        with _SESSION_LOG.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
        with _LATEST_LOG.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass

class _Tee:
    def __init__(self, stream):
        self.stream = stream
    def write(self, data):
        if not data:
            return
        self.stream.write(data)
        self.stream.flush()
        # Preserve line structure; avoid double timestamps for partial writes.
        for part in data.rstrip("\n").splitlines():
            if part.strip():
                _append_log(part)
    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass

# Start each latest.log fresh for the current agent run.
try:
    _LATEST_LOG.write_text("", encoding="utf-8")
except Exception:
    pass

# Capture normal prints and uncaught exceptions automatically.
sys.stdout = _Tee(sys.__stdout__)
sys.stderr = _Tee(sys.__stderr__)

def _log_exception(prefix, exc):
    _append_log(f"{prefix}: {type(exc).__name__}: {exc}")
    try:
        for line in traceback.format_exc().splitlines():
            if line.strip():
                _append_log(line)
    except Exception:
        pass

def load_local_config():
    """環境変数が無い場合は、同じフォルダの config.txt を自動読込する。"""
    cfg = {}
    p = Path(__file__).resolve().parent / "config.txt"
    if not p.exists():
        return cfg
    text = p.read_text(encoding="utf-8-sig")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg

_local = load_local_config()
SERVER_URL = (os.environ.get("WT_SERVER_URL") or _local.get("WT_SERVER_URL") or "").strip().rstrip("/")
AGENT_TOKEN = (os.environ.get("WT_AGENT_TOKEN") or _local.get("WT_AGENT_TOKEN") or "").strip()
POLL_SECONDS = float(os.environ.get("WT_POLL_SECONDS") or _local.get("WT_POLL_SECONDS") or "1.5")

# メール通知版PCエージェントだけを対象にした専用ファイルロック。
# TCPポートを使わないため、別のPythonツール（全データ保存版など）とは干渉しない。
import tempfile
_AGENT_LOCK_PATH = os.path.join(tempfile.gettempdir(), "WINTICKET_F2_MAIL_AGENT.lock")
_AGENT_LOCK_FILE = open(_AGENT_LOCK_PATH, "a+b")
try:
    _AGENT_LOCK_FILE.seek(0)
    if os.name == "nt":
        import msvcrt
        if os.path.getsize(_AGENT_LOCK_PATH) == 0:
            _AGENT_LOCK_FILE.write(b"0")
            _AGENT_LOCK_FILE.flush()
            _AGENT_LOCK_FILE.seek(0)
        msvcrt.locking(_AGENT_LOCK_FILE.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(_AGENT_LOCK_FILE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except (OSError, IOError):
    raise SystemExit("[ERROR] メール通知版PCエージェントはすでに起動しています。\n同梱の 01_前のメール通知版を終了して起動.bat を使ってください。")

_AGENT_STARTED_AT = time.time()
_last_mail_sent_at = 0.0
_last_watched_done_key = None

# ===== 完全自動検索スケジューラ =====
# 07:00 = F2の M/D/N、15:00 = F2の MN
# PCエージェントが指定時刻より後に起動した場合も、その日の未実行枠は1回だけ追いつき実行する。
AUTO_SEARCH_ENABLED = (_local.get("AUTO_SEARCH_ENABLED", "1").strip().lower() not in ("0","false","off","no"))

_SCHEDULE_CONFIG_PATH = Path(__file__).resolve().parent / "auto_schedule_config.json"

def _valid_hhmm(v):
    try:
        hh, mm = [int(x) for x in str(v).split(":", 1)]
        return 0 <= hh <= 23 and 0 <= mm <= 59 and len(str(v)) == 5
    except Exception:
        return False

def _load_auto_schedule_config():
    base = {
        "morning_time": (_local.get("AUTO_MORNING_TIME", "07:00").strip() or "07:00"),
        "mn_time": (_local.get("AUTO_MN_TIME", "15:00").strip() or "15:00"),
    }
    try:
        if _SCHEDULE_CONFIG_PATH.exists():
            d = json.loads(_SCHEDULE_CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if _valid_hhmm(d.get("morning_time")):
                base["morning_time"] = d["morning_time"]
            if _valid_hhmm(d.get("mn_time")):
                base["mn_time"] = d["mn_time"]
    except Exception as e:
        print("[AUTO] schedule config load fail", type(e).__name__, e, flush=True)
    return base

def _save_auto_schedule_config(morning_time, mn_time):
    data = {
        "morning_time": morning_time,
        "mn_time": mn_time,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _SCHEDULE_CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8-sig"
    )

_auto_schedule = _load_auto_schedule_config()
AUTO_MORNING_TIME = _auto_schedule["morning_time"]
AUTO_MN_TIME = _auto_schedule["mn_time"]

AUTO_MN_RECHECK_ENABLED = (_local.get("AUTO_MN_RECHECK_ENABLED", "1").strip().lower() not in ("0","false","off","no"))
AUTO_MN_RECHECK_MINUTES = max(5, int(_local.get("AUTO_MN_RECHECK_MINUTES", "20") or "20"))
AUTO_MN_RECHECK_MAX = max(0, int(_local.get("AUTO_MN_RECHECK_MAX", "3") or "3"))

# v3.21: 取りこぼし防止の固定2回目検索。
# 1回目の設定時刻は従来どおり画面から変更可能。
# 2回目は M/D/N=09:30, MN=17:00 を既定とし config.txt で上書き可能。
AUTO_MORNING_RECHECK_TIME = (_local.get("AUTO_MORNING_RECHECK_TIME", "09:30").strip() or "09:30")
AUTO_MN_DAILY_RECHECK_TIME = (_local.get("AUTO_MN_DAILY_RECHECK_TIME", "17:00").strip() or "17:00")
if not _valid_hhmm(AUTO_MORNING_RECHECK_TIME):
    AUTO_MORNING_RECHECK_TIME = "09:30"
if not _valid_hhmm(AUTO_MN_DAILY_RECHECK_TIME):
    AUTO_MN_DAILY_RECHECK_TIME = "17:00"

_current_slot_label = ""
_mn_recheck = {"day":"","due_at":0.0,"count":0}
_SCHEDULE_STATE_PATH = os.path.join(Path(__file__).resolve().parent, "auto_search_state.json")
_SEEN_CANDIDATES_PATH = Path(__file__).resolve().parent / "auto_seen_candidates.json"


def _schedule_snapshot(source="pc"):
    return {
        "morning_time": AUTO_MORNING_TIME,
        "mn_time": AUTO_MN_TIME,
        "source": source,
        "updated_at": time.time(),
    }

def apply_schedule_config(morning_time, mn_time):
    global AUTO_MORNING_TIME, AUTO_MN_TIME, _auto_schedule
    morning = str(morning_time or "").strip()
    mn = str(mn_time or "").strip()
    if not _valid_hhmm(morning) or not _valid_hhmm(mn):
        raise ValueError("時刻は HH:MM 形式で指定してください。")
    _save_auto_schedule_config(morning, mn)
    AUTO_MORNING_TIME = morning
    AUTO_MN_TIME = mn
    _auto_schedule = {"morning_time": morning, "mn_time": mn}
    print(f"[AUTO] schedule updated morning={morning} M/D/N mn={mn} MN", flush=True)
    return _schedule_snapshot("pc_saved")

def _load_schedule_state():
    try:
        p = Path(_SCHEDULE_STATE_PATH)
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as e:
        print("[AUTO] state load fail", type(e).__name__, e, flush=True)
    return {}

def _save_schedule_state():
    try:
        Path(_SCHEDULE_STATE_PATH).write_text(
            json.dumps(_schedule_state, ensure_ascii=False, indent=2),
            encoding="utf-8-sig"
        )
    except Exception as e:
        print("[AUTO] state save fail", type(e).__name__, e, flush=True)

_schedule_state = _load_schedule_state()


def _time_token(hhmm):
    return str(hhmm or "").replace(":", "")

def _scheduled_slot_key(day, slot, hhmm):
    # New format: 2026-08-30:MDN_1835 / 2026-08-30:MN_1840
    return f"{day}:{slot}_{_time_token(hhmm)}"

def _migrate_legacy_schedule_state(day):
    """
    v3.15.1 migration:
    Preserve already-completed old fixed slots so returning to 07:00/15:00
    on the same day does not fire them again.
    """
    changed = False
    pairs = (
        (f"{day}:07_MDN", _scheduled_slot_key(day, "MDN", "07:00")),
        (f"{day}:15_MN", _scheduled_slot_key(day, "MN", "15:00")),
    )
    for legacy, newkey in pairs:
        rec = _schedule_state.get(legacy)
        if isinstance(rec, dict) and newkey not in _schedule_state:
            _schedule_state[newkey] = dict(rec)
            changed = True
    if changed:
        _save_schedule_state()

def _slot_completed(key):
    x=_schedule_state.get(key)
    return isinstance(x, dict) and bool(x.get("completed_at"))

def _slot_start_allowed(key, now):
    x=_schedule_state.get(key)
    if not isinstance(x, dict):
        return True
    if x.get("completed_at"):
        return False
    try:
        started=datetime.fromisoformat(str(x.get("started_at") or ""))
        return (now-started).total_seconds() >= 600
    except Exception:
        return True

def _mark_slot_completed(command_id):
    # command id format AUTO-YYYY-MM-DD:MDN_HHMM / AUTO-YYYY-MM-DD:MN_HHMM
    cid=str(command_id or "")
    if not cid.startswith("AUTO-"):
        return
    key=cid[5:]
    if "_RECHECK_" in key:
        return
    rec=_schedule_state.setdefault(key,{})
    rec["completed_at"]=datetime.now().isoformat(timespec="seconds")
    _save_schedule_state()

def _hhmm_reached(hhmm, now):
    try:
        hh, mm = [int(x) for x in hhmm.split(":", 1)]
        return (now.hour, now.minute) >= (hh, mm)
    except Exception:
        return False


def _load_seen_candidates():
    try:
        if _SEEN_CANDIDATES_PATH.exists():
            d = json.loads(_SEEN_CANDIDATES_PATH.read_text(encoding="utf-8-sig"))
            return d if isinstance(d, dict) else {}
    except Exception as e:
        print("[AUTO] seen candidate state load fail", type(e).__name__, e, flush=True)
    return {}

def _save_seen_candidates():
    try:
        # 直近7日程度だけ保持。
        days = sorted({str(k).split(":",1)[0] for k in _seen_candidates.keys()})
        keep_days = set(days[-7:])
        compact = {k:v for k,v in _seen_candidates.items() if str(k).split(":",1)[0] in keep_days}
        _SEEN_CANDIDATES_PATH.write_text(
            json.dumps(compact, ensure_ascii=False, indent=2),
            encoding="utf-8-sig"
        )
        _seen_candidates.clear()
        _seen_candidates.update(compact)
    except Exception as e:
        print("[AUTO] seen candidate state save fail", type(e).__name__, e, flush=True)

def _match_identity(r):
    if not isinstance(r, dict):
        return ""
    payload = {k:r.get(k) for k in (
        "venue","slug","race","session","grade","line","three_order","order",
        "rule_id","condition_name","bet","bet_type","tickets","decision","rank"
    )}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

_seen_candidates = _load_seen_candidates()

def _split_new_matches(bucket, matches, initialize_only=False):
    bucket = str(bucket or "")
    current = [r for r in list(matches or []) if isinstance(r, dict)]
    current_ids = [_match_identity(r) for r in current]
    rec = _seen_candidates.get(bucket)
    known = set((rec or {}).get("ids") or []) if isinstance(rec, dict) else set()

    if initialize_only or rec is None:
        new_matches = list(current)
    else:
        new_matches = [r for r in current if _match_identity(r) not in known]

    merged = list(dict.fromkeys(list(known) + current_ids))
    _seen_candidates[bucket] = {
        "ids": merged,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save_seen_candidates()
    return new_matches

def run_scheduled_search(slot, sessions, command_id, seen_bucket=None, new_only=False):
    """開催場取得→F2自動選択→指定時間帯だけ検索。既存の同じ取得・検索経路だけを使う。"""
    global board_cache, _current_slot_label
    _current_slot_label = slot
    _auto_local_search.set()
    scheduled_ok=False
    try:
        stop_event.clear()
        print(f"[AUTO] {slot} load_board start sessions={sessions}", flush=True)
        result = load_today_board(progress, stop_event)
        board_cache = list(result.get("venues_info", []))
        if result.get("stopped"):
            finish({
                "phase":"stopped","current":"途中停止","detail":"自動検索を停止しました。",
                "venues_info":board_cache,"matches":[],"errors":result.get("errors",[]),
                "counters":result.get("counters",{})
            })
            return

        # IMPORTANT:
        # scan_selected() が受け取る識別子は手動検索と同じ venue slug を使う。
        # 旧自動検索は日本語場名（例: 松戸）を渡していたため、
        # 実レース巡回に入らず数秒で0件終了するケースがあった。
        selected_rows = [
            v for v in board_cache
            if str(v.get("grade") or "").upper() == "F2"
            and str(v.get("session") or "").upper() in sessions
        ]
        selected = [
            str(v.get("slug") or "").strip()
            for v in selected_rows
            if str(v.get("slug") or "").strip()
        ]
        selected_map = [
            (str(v.get("venue") or "").strip(), str(v.get("slug") or "").strip())
            for v in selected_rows
        ]
        print(f"[AUTO] {slot} selected_slugs={selected}", flush=True)
        print(f"[AUTO] {slot} venue_slug_map={selected_map}", flush=True)

        missing_slug = [
            str(v.get("venue") or "").strip()
            for v in selected_rows
            if not str(v.get("slug") or "").strip()
        ]
        if missing_slug:
            print(f"[AUTO] {slot} WARNING missing slug venues={missing_slug}", flush=True)

        if selected:
            run_search(selected, command_id, seen_bucket=seen_bucket, new_only=new_only)
            scheduled_ok=True
        else:
            # 1回目は対象場0でも完了メールを送る。2回目は新規候補通知だけなので送らない。
            empty_result = {
                "matches": [], "skipped": [], "errors": [],
                "counters": {}, "venues_info": board_cache, "stopped": False
            }
            if seen_bucket:
                _split_new_matches(seen_bucket, [], initialize_only=not new_only)
            if mail_enabled(_local) and not new_only:
                try:
                    subject = send_search_result(_local, empty_result, update=False, slot_label=slot)
                    print("[MAIL] sent", subject, flush=True)
                except Exception as mail_exc:
                    print("[MAIL] send failed", type(mail_exc).__name__, mail_exc, flush=True)
            elif new_only:
                print(f"[MAIL] {slot} 新規候補なし -> 追加メールなし", flush=True)
            finish({
                "phase":"done","current":"検索完了",
                "detail":f"{slot} 自動検索：対象F2開催場なし",
                "venues_info":board_cache,"matches":[],"errors":[],"counters":{}
            })
            scheduled_ok=True
    except Exception as e:
        traceback.print_exc()
        finish({"phase":"error","current":"自動検索エラー","detail":str(e),
                "errors":[f"{type(e).__name__}: {e}"]})
    finally:
        if scheduled_ok:
            _mark_slot_completed(command_id)
        _auto_local_search.clear()

def check_auto_schedule():
    if not AUTO_SEARCH_ENABLED:
        return
    if job_thread and job_thread.is_alive():
        return

    now = datetime.now()
    day = now.strftime("%Y-%m-%d")

    # Keep compatibility with completed slots from the former fixed-key version.
    _migrate_legacy_schedule_state(day)

    # 朝：M/D/N
    # The configured HH:MM is part of the slot identity.
    # Example: a completed 07:00 slot does not block a newly configured 18:35 slot.
    morning_key = _scheduled_slot_key(day, "MDN", AUTO_MORNING_TIME)
    if _hhmm_reached(AUTO_MORNING_TIME, now) and _slot_start_allowed(morning_key, now):
        _schedule_state[morning_key] = {
            "started_at": now.isoformat(timespec="seconds"),
            "configured_time": AUTO_MORNING_TIME,
            "slot": "MDN",
        }
        _save_schedule_state()
        print(f"[AUTO] scheduled morning search {AUTO_MORNING_TIME} key={morning_key}", flush=True)
        seen_bucket = f"{day}:MDN"
        start_job(run_scheduled_search, "朝", {"M","D","N"}, "AUTO-" + morning_key, seen_bucket, False)
        wake_render_async(f"{AUTO_MORNING_TIME} M/D/N")
        return

    # 朝の再確認：M/D/N。新しく増えた候補だけ追加メール。
    morning_recheck_key = _scheduled_slot_key(day, "MDN_RECHECK", AUTO_MORNING_RECHECK_TIME)
    if _hhmm_reached(AUTO_MORNING_RECHECK_TIME, now) and _slot_start_allowed(morning_recheck_key, now):
        _schedule_state[morning_recheck_key] = {
            "started_at": now.isoformat(timespec="seconds"),
            "configured_time": AUTO_MORNING_RECHECK_TIME,
            "slot": "MDN_RECHECK",
        }
        _save_schedule_state()
        print(f"[AUTO] scheduled morning recheck {AUTO_MORNING_RECHECK_TIME} key={morning_recheck_key}", flush=True)
        seen_bucket = f"{day}:MDN"
        start_job(run_scheduled_search, "朝再確認", {"M","D","N"}, "AUTO-" + morning_recheck_key, seen_bucket, True)
        wake_render_async(f"{AUTO_MORNING_RECHECK_TIME} M/D/N 再確認")
        return

    # MN
    mn_key = _scheduled_slot_key(day, "MN", AUTO_MN_TIME)
    if _hhmm_reached(AUTO_MN_TIME, now) and _slot_start_allowed(mn_key, now):
        _schedule_state[mn_key] = {
            "started_at": now.isoformat(timespec="seconds"),
            "configured_time": AUTO_MN_TIME,
            "slot": "MN",
        }
        _save_schedule_state()
        print(f"[AUTO] scheduled MN search {AUTO_MN_TIME} key={mn_key}", flush=True)
        seen_bucket = f"{day}:MN"
        start_job(run_scheduled_search, "MN", {"MN"}, "AUTO-" + mn_key, seen_bucket, False)
        wake_render_async(f"{AUTO_MN_TIME} MN")
        return

    # MNの固定再確認：新しく増えた候補だけ追加メール。
    mn_daily_recheck_key = _scheduled_slot_key(day, "MN_DAILY_RECHECK", AUTO_MN_DAILY_RECHECK_TIME)
    if _hhmm_reached(AUTO_MN_DAILY_RECHECK_TIME, now) and _slot_start_allowed(mn_daily_recheck_key, now):
        _schedule_state[mn_daily_recheck_key] = {
            "started_at": now.isoformat(timespec="seconds"),
            "configured_time": AUTO_MN_DAILY_RECHECK_TIME,
            "slot": "MN_DAILY_RECHECK",
        }
        _save_schedule_state()
        print(f"[AUTO] scheduled MN daily recheck {AUTO_MN_DAILY_RECHECK_TIME} key={mn_daily_recheck_key}", flush=True)
        seen_bucket = f"{day}:MN"
        start_job(run_scheduled_search, "MN再確認", {"MN"}, "AUTO-" + mn_daily_recheck_key, seen_bucket, True)
        wake_render_async(f"{AUTO_MN_DAILY_RECHECK_TIME} MN 再確認")

# 検索コマンドIDごとに「最後に通知した実質的な結果」を保存する。
# 同じ検索ID＋同じ内容は送らない。内容が変わった時だけ更新通知する。
_NOTIFY_STATE_PATH = os.path.join(tempfile.gettempdir(), "WINTICKET_F2_MAIL_NOTIFY_STATE.json")

def _load_notify_states():
    try:
        with open(_NOTIFY_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_notify_states():
    try:
        # 古い履歴が増えすぎないよう直近50検索だけ保持。
        items = list(_notified_states.items())[-50:]
        with open(_NOTIFY_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(dict(items), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[MAIL] notify state save warning: {e}", flush=True)

def _meaningful_result_signature(result):
    matches=[]
    for r in list(result.get("matches") or []):
        if not isinstance(r, dict):
            continue
        matches.append({k:r.get(k) for k in (
            "venue","slug","race","session","grade","star","line",
            "three_order","order","bet","bet_type","tickets","decision","stars","rank","rule_id",
            "roi","roi_ex_top1","sample_n","bank_group","condition_name","prediction_url","winticket_url"
        )})
    matches.sort(key=lambda x:(str(x.get("venue")), str(x.get("race")), str(x.get("line")), str(x.get("three_order"))))

    pending=[r for r in list(result.get("skipped") or []) if isinstance(r, dict) and r.get("reason") == "AI予想未発表"]
    errors=[str(x) for x in list(result.get("errors") or [])]
    errors.sort()
    counters=dict(result.get("counters") or {})
    # 現在稼働中の旧Renderは skipped を保持していないため、
    # 重複判定は「未公開件数」で統一する。これでPC側直送と監視側が同じ署名になる。
    pending_count=max(len(pending), int(counters.get("unpublished") or 0))

    payload={
        "matches":matches,
        "pending_count":pending_count,
        "errors":errors,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

def _notify_key(command_id, control_at=0):
    if command_id:
        return str(command_id)
    return "control:" + str(round(float(control_at or 0), 3))

def _remember_notified(key, sig):
    _notified_states[str(key)] = str(sig)
    _save_notify_states()

_notified_states = _load_notify_states()

if not SERVER_URL:
    raise SystemExit("WT_SERVER_URL が未設定です。config.txt を確認してください。")
if not AGENT_TOKEN:
    raise SystemExit("WT_AGENT_TOKEN が未設定です。config.txt を確認してください。")

HEADERS = {"X-Agent-Token": AGENT_TOKEN, "Content-Type": "application/json"}
stop_event = threading.Event()
board_cache = []
running_lock = threading.Lock()
job_thread = None
latest_progress = {"venues_info": [], "matches": [], "errors": [], "counters": {}}

# Renderスリープ対策:
# 自動検索はPC側を先に開始し、Renderの起床は別スレッドで並行実行する。
# Renderが寝ていてもWINTICKET取得・判定・メール送信は止めない。
_render_ready = threading.Event()
_render_waking = threading.Event()
_auto_local_search = threading.Event()
_deferred_finish = {"payload": None}

def _render_health_url():
    return SERVER_URL + "/health"

def _flush_render_state_when_ready():
    if not _render_ready.is_set():
        return
    try:
        snap = dict(latest_progress)
        if snap:
            post("/api/agent/progress", {"running": True, **snap})
        payload = _deferred_finish.get("payload")
        if payload:
            post("/api/agent/finish", payload)
            _deferred_finish["payload"] = None
    except Exception as e:
        print("[RENDER] flush state warning", type(e).__name__, e, flush=True)

def wake_render_async(reason="auto"):
    # v3.12: Render may sleep again after a previous successful wake.
    # Never trust an old _render_ready flag for a new scheduled wake request.
    if _render_waking.is_set():
        return
    _render_ready.clear()
    _render_waking.set()
    def worker():
        try:
            print(f"[RENDER] wake start reason={reason}", flush=True)
            for attempt in range(1, 8):
                try:
                    r = requests.get(_render_health_url(), timeout=12)
                    if r.ok:
                        _render_ready.set()
                        print(f"[RENDER] awake attempt={attempt}", flush=True)
                        _flush_render_state_when_ready()
                        return
                except Exception as e:
                    print(f"[RENDER] wake wait {attempt}/7 {type(e).__name__}", flush=True)
                time.sleep(5)
            print("[RENDER] wake timeout - PC検索/メールは継続", flush=True)
        finally:
            _render_waking.clear()
    threading.Thread(target=worker, daemon=True).start()


def post(path, data):
    try:
        r = requests.post(SERVER_URL + path, headers=HEADERS, json=data, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        _render_ready.clear()
        print("[AGENT] POST FAIL", path, type(e).__name__, e, flush=True)
        return None

def progress(payload):
    global latest_progress
    d = dict(payload or {})
    d["running"] = True
    d["schedule_config"] = _schedule_snapshot("pc")

    snap = dict(latest_progress)
    for k in ("venues_info", "matches", "errors", "counters", "phase", "current", "detail", "schedule_config"):
        if k in d:
            snap[k] = d[k]
    latest_progress = snap

    if _auto_local_search.is_set() and not _render_ready.is_set():
        return
    post("/api/agent/progress", d)

def finish(payload):
    global latest_progress
    d = dict(payload or {})
    d["schedule_config"] = _schedule_snapshot("pc")

    if d.get("phase") == "stopped":
        saved_matches = list(latest_progress.get("matches") or [])
        result_matches = list(d.get("matches") or [])
        if len(saved_matches) > len(result_matches):
            d["matches"] = saved_matches

        if not d.get("venues_info") and latest_progress.get("venues_info"):
            d["venues_info"] = latest_progress.get("venues_info")

        saved_counters = dict(latest_progress.get("counters") or {})
        result_counters = dict(d.get("counters") or {})
        for k, v in saved_counters.items():
            if isinstance(v, (int, float)) and v > result_counters.get(k, 0):
                result_counters[k] = v
        if result_counters:
            d["counters"] = result_counters

        print("[AGENT] stop finished partial_matches", len(d.get("matches") or []), flush=True)

    d["running"] = False
    if _auto_local_search.is_set() and not _render_ready.is_set():
        _deferred_finish["payload"] = dict(d)
    else:
        post("/api/agent/finish", d)
    latest_progress = {
        "venues_info": list(d.get("venues_info") or latest_progress.get("venues_info") or []),
        "matches": list(d.get("matches") or []),
        "errors": list(d.get("errors") or []),
        "counters": dict(d.get("counters") or {}),
        "phase": d.get("phase", ""),
        "current": d.get("current", ""),
        "detail": d.get("detail", ""),
        "schedule_config": d.get("schedule_config", _schedule_snapshot("pc")),
    }

def run_load_board():
    global board_cache
    try:
        stop_event.clear()
        print("[AGENT] load_board start", flush=True)
        result = load_today_board(progress, stop_event)
        board_cache = list(result.get("venues_info", []))
        finish({
            "phase": "stopped" if result.get("stopped") else "select",
            "current": "途中停止" if result.get("stopped") else "開催場を選択",
            "detail": "F2開催場にチェックしてください。",
            "venues_info": board_cache,
            "matches": [],
            "errors": result.get("errors", []),
            "counters": result.get("counters", {}),
        })
        print("[AGENT] load_board done", len(board_cache), flush=True)
    except Exception as e:
        traceback.print_exc()
        finish({"phase":"error","current":"開催場取得エラー","detail":str(e),"errors":[f"{type(e).__name__}: {e}"]})

def _filter_expired_matches(result):
    """
    Remove only already-started races from the final actionable candidate set.

    Safety policy:
    - No extra network access.
    - No changes to F2 / girls / AI star / line / mark / profit-rule acquisition.
    - Uses only start_time metadata already collected from the opened race header.
    - If start_time is missing/unparseable, keep the candidate rather than risk a false exclusion.
    """
    d = dict(result or {})
    matches = list(d.get("matches") or [])
    if not matches:
        return d

    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst)
    kept = []
    expired = []
    for r in matches:
        if not isinstance(r, dict):
            kept.append(r)
            continue
        st = str(r.get("start_time") or "").strip()
        race_date = str(r.get("race_date") or now_jst.strftime("%Y-%m-%d"))[:10]
        try:
            scheduled = datetime.strptime(f"{race_date} {st}", "%Y-%m-%d %H:%M").replace(tzinfo=jst)
            if now_jst >= scheduled:
                expired.append(r)
                continue
        except Exception:
            kept.append(r)
            continue
        kept.append(r)

    if expired:
        skipped = list(d.get("skipped") or [])
        for r in expired:
            skipped.append({
                "venue": r.get("venue"),
                "race": r.get("race"),
                "reason": "発走済みのため買える候補から除外",
                "start_time": r.get("start_time"),
            })
            print(
                f"[AGENT] expired candidate removed {r.get('venue')} {r.get('race')}R start={r.get('start_time')}",
                flush=True,
            )
        d["skipped"] = skipped
        counters = dict(d.get("counters") or {})
        counters["expired"] = int(counters.get("expired") or 0) + len(expired)
        counters["matched"] = len(kept)
        d["counters"] = counters

    d["matches"] = kept
    return d


def run_search(selected, command_id=None, seen_bucket=None, new_only=False):
    global board_cache, latest_progress
    try:
        stop_event.clear()
        latest_progress = {
            "venues_info": list(board_cache),
            "matches": [],
            "errors": [],
            "counters": {},
            "phase": "races",
            "current": "検索開始",
            "detail": "",
        }
        print("[AGENT] search start", selected, flush=True)
        result = scan_selected(selected, board_cache, progress, stop_event)
        # v3.13.3: 検索・利益判定はそのまま。最終表示/通知の直前だけ発走済みを除外。
        result = _filter_expired_matches(result)

        # 通常検索の結果メールは検索本体から1通だけ送る。
        # 固定再確認では、1回目以降に新しく出現した候補だけを追加通知する。
        if not result.get("stopped"):
            mail_result = result
            should_mail = True
            if seen_bucket:
                new_matches = _split_new_matches(seen_bucket, result.get("matches", []), initialize_only=not new_only)
                if new_only:
                    should_mail = len(new_matches) > 0
                    mail_result = dict(result)
                    mail_result["matches"] = new_matches
                    counters = dict(result.get("counters") or {})
                    counters["matched"] = len(new_matches)
                    mail_result["counters"] = counters
                    if should_mail:
                        print(f"[AUTO] {_current_slot_label} 新規候補 {len(new_matches)}件 -> 追加通知", flush=True)
                    else:
                        print(f"[AUTO] {_current_slot_label} 新規候補 0件 -> 追加メールなし", flush=True)

            if mail_enabled(_local) and should_mail:
                try:
                    subject = send_search_result(_local, mail_result, update=new_only, slot_label=_current_slot_label)
                    globals()["_last_mail_sent_at"] = time.time()
                    notify_key = _notify_key(command_id)
                    notify_sig = _meaningful_result_signature(mail_result)
                    _remember_notified(notify_key, notify_sig)
                    print("[MAIL] sent", subject, flush=True)
                except Exception as mail_exc:
                    print("[MAIL] send failed", type(mail_exc).__name__, mail_exc, flush=True)
                    _append_log(f"MAIL_SEND_FAIL: {type(mail_exc).__name__}: {mail_exc}")
            elif not mail_enabled(_local):
                _, reason = validate_mail_config(_local)
                print("[MAIL] disabled", reason, flush=True)

        finish({
            "phase": "stopped" if result.get("stopped") else "done",
            "current": "途中停止" if result.get("stopped") else "検索完了",
            "detail": "停止しました。" if result.get("stopped") else "選択した開催場の検索が完了しました。",
            "venues_info": board_cache,
            "matches": result.get("matches", []),
            "skipped": result.get("skipped", []),
            "errors": result.get("errors", []),
            "counters": result.get("counters", {}),
        })
        print("[AGENT] search done", len(result.get("matches", [])), flush=True)

        # 15:00 MNだけ、AI未公開が残った時に同じ検索経路を後で再利用する。
        # 何も変わらなければメール署名が同じなので重複送信しない。
        if _current_slot_label.startswith("MN") and AUTO_MN_RECHECK_ENABLED:
            unpublished = int((result.get("counters") or {}).get("unpublished") or 0)
            if unpublished > 0:
                _mn_recheck["day"] = datetime.now().strftime("%Y-%m-%d")
                _mn_recheck["due_at"] = time.time() + AUTO_MN_RECHECK_MINUTES * 60
                _mn_recheck["count"] = 0
                print(f"[AUTO] MN AI未公開 {unpublished}R -> {AUTO_MN_RECHECK_MINUTES}分後に自動再確認", flush=True)
            else:
                _mn_recheck.update({"day":"","due_at":0.0,"count":0})
    except Exception as e:
        traceback.print_exc()
        finish({"phase":"error","current":"検索エラー","detail":str(e),"errors":[f"{type(e).__name__}: {e}"]})

def start_job(target, *args):
    global job_thread
    if job_thread and job_thread.is_alive():
        return False
    job_thread = threading.Thread(target=target, args=args, daemon=True)
    job_thread.start()
    return True

def watch_server_completion():
    """
    Render側の検索完了を監視する保険経路。
    通常検索がすでに同じ command_id＋同じ内容を通知済みなら何もしない。
    同じ検索の結果内容が実質的に変わった時だけ「更新」として再通知する。
    """
    global _last_watched_done_key, _last_mail_sent_at, board_cache
    try:
        r = requests.get(SERVER_URL + "/api/status", timeout=8)
        r.raise_for_status()
        st = r.json() or {}

        if not (job_thread and job_thread.is_alive()) and st.get("venues_info"):
            board_cache = list(st.get("venues_info") or board_cache)

        if st.get("running") or st.get("phase") != "done":
            return
        if st.get("last_control") not in ("検索", "再検索", "自動検索"):
            return
        control_at = float(st.get("last_control_at") or 0)
        if control_at < _AGENT_STARTED_AT - 2:
            return

        key = (str(st.get("active_command_id") or ""), round(control_at, 3), round(float(st.get("updated_at") or 0), 3))
        if key == _last_watched_done_key:
            return
        _last_watched_done_key = key

        result = {
            "matches": list(st.get("matches") or []),
            "skipped": list(st.get("skipped") or []),
            "errors": list(st.get("errors") or []),
            "counters": dict(st.get("counters") or {}),
            "venues_info": list(st.get("venues_info") or []),
            "stopped": False,
        }
        command_key = _notify_key(st.get("active_command_id"), control_at)
        current_sig = _meaningful_result_signature(result)
        previous_sig = _notified_states.get(command_key)

        # 検索本体がすでに同じ内容を送信済み → 完全に無視。
        if previous_sig == current_sig:
            return

        if mail_enabled(_local):
            # 同じ検索IDに過去通知があり内容だけ変わった場合は「更新通知」。
            is_update = previous_sig is not None
            subject = send_search_result(_local, result, update=is_update)
            _last_mail_sent_at = time.time()
            _remember_notified(command_key, current_sig)
            print("[MAIL] update sent" if is_update else "[MAIL] watched completion sent", subject, flush=True)
        else:
            _, reason = validate_mail_config(_local)
            print("[MAIL] watched completion disabled", reason, flush=True)
    except Exception as e:
        print("[MAIL] completion watch fail", type(e).__name__, e, flush=True)

def check_mn_recheck():
    global _current_slot_label
    if not AUTO_MN_RECHECK_ENABLED or AUTO_MN_RECHECK_MAX <= 0:
        return
    if job_thread and job_thread.is_alive():
        return
    today = datetime.now().strftime("%Y-%m-%d")
    if _mn_recheck.get("day") != today or float(_mn_recheck.get("due_at") or 0) <= 0:
        return
    if time.time() < float(_mn_recheck.get("due_at") or 0):
        return
    count = int(_mn_recheck.get("count") or 0)
    if count >= AUTO_MN_RECHECK_MAX:
        print("[AUTO] MN AI未公開の自動再確認 上限到達", flush=True)
        _mn_recheck.update({"day":"","due_at":0.0,"count":0})
        return
    _mn_recheck["count"] = count + 1
    _mn_recheck["due_at"] = time.time() + AUTO_MN_RECHECK_MINUTES * 60
    _current_slot_label = f"MN再確認{count+1}"
    command_id = f"AUTO-{today}:15_MN_RECHECK_{count+1}"
    print(f"[AUTO] MN AI未公開 再確認 {count+1}/{AUTO_MN_RECHECK_MAX}", flush=True)
    start_job(run_scheduled_search, f"MN再確認{count+1}", {"MN"}, command_id)
    wake_render_async(f"MN再確認{count+1}")

print("[AUTO] schedule slot logic v3.22.0 (AUTO slug fix + daily second-pass recheck)", flush=True)
print(f"[AUTO] active schedule morning={AUTO_MORNING_TIME} M/D/N | morning recheck={AUTO_MORNING_RECHECK_TIME} M/D/N | mn={AUTO_MN_TIME} MN | mn recheck={AUTO_MN_DAILY_RECHECK_TIME} MN", flush=True)
print("[AGENT] PC relay agent started", SERVER_URL, flush=True)
print("[AGENT] config loaded OK", flush=True)
print("[AGENT] mail-agent exclusive lock OK (other Python tools can coexist)", flush=True)
_mail_ok, _mail_reason = validate_mail_config(_local)
print("[MAIL] " + ("enabled" if _mail_ok else _mail_reason), flush=True)

while True:
    try:
        r = requests.get(SERVER_URL + "/api/agent/next", headers=HEADERS, timeout=8)
        if r.status_code == 401:
            print("[AGENT] token mismatch", flush=True)
            time.sleep(5)
            continue
        r.raise_for_status()
        cmd = (r.json() or {}).get("command")
        if cmd:
            kind = cmd.get("kind")
            payload = cmd.get("payload") or {}
            print("[AGENT] command", kind, flush=True)
            if kind == "set_schedule":
                try:
                    morning = str(payload.get("morning_time") or "").strip()
                    mn = str(payload.get("mn_time") or "").strip()
                    snap = apply_schedule_config(morning, mn)
                    finish({
                        "phase": "idle",
                        "current": "自動検索時刻を更新",
                        "detail": f"朝 {morning}（M/D/N） / MN {mn}（MN）へ更新しました。",
                        "venues_info": board_cache,
                        "matches": latest_progress.get("matches", []),
                        "errors": [],
                        "counters": latest_progress.get("counters", {}),
                        "schedule_config": snap,
                    })
                except Exception as e:
                    print("[AUTO] schedule update fail", type(e).__name__, e, flush=True)
                    finish({
                        "phase": "error",
                        "current": "自動検索時刻の更新エラー",
                        "detail": str(e),
                        "errors": [f"{type(e).__name__}: {e}"],
                        "schedule_config": _schedule_snapshot("pc"),
                    })
            elif kind == "stop":
                stop_event.set()
                if job_thread and job_thread.is_alive():
                    snap = dict(latest_progress)
                    post("/api/agent/progress", {
                        "running": True,
                        "phase": "stopping",
                        "current": "停止処理中",
                        "detail": "現在の処理が安全に止まるのを待っています。",
                        "venues_info": snap.get("venues_info", board_cache),
                        "matches": snap.get("matches", []),
                        "errors": snap.get("errors", []),
                        "counters": snap.get("counters", {}),
                    })
                else:
                    snap = dict(latest_progress)
                    finish({
                        "phase": "stopped",
                        "current": "途中停止",
                        "detail": "停止しました。",
                        "venues_info": snap.get("venues_info", board_cache),
                        "matches": snap.get("matches", []),
                        "errors": snap.get("errors", []),
                        "counters": snap.get("counters", {}),
                    })
            elif kind == "load_board":
                if not start_job(run_load_board):
                    post("/api/agent/progress", {"running":True,"detail":"すでにPCで取得中です。"})
            elif kind == "search_selected":
                selected = payload.get("selected") or []
                # 手動検索メールが直前の自動スロット名（MN/朝）を引き継がないようにする。
                _current_slot_label = "手動"
                if not start_job(run_search, selected, cmd.get("id")):
                    post("/api/agent/progress", {"running":True,"detail":"すでにPCで取得中です。"})
        check_auto_schedule()
        check_mn_recheck()
        watch_server_completion()
        time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        break
    except Exception as e:
        print("[AGENT] poll fail", type(e).__name__, e, flush=True)
        time.sleep(3)