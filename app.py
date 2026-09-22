from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash
from dotenv import load_dotenv
from groq import Groq
import os
import requests
import sqlite3
import json
from datetime import datetime
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "agrosmart-development-key")
DATABASE = os.path.join(app.instance_path, "agrosmart.db")


def get_db():
    os.makedirs(app.instance_path, exist_ok=True)
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with get_db() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS farm_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                name TEXT,
                location TEXT NOT NULL,
                crop TEXT NOT NULL,
                soil TEXT NOT NULL,
                farm_size REAL NOT NULL DEFAULT 0,
                water TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                farm_profile_id INTEGER,
                farm_id INTEGER,
                crop TEXT NOT NULL,
                soil TEXT NOT NULL,
                moisture REAL NOT NULL,
                temperature REAL NOT NULL,
                humidity REAL NOT NULL,
                rainfall REAL NOT NULL,
                irrigation_required INTEGER NOT NULL,
                risk TEXT NOT NULL,
                water_quantity REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (farm_profile_id) REFERENCES farm_profiles(id)
            );
            CREATE TABLE IF NOT EXISTS weather_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                farm_id INTEGER,
                location TEXT NOT NULL,
                country TEXT NOT NULL DEFAULT '',
                temperature REAL NOT NULL,
                humidity REAL NOT NULL,
                rainfall REAL NOT NULL,
                rain_probability REAL NOT NULL,
                forecast_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                farm_id INTEGER,
                message TEXT NOT NULL,
                reply TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS crop_health_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                farm_id INTEGER NOT NULL,
                health_score REAL NOT NULL,
                moisture REAL NOT NULL,
                temperature REAL NOT NULL,
                humidity REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (farm_id) REFERENCES farm_profiles(id)
            );
        """)

        # ── Ensure demo user exists ──────────────────────────────────────
        demo = connection.execute(
            "SELECT id FROM users WHERE email = ?", ("demo@agrosmart.com",)
        ).fetchone()
        if not demo:
            connection.execute(
                "INSERT INTO users (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                ("Demo Farmer", "demo@agrosmart.com", generate_password_hash("demo123"), datetime.utcnow().isoformat()),
            )
        demo_id = connection.execute(
            "SELECT id FROM users WHERE email = ?", ("demo@agrosmart.com",)
        ).fetchone()["id"]

        # ── Migrate older tables that may be missing user_id column ──────
        for table in ("farm_profiles", "analyses", "chat_messages"):
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            if "user_id" not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")
            connection.execute(
                f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (demo_id,)
            )

        migrations = {
            "farm_profiles": ("name",),
            "analyses": ("farm_id",),
            "weather_records": ("farm_id",),
            "chat_messages": ("user_id", "farm_id"),
        }
        for table, required_columns in migrations.items():
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            for column in required_columns:
                if column not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER" if column in ("farm_id", "user_id") else f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

        connection.execute("UPDATE farm_profiles SET name = COALESCE(NULLIF(name, ''), crop || ' Farm')")
        connection.execute("UPDATE analyses SET farm_id = farm_profile_id WHERE farm_id IS NULL")

        # ── Indexes (run individually so any pre-existing ones are fine) ─
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_farm_profiles_user_id ON farm_profiles(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_analyses_user_id ON analyses(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_weather_records_user_id ON weather_records(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_user_id ON chat_messages(user_id)",
            "CREATE INDEX IF NOT EXISTS idx_farm_profiles_user_name ON farm_profiles(user_id, name)",
            "CREATE INDEX IF NOT EXISTS idx_analyses_farm_id ON analyses(farm_id)",
            "CREATE INDEX IF NOT EXISTS idx_weather_records_farm_id ON weather_records(farm_id)",
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_farm_id ON chat_messages(farm_id)",
            "CREATE INDEX IF NOT EXISTS idx_crop_health_farm_id ON crop_health_records(farm_id)",
        ):
            connection.execute(stmt)



init_db()


def get_farm_for_user(farm_id=None):
    """Return an owned farm, or None when the user has no farms."""
    requested_id = farm_id or session.get("active_farm_id")
    with get_db() as connection:
        farm = None
        if requested_id:
            farm = connection.execute(
                "SELECT * FROM farm_profiles WHERE id = ? AND user_id = ?",
                (requested_id, session["user_id"]),
            ).fetchone()
        if not farm:
            farm = connection.execute(
                "SELECT * FROM farm_profiles WHERE user_id = ? ORDER BY id LIMIT 1",
                (session["user_id"],),
            ).fetchone()
    if farm:
        session["active_farm_id"] = farm["id"]
    return farm


@app.context_processor
def inject_active_farm():
    return {"active_farm": get_farm_for_user() if session.get("user_id") else None}


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"success": False, "error": "Authentication required."}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped_view

# -----------------------------
# GROQ CONFIGURATION
# -----------------------------

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

client = None

if GROQ_API_KEY:
    try:
        client = Groq(api_key=GROQ_API_KEY)
    except TypeError as error:
        print("GROQ CLIENT ERROR:", error)


# -----------------------------
# PAGES
# -----------------------------

@app.route("/")
def home():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with get_db() as connection:
            user = connection.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("The email or password is incorrect.", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or not email or len(password) < 6:
            flash("Enter your name, a valid email, and a password of at least 6 characters.", "error")
        else:
            try:
                with get_db() as connection:
                    connection.execute(
                        "INSERT INTO users (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                        (name, email, generate_password_hash(password), datetime.utcnow().isoformat()),
                    )
                flash("Account created. You can sign in now.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("An account with that email already exists.", "error")
    return render_template("register.html")


@app.route("/demo-login")
def demo_login():
    with get_db() as connection:
        user = connection.execute("SELECT id, name FROM users WHERE email = ?", ("demo@agrosmart.com",)).fetchone()
    session.clear()
    session["user_id"] = user["id"]
    session["user_name"] = user["name"]
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/farms")
@login_required
def farms_page():
    with get_db() as connection:
        farms = connection.execute(
            "SELECT * FROM farm_profiles WHERE user_id = ? ORDER BY id DESC",
            (session["user_id"],),
        ).fetchall()
    return render_template("farms.html", farms=farms)


@app.route("/api/farms", methods=["GET", "POST"])
@login_required
def farms_api():
    if request.method == "GET":
        with get_db() as connection:
            farms = connection.execute(
                "SELECT * FROM farm_profiles WHERE user_id = ? ORDER BY id DESC",
                (session["user_id"],),
            ).fetchall()
        active = get_farm_for_user()
        return jsonify({
            "success": True,
            "farms": [dict(farm) for farm in farms],
            "active_farm_id": active["id"] if active else None,
        })

    data = request.get_json(silent=True) or {}
    fields = {
        "name": str(data.get("name", "")).strip(),
        "location": str(data.get("location", "")).strip(),
        "crop": str(data.get("crop", "")).strip(),
        "soil": str(data.get("soil", "")).strip(),
        "water": str(data.get("water", "")).strip(),
    }
    try:
        farm_size = float(data.get("farm_size", 0))
    except (TypeError, ValueError):
        farm_size = 0
    if not all(fields.values()) or farm_size <= 0:
        return jsonify({"success": False, "error": "Complete all farm fields with a valid farm size."}), 400

    with get_db() as connection:
        cursor = connection.execute(
            """INSERT INTO farm_profiles
               (user_id, name, location, crop, soil, farm_size, water, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (session["user_id"], fields["name"], fields["location"], fields["crop"],
             fields["soil"], farm_size, fields["water"], datetime.utcnow().isoformat()),
        )
        farm = connection.execute("SELECT * FROM farm_profiles WHERE id = ?", (cursor.lastrowid,)).fetchone()
    session["active_farm_id"] = farm["id"]
    return jsonify({"success": True, "farm": dict(farm), "active_farm_id": farm["id"]}), 201


@app.route("/api/farms/<int:farm_id>", methods=["PUT", "DELETE"])
@login_required
def farm_api(farm_id):
    with get_db() as connection:
        farm = connection.execute(
            "SELECT * FROM farm_profiles WHERE id = ? AND user_id = ?",
            (farm_id, session["user_id"]),
        ).fetchone()
        if not farm:
            return jsonify({"success": False, "error": "Farm not found."}), 404

        if request.method == "DELETE":
            connection.execute("DELETE FROM crop_health_records WHERE farm_id = ?", (farm_id,))
            connection.execute("DELETE FROM analyses WHERE farm_id = ?", (farm_id,))
            connection.execute("DELETE FROM weather_records WHERE farm_id = ?", (farm_id,))
            connection.execute("DELETE FROM chat_messages WHERE farm_id = ?", (farm_id,))
            connection.execute("DELETE FROM farm_profiles WHERE id = ?", (farm_id,))
            if session.get("active_farm_id") == farm_id:
                session.pop("active_farm_id", None)
                get_farm_for_user()
            return jsonify({"success": True})

        data = request.get_json(silent=True) or {}
        fields = {
            "name": str(data.get("name", "")).strip(),
            "location": str(data.get("location", "")).strip(),
            "crop": str(data.get("crop", "")).strip(),
            "soil": str(data.get("soil", "")).strip(),
            "water": str(data.get("water", "")).strip(),
        }
        try:
            farm_size = float(data.get("farm_size", 0))
        except (TypeError, ValueError):
            farm_size = 0
        if not all(fields.values()) or farm_size <= 0:
            return jsonify({"success": False, "error": "Complete all farm fields with a valid farm size."}), 400
        connection.execute(
            """UPDATE farm_profiles SET name = ?, location = ?, crop = ?, soil = ?,
               farm_size = ?, water = ? WHERE id = ? AND user_id = ?""",
            (fields["name"], fields["location"], fields["crop"], fields["soil"],
             farm_size, fields["water"], farm_id, session["user_id"]),
        )
        updated = connection.execute("SELECT * FROM farm_profiles WHERE id = ?", (farm_id,)).fetchone()
    return jsonify({"success": True, "farm": dict(updated)})


@app.route("/api/farms/<int:farm_id>/select", methods=["POST"])
@login_required
def select_farm(farm_id):
    farm = get_farm_for_user(farm_id)
    if not farm or farm["id"] != farm_id:
        return jsonify({"success": False, "error": "Farm not found."}), 404
    session["active_farm_id"] = farm_id
    return jsonify({"success": True, "farm": dict(farm)})


@app.route("/irrigation")
@login_required
def irrigation_page():
    return render_template("irrigation.html")


@app.route("/crop-health")
@login_required
def crop_health_page():
    return render_template("crop_health.html")


@app.route("/analytics")
@login_required
def analytics_page():
    farm = get_farm_for_user()
    return render_template("analytics.html", analyses=get_recent_analyses(farm["id"] if farm else None))


@app.route("/weather")
@login_required
def weather_page():
    return render_template("weather.html")


@app.route("/ai-assistant")
@login_required
def ai_assistant_page():
    return render_template("ai_assistant.html")


def get_recent_analyses(farm_id=None, limit=20):
    with get_db() as connection:
        if farm_id:
            return connection.execute(
                "SELECT analyses.*, farm_profiles.name AS farm_name FROM analyses "
                "LEFT JOIN farm_profiles ON farm_profiles.id = analyses.farm_id "
                "WHERE analyses.user_id = ? AND analyses.farm_id = ? ORDER BY analyses.id DESC LIMIT ?",
                (session["user_id"], farm_id, limit),
            ).fetchall()
        return connection.execute(
            "SELECT * FROM analyses WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (session["user_id"], limit),
        ).fetchall()


@app.route("/api/analytics")
@login_required
def analytics_api():
    farm = get_farm_for_user(request.args.get("farm_id"))
    with get_db() as connection:
        summary = connection.execute("""
            SELECT COUNT(*) AS total,
                   COALESCE(AVG(moisture), 0) AS avg_moisture,
                   COALESCE(AVG(water_quantity), 0) AS avg_water,
                   SUM(irrigation_required) AS irrigation_count
            FROM analyses
            WHERE user_id = ? AND farm_id = ?
        """, (session["user_id"], farm["id"] if farm else -1)).fetchone()
    return jsonify(dict(summary))


# -----------------------------
# IRRIGATION API
# -----------------------------

@app.route("/api/irrigation", methods=["POST"])
@login_required
def irrigation():

    data = request.get_json(silent=True) or {}

    requested_farm_id = data.get("farm_id")
    try:
        requested_farm_id = int(requested_farm_id) if requested_farm_id is not None else None
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Select a valid farm before running an analysis."}), 400
    farm = get_farm_for_user(requested_farm_id)
    if not farm or requested_farm_id and farm["id"] != requested_farm_id:
        return jsonify({"success": False, "error": "Select a farm before running an analysis."}), 400

    crop = data.get("crop")
    soil = data.get("soil")
    moisture = float(data.get("moisture", 0))
    temperature = float(data.get("temperature", 0))
    humidity = float(data.get("humidity", 0))
    rainfall = float(data.get("rainfall", 0))
    farm_size = float(data.get("farm_size", 0))
    water = data.get("water")

    # --------------------------------
    # BASIC IRRIGATION DECISION
    # --------------------------------

    irrigation_required = False

    if moisture < 30:
        irrigation_required = True

    # If considerable rain is expected,
    # recommend delaying irrigation.

    if rainfall > 40:
        irrigation_required = False

    # --------------------------------
    # RECOMMENDED TIME
    # --------------------------------

    if irrigation_required:

        if temperature > 32:
            recommended_time = "6:00 AM"
        else:
            recommended_time = "7:00 AM"

    else:

        if rainfall > 40:
            recommended_time = "Delay irrigation"
        else:
            recommended_time = "Not required"

    # --------------------------------
    # DURATION
    # --------------------------------

    if irrigation_required:

        if water == "Low":
            duration = 15
        elif water == "Medium":
            duration = 25
        else:
            duration = 30

    else:

        duration = 0

    # --------------------------------
    # APPROXIMATE WATER
    # --------------------------------

    if irrigation_required:
        water_quantity = round(farm_size * 600)
    else:
        water_quantity = 0

    # --------------------------------
    # RISK
    # --------------------------------

    risk = "Low"

    if temperature > 32 or moisture < 20:
        risk = "Medium"

    if temperature > 38 or moisture < 10:
        risk = "High"

    # --------------------------------
    # CONFIDENCE
    # --------------------------------

    confidence = 90

    if rainfall > 40:
        confidence = 85

    # --------------------------------
    # CLEAR EXPLANATION
    # --------------------------------

    explanation = build_irrigation_explanation(
        crop,
        moisture,
        temperature,
        humidity,
        rainfall,
        irrigation_required,
    )

    health = max(0, 100
        - (moisture < 30) * 10
        - (temperature > 32) * 8
        - (humidity < 35) * 5
        - (rainfall > 70) * 5)

    with get_db() as connection:
        connection.execute(
            """INSERT INTO analyses
                    (user_id, farm_profile_id, farm_id, crop, soil, moisture, temperature, humidity,
                     rainfall, irrigation_required, risk, water_quantity, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session["user_id"], farm["id"], farm["id"], crop, soil, moisture, temperature, humidity,
             rainfall, int(irrigation_required), risk, water_quantity,
             datetime.utcnow().isoformat()),
        )
        connection.execute(
            """INSERT INTO crop_health_records
               (user_id, farm_id, health_score, moisture, temperature, humidity, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (session["user_id"], farm["id"], health, moisture, temperature, humidity,
             datetime.utcnow().isoformat()),
        )

    return jsonify({
        "success": True,
        "irrigation_required": irrigation_required,
        "recommended_time": recommended_time,
        "duration": duration,
        "water_quantity": water_quantity,
        "risk": risk,
        "confidence": confidence,
        "explanation": explanation,
        "farm_id": farm["id"],
        "farm_name": farm["name"],
        "health": health,
    })


# -----------------------------
# IRRIGATION EXPLANATION
# -----------------------------

def build_irrigation_explanation(
    crop,
    moisture,
    temperature,
    humidity,
    rainfall,
    irrigation_required,
):
    """Build a short explanation from the same values used by the rule engine."""

    if irrigation_required:
        return (
            f"Why: The {crop} field has {moisture:.1f}% soil moisture, "
            f"so irrigation is recommended.\n"
            f"Main condition: Rain probability is {rainfall:.0f}%, "
            f"with a temperature of {temperature:.1f}°C and humidity of "
            f"{humidity:.0f}%.\n"
            "Water-saving tip: Irrigate during the cooler part of the day "
            "and use only the recommended duration.\n"
            "Caution: Recheck the soil after watering and reassess if rain "
            "arrives or conditions change."
        )

    if rainfall > 40:
        return (
            "Why: Delay irrigation because the field already has "
            f"{moisture:.1f}% soil moisture and rain is likely soon.\n"
            f"Main condition: Rain probability is {rainfall:.0f}%, so the "
            "upcoming rain should provide sufficient water.\n"
            "Water-saving tip: Keep the irrigation lines turned off. Turn "
            "them on only if the rain does not arrive or the soil dries out "
            "sharply.\n"
            "Caution: Watch the forecast. If the rain stalls or humidity "
            "drops, reassess the crop for water stress."
        )

    return (
        "Why: Irrigation is not needed right now based on the current field "
        f"conditions, including {moisture:.1f}% soil moisture.\n"
        f"Main condition: Rain probability is only {rainfall:.0f}%, so keep "
        "monitoring the soil before watering.\n"
        "Water-saving tip: Irrigate only when the soil becomes too dry for "
        "the crop.\n"
        "Caution: Recheck the forecast and soil moisture if the weather "
        "becomes hotter or drier."
    )


# -----------------------------
# AI EXPLANATION (legacy helper)
# -----------------------------

def generate_ai_explanation(
    crop,
    soil,
    moisture,
    temperature,
    humidity,
    rainfall,
    farm_size,
    water,
    irrigation_required,
    recommended_time,
    duration,
    water_quantity,
    risk
):

    # If API key isn't configured,
    # return a fallback explanation.

    if not client:

        if irrigation_required:

            return (
                f"The soil moisture is relatively low for the "
                f"{crop} field. Irrigation is recommended during "
                f"the cooler part of the day. Monitor soil moisture "
                f"after irrigation and avoid unnecessary watering."
            )

        return (
            "Irrigation is currently not recommended based on "
            "the available farm conditions. Continue monitoring "
            "soil moisture and rainfall."
        )

    prompt = f"""
You are AgroMind, an agriculture decision-support assistant.

Analyze the following farm information.

Crop: {crop}
Soil Type: {soil}
Soil Moisture: {moisture}%
Temperature: {temperature}°C
Humidity: {humidity}%
Rain Probability: {rainfall}%
Farm Size: {farm_size} acres
Water Availability: {water}

System irrigation decision:
Irrigation Required: {irrigation_required}
Recommended Time: {recommended_time}
Duration: {duration} minutes
Approximate Water Quantity: {water_quantity} L
Risk Level: {risk}

Explain the recommendation to a farmer in simple language.

Give:
1. Why irrigation is or isn't recommended.
2. Main condition affecting the decision.
3. One water-saving suggestion.
4. One caution.

Do not claim certainty.
Do not invent sensor measurements.
Keep the answer below 120 words.
"""

    try:

        response = client.chat.completions.create(

            model="openai/gpt-oss-120b",

            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful agriculture "
                        "decision-support assistant."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],

            temperature=0.3,

            max_tokens=250
        )

        return response.choices[0].message.content

    except Exception as e:

        print("AI ERROR:", e)

        return (
            "The irrigation recommendation was calculated "
            "from the available farm conditions. Please "
            "monitor soil moisture and weather conditions "
            "before irrigating."
        )


# -----------------------------
# CHATBOT API
# -----------------------------

@app.route("/api/chat", methods=["POST"])
@login_required
def chat():

    data = request.get_json(silent=True) or {}

    message = data.get("message", "").strip()
    context = data.get("context", {})
    farm = get_farm_for_user(context.get("farm_id"))
    if not farm:
        return jsonify({"success": False, "error": "Select a farm before using the AI assistant."}), 400

    if not message:
        return jsonify({
            "success": False,
            "error": "Message is required."
        }), 400

    if not client:
        reply = (
            "Please configure the GROQ_API_KEY in your "
            ".env file to enable the AgroMind AI assistant."
        )
        with get_db() as connection:
            connection.execute(
                "INSERT INTO chat_messages (user_id, farm_id, message, reply, created_at) VALUES (?, ?, ?, ?, ?)",
                (session["user_id"], farm["id"], message, reply, datetime.utcnow().isoformat()),
            )
        return jsonify({
            "success": True,
            "reply": reply
        })

    prompt = f"""
You are AgroMind, an agriculture assistant.

Answer the farmer's question clearly and simply.

Farmer question:
{message}

Farm Context:
Crop: {context.get('crop', 'Unknown')}
Soil: {context.get('soil', 'Unknown')}
Soil Moisture: {context.get('moisture', 'Unknown')}%
Temperature: {context.get('temperature', 'Unknown')}°C
Humidity: {context.get('humidity', 'Unknown')}%
Rain Probability: {context.get('rainfall', 'Unknown')}%
Farm Size: {context.get('farm_size', 'Unknown')} acres
Water Availability: {context.get('water', 'Unknown')}
Latest Irrigation Recommendation: {context.get('irrigation_required', 'Unknown')}
Crop Health Score: {context.get('health', 'Unknown')}%

Rules:
- Give practical general agricultural guidance based on the context provided.
- Keep the answer concise.
- Do not claim certainty.
- Do not invent missing data.
- Do not override the rule-based irrigation engine's recommendation.
- If the issue could require professional agricultural inspection, recommend contacting a local agriculture expert.
"""

    try:

        response = client.chat.completions.create(

            model="openai/gpt-oss-120b",

            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful agriculture assistant named AgroMind AI."
                    )
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],

            temperature=0.4,

            max_tokens=400
        )

        reply = response.choices[0].message.content

        with get_db() as connection:
            connection.execute(
                "INSERT INTO chat_messages (user_id, farm_id, message, reply, created_at) VALUES (?, ?, ?, ?, ?)",
                (session["user_id"], farm["id"], message, reply, datetime.utcnow().isoformat()),
            )

        return jsonify({
            "success": True,
            "reply": reply
        })

    except Exception as e:

        print("CHAT ERROR:", e)

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# -----------------------------
# RUN
# -----------------------------
@app.route("/api/weather")
@login_required
def get_weather():

    location = request.args.get("location")
    lat_arg = request.args.get("lat")
    lon_arg = request.args.get("lon")
    farm = get_farm_for_user(request.args.get("farm_id"))
    if not farm:
        return jsonify({"success": False, "error": "Select a farm before loading weather data."}), 400

    if not location and not (lat_arg and lon_arg):
        return jsonify({
            "success": False,
            "error": "Location or coordinates are required"
        }), 400

    try:

        if lat_arg and lon_arg:
            try:
                latitude = float(lat_arg)
                longitude = float(lon_arg)
            except ValueError:
                return jsonify({"success": False, "error": "Invalid coordinates"}), 400

            place_name = location.strip() if (location and location.strip() and location.strip().lower() != "current location") else ""
            country_name = ""

            # Reverse geocode to find friendly locality name
            if not place_name:
                try:
                    rev_res = requests.get(
                        "https://api.bigdatacloud.net/data/reverse-geocode-client",
                        params={"latitude": latitude, "longitude": longitude, "localityLanguage": "en"},
                        timeout=5
                    )
                    if rev_res.status_code == 200:
                        rev_data = rev_res.json()
                        place_name = rev_data.get("city") or rev_data.get("locality") or rev_data.get("principalSubdivision") or "Current Location"
                        country_name = rev_data.get("countryName", "")
                except Exception:
                    place_name = place_name or "Current Location"

            if not place_name:
                place_name = "Current Location"

            place = {
                "name": place_name,
                "country": country_name,
                "latitude": latitude,
                "longitude": longitude
            }
        else:
            # Step 1: Convert location name to coordinates
            geo_url = "https://geocoding-api.open-meteo.com/v1/search"

            geo_params = {
                "name": location,
                "count": 1,
                "language": "en",
                "format": "json"
            }

            geo_response = requests.get(
                geo_url,
                params=geo_params,
                timeout=10
            )

            geo_data = geo_response.json()

            if not geo_data.get("results"):
                return jsonify({
                    "success": False,
                    "error": "Location not found"
                }), 404

            place = geo_data["results"][0]

            latitude = place["latitude"]
            longitude = place["longitude"]

        # Step 2: Get weather
        weather_url = "https://api.open-meteo.com/v1/forecast"

        weather_params = {
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,relative_humidity_2m,precipitation,rain",
            "hourly": "precipitation_probability",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,rain_sum",
            "timezone": "auto",
            "forecast_days": 3
        }

        weather_response = requests.get(
            weather_url,
            params=weather_params,
            timeout=10
        )

        weather_data = weather_response.json()

        current = weather_data["current"]
        daily = weather_data["daily"]

        temperature = current["temperature_2m"]
        humidity = current["relative_humidity_2m"]
        rainfall = current["precipitation"]

        rain_probability = daily["precipitation_probability_max"][0]

        forecast = {
            "today": {
                "max": daily["temperature_2m_max"][0],
                "min": daily["temperature_2m_min"][0],
                "rain_probability": daily["precipitation_probability_max"][0],
                "rain": daily["rain_sum"][0]
            },
            "tomorrow": {
                "max": daily["temperature_2m_max"][1],
                "min": daily["temperature_2m_min"][1],
                "rain_probability": daily["precipitation_probability_max"][1],
                "rain": daily["rain_sum"][1]
            },
            "day3": {
                "max": daily["temperature_2m_max"][2],
                "min": daily["temperature_2m_min"][2],
                "rain_probability": daily["precipitation_probability_max"][2],
                "rain": daily["rain_sum"][2]
            }
        }

        with get_db() as connection:
            connection.execute(
                """INSERT INTO weather_records
                         (user_id, farm_id, location, country, temperature, humidity, rainfall,
                          rain_probability, forecast_json, created_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (session["user_id"], farm["id"] if farm else None, place["name"], place.get("country", ""),
                 temperature, humidity, rainfall, rain_probability,
                 json.dumps(forecast), datetime.utcnow().isoformat()),
            )

        return jsonify({

            "success": True,

            "location": place["name"],

            "country": place.get("country", ""),

            "temperature": temperature,

            "humidity": humidity,

            "rainfall": rainfall,

            "rain_probability": rain_probability,

            "forecast": forecast

        })

    except Exception as e:

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

if __name__ == "__main__":
    app.run(debug=True)