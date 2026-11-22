import csv
import io
import os
import re
import sqlite3
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask,
    Response,
    jsonify,
    request,
    send_from_directory,
    session,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "billing_records.db"))

# Owner credentials (can also be overridden via environment variables when hosting)
OWNER_MOBILE = os.environ.get("OWNER_MOBILE", "9876543210")
OWNER_PASSWORD = os.environ.get("OWNER_PASSWORD", "0123456789")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key-in-production")

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MOBILE_RE = re.compile(r"^\+?[0-9\s\-().]{7,20}$")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS billing_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact TEXT NOT NULL,
                address TEXT NOT NULL,
                action TEXT NOT NULL DEFAULT 'Log In',
                billed INTEGER NOT NULL DEFAULT 0,
                saved_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def is_valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match(value))


def is_valid_mobile(value: str) -> bool:
    if not MOBILE_RE.match(value):
        return False
    digits = re.sub(r"\D", "", value)
    return 7 <= len(digits) <= 15


def is_owner_credentials(contact: str, second_field: str) -> bool:
    cleaned_mobile = re.sub(r"\D", "", contact)
    return cleaned_mobile == OWNER_MOBILE and second_field == OWNER_PASSWORD


def row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "contact": row["contact"],
        "address": row["address"],
        "action": row["action"],
        "billed": bool(row["billed"]),
        "savedAt": row["saved_at"],
    }


def owner_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("is_owner"):
            return jsonify({"ok": False, "error": "Owner login required."}), 401
        return fn(*args, **kwargs)

    return wrapper


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/owner/status", methods=["GET"])
def owner_status():
    return jsonify({"ok": True, "isOwner": bool(session.get("is_owner"))})


@app.route("/api/owner/logout", methods=["POST"])
def owner_logout():
    session.pop("is_owner", None)
    return jsonify({"ok": True})


@app.route("/api/submit", methods=["POST"])
def submit_entry():
    data = request.get_json(silent=True) or {}
    contact = str(data.get("contact", "")).strip()
    address = str(data.get("address", "")).strip()
    mode = str(data.get("mode", "login")).strip().lower()

    if not contact or not address:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Invalid mobile no , email or password.",
                }
            ),
            400,
        )

    # Check owner credentials first so owner login is never saved as a customer record
    if is_owner_credentials(contact, address):
        session["is_owner"] = True
        return jsonify({"ok": True, "role": "owner"})

    if not is_valid_email(contact) and not is_valid_mobile(contact):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Enter a valid mobile number or email address.",
                }
            ),
            400,
        )

    if len(contact) > 254 or len(address) > 255:
        return (
            jsonify({"ok": False, "error": "Input exceeds maximum allowed length."}),
            400,
        )

    action_label = "Create Account" if mode == "register" else "Log In"
    saved_at = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO billing_records (contact, address, action, billed, saved_at)
            VALUES (?, ?, ?, 0, ?)
            """,
            (contact, address, action_label, saved_at),
        )
        record_id = cursor.lastrowid
        conn.commit()
        row = conn.execute(
            "SELECT * FROM billing_records WHERE id = ?", (record_id,)
        ).fetchone()

    return jsonify({"ok": True, "role": "customer", "record": row_to_dict(row)}), 201


@app.route("/api/records", methods=["GET"])
@owner_required
def list_records():
    query = request.args.get("q", "").strip().lower()

    with get_db() as conn:
        all_rows = conn.execute(
            "SELECT * FROM billing_records ORDER BY id DESC"
        ).fetchall()

    all_records = [row_to_dict(r) for r in all_rows]
    total = len(all_records)
    billed = sum(1 for r in all_records if r["billed"])
    pending = total - billed

    if query:
        filtered = [
            r
            for r in all_records
            if query in r["contact"].lower() or query in r["address"].lower()
        ]
    else:
        filtered = all_records

    return jsonify(
        {
            "ok": True,
            "stats": {"total": total, "pending": pending, "billed": billed},
            "records": filtered,
        }
    )


@app.route("/api/records/<int:record_id>", methods=["PATCH"])
@owner_required
def toggle_record_billed(record_id: int):
    data = request.get_json(silent=True) or {}

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM billing_records WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            return jsonify({"ok": False, "error": "Record not found."}), 404

        if "billed" in data:
            new_billed = 1 if bool(data["billed"]) else 0
        else:
            new_billed = 0 if row["billed"] else 1

        conn.execute(
            "UPDATE billing_records SET billed = ? WHERE id = ?",
            (new_billed, record_id),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM billing_records WHERE id = ?", (record_id,)
        ).fetchone()

    return jsonify({"ok": True, "record": row_to_dict(updated)})


@app.route("/api/records/<int:record_id>", methods=["DELETE"])
@owner_required
def delete_record(record_id: int):
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM billing_records WHERE id = ?", (record_id,))
        conn.commit()
        if cursor.rowcount == 0:
            return jsonify({"ok": False, "error": "Record not found."}), 404

    return jsonify({"ok": True})


@app.route("/api/records", methods=["DELETE"])
@owner_required
def clear_records():
    with get_db() as conn:
        conn.execute("DELETE FROM billing_records")
        conn.commit()

    return jsonify({"ok": True})


@app.route("/api/records/export.csv", methods=["GET"])
@owner_required
def export_csv():
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM billing_records ORDER BY id DESC"
        ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Contact", "Address", "Type", "Status", "Saved At (UTC)"])
    for row in rows:
        writer.writerow(
            [
                row["id"],
                row["contact"],
                row["address"],
                row["action"],
                "Billed" if row["billed"] else "Pending",
                row["saved_at"],
            ]
        )

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=messagefulbook-billing-records.csv"
        },
    )


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
