import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DB_PATH = Path(os.environ.get("DB_PATH", str(BASE_DIR / "galactic_fleet.db")))

TEAM_ORDER = [
    "ORANGE_NOVA",
    "PURPLE_PULSE",
    "PINK_GALAXY",
    "GREEN_ORBIT",
    "BLUE_COMET",
]

TEAM_META = {
    "ORANGE_NOVA": {"name": "ORANGE NOVA", "color": "#FF6B00", "accent": "#FF9E00"},
    "PURPLE_PULSE": {"name": "PURPLE PULSE", "color": "#9D4EDD", "accent": "#C77DFF"},
    "PINK_GALAXY": {"name": "PINK GALAXY", "color": "#FF3E9D", "accent": "#FF75B5"},
    "GREEN_ORBIT": {"name": "GREEN ORBIT", "color": "#00E676", "accent": "#38EF7D"},
    "BLUE_COMET": {"name": "BLUE COMET", "color": "#0EA5E9", "accent": "#38BDF8"},
}

REQUIRED_SHIPS = {
    "flagship": 4,
    "cruiser": 3,
    "scout": 2,
    "fighter_1": 1,
    "fighter_2": 1,
}
TOTAL_SHIP_CELLS = sum(REQUIRED_SHIPS.values())  # 12
INITIAL_BULLETS = 5
GRID_SIZE = 8

app = FastAPI(title="RC BEYOND TOGETHER: GALACTIC FLEET")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rooms (
                room_code TEXT PRIMARY KEY,
                phase TEXT NOT NULL DEFAULT 'lobby',
                current_turn TEXT,
                winner TEXT,
                host_token TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS teams (
                room_code TEXT NOT NULL,
                team_id TEXT NOT NULL,
                player_token TEXT,
                player_name TEXT,
                confirmed INTEGER NOT NULL DEFAULT 0,
                bullets INTEGER NOT NULL DEFAULT 5,
                eliminated INTEGER NOT NULL DEFAULT 0,
                last_target_team TEXT,
                ships_json TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (room_code, team_id),
                FOREIGN KEY (room_code) REFERENCES rooms(room_code) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS shots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_code TEXT NOT NULL,
                shooter_team TEXT NOT NULL,
                target_team TEXT NOT NULL,
                row_idx INTEGER NOT NULL,
                col_idx INTEGER NOT NULL,
                coord TEXT NOT NULL,
                is_hit INTEGER NOT NULL,
                ship_id TEXT,
                ship_sunk INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                UNIQUE (room_code, target_team, row_idx, col_idx)
            );

            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_code TEXT NOT NULL,
                message TEXT NOT NULL,
                log_type TEXT NOT NULL,
                shooter_team TEXT,
                target_team TEXT,
                created_at TEXT NOT NULL
            );
            """
        )


init_db()


class ConnectionManager:
    def __init__(self):
        self.rooms: Dict[str, List[WebSocket]] = {}

    async def connect(self, room_code: str, websocket: WebSocket):
        await websocket.accept()
        self.rooms.setdefault(room_code, []).append(websocket)

    def disconnect(self, room_code: str, websocket: WebSocket):
        if room_code in self.rooms and websocket in self.rooms[room_code]:
            self.rooms[room_code].remove(websocket)

    async def broadcast(self, room_code: str, event_type: str = "state_update"):
        conns = list(self.rooms.get(room_code, []))
        dead = []
        for ws in conns:
            try:
                await ws.send_json({"type": event_type, "room_code": room_code})
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(room_code, ws)


manager = ConnectionManager()


def normalize_room(code: str) -> str:
    cleaned = "".join(ch for ch in code.strip().upper() if ch.isalnum() or ch in "-_")
    if not cleaned:
        raise HTTPException(status_code=400, detail="กรุณากรอกรหัสห้องที่ถูกต้อง")
    return cleaned[:24]


def ensure_room(conn: sqlite3.Connection, room_code: str):
    row = conn.execute("SELECT * FROM rooms WHERE room_code = ?", (room_code,)).fetchone()
    if row is None:
        now = datetime.utcnow().isoformat()
        host_token = secrets.token_urlsafe(16)
        conn.execute(
            "INSERT INTO rooms (room_code, phase, current_turn, winner, host_token, created_at) VALUES (?, 'lobby', NULL, NULL, ?, ?)",
            (room_code, host_token, now),
        )
        for tid in TEAM_ORDER:
            conn.execute(
                "INSERT INTO teams (room_code, team_id, bullets) VALUES (?, ?, ?)",
                (room_code, tid, INITIAL_BULLETS),
            )
        conn.execute(
            "INSERT INTO logs (room_code, message, log_type, created_at) VALUES (?, ?, 'system', ?)",
            (room_code, f"สร้างห้อง {room_code} สำเร็จ รอผู้เล่นครบ 5 ทีม", now),
        )
        row = conn.execute("SELECT * FROM rooms WHERE room_code = ?", (room_code,)).fetchone()
    return row


def coord_label(r: int, c: int) -> str:
    return f"{chr(65 + c)}{r + 1}"


def build_state_payload(conn: sqlite3.Connection, room_code: str, viewer_token: Optional[str] = None) -> dict:
    room = ensure_room(conn, room_code)
    teams_rows = conn.execute(
        "SELECT * FROM teams WHERE room_code = ?", (room_code,)
    ).fetchall()
    team_map = {row["team_id"]: row for row in teams_rows}

    shots_rows = conn.execute(
        "SELECT * FROM shots WHERE room_code = ? ORDER BY id ASC", (room_code,)
    ).fetchall()
    logs_rows = conn.execute(
        "SELECT * FROM logs WHERE room_code = ? ORDER BY id DESC LIMIT 30", (room_code,)
    ).fetchall()

    viewer_team = None
    if viewer_token:
        for tid in TEAM_ORDER:
            if team_map[tid]["player_token"] == viewer_token:
                viewer_team = tid
                break

    # Compute hits per target team
    hits_by_target: Dict[str, List[dict]] = {tid: [] for tid in TEAM_ORDER}
    hit_coords_by_target: Dict[str, set] = {tid: set() for tid in TEAM_ORDER}
    for s in shots_rows:
        hits_by_target[s["target_team"]].append(
            {
                "row": s["row_idx"],
                "col": s["col_idx"],
                "coord": s["coord"],
                "is_hit": bool(s["is_hit"]),
                "shooter_team": s["shooter_team"],
                "ship_sunk": bool(s["ship_sunk"]),
            }
        )
        if s["is_hit"]:
            hit_coords_by_target[s["target_team"]].add((s["row_idx"], s["col_idx"]))

    teams_payload = {}
    for tid in TEAM_ORDER:
        t = team_map[tid]
        ships = json.loads(t["ships_json"] or "[]")
        hit_set = hit_coords_by_target[tid]

        # Count surviving cells & ships
        total_cells = sum(s["size"] for s in ships) if ships else TOTAL_SHIP_CELLS
        hit_cells = len(hit_set)
        remaining_cells = max(0, total_cells - hit_cells) if t["confirmed"] else TOTAL_SHIP_CELLS

        remaining_ships = 0
        sunk_ships_public = []
        if ships:
            for s in ships:
                cells = {(c["row"], c["col"]) for c in s["cells"]}
                if not cells.issubset(hit_set):
                    remaining_ships += 1
                else:
                    sunk_ships_public.append(s)
        else:
            remaining_ships = len(REQUIRED_SHIPS)

        # Anti-cheat: Only send full ship positions to the owning team!
        # Opponents only receive sunk ships + hit/miss shots.
        is_owner = viewer_team == tid
        teams_payload[tid] = {
            "team_id": tid,
            "name": TEAM_META[tid]["name"],
            "color": TEAM_META[tid]["color"],
            "accent": TEAM_META[tid]["accent"],
            "claimed": bool(t["player_token"]),
            "player_name": t["player_name"] or "",
            "confirmed": bool(t["confirmed"]),
            "bullets": t["bullets"],
            "eliminated": bool(t["eliminated"]),
            "last_target_team": t["last_target_team"],
            "remaining_cells": remaining_cells,
            "remaining_ships": remaining_ships if not t["eliminated"] else 0,
            "shots_received": hits_by_target[tid],
            "ships": ships if is_owner else [],
            "sunk_ships": sunk_ships_public,
        }

    return {
        "room_code": room["room_code"],
        "phase": room["phase"],
        "current_turn": room["current_turn"],
        "winner": room["winner"],
        "viewer_team": viewer_team,
        "teams": teams_payload,
        "team_order": TEAM_ORDER,
        "logs": [
            {
                "id": l["id"],
                "message": l["message"],
                "type": l["log_type"],
                "shooter_team": l["shooter_team"],
                "target_team": l["target_team"],
                "created_at": l["created_at"],
            }
            for l in logs_rows
        ],
    }


class JoinRequest(BaseModel):
    room_code: str
    team_id: str
    player_name: Optional[str] = ""
    player_token: Optional[str] = None


class ShipInput(BaseModel):
    id: str
    name: str
    size: int
    row: int
    col: int
    orientation: str  # 'H' or 'V'


class ConfirmFleetRequest(BaseModel):
    room_code: str
    team_id: str
    player_token: str
    ships: List[ShipInput]


class FireRequest(BaseModel):
    room_code: str
    team_id: str
    player_token: str
    target_team: str
    row: int
    col: int


class SkipRequest(BaseModel):
    room_code: str
    team_id: str
    player_token: str


class ResetRequest(BaseModel):
    room_code: str


@app.get("/api/health")
def health():
    return {"status": "ok", "game": "RC BEYOND TOGETHER: GALACTIC FLEET"}


@app.get("/api/room/{room_code}")
def get_room_state(room_code: str, player_token: Optional[str] = None):
    code = normalize_room(room_code)
    with get_db() as conn:
        return build_state_payload(conn, code, player_token)


@app.post("/api/room/join")
async def join_team(req: JoinRequest):
    code = normalize_room(req.room_code)
    if req.team_id not in TEAM_META:
        raise HTTPException(status_code=400, detail="ไม่พบทีมที่เลือก")

    with get_db() as conn:
        ensure_room(conn, code)
        row = conn.execute(
            "SELECT * FROM teams WHERE room_code = ? AND team_id = ?", (code, req.team_id)
        ).fetchone()

        # Allow reconnect if caller already holds the token
        if row["player_token"]:
            if req.player_token and req.player_token == row["player_token"]:
                token = row["player_token"]
            else:
                raise HTTPException(status_code=409, detail="ทีมนี้ถูกเลือกแล้ว")
        else:
            # Ensure caller isn't claiming a second team with the same token
            if req.player_token:
                existing = conn.execute(
                    "SELECT team_id FROM teams WHERE room_code = ? AND player_token = ?",
                    (code, req.player_token),
                ).fetchone()
                if existing:
                    raise HTTPException(status_code=409, detail="อุปกรณ์นี้เลือกทีมไปแล้ว")
            token = secrets.token_urlsafe(16)
            now = datetime.utcnow().isoformat()
            conn.execute(
                "UPDATE teams SET player_token = ?, player_name = ? WHERE room_code = ? AND team_id = ?",
                (token, (req.player_name or "").strip()[:30], code, req.team_id),
            )
            conn.execute(
                "INSERT INTO logs (room_code, message, log_type, shooter_team, created_at) VALUES (?, ?, 'join', ?, ?)",
                (code, f"{TEAM_META[req.team_id]['name']} เข้าร่วมห้องแล้ว", req.team_id, now),
            )

        state = build_state_payload(conn, code, token)

    await manager.broadcast(code)
    return {"player_token": token, "state": state}


@app.post("/api/fleet/confirm")
async def confirm_fleet(req: ConfirmFleetRequest):
    code = normalize_room(req.room_code)
    if req.team_id not in TEAM_META:
        raise HTTPException(status_code=400, detail="ไม่พบทีม")

    with get_db() as conn:
        room = ensure_room(conn, code)
        if room["phase"] not in ("lobby", "placement"):
            raise HTTPException(status_code=400, detail="เกมเริ่มไปแล้ว ไม่สามารถแก้ไขตำแหน่งยานได้")

        team = conn.execute(
            "SELECT * FROM teams WHERE room_code = ? AND team_id = ?", (code, req.team_id)
        ).fetchone()
        if not team["player_token"] or team["player_token"] != req.player_token:
            raise HTTPException(status_code=403, detail="คุณไม่มีสิทธิ์แก้ไขกองยานของทีมนี้")
        if team["confirmed"]:
            raise HTTPException(status_code=400, detail="ทีมนี้ยืนยันกองยานแล้ว")

        # Validate exact 4 ships, sizes, bounds, and no overlaps
        if len(req.ships) != len(REQUIRED_SHIPS):
            raise HTTPException(status_code=400, detail="ต้องวางยานให้ครบทั้ง 5 ลำ")

        seen_ids = set()
        occupied = set()
        normalized_ships = []
        for s in req.ships:
            if s.id not in REQUIRED_SHIPS or s.id in seen_ids:
                raise HTTPException(status_code=400, detail="ข้อมูลยานไม่ถูกต้อง")
            expected_size = REQUIRED_SHIPS[s.id]
            if s.size != expected_size:
                raise HTTPException(status_code=400, detail="ขนาดยานไม่ตรงตามกติกา")
            if s.orientation not in ("H", "V"):
                raise HTTPException(status_code=400, detail="ทิศทางยานต้องเป็นแนวนอนหรือแนวตั้ง")
            seen_ids.add(s.id)

            cells = []
            for i in range(expected_size):
                r = s.row + (i if s.orientation == "V" else 0)
                c = s.col + (i if s.orientation == "H" else 0)
                if not (0 <= r < GRID_SIZE and 0 <= c < GRID_SIZE):
                    raise HTTPException(status_code=400, detail="วางยานออกนอกกระดาน 8×8")
                if (r, c) in occupied:
                    raise HTTPException(status_code=400, detail="ห้ามวางยานทับกัน")
                occupied.add((r, c))
                cells.append({"row": r, "col": c})

            normalized_ships.append(
                {
                    "id": s.id,
                    "name": s.name,
                    "size": expected_size,
                    "row": s.row,
                    "col": s.col,
                    "orientation": s.orientation,
                    "cells": cells,
                }
            )

        now = datetime.utcnow().isoformat()
        conn.execute(
            "UPDATE teams SET confirmed = 1, ships_json = ? WHERE room_code = ? AND team_id = ?",
            (json.dumps(normalized_ships), code, req.team_id),
        )
        conn.execute(
            "INSERT INTO logs (room_code, message, log_type, shooter_team, created_at) VALUES (?, ?, 'confirm', ?, ?)",
            (code, f"{TEAM_META[req.team_id]['name']} ยืนยันกองยานพร้อมรบแล้ว", req.team_id, now),
        )

        # Check if all 5 teams are confirmed
        confirmed_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM teams WHERE room_code = ? AND confirmed = 1", (code,)
        ).fetchone()["cnt"]
        if confirmed_count == len(TEAM_ORDER):
            conn.execute(
                "UPDATE rooms SET phase = 'battle', current_turn = ? WHERE room_code = ?",
                (TEAM_ORDER[0], code),
            )
            conn.execute(
                "INSERT INTO logs (room_code, message, log_type, shooter_team, created_at) VALUES (?, ?, 'system', ?, ?)",
                (code, "ครบทั้ง 5 ทีมแล้ว! เริ่มการต่อสู้ — เทิร์นแรก: ORANGE NOVA", TEAM_ORDER[0], now),
            )
        else:
            conn.execute(
                "UPDATE rooms SET phase = 'placement' WHERE room_code = ? AND phase = 'lobby'",
                (code,),
            )

        state = build_state_payload(conn, code, req.player_token)

    await manager.broadcast(code)
    return {"state": state}


def advance_turn(conn: sqlite3.Connection, room_code: str, current_team: str) -> Optional[str]:
    teams = conn.execute(
        "SELECT team_id, eliminated FROM teams WHERE room_code = ?", (room_code,)
    ).fetchall()
    elim_map = {t["team_id"]: bool(t["eliminated"]) for t in teams}
    alive = [tid for tid in TEAM_ORDER if not elim_map[tid]]
    if len(alive) <= 1:
        return None
    idx = TEAM_ORDER.index(current_team)
    for offset in range(1, len(TEAM_ORDER) + 1):
        cand = TEAM_ORDER[(idx + offset) % len(TEAM_ORDER)]
        if not elim_map[cand]:
            return cand
    return None


@app.post("/api/battle/fire")
async def fire_shot(req: FireRequest):
    code = normalize_room(req.room_code)
    if req.team_id not in TEAM_META or req.target_team not in TEAM_META:
        raise HTTPException(status_code=400, detail="ข้อมูลทีมไม่ถูกต้อง")
    if req.team_id == req.target_team:
        raise HTTPException(status_code=400, detail="ไม่สามารถยิงทีมตัวเองได้")
    if not (0 <= req.row < GRID_SIZE and 0 <= req.col < GRID_SIZE):
        raise HTTPException(status_code=400, detail="พิกัดอยู่นอกกระดาน")

    with get_db() as conn:
        room = ensure_room(conn, code)
        if room["phase"] != "battle":
            raise HTTPException(status_code=400, detail="เกมไม่ได้อยู่ในช่วงต่อสู้")
        if room["current_turn"] != req.team_id:
            raise HTTPException(status_code=403, detail="ยังไม่ถึงเทิร์นของคุณ")

        shooter = conn.execute(
            "SELECT * FROM teams WHERE room_code = ? AND team_id = ?", (code, req.team_id)
        ).fetchone()
        if not shooter["player_token"] or shooter["player_token"] != req.player_token:
            raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์ยิงแทนทีมอื่น")
        if shooter["eliminated"]:
            raise HTTPException(status_code=400, detail="ทีมของคุณถูกทำลายแล้ว")
        if shooter["bullets"] <= 0:
            raise HTTPException(status_code=400, detail="กระสุนหมด กรุณากดข้ามเทิร์น")

        target = conn.execute(
            "SELECT * FROM teams WHERE room_code = ? AND team_id = ?", (code, req.target_team)
        ).fetchone()
        if target["eliminated"]:
            raise HTTPException(status_code=400, detail="ทีมเป้าหมายถูกทำลายไปแล้ว")

        alive_opponents = conn.execute(
            "SELECT team_id FROM teams WHERE room_code = ? AND team_id != ? AND eliminated = 0",
            (code, req.team_id),
        ).fetchall()
        if len(alive_opponents) > 1 and shooter["last_target_team"] == req.target_team:
            raise HTTPException(status_code=400, detail="ห้ามยิงทีมเดิมซ้ำกับรอบก่อนหน้า กรุณาเลือกทีมอื่น")

        dup = conn.execute(
            "SELECT id FROM shots WHERE room_code = ? AND target_team = ? AND row_idx = ? AND col_idx = ?",
            (code, req.target_team, req.row, req.col),
        ).fetchone()
        if dup:
            raise HTTPException(status_code=400, detail="ห้ามยิงพิกัดเดิมซ้ำ")

        ships = json.loads(target["ships_json"] or "[]")
        hit_ship = None
        for s in ships:
            for c in s["cells"]:
                if c["row"] == req.row and c["col"] == req.col:
                    hit_ship = s
                    break
            if hit_ship:
                break

        is_hit = 1 if hit_ship else 0
        coord = coord_label(req.row, req.col)
        now = datetime.utcnow().isoformat()

        # Check if ship sunk & if target eliminated
        existing_hits = {
            (r["row_idx"], r["col_idx"])
            for r in conn.execute(
                "SELECT row_idx, col_idx FROM shots WHERE room_code = ? AND target_team = ? AND is_hit = 1",
                (code, req.target_team),
            ).fetchall()
        }
        if is_hit:
            existing_hits.add((req.row, req.col))

        ship_sunk = 0
        if hit_ship:
            ship_cells = {(c["row"], c["col"]) for c in hit_ship["cells"]}
            if ship_cells.issubset(existing_hits):
                ship_sunk = 1

        conn.execute(
            """
            INSERT INTO shots (room_code, shooter_team, target_team, row_idx, col_idx, coord, is_hit, ship_id, ship_sunk, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                req.team_id,
                req.target_team,
                req.row,
                req.col,
                coord,
                is_hit,
                hit_ship["id"] if hit_ship else None,
                ship_sunk,
                now,
            ),
        )

        conn.execute(
            "UPDATE teams SET last_target_team = ? WHERE room_code = ? AND team_id = ?",
            (req.target_team, code, req.team_id),
        )

        s_name = TEAM_META[req.team_id]["name"]
        t_name = TEAM_META[req.target_team]["name"]

        if is_hit:
            # Shooter keeps bullets
            msg = f"{s_name} ยิงโดน {t_name} ที่ {coord}"
            if ship_sunk:
                msg += f" ({hit_ship['name']} ถูกทำลาย!)"
            conn.execute(
                "INSERT INTO logs (room_code, message, log_type, shooter_team, target_team, created_at) VALUES (?, ?, 'hit', ?, ?, ?)",
                (code, msg, req.team_id, req.target_team, now),
            )
            # Check if all target ship cells destroyed
            all_target_cells = {
                (c["row"], c["col"]) for s in ships for c in s["cells"]
            }
            if all_target_cells.issubset(existing_hits):
                conn.execute(
                    "UPDATE teams SET eliminated = 1 WHERE room_code = ? AND team_id = ?",
                    (code, req.target_team),
                )
                conn.execute(
                    "INSERT INTO logs (room_code, message, log_type, shooter_team, target_team, created_at) VALUES (?, ?, 'eliminated', ?, ?, ?)",
                    (code, f"ยานทุกลำของ {t_name} ถูกทำลาย! {t_name} ออกจากเกม", req.team_id, req.target_team, now),
                )
        else:
            # Miss: shooter -1 bullet, target +1 bullet
            conn.execute(
                "UPDATE teams SET bullets = MAX(0, bullets - 1) WHERE room_code = ? AND team_id = ?",
                (code, req.team_id),
            )
            conn.execute(
                "UPDATE teams SET bullets = bullets + 1 WHERE room_code = ? AND team_id = ?",
                (code, req.target_team),
            )
            msg = f"{s_name} ยิงพลาด {t_name} ที่ {coord} และ {t_name} ได้กระสุนเพิ่ม 1 นัด"
            conn.execute(
                "INSERT INTO logs (room_code, message, log_type, shooter_team, target_team, created_at) VALUES (?, ?, 'miss', ?, ?, ?)",
                (code, msg, req.team_id, req.target_team, now),
            )

        # Check winner
        alive_rows = conn.execute(
            "SELECT team_id FROM teams WHERE room_code = ? AND eliminated = 0", (code,)
        ).fetchall()
        if len(alive_rows) == 1:
            winner_id = alive_rows[0]["team_id"]
            conn.execute(
                "UPDATE rooms SET phase = 'finished', winner = ?, current_turn = NULL WHERE room_code = ?",
                (winner_id, code),
            )
            conn.execute(
                "INSERT INTO logs (room_code, message, log_type, shooter_team, created_at) VALUES (?, ?, 'win', ?, ?)",
                (code, f"🏆 {TEAM_META[winner_id]['name']} เป็นผู้ชนะแห่ง GALACTIC FLEET!", winner_id, now),
            )
        else:
            next_turn = advance_turn(conn, code, req.team_id)
            conn.execute(
                "UPDATE rooms SET current_turn = ? WHERE room_code = ?", (next_turn, code)
            )

        state = build_state_payload(conn, code, req.player_token)

    await manager.broadcast(code)
    return {
        "result": {
            "is_hit": bool(is_hit),
            "ship_sunk": bool(ship_sunk),
            "coord": coord,
            "target_team": req.target_team,
        },
        "state": state,
    }


@app.post("/api/battle/skip")
async def skip_turn(req: SkipRequest):
    code = normalize_room(req.room_code)
    with get_db() as conn:
        room = ensure_room(conn, code)
        if room["phase"] != "battle" or room["current_turn"] != req.team_id:
            raise HTTPException(status_code=403, detail="ยังไม่ถึงเทิร์นของคุณ")
        team = conn.execute(
            "SELECT * FROM teams WHERE room_code = ? AND team_id = ?", (code, req.team_id)
        ).fetchone()
        if not team["player_token"] or team["player_token"] != req.player_token:
            raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์ดำเนินการ")
        if team["bullets"] > 0:
            raise HTTPException(status_code=400, detail="คุณยังมีกระสุนเหลืออยู่ ไม่สามารถข้ามเทิร์นได้")

        now = datetime.utcnow().isoformat()
        next_turn = advance_turn(conn, code, req.team_id)
        conn.execute("UPDATE rooms SET current_turn = ? WHERE room_code = ?", (next_turn, code))
        conn.execute(
            "INSERT INTO logs (room_code, message, log_type, shooter_team, created_at) VALUES (?, ?, 'skip', ?, ?)",
            (code, f"{TEAM_META[req.team_id]['name']} กระสุนหมด — ข้ามเทิร์น", req.team_id, now),
        )
        state = build_state_payload(conn, code, req.player_token)

    await manager.broadcast(code)
    return {"state": state}


@app.post("/api/room/reset")
async def reset_room(req: ResetRequest):
    code = normalize_room(req.room_code)
    now = datetime.utcnow().isoformat()
    with get_db() as conn:
        ensure_room(conn, code)
        conn.execute("DELETE FROM shots WHERE room_code = ?", (code,))
        conn.execute("DELETE FROM logs WHERE room_code = ?", (code,))
        conn.execute(
            "UPDATE rooms SET phase = 'lobby', current_turn = NULL, winner = NULL WHERE room_code = ?",
            (code,),
        )
        conn.execute(
            """
            UPDATE teams
            SET player_token = NULL, player_name = NULL, confirmed = 0, bullets = ?, eliminated = 0, last_target_team = NULL, ships_json = '[]'
            WHERE room_code = ?
            """,
            (INITIAL_BULLETS, code),
        )
        conn.execute(
            "INSERT INTO logs (room_code, message, log_type, created_at) VALUES (?, ?, 'system', ?)",
            (code, f"ผู้ดูแลห้องรีเซ็ตเกม {code} แล้ว พร้อมเริ่มรอบใหม่!", now),
        )
        state = build_state_payload(conn, code, None)

    await manager.broadcast(code, "room_reset")
    return {"state": state}


@app.websocket("/ws/{room_code}")
async def websocket_endpoint(websocket: WebSocket, room_code: str):
    code = normalize_room(room_code)
    await manager.connect(code, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(code, websocket)
    except Exception:
        manager.disconnect(code, websocket)


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))
