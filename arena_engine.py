import math
import random
import secrets
import time
import json
import hashlib
import urllib.parse
from datetime import datetime, timezone, timedelta
MSK_TZ = timezone(timedelta(hours=3))
ARENA_ZONE_COLORS = [
    "#2563eb",  
    "#10b981",  
    "#f59e0b",  
    "#8b5cf6",  
    "#ec4899",  
    "#06b6d4",  
    "#f97316",  
    "#84cc16",  
    "#e11d48",  
    "#6366f1",  
]
class ArenaEngine:
    def __init__(self):
        self.init_db()
        self.current_round = None
        self.round_counter = self.get_max_round_id()
        self.history = self.load_recent_history(25)
        self.active_users = {}  
        self.create_new_round()
    def get_db(self):
        from pg_adapter import get_db_connection
        return get_db_connection()
    def init_db(self):
        with self.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS arena_history (
                id BIGSERIAL PRIMARY KEY,
                round_id BIGINT UNIQUE,
                total_bank BIGINT,
                winner_id BIGINT,
                winner_name TEXT,
                winner_username TEXT,
                winner_avatar TEXT,
                winner_color TEXT,
                winner_bet BIGINT,
                winner_share REAL,
                players_json TEXT,
                zones_json TEXT,
                ball_trajectory_json TEXT,
                round_hash TEXT,
                server_seed TEXT,
                created_at TEXT
            )
            ''')
            try:
                cursor.execute('ALTER TABLE arena_history ADD COLUMN IF NOT EXISTS round_hash TEXT')
                cursor.execute('ALTER TABLE arena_history ADD COLUMN IF NOT EXISTS server_seed TEXT')
            except Exception:
                pass
            conn.commit()
    def get_max_round_id(self):
        try:
            with self.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT MAX(round_id) FROM arena_history')
                row = cursor.fetchone()
                return row[0] if (row and row[0]) else 0
        except Exception:
            return 0
    def load_recent_history(self, limit=25):
        items = []
        try:
            with self.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                SELECT round_id, total_bank, winner_id, winner_name, winner_username,
                       winner_avatar, winner_color, winner_bet, winner_share,
                       players_json, zones_json, ball_trajectory_json,
                       COALESCE(round_hash, '') as round_hash,
                       COALESCE(server_seed, '') as server_seed,
                       created_at
                FROM arena_history
                ORDER BY round_id DESC
                LIMIT ?
                ''', (limit,))
                rows = cursor.fetchall()
                for r in rows:
                    items.append({
                        "roundId": r["round_id"],
                        "totalBank": r["total_bank"],
                        "winner": {
                            "id": r["winner_id"],
                            "name": r["winner_name"],
                            "username": r["winner_username"],
                            "avatar": r["winner_avatar"],
                            "color": r["winner_color"],
                            "bet": r["winner_bet"],
                            "share": r["winner_share"]
                        },
                        "players": json.loads(r["players_json"]) if r["players_json"] else [],
                        "zones": json.loads(r["zones_json"]) if r["zones_json"] else [],
                        "ballTrajectory": json.loads(r["ball_trajectory_json"]) if r["ball_trajectory_json"] else {},
                        "roundHash": r.get("round_hash", ""),
                        "serverSeed": r.get("server_seed", ""),
                        "createdAt": r["created_at"]
                    })
        except Exception:
            pass
        return items
    def touch_user(self, user_id):
        if user_id:
            self.active_users[user_id] = time.time()
    def get_online_count(self):
        now = time.time()
        active = []
        for uid, val in list(self.active_users.items()):
            t = val.get("lastSeen", 0) if isinstance(val, dict) else (val if isinstance(val, (int, float)) else 0)
            if now - t < 60.0:
                active.append(uid)
            else:
                self.active_users.pop(uid, None)
        return max(1, len(active))
    def create_new_round(self):
        now = datetime.now(MSK_TZ)
        self.round_counter += 1
        server_seed = secrets.token_hex(32)
        round_hash = hashlib.sha256(f"{self.round_counter}:{server_seed}".encode('utf-8')).hexdigest()
        self.current_round = {
            "id": self.round_counter,
            "server_seed": server_seed,
            "round_hash": round_hash,
            "status": "waiting",  
            "createdAt": now.isoformat(),
            "createdAtStr": now.strftime("%d-%m-%Y %H:%M:%S"),
            "statusStartedAt": time.time(),
            "countdownDuration": 15.0,  
            "liveDuration": 8.0,        
            "finishedDuration": 6.0,    
            "totalBank": 0,
            "players": [],
            "zones": [],
            "winner": None,
            "ballTrajectory": None
        }
    def compute_zones(self, players, total_bank):
        if not players:
            return []
        n = len(players)
        if n == 1:
            p = players[0]
            return [{
                "playerId": p["id"],
                "name": p["name"],
                "color": p["color"],
                "avatar": p["avatar"],
                "x": 0.0,
                "y": 0.0,
                "width": 100.0,
                "height": 100.0,
                "share": 100.0,
                "labelX": 50.0,
                "labelY": 50.0,
                "clipPath": "none"
            }]

        total_b = sum(p["bet"] for p in players)
        if total_b <= 0:
            total_b = 1

        sorted_p = sorted(players, key=lambda x: x["bet"], reverse=True)
        zones = []

        def partition(p_list, x, y, w, h):
            if len(p_list) == 1:
                p = p_list[0]
                sh = round((p["bet"] / total_b) * 100.0, 2)
                zx = round(x, 2)
                zy = round(y, 2)
                zw = round(w, 2)
                zh = round(h, 2)
                zones.append({
                    "playerId": p["id"],
                    "name": p["name"],
                    "color": p["color"],
                    "avatar": p["avatar"],
                    "x": zx,
                    "y": zy,
                    "width": zw,
                    "height": zh,
                    "share": sh,
                    "labelX": round(zx + zw / 2, 2),
                    "labelY": round(zy + zh / 2, 2),
                    "clipPath": "none"
                })
                return

            total_weight = sum(p["bet"] for p in p_list)
            if total_weight <= 0:
                total_weight = len(p_list)
                weights = [1] * len(p_list)
            else:
                weights = [p["bet"] for p in p_list]

            half_weight = total_weight / 2.0
            cum = 0
            split_idx = 0
            best_diff = float("inf")
            for i, wt in enumerate(weights[:-1]):
                cum += wt
                diff = abs(cum - half_weight)
                if diff < best_diff:
                    best_diff = diff
                    split_idx = i

            g1 = p_list[:split_idx + 1]
            g2 = p_list[split_idx + 1:]
            w1 = sum(p["bet"] for p in g1) if total_weight > len(p_list) else len(g1)
            ratio1 = w1 / total_weight

            if w >= h:
                w_left = w * ratio1
                w_right = w - w_left
                partition(g1, x, y, w_left, h)
                partition(g2, x + w_left, y, w_right, h)
            else:
                h_top = h * ratio1
                h_bottom = h - h_top
                partition(g1, x, y, w, h_top)
                partition(g2, x, y + h_top, w, h_bottom)

        partition(sorted_p, 0.0, 0.0, 100.0, 100.0)
        return zones
    def generate_ball_trajectory(self, winner_zone, duration_sec=7.0, min_bounces=7, max_bounces=12):
        xmin, xmax = 3.0, 97.0
        ymin, ymax = 3.0, 97.0
        W = xmax - xmin
        H = ymax - ymin
        x0, y0 = 50.0, 50.0

        pad_x = max(0.5, winner_zone["width"]  * 0.2)
        pad_y = max(0.5, winner_zone["height"] * 0.2)
        target_x = winner_zone["x"] + pad_x + random.uniform(0, max(0.1, winner_zone["width"]  - 2 * pad_x))
        target_y = winner_zone["y"] + pad_y + random.uniform(0, max(0.1, winner_zone["height"] - 2 * pad_y))
        target_x = max(xmin + 1.0, min(xmax - 1.0, target_x))
        target_y = max(ymin + 1.0, min(ymax - 1.0, target_y))

        candidates = []
        for kx in range(-12, 13):
            for ky in range(-12, 13):
                if kx == 0 and ky == 0:
                    continue
                total_b = abs(kx) + abs(ky)
                if min_bounces <= total_b <= max_bounces:
                    candidates.append((kx, ky))

        if not candidates:
            candidates = [(4, 4), (-4, 4), (4, -4), (-4, -4), (3, 4), (4, 3)]

        random.shuffle(candidates)
        kx, ky = candidates[0]

        rel_tx = target_x - xmin
        if kx >= 0:
            virt_rel_x = kx * W + (rel_tx if kx % 2 == 0 else (W - rel_tx))
        else:
            abs_k = abs(kx)
            virt_rel_x = -abs_k * W + ((W - rel_tx) if abs_k % 2 == 1 else rel_tx)
        X_virt = xmin + virt_rel_x

        rel_ty = target_y - ymin
        if ky >= 0:
            virt_rel_y = ky * H + (rel_ty if ky % 2 == 0 else (H - rel_ty))
        else:
            abs_k = abs(ky)
            virt_rel_y = -abs_k * H + ((H - rel_ty) if abs_k % 2 == 1 else rel_ty)
        Y_virt = ymin + virt_rel_y

        dx_total = X_virt - x0
        dy_total = Y_virt - y0
        init_angle = math.degrees(math.atan2(dy_total, dx_total)) % 360

        intersections = []
        start_ix = math.floor((x0 - xmin) / W)
        end_ix = math.floor((X_virt - xmin) / W)
        min_gx = min(start_ix, end_ix)
        max_gx = max(start_ix, end_ix)
        for gx in range(min_gx, max_gx + 1):
            if dx_total > 0 and gx > start_ix:
                grid_x_val = xmin + gx * W
                t_d = (grid_x_val - x0) / dx_total
                if 0 < t_d < 1:
                    intersections.append((t_d, 'x'))
            elif dx_total < 0 and gx < start_ix + 1:
                grid_x_val = xmin + gx * W
                t_d = (grid_x_val - x0) / dx_total
                if 0 < t_d < 1:
                    intersections.append((t_d, 'x'))

        start_iy = math.floor((y0 - ymin) / H)
        end_iy = math.floor((Y_virt - ymin) / H)
        min_gy = min(start_iy, end_iy)
        max_gy = max(start_iy, end_iy)
        for gy in range(min_gy, max_gy + 1):
            if dy_total > 0 and gy > start_iy:
                grid_y_val = ymin + gy * H
                t_d = (grid_y_val - y0) / dy_total
                if 0 < t_d < 1:
                    intersections.append((t_d, 'y'))
            elif dy_total < 0 and gy < start_iy + 1:
                grid_y_val = ymin + gy * H
                t_d = (grid_y_val - y0) / dy_total
                if 0 < t_d < 1:
                    intersections.append((t_d, 'y'))

        intersections.sort(key=lambda item: item[0])

        def fold_back(xv, yv):
            rx = (xv - xmin) / W
            n_x = math.floor(rx)
            frac_x = rx - n_x
            real_x = xmin + frac_x * W if n_x % 2 == 0 else xmin + (1.0 - frac_x) * W

            ry = (yv - ymin) / H
            n_y = math.floor(ry)
            frac_y = ry - n_y
            real_y = ymin + frac_y * H if n_y % 2 == 0 else ymin + (1.0 - frac_y) * H

            return real_x, real_y

        P_POWER = 7.2
        def s_to_t(s_val):
            s_clamped = max(0.0, min(1.0, s_val))
            return 1.0 - (1.0 - s_clamped) ** (1.0 / P_POWER)

        bounce_points = [{"s": 0.0, "x": x0, "y": y0, "bounced": False}]

        for t_dist, _ in intersections:
            xv = x0 + t_dist * dx_total
            yv = y0 + t_dist * dy_total
            rx, ry = fold_back(xv, yv)
            bounce_points.append({"s": t_dist, "x": rx, "y": ry, "bounced": True})

        bounce_points.append({"s": 1.0, "x": target_x, "y": target_y, "bounced": False})

        keyframes = []
        for i in range(len(bounce_points)):
            bp = bounce_points[i]
            t_val = s_to_t(bp["s"])

            if i < len(bounce_points) - 1:
                next_bp = bounce_points[i+1]
                dx_seg = next_bp["x"] - bp["x"]
                dy_seg = next_bp["y"] - bp["y"]
                seg_angle = math.degrees(math.atan2(dy_seg, dx_seg)) % 360
            else:
                seg_angle = keyframes[-1]["angle"] if keyframes else init_angle

            if i > 0:
                prev_bp = bounce_points[i-1]
                ds = bp["s"] - prev_bp["s"]
                if ds > 0.10:
                    num_sub = max(2, int(ds / 0.06))
                    for sub in range(1, num_sub):
                        sub_s = prev_bp["s"] + (ds * sub / num_sub)
                        sub_t = s_to_t(sub_s)
                        sub_frac = sub / num_sub
                        sub_x = prev_bp["x"] + (bp["x"] - prev_bp["x"]) * sub_frac
                        sub_y = prev_bp["y"] + (bp["y"] - prev_bp["y"]) * sub_frac
                        keyframes.append({
                            "t": round(sub_t, 3),
                            "x": round(sub_x, 2),
                            "y": round(sub_y, 2),
                            "angle": round(seg_angle, 1),
                            "bounced": False
                        })

            keyframes.append({
                "t": round(t_val, 3),
                "x": round(bp["x"], 2),
                "y": round(bp["y"], 2),
                "angle": round(seg_angle, 1),
                "bounced": bp["bounced"]
            })

        keyframes[0]["t"] = 0.0
        keyframes[0]["angle"] = round(init_angle, 1)
        keyframes[-1]["t"] = 1.0
        keyframes[-1]["x"] = round(target_x, 2)
        keyframes[-1]["y"] = round(target_y, 2)

        return {
            "initAngle":  round(init_angle, 2),
            "target":     {"x": round(target_x, 2), "y": round(target_y, 2)},
            "keyframes":  keyframes,
            "durationMs": int(duration_sec * 1000)
        }
    def add_bet(self, user_id, user_name, user_username, bet_amount, user_avatar=None, user_color=None):
        if not self.current_round:
            self.create_new_round()
        r = self.current_round
        if r["status"] not in ["waiting", "launching"]:
            return False, "Раунд уже запущен, дождитесь следующего!"
        if bet_amount <= 0:
            return False, "Сумма ставки должна быть больше 0!"
        self.active_users[user_id] = {
            "name": user_name or f"Игрок #{user_id}",
            "username": user_username or "",
            "avatar": user_avatar or "",
            "lastSeen": time.time()
        }
        existing = next((p for p in r["players"] if p["id"] == user_id), None)
        if existing:
            existing["bet"] += bet_amount
            if user_avatar:
                existing["avatar"] = user_avatar
            if user_name:
                existing["name"] = user_name
            if user_username:
                existing["username"] = user_username
        else:
            used_colors = {p["color"] for p in r["players"]}
            avail_colors = [c for c in ARENA_ZONE_COLORS if c not in used_colors]
            color = user_color if user_color else (avail_colors[0] if avail_colors else random.choice(ARENA_ZONE_COLORS))
            avatar = user_avatar if user_avatar else ""
            r["players"].append({
                "id": user_id,
                "name": user_name or f"Игрок #{user_id}",
                "username": user_username or "",
                "avatar": avatar,
                "color": color,
                "bet": bet_amount,
                "share": 0.0
            })
        r["totalBank"] = sum(p["bet"] for p in r["players"])
        for p in r["players"]:
            p["share"] = round((p["bet"] / r["totalBank"]) * 100.0, 2) if r["totalBank"] > 0 else 0.0
        r["zones"] = self.compute_zones(r["players"], r["totalBank"])
        if len(r["players"]) >= 2:
            if r["status"] == "waiting":
                r["status"] = "launching"
                r["statusStartedAt"] = time.time()
                r["countdownDuration"] = 15.0
            elif r["status"] == "launching":
                time_left = r["countdownDuration"] - (time.time() - r["statusStartedAt"])
                if time_left < 5.0:
                    r["countdownDuration"] = (time.time() - r["statusStartedAt"]) + 6.0
        else:
            r["status"] = "waiting"
        return True, "Ставка принята!"
    def tick(self):
        r = self.current_round
        if not r:
            self.create_new_round()
            return
        now = time.time()
        if r["status"] == "launching":
            elapsed = now - r["statusStartedAt"]
            if elapsed >= r["countdownDuration"]:
                if len(r["players"]) >= 2:
                    total_bank = r["totalBank"]
                    rand_val = random.uniform(0, total_bank)
                    cum = 0
                    winner_player = r["players"][0]
                    for p in r["players"]:
                        cum += p["bet"]
                        if rand_val <= cum:
                            winner_player = p
                            break
                    r["winner"] = winner_player
                    winner_zone = next((z for z in r["zones"] if z["playerId"] == winner_player["id"]), r["zones"][0])
                    r["ballDuration"] = 7.0
                    traj = self.generate_ball_trajectory(winner_zone, r["ballDuration"])
                    r["ballAngle"] = traj["initAngle"]
                    r["ballSpeed"] = 1100.0
                    r["ballTrajectory"] = traj
                    r["ballSpawnPosition"] = {"x": 50, "y": 50}
                    r["targetPosition"] = traj["target"]
                    r["liveStartedAt"] = datetime.now(MSK_TZ).isoformat()
                    r["status"] = "live"
                    r["statusStartedAt"] = now
                elif len(r["players"]) == 1:
                    r["status"] = "waiting"
        elif r["status"] == "live":
            elapsed = now - r["statusStartedAt"]
            if elapsed >= r.get("ballDuration", 9.0):
                r["status"] = "finished"
                r["statusStartedAt"] = now
                self.finish_round(r)
        elif r["status"] == "finished":
            elapsed = now - r["statusStartedAt"]
            if elapsed >= r["finishedDuration"]:
                self.create_new_round()
    def finish_round(self, round_data):
        winner = round_data.get("winner")
        total_bank = round_data.get("totalBank", 0)
        round_id = round_data.get("id")
        if not winner or total_bank <= 0:
            return
        winner_id = winner["id"]
        try:
            with self.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT balance, games, max_balance FROM users WHERE user_id = ?', (winner_id,))
                user = cursor.fetchone()
                if user:
                    new_bal = user["balance"] + total_bank
                    new_games = user["games"] + 1
                    cur_max = user["max_balance"] if "max_balance" in user.keys() and user["max_balance"] is not None else user["balance"]
                    new_max = max(cur_max, new_bal)
                    cursor.execute('UPDATE users SET balance = ?, games = ?, max_balance = ? WHERE user_id = ?', (new_bal, new_games, new_max, winner_id))
                    conn.commit()
        except Exception:
            pass
        try:
            with self.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                INSERT INTO arena_history (
                    round_id, total_bank, winner_id, winner_name, winner_username,
                    winner_avatar, winner_color, winner_bet, winner_share,
                    players_json, zones_json, ball_trajectory_json,
                    round_hash, server_seed, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (round_id) DO UPDATE SET
                    total_bank = EXCLUDED.total_bank,
                    winner_id = EXCLUDED.winner_id,
                    winner_name = EXCLUDED.winner_name,
                    winner_username = EXCLUDED.winner_username,
                    winner_avatar = EXCLUDED.winner_avatar,
                    winner_color = EXCLUDED.winner_color,
                    winner_bet = EXCLUDED.winner_bet,
                    winner_share = EXCLUDED.winner_share,
                    players_json = EXCLUDED.players_json,
                    zones_json = EXCLUDED.zones_json,
                    ball_trajectory_json = EXCLUDED.ball_trajectory_json,
                    round_hash = EXCLUDED.round_hash,
                    server_seed = EXCLUDED.server_seed,
                    created_at = EXCLUDED.created_at
                ''', (
                    round_id,
                    total_bank,
                    winner["id"],
                    winner["name"],
                    winner.get("username", ""),
                    winner.get("avatar", ""),
                    winner.get("color", ""),
                    winner.get("bet", 0),
                    winner.get("share", 0.0),
                    json.dumps(round_data["players"], ensure_ascii=False),
                    json.dumps(round_data["zones"], ensure_ascii=False),
                    json.dumps(round_data.get("ballTrajectory", {}), ensure_ascii=False),
                    round_data.get("round_hash", ""),
                    round_data.get("server_seed", ""),
                    round_data["createdAtStr"]
                ))
                conn.commit()
        except Exception:
            pass
        history_item = {
            "roundId": round_id,
            "totalBank": total_bank,
            "winner": winner,
            "players": round_data["players"],
            "zones": round_data["zones"],
            "ballTrajectory": round_data.get("ballTrajectory"),
            "roundHash": round_data.get("round_hash", ""),
            "serverSeed": round_data.get("server_seed", ""),
            "createdAt": round_data["createdAtStr"]
        }
        self.history.insert(0, history_item)
        if len(self.history) > 30:
            self.history = self.history[:30]
    def get_public_state(self):
        r = self.current_round
        if not r:
            return None
        now = time.time()
        time_left = 0
        if r["status"] == "launching":
            time_left = max(0, int(r["countdownDuration"] - (now - r["statusStartedAt"])))
        elif r["status"] == "live":
            time_left = max(0, int(r.get("ballDuration", 9.0) - (now - r["statusStartedAt"])))
        return {
            "id": r["id"],
            "roundHash": r.get("round_hash", ""),
            "status": r["status"],
            "totalBank": r["totalBank"],
            "onlineCount": self.get_online_count(),
            "timeLeft": time_left,
            "countdownDuration": r["countdownDuration"],
            "liveDuration": r.get("ballDuration", 9.0),
            "players": r["players"],
            "zones": r["zones"],
            "winner": r["winner"] if r["status"] in ["live", "finished"] else None,
            "ballSpawnPosition": r.get("ballSpawnPosition", {"x": 50, "y": 50}),
            "targetPosition": r.get("targetPosition"),
            "ballAngle": r.get("ballAngle", 0),
            "ballSpeed": r.get("ballSpeed", 140),
            "ballDuration": r.get("ballDuration", 9.0),
            "ballTrajectory": r.get("ballTrajectory"),
            "liveStartedAt": r.get("liveStartedAt"),
            "serverTime": int(now * 1000)
        }
arena_engine = ArenaEngine()