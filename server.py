from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import json
import os
import random
import re
import socket
import time
import uuid
import urllib.error
import urllib.request

VERSION = "liarV1.0"


BASE_DIR = Path(__file__).resolve().parent
HTML_FILE = BASE_DIR / "谁是卧底1.0.0.html"
ENV_FILE = BASE_DIR / ".env"
HISTORY_FILE = BASE_DIR / "word_history.json"

DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"
MAX_HISTORY_WORDS = 1000
ROOM_TTL_SECONDS = 8 * 60 * 60

rooms = {}


def load_env():
    if not ENV_FILE.exists():
        return

    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def normalize_word(word):
    return "".join(str(word).strip().lower().split())


def sanitize_word(word):
    return str(word).strip().strip("`\"'“”")


def read_history():
    if not HISTORY_FILE.exists():
        return []

    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(data, list) or len(data) > MAX_HISTORY_WORDS:
        return []

    history = []
    for item in data:
        word = sanitize_word(item)
        if word and normalize_word(word) not in {normalize_word(w) for w in history}:
            history.append(word)
    return history


def write_history(history):
    HISTORY_FILE.write_text(
        json.dumps(history[-MAX_HISTORY_WORDS:], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def add_history_words(*words):
    history = read_history()
    if len(history) + len(words) > MAX_HISTORY_WORDS:
        history = []

    normalized = {normalize_word(word) for word in history}
    for word in words:
        clean_word = sanitize_word(word)
        if clean_word and normalize_word(clean_word) not in normalized:
            history.append(clean_word)
            normalized.add(normalize_word(clean_word))
    write_history(history)


def has_history_word(word):
    normalized = normalize_word(word)
    return any(normalize_word(history_word) == normalized for history_word in read_history())


def make_room_code():
    for _ in range(100):
        code = f"{random.randint(1000, 9999)}"
        if code not in rooms:
            return code
    return str(random.randint(100000, 999999))


def default_settings():
    return {
        "player_count": 6,
        "undercover_count": 1,
        "blank_count": 0,
        "topic": "",
        "difficulty": "medium",
        "word_length": 0,
        "model": "deepseek-v4-flash",
    }


def clean_rooms():
    now = time.time()
    expired = [
        code
        for code, room in rooms.items()
        if now - room.get("updated_at", now) > ROOM_TTL_SECONDS
    ]
    for code in expired:
        rooms.pop(code, None)


def touch(room):
    room["updated_at"] = time.time()


def can_player_burst(room, player):
    if not player:
        return False
    if room["status"] not in {"playing", "ended"}:
        return False
    if player.get("burst_used"):
        return False
    if player.get("role") not in {"undercover", "blank"}:
        return False
    if room["status"] == "ended":
        return True
    return player.get("eliminated")


def public_state(room_code, player_id):
    room = rooms[room_code]
    player = room["players"].get(player_id)
    players = sorted(room["players"].values(), key=lambda item: item["number"])
    active_players = [p for p in players if not p["eliminated"]]
    my_vote = room["votes"].get(player_id)
    state = {
        "room_code": room_code,
        "status": room["status"],
        "settings": room["settings"],
        "game_id": room.get("game_id", 0),
        "is_host": player_id == room["host_id"],
        "my_number": player["number"] if player else None,
        "my_word": player.get("word") if player and room["status"] in {"playing", "ended"} else "",
        "my_can_burst": can_player_burst(room, player),
        "my_burst_used": bool(player.get("burst_used")) if player else False,
        "players": [
            {
                "number": item["number"],
                "name": item["name"],
                "eliminated": item["eliminated"],
                "is_you": item["id"] == player_id,
            }
            for item in players
        ],
        "winner": room.get("winner"),
        "undercover_numbers": room.get("undercover_numbers", []) if room["status"] == "ended" else [],
        "vote_info": {
            "votes": dict(room["votes"]),
            "my_vote": my_vote,
            "active_count": len(active_players),
            "voted_count": len(room["votes"]),
            "last_result": room.get("last_vote_result"),
        },
        "vote_record": room.get("vote_record", []),
        "burst_record": room.get("burst_record", []),
    }
    return state


def create_player(name, number, client_id="", client_ip=""):
    return {
        "id": uuid.uuid4().hex,
        "client_id": client_id,
        "client_ip": client_ip,
        "name": name or f"玩家{number}",
        "number": number,
        "role": "",
        "word": "",
        "eliminated": False,
        "burst_used": False,
    }


def find_existing_player(room, client_id="", client_ip=""):
    if client_id:
        for player in room["players"].values():
            if player.get("client_id") == client_id:
                return player
        return None

    if client_ip:
        for player in room["players"].values():
            if player.get("client_ip") == client_ip:
                return player

    return None


def normalize_name(name):
    if not name:
        return "玩家"
    return name.strip()[:5]


def create_room(name, client_id="", client_ip=""):
    room_code = make_room_code()
    player = create_player(normalize_name(name) or "房主", 1, client_id, client_ip)
    player_id = player["id"]
    rooms[room_code] = {
        "host_id": player_id,
        "status": "waiting",
        "settings": default_settings(),
        "words": {"civilian": "", "undercover": ""},
        "players": {player_id: player},
        "winner": None,
        "undercover_numbers": [],
        "votes": {},
        "vote_record": [],
        "last_vote_result": None,
        "burst_record": [],
        "game_id": 0,
        "updated_at": time.time(),
    }
    return room_code, player_id


def join_room(room_code, name, client_id="", client_ip=""):
    if room_code not in rooms:
        raise ValueError("房间不存在")

    room = rooms[room_code]
    if room["status"] == "playing":
        raise ValueError("游戏已开始，不能加入")

    existing_player = find_existing_player(room, client_id, client_ip)
    if existing_player:
        if name:
            existing_player["name"] = normalize_name(name)
        if client_id:
            existing_player["client_id"] = client_id
        if client_ip:
            existing_player["client_ip"] = client_ip
        touch(room)
        return existing_player["id"]

    number = max((player["number"] for player in room["players"].values()), default=0) + 1
    player = create_player(normalize_name(name), number, client_id, client_ip)
    player_id = player["id"]
    room["players"][player_id] = player
    touch(room)
    return player_id


def leave_room(room_code, player_id):
    room = ensure_room_player(room_code, player_id)
    was_host = room["host_id"] == player_id
    room["players"].pop(player_id, None)

    if was_host:
        rooms.pop(room_code, None)
        return None

    if not room["players"]:
        rooms.pop(room_code, None)
        return None

    if room["status"] == "playing":
        evaluate_game(room)

    touch(room)
    return room


def rename_player(room, player_id, name):
    clean_name = str(name).strip()
    if not clean_name:
        raise ValueError("昵称不能为空")
    room["players"][player_id]["name"] = clean_name
    touch(room)


def ensure_room_player(room_code, player_id):
    if room_code not in rooms:
        raise ValueError("房间不存在")
    room = rooms[room_code]
    if player_id not in room["players"]:
        raise PermissionError("玩家不在房间内")
    touch(room)
    return room


def ensure_host(room, player_id):
    if room["host_id"] != player_id:
        raise PermissionError("只有房主可以操作")


def apply_settings(room, data):
    settings = room["settings"]
    player_count = max(3, min(15, int(data.get("player_count", settings["player_count"]))))
    undercover_count = max(1, min(player_count - 1, int(data.get("undercover_count", settings["undercover_count"]))))
    blank_count = max(0, min(player_count - undercover_count - 1, int(data.get("blank_count", settings["blank_count"]))))
    if undercover_count + blank_count >= player_count:
        raise ValueError("卧底数量加空白数量必须小于玩家人数")

    settings["player_count"] = player_count
    settings["undercover_count"] = undercover_count
    settings["blank_count"] = blank_count
    settings["topic"] = str(data.get("topic", settings["topic"])).strip()
    settings["difficulty"] = str(data.get("difficulty", settings["difficulty"]))
    settings["word_length"] = max(0, int(data.get("word_length", settings["word_length"])))
    settings["model"] = str(data.get("model", settings["model"]))


def difficulty_prompt(difficulty):
    prompts = {
        "low": "低难度：两个词差异要比较明显，普通玩家容易通过描述区分，但仍属于同一大类。",
        "medium": "中等难度：两个词有明显共同点，也有清晰差异，描述时会产生一定混淆。",
        "high": "高难度：两个词关系更微妙，共同特征更多，差异需要通过细节描述才能区分，但不能是近义词或强绑定关系。",
        "extreme": "超高难度：允许近义词，两个词可以高度相似且非常容易混淆；但仍必须避免完全同义词、别名、上下位词、只差一个字和强绑定关系。",
    }
    return prompts.get(difficulty, prompts["medium"])


def parse_deepseek_words(text):
    undercover_match = re.search(r"卧底词\s*[:：]\s*([^；;\n\r]+)", text)
    civilian_match = re.search(r"平民词\s*[:：]\s*([^；;\n\r]+)", text)
    if not undercover_match or not civilian_match:
        raise ValueError("DeepSeek 返回格式不符合要求")
    return {
        "undercover": sanitize_word(undercover_match.group(1)),
        "civilian": sanitize_word(civilian_match.group(1)),
    }


def generate_words(settings):
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("服务端未配置 DEEPSEEK_API_KEY")

    history = read_history()
    history_text = "、".join(history) if history else "无"
    topic = settings.get("topic") or "不限"
    word_length = int(settings.get("word_length") or 0)
    length_prompt = (
        f"字数偏好：优先生成不超过{word_length}个汉字的自然词语；卧底词和平民词不要求字数相同。如果为了词语质量必须超过，可以返回更自然的词。"
        if word_length > 0
        else ""
    )
    prompt = f"""为谁是卧底游戏生成一组词语。
词语类型：{topic}。
难度要求：{difficulty_prompt(settings.get("difficulty"))}
{length_prompt}

两个词必须满足:
1. 属于同一大类，玩家能通过描述产生混淆。
2. 低难度、中等难度、高难度不能是同义词、近义词、上下位词、反义词；超高难度允许近义词，但不能是完全同义词。
3. 不能只差一个字，不能是别名、职业和具体人物、品牌和产品、地点和建筑这种强绑定关系。
4. 差异要明显，但不能完全无关。
5. 适合口头描述，普通人能理解。

禁止使用以下历史词语，也不要生成与它们明显相关的词：{history_text}。
要求卧底词和平民词都必须是新词。
严格按这个格式返回：卧底词：ABC；平民词：EFG;
不要返回英文、数字、标点或解释。"""
    payload = {
        "model": settings.get("model") or "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "你只返回一行中文文本，不要解释，不要 Markdown。"},
            {"role": "user", "content": prompt},
        ],
        "thinking": {"type": "disabled"},
        "stream": False,
        "temperature": 0.8,
        "max_tokens": 100,
    }
    request = urllib.request.Request(
        DEEPSEEK_ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"DeepSeek 请求失败：HTTP {error.code} {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"DeepSeek 请求失败：{error.reason}") from error

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    words = parse_deepseek_words(content)
    
    if normalize_word(words["undercover"]) == normalize_word(words["civilian"]):
        raise ValueError("卧底词和平民词相同，请重新获取词语")
    
    if has_history_word(words["undercover"]) or has_history_word(words["civilian"]):
        raise ValueError("返回词语与历史重复，请重新获取词语")
    add_history_words(words["undercover"], words["civilian"])
    return words


def start_game(room):
    players = sorted(room["players"].values(), key=lambda item: item["number"])
    player_count = int(room["settings"]["player_count"])
    joined_count = len(players)
    undercover_count = int(room["settings"]["undercover_count"])
    blank_count = int(room["settings"]["blank_count"])
    civilian_count = player_count - undercover_count - blank_count

    if joined_count != player_count:
        raise ValueError(f"当前已加入 {joined_count} 人，设置玩家人数为 {player_count} 人")
    if player_count < 3:
        raise ValueError("至少需要 3 名玩家")
    if undercover_count < 1:
        raise ValueError("至少需要 1 名卧底")
    if civilian_count < 1:
        raise ValueError("平民数量必须至少为 1")

    words = generate_words(room["settings"])
    roles = (
        ["civilian"] * civilian_count
        + ["undercover"] * undercover_count
        + ["blank"] * blank_count
    )
    random.shuffle(roles)
    for player, role in zip(players, roles):
        player["role"] = role
        player["word"] = (
            words["civilian"]
            if role == "civilian"
            else words["undercover"]
            if role == "undercover"
            else "空白"
        )
        player["eliminated"] = False
        player["burst_used"] = False

    room["words"] = words
    room["status"] = "playing"
    room["winner"] = None
    room["undercover_numbers"] = []
    room["votes"] = {}
    room["vote_record"] = []
    room["last_vote_result"] = None
    room["burst_record"] = []
    room["game_id"] = room.get("game_id", 0) + 1
    touch(room)


def evaluate_game(room):
    active = [player for player in room["players"].values() if not player["eliminated"]]
    active_civilian = sum(1 for player in active if player["role"] == "civilian")
    active_undercover = sum(1 for player in active if player["role"] == "undercover")
    active_blank = sum(1 for player in active if player["role"] == "blank")
    if not active:
        return

    if active_undercover == 0 and active_blank == 0:
        room["status"] = "ended"
        room["winner"] = "civilian"
    elif active_civilian <= 1:
        room["status"] = "ended"
        room["winner"] = "undercover"

    if room["status"] == "ended":
        room["undercover_numbers"] = sorted(
            player["number"] for player in room["players"].values() if player["role"] == "undercover"
        )


def reset_votes(room):
    room["votes"] = {}
    room["last_vote_result"] = None


def build_vote_details(room):
    vote_details = []
    for voter_id, voted_number in room["votes"].items():
        voter = room["players"].get(voter_id)
        if voter:
            vote_details.append({
                "voter_number": voter["number"],
                "voter_name": voter["name"],
                "voted_number": voted_number
            })
    return vote_details


def tally_votes(room):
    vote_count = {}
    for voter_id, voted_number in room["votes"].items():
        if voted_number not in vote_count:
            vote_count[voted_number] = []
        vote_count[voted_number].append(voter_id)

    if not vote_count:
        return None

    max_votes = max(len(voters) for voters in vote_count.values())
    tied_numbers = [num for num, voters in vote_count.items() if len(voters) == max_votes]
    vote_details = build_vote_details(room)

    if len(tied_numbers) > 1 and max_votes >= 2:
        eliminated_list = []
        for tied_number in tied_numbers:
            eliminated_player = next(
                (p for p in room["players"].values() if p["number"] == tied_number and not p["eliminated"]),
                None
            )
            if eliminated_player:
                eliminated_player["eliminated"] = True
                eliminated_list.append({"number": tied_number, "role": eliminated_player["role"], "name": eliminated_player["name"]})

        vote_count_detail = {}
        for num, voters in vote_count.items():
            player = next((p for p in room["players"].values() if p["number"] == num), None)
            vote_count_detail[num] = {"name": player["name"] if player else str(num), "count": len(voters)}

        room["vote_record"].append({"type": "multiple_eliminated", "eliminated_list": eliminated_list, "votes": dict(room["votes"]), "vote_details": vote_details})
        room["votes"] = {}
        room["last_vote_result"] = {
            "eliminated_list": [
                {
                    "number": e["number"],
                    "role": e["role"],
                    "name": e["name"],
                    "is_undercover": e["role"] == "undercover",
                    "is_blank": e["role"] == "blank",
                    "is_civilian": e["role"] == "civilian",
                }
                for e in eliminated_list
            ],
            "vote_details": vote_details,
            "vote_count": vote_count_detail
        }
        evaluate_game(room)
        return {"type": "multiple_eliminated", "eliminated_list": eliminated_list, "vote_details": vote_details, "vote_count": vote_count_detail}

    if len(tied_numbers) > 1:
        active_players = [p for p in room["players"].values() if not p["eliminated"]]
        if len(active_players) <= 3:
            room["status"] = "ended"
            room["winner"] = "undercover"
            room["undercover_numbers"] = sorted(
                player["number"] for player in room["players"].values() if player["role"] == "undercover"
            )
            vote_count_detail = {}
            for num, voters in vote_count.items():
                player_obj = next((p for p in room["players"].values() if p["number"] == num), None)
                vote_count_detail[num] = {"name": player_obj["name"] if player_obj else str(num), "count": len(voters)}
            room["vote_record"].append({"type": "tie", "tied_numbers": tied_numbers, "votes": dict(room["votes"]), "vote_details": vote_details})
            room["votes"] = {}
            room["last_vote_result"] = {
                "type": "draw",
                "tied_numbers": sorted(tied_numbers),
                "vote_details": vote_details,
                "vote_count": vote_count_detail
            }
            return {"type": "draw", "tied_numbers": sorted(tied_numbers), "vote_details": vote_details, "vote_count": vote_count_detail}

        vote_count_detail = {}
        for num, voters in vote_count.items():
            player = next((p for p in room["players"].values() if p["number"] == num), None)
            vote_count_detail[num] = {"name": player["name"] if player else str(num), "count": len(voters)}

        room["vote_record"].append({"type": "tie", "tied_numbers": tied_numbers, "votes": dict(room["votes"]), "vote_details": vote_details})
        room["votes"] = {}
        return {"type": "tie", "tied_numbers": sorted(tied_numbers), "vote_count": max_votes, "vote_details": vote_details, "vote_count_detail": vote_count_detail}

    eliminated_number = tied_numbers[0]
    eliminated_player = next(
        (p for p in room["players"].values() if p["number"] == eliminated_number and not p["eliminated"]),
        None
    )

    if eliminated_player:
        eliminated_player["eliminated"] = True
        room["vote_record"].append({
            "type": "eliminated",
            "number": eliminated_number,
            "role": eliminated_player["role"],
            "votes": dict(room["votes"]),
            "vote_details": vote_details
        })
        vote_count_detail = {}
        for num, voters in vote_count.items():
            player = next((p for p in room["players"].values() if p["number"] == num), None)
            vote_count_detail[num] = {"name": player["name"] if player else str(num), "count": len(voters)}
        room["votes"] = {}
        room["last_vote_result"] = {
            "number": eliminated_number,
            "name": eliminated_player["name"],
            "role": eliminated_player["role"],
            "is_undercover": eliminated_player["role"] == "undercover",
            "is_blank": eliminated_player["role"] == "blank",
            "is_civilian": eliminated_player["role"] == "civilian",
            "vote_details": vote_details,
            "vote_count": vote_count_detail
        }
        evaluate_game(room)
        return {"type": "eliminated", "number": eliminated_number, "name": eliminated_player["name"], "role": eliminated_player["role"], "vote_details": vote_details, "vote_count": vote_count_detail}

    return None


def vote(room, player_id, target_number):
    if room["status"] != "playing":
        raise ValueError("当前不能投票")

    player = room["players"].get(player_id)
    if not player:
        raise ValueError("玩家不存在")

    if player["eliminated"]:
        raise ValueError("已出局玩家不能投票")

    active_players = [p for p in room["players"].values() if not p["eliminated"]]

    target = next((p for p in room["players"].values() if p["number"] == target_number and not p["eliminated"]), None)
    if not target:
        raise ValueError("投票目标不存在或已出局")

    room["votes"][player_id] = target_number

    if len(room["votes"]) >= len(active_players):
        return tally_votes(room)

    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "UndercoverServer/0.1"

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        clean_rooms()
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self.send_text(HTML_FILE.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/health":
            self.send_json({"ok": True})
            return

        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 4 and parts[:2] == ["api", "rooms"] and parts[3] == "state":
            room_code = parts[2]
            query = parse_qs(parsed.query)
            player_id = query.get("player_id", [""])[0]
            try:
                room = ensure_room_player(room_code, player_id)
                self.send_json(public_state(room_code, player_id))
            except Exception as error:
                self.send_error_json(error)
            return

        self.send_error_json(ValueError("接口不存在"), status=404)

    def do_POST(self):
        clean_rooms()
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        data = self.read_json()

        try:
            if parts == ["api", "rooms"]:
                room_code, player_id = create_room(
                    str(data.get("name", "")).strip(),
                    str(data.get("client_id", "")).strip(),
                    self.client_ip(),
                )
                apply_settings(rooms[room_code], data)
                self.send_json({"player_id": player_id, "state": public_state(room_code, player_id)})
                return

            if len(parts) == 4 and parts[:2] == ["api", "rooms"]:
                room_code = parts[2]
                action = parts[3]

                if action == "join":
                    player_id = join_room(
                        room_code,
                        str(data.get("name", "")).strip(),
                        str(data.get("client_id", "")).strip(),
                        self.client_ip(),
                    )
                    self.send_json({"player_id": player_id, "state": public_state(room_code, player_id)})
                    return

                player_id = str(data.get("player_id", ""))
                room = ensure_room_player(room_code, player_id)

                if action == "leave":
                    result = leave_room(room_code, player_id)
                    if result is None:
                        self.send_json({"left": True, "room_code": None})
                    else:
                        self.send_json({"left": True, "room_code": room_code})
                    return

                if action == "settings":
                    ensure_host(room, player_id)
                    if room["status"] == "playing":
                        raise ValueError("游戏进行中不能修改设置")
                    apply_settings(room, data)
                    touch(room)
                    self.send_json({"state": public_state(room_code, player_id)})
                    return

                if action == "start":
                    ensure_host(room, player_id)
                    clean_rooms()
                    apply_settings(room, data)
                    start_game(room)
                    self.send_json({"state": public_state(room_code, player_id)})
                    return

                if action == "reset":
                    ensure_host(room, player_id)
                    clean_rooms()
                    for player in room["players"].values():
                        player["role"] = ""
                        player["word"] = ""
                        player["eliminated"] = False
                        player["burst_used"] = False
                    room["status"] = "waiting"
                    room["winner"] = None
                    room["undercover_numbers"] = []
                    room["votes"] = {}
                    room["vote_record"] = []
                    room["last_vote_result"] = None
                    room["burst_record"] = []
                    touch(room)
                    self.send_json({"state": public_state(room_code, player_id)})
                    return

                if action == "update_settings":
                    ensure_host(room, player_id)
                    if room["status"] != "waiting":
                        raise ValueError("当前不能修改设置")
                    new_settings = data.get("settings", {})
                    room["settings"].update(new_settings)
                    touch(room)
                    self.send_json({"state": public_state(room_code, player_id)})
                    return

                if action == "eliminate":
                    ensure_host(room, player_id)
                    if room["status"] != "playing":
                        raise ValueError("当前不能出局")
                    number = int(data.get("number", 0))
                    target = next((p for p in room["players"].values() if p["number"] == number), None)
                    if not target:
                        raise ValueError("玩家不存在")
                    target["eliminated"] = True
                    evaluate_game(room)
                    touch(room)
                    self.send_json({"state": public_state(room_code, player_id)})
                    return

                if action == "burst":
                    player = room["players"].get(player_id)
                    if not player:
                        raise ValueError("玩家不在房间内")
                    if player.get("burst_used"):
                        raise ValueError("本局已经爆词过")
                    if not can_player_burst(room, player):
                        raise ValueError("只有出局的卧底或空白词玩家可以爆词")
                    guess = normalize_word(data.get("guess", ""))
                    if not guess:
                        raise ValueError("请输入猜测的词语")
                    civilian_word = normalize_word(room["words"].get("civilian", ""))
                    success = bool(guess and guess == civilian_word)
                    player["burst_used"] = True
                    burst_result = {
                        "player_number": player["number"] if player else 0,
                        "player_name": player["name"] if player else "",
                        "guess": "" if success else guess,
                        "success": success
                    }
                    room["burst_record"].append(burst_result)
                    touch(room)
                    self.send_json({"burst_result": burst_result, "state": public_state(room_code, player_id)})
                    return

                if action == "vote":
                    number = int(data.get("number", 0))
                    result = vote(room, player_id, number)
                    if result:
                        self.send_json({"vote_result": result, "state": public_state(room_code, player_id)})
                    else:
                        self.send_json({"vote_result": None, "state": public_state(room_code, player_id)})
                    return

            self.send_error_json(ValueError("接口不存在"), status=404)
        except Exception as error:
            self.send_error_json(error)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def client_ip(self):
        forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        return forwarded or self.client_address[0]

    def send_text(self, text, content_type):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, error, status=400):
        if isinstance(error, PermissionError):
            status = 403
        self.send_json({"error": str(error)}, status=status)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def check_port(port):
    if is_port_in_use(port):
        raise RuntimeError(f"端口 {port} 已被占用，请先关闭占用该端口的程序")


def main():
    load_env()
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    check_port(port)
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Who is the Undercover Server {VERSION}")
    print(f"Serving on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
