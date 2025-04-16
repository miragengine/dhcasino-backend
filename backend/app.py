from flask import Flask, render_template, jsonify, request, session, redirect, url_for, abort
from flask_socketio import SocketIO, emit
from functools import wraps
from werkzeug.security import generate_password_hash
from pymongo import MongoClient
from flask_session import Session
from flask_pymongo import PyMongo
from requests_oauthlib import OAuth2Session
from flask_cors import CORS
import random
import time
import uuid
import threading
import requests
import os
from datetime import datetime

DISCORD_CLIENT_ID = "1360579230278352987"
DISCORD_CLIENT_SECRET = "c4-ICfZhLvST9aPeF6OsdWbfSIYMGH9J"
DISCORD_REDIRECT_URI = "https://9c8f-31-32-166-161.ngrok-free.app/callback"
DISCORD_API_BASE_URL = "https://discord.com/api"

app = Flask(__name__)
app.secret_key = "DHCasinoSecretKey"
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

app.config.update(
    SESSION_COOKIE_SAMESITE='None',
    SESSION_COOKIE_SECURE=True
)

app.config["SESSION_TYPE"] = "mongodb"
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_USE_SIGNER"] = True
app.config["SESSION_MONGODB"] = PyMongo(app, uri="mongodb://localhost:27017/casino_db").cx

Session(app)

client = MongoClient("mongodb://localhost:27017/")
db = client["casino_db"]
users_collection = db["users"]

hosted_flips = {}
active_flips = {}
connected_users = {}

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        username = session.get("user")
        if not username:
            return abort(403)
        user = users_collection.find_one({"username": username})
        if not user or not user.get("admin"):
            return abort(403)
        return f(*args, **kwargs)
    return decorated

@app.route("/admin")
@admin_required
def admin_panel():
    return render_template("admin.html", version=time.time())

@socketio.on('connect')
def track_connect():
    username = session.get("user")
    if username:
        connected_users[username] = time.time()

@socketio.on('disconnect')
def track_disconnect():
    username = session.get("user")
    if username and username in connected_users:
        del connected_users[username]

@app.route("/admin/api/connected")
@admin_required
def get_connected_users():
    return jsonify(list(connected_users.keys()))

@app.route("/admin/api/active-players")
@admin_required
def get_active_players():
    active = set()
    for flip in active_flips.values():
        if flip.get("state") in ["open", "countdown"]:
            active.add(flip.get("host"))
            active.add(flip.get("guest"))
    return jsonify(list(filter(None, active)))

@app.route("/admin/api/logs")
@admin_required
def get_logs():
    logs = list(db["flips"].find().sort("finishedAt", -1).limit(100))
    for log in logs:
        log["_id"] = str(log["_id"])
    return jsonify(logs)

@app.route("/admin/api/users")
@admin_required
def get_all_users():
    users = list(users_collection.find())
    for user in users:
        user["_id"] = str(user["_id"])
    return jsonify(users)

@app.route("/admin/api/player/<username>")
@admin_required
def get_player_details(username):
    user = users_collection.find_one({"username": username})
    if not user:
        return jsonify({"error": "User not found"}), 404

    flips = list(db["flips"].find({"host": username}))
    profit = sum([f["bet"] if f.get("winner") == username else -f["bet"] for f in flips])
    joined_at = user.get("joined_at", "unknown")
    if isinstance(joined_at, int):
        joined_at = datetime.utcfromtimestamp(joined_at).strftime('%Y-%m-%d %H:%M:%S')

    data = {
        "username": username,
        "balance": round(user.get("balance", 0.0), 2),
        "profit": round(profit, 2),
        "hosted_flips": len(flips),
        "joined_at": joined_at
    }
    return jsonify(data)

@app.route("/admin/api/change-balance", methods=["POST"])
@admin_required
def change_balance():
    data = request.get_json()
    username = data.get("username")
    try:
        amount = float(data.get("amount"))
    except:
        return jsonify({"error": "Invalid amount"}), 400

    user = users_collection.find_one({"username": username})
    if not user:
        return jsonify({"error": "User not found"}), 404

    current_balance = user.get("balance", 0.0)
    new_balance = round(current_balance + amount, 2)

    users_collection.update_one({"username": username}, {"$set": {"balance": new_balance}})
    socketio.emit("balance_update", {"username": username, "balance": new_balance}, to=None)
    return jsonify({"message": f"Balance updated to ${new_balance}"})

@app.route("/discord-login")
def discord_login():
    discord = OAuth2Session(DISCORD_CLIENT_ID, redirect_uri=DISCORD_REDIRECT_URI, scope=["identify"])
    auth_url, state = discord.authorization_url(f"{DISCORD_API_BASE_URL}/oauth2/authorize")
    session["oauth_state"] = state
    session.modified = True
    return redirect(auth_url)

@app.route("/callback")
def callback():
    if "oauth_state" not in session:
        return jsonify({"error": "Missing OAuth state"}), 400
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    discord = OAuth2Session(DISCORD_CLIENT_ID, redirect_uri=DISCORD_REDIRECT_URI, state=session["oauth_state"])
    token = discord.fetch_token(
        f"{DISCORD_API_BASE_URL}/oauth2/token",
        client_secret=DISCORD_CLIENT_SECRET,
        authorization_response=request.url,
    )
    discord = OAuth2Session(DISCORD_CLIENT_ID, token=token)
    user_info = discord.get(f"{DISCORD_API_BASE_URL}/users/@me").json()
    username = user_info.get("global_name") or user_info["username"]
    user_id = user_info["id"]
    avatar = user_info["avatar"]
    user = users_collection.find_one({"username": username})
    if not user:
        users_collection.insert_one({
            "username": username,
            "balance": 0.0,
            "avatar": avatar,
            "user_id": user_id,
            "joined_at": int(time.time()),
            "admin": False
        })
    else:
        users_collection.update_one({"username": username}, {"$set": {"avatar": avatar, "user_id": user_id}})
    session["user"] = username
    session["avatar"] = avatar
    session["user_id"] = user_id
    return redirect("/")

def preload_users():
    predefined_users = [{"username": "admin", "password": "admin123"}, {"username": "test", "password": "testpass"}]
    for user in predefined_users:
        if not users_collection.find_one({"username": user["username"]}):
            hashed = generate_password_hash(user["password"])
            users_collection.insert_one({"username": user["username"], "password": hashed})

@app.route("/host")
def host_page():
    return render_template("host.html", version=time.time())

@socketio.on("connect")
def on_connect(auth):
    username = session.get("user")
    if username:
        connected_users[username] = time.time()
    all_flips = list(hosted_flips.values()) + list(active_flips.values())
    
    for flip in all_flips:
        if "_id" in flip:
            flip["_id"] = str(flip["_id"])

    emit("available_flips", all_flips)

@app.route("/host-flip", methods=["POST"])
def host_flip():
    if "user" not in session:
        return jsonify({"error": "Not logged in"}), 403
    data = request.get_json()
    side = data.get("side")
    amount = data.get("amount")
    if not side or not amount:
        return jsonify({"error": "Invalid flip data"}), 400
    flip_id = str(uuid.uuid4())
    flip = {
        "id": flip_id,
        "host": session["user"],
        "host_id": session.get("user_id"),
        "host_avatar": session.get("avatar"),
        "side": side,
        "bet": float(amount),
        "state": "waiting",
        "user": session["user"]
    }
    hosted_flips[flip_id] = flip
    socketio.emit("available_flips", list(hosted_flips.values()))
    return jsonify({"success": True, "flip": flip})

@socketio.on("join_flip")
def join_flip(data):
    username = session.get("user")
    flip_id = data.get("id")
    if flip_id not in hosted_flips:
        return
    flip = hosted_flips.pop(flip_id)
    flip["guest"] = username
    flip["guest_id"] = session.get("user_id")
    flip["guest_avatar"] = session.get("avatar")
    flip["state"] = "open"
    flip["opened"] = []
    flip["user"] = username
    active_flips[flip_id] = flip
    socketio.emit("flip_updated", flip, to=None)

@socketio.on("click_open")
def click_open(data):
    flip_id = data.get("id")
    username = data.get("user")
    flip = active_flips.get(flip_id)
    if not flip or flip["state"] != "open":
        return
    if "opened" not in flip:
        flip["opened"] = []
    if username not in flip["opened"]:
        flip["opened"].append(username)
    socketio.emit("flip_updated", flip, to=None)
    if len(flip["opened"]) == 1:
        flip["waiting_for"] = flip["guest"] if username == flip["host"] else flip["host"]
        socketio.emit("waiting_for_player", {
            "id": flip_id,
            "waiting_for": flip["waiting_for"]
        }, to=None)
    if len(flip["opened"]) == 2:
        flip["state"] = "countdown"
        flip["result"] = "Heads" if random.random() < 0.5 else "Tails"
        flip["start_time"] = time.time() + 10
        flip["host_side"] = flip["side"]
        flip["guest_side"] = "Tails" if flip["side"] == "Heads" else "Heads"
        socketio.emit("start_match", {
            "id": flip_id,
            "result": flip["result"],
            "start_time": flip["start_time"]
        }, to=None)
        threading.Thread(target=resolve_flip_after_delay, args=(flip_id,), daemon=True).start()

@app.route("/api/flip/<flip_id>")
def get_flip(flip_id):
    flip = active_flips.get(flip_id)
    if not flip:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "id": flip["id"],
        "state": flip["state"],
        "result": flip.get("result"),
        "start_time": flip.get("start_time"),
        "host": flip["host"],
        "guest": flip.get("guest"),
        "host_side": flip.get("host_side") or flip.get("side"),
        "guest_side": flip.get("guest_side") or ("Tails" if flip.get("side") == "Heads" else "Heads"),
        "waiting_for": flip.get("waiting_for"),
        "bet": flip.get("bet", 0.0)
    })

def resolve_flip_after_delay(flip_id):
    time.sleep(10)
    flip = active_flips.get(flip_id)
    if not flip:
        return
    result = flip["result"]
    host = flip["host"]
    guest = flip["guest"]
    bet = float(flip["bet"])
    host_side = flip["host_side"]
    guest_side = flip["guest_side"]
    winner = host if result == host_side else guest
    loser = guest if winner == host else host
    winner_user = users_collection.find_one({"username": winner})
    loser_user = users_collection.find_one({"username": loser})
    if winner_user and loser_user:
        new_winner_balance = round(winner_user.get("balance", 0.0) + bet, 2)
        new_loser_balance = round(loser_user.get("balance", 0.0) - bet, 2)
        users_collection.update_one({"username": winner}, {"$set": {"balance": new_winner_balance}})
        users_collection.update_one({"username": loser}, {"$set": {"balance": new_loser_balance}})
        socketio.emit("balance_update", {"username": winner, "balance": new_winner_balance}, to=None)
        socketio.emit("balance_update", {"username": loser, "balance": new_loser_balance}, to=None)
    flip["state"] = "done"
    flip["winner"] = winner
    flip["finishedAt"] = int(time.time() * 1000)
    flip["host_id"] = flip.get("host_id")
    flip["guest_id"] = flip.get("guest_id")
    flip["host_avatar"] = flip.get("host_avatar")
    flip["guest_avatar"] = flip.get("guest_avatar")
    db["flips"].insert_one(flip)
    socketio.emit("flip_resolved", {
        "id": flip_id,
        "result": result,
        "winner": winner,
        "finishedAt": flip["finishedAt"],
        "host": flip["host"],
        "guest": flip["guest"],
        "host_id": flip["host_id"],
        "guest_id": flip["guest_id"],
        "host_avatar": flip["host_avatar"],
        "guest_avatar": flip["guest_avatar"],
        "bet": bet
    }, to=None)

@app.route("/host/<flip_id>")
def host_match(flip_id):
    return render_template("coinflip.html", version=time.time())

@app.route("/")
def index():
    return render_template("index.html", version=time.time())

@app.route("/coin-flip")
def coin_flip():
    return render_template("coinflip.html", version=time.time())

@app.route("/logout", methods=["POST"])
def logout():
    session.pop("user", None)
    return jsonify({"message": "Logged out successfully."}), 200

@app.route("/check_session")
def check_session():
    if "user" in session:
        return jsonify({
            "logged_in": True,
            "username": session["user"],
            "avatar": session.get("avatar"),
            "user_id": session.get("user_id")
        })
    return jsonify({ "logged_in": False })

@app.route("/balance")
def balance():
    if "user" not in session:
        return jsonify({"error": "Not logged in"}), 403
    user = users_collection.find_one({"username": session["user"]})
    return jsonify({"balance_usd": round(user.get("balance", 0.0), 2)})

@app.route("/set-balance", methods=["POST"])
def set_balance():
    if "user" not in session:
        return jsonify({"error": "Not logged in"}), 403
    data = request.get_json()
    try:
        amount = float(data.get("amount", 0))
    except:
        return jsonify({"error": "Invalid amount"}), 400
    users_collection.update_one({"username": session["user"]}, {"$set": {"balance": round(amount, 2)}})
    return jsonify({"message": "Balance updated", "new_balance": amount})

@app.route("/play-coinflip", methods=["POST"])
def play_coinflip():
    if "user" not in session:
        return jsonify({"error": "Not logged in"}), 403
    data = request.get_json()
    choice = data.get("choice")
    try:
        amount_usd = float(data.get("amount", 0))
    except:
        return jsonify({"error": "Invalid amount format."}), 400
    if choice not in ["Heads", "Tails"] or amount_usd <= 0:
        return jsonify({"error": "Invalid input"}), 400
    user = users_collection.find_one({"username": session["user"]})
    current_balance = user.get("balance", 0.0)
    if amount_usd > current_balance:
        return jsonify({"error": "Insufficient funds"}), 400
    result = "Heads" if random.random() < 0.5 else "Tails"
    won = result == choice
    new_balance = current_balance + amount_usd if won else current_balance - amount_usd
    users_collection.update_one({"username": session["user"]}, {"$set": {"balance": round(new_balance, 2)}})
    return jsonify({"result": result, "won": won, "new_balance": new_balance})

@app.route("/get-ltc-price")
def get_ltc_price():
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=LTCUSDT"
        response = requests.get(url).json()
        if "price" in response:
            return jsonify({"ltc_price": float(response["price"])})
    except:
        pass
    return jsonify({"ltc_price": 0.0})

@app.route("/reset-db", methods=["POST"])
def reset_db():
    users_collection.delete_many({})
    preload_users()
    return jsonify({"message": "Database reset complete."})

if __name__ == "__main__":
    preload_users()
    socketio.run(app, debug=True, host="0.0.0.0")
