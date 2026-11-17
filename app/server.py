"""FilamentDB: small self-hosted filament inventory.

Standard library only (no pip install needed), so it runs on the plain
python image. Data lives in a single SQLite file in DATA_DIR.

Environment variables:
  DATA_DIR        where filaments.db is stored (default /data)
  PORT            listen port (default 8080)
  APP_PASSWORD    optional; enables HTTP Basic auth (any username)
  TLS_CERT/TLS_KEY optional; serve HTTPS directly instead of via a proxy
  BAMBU_PRINTER   printer preset name for Bambu Studio export
                  (default "Bambu Lab P1S 0.4 nozzle")
  ANTHROPIC_API_KEY  optional; fills in new barcodes with Claude + web search
                  (see lookup.py for ANTHROPIC_MODEL, LOOKUP_LIMIT_PER_HOUR)
"""

import base64
import csv
import io
import json
import os
import re
import secrets
import sqlite3
import ssl
import uuid
import zipfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import lookup

APP_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "filaments.db"
PORT = int(os.environ.get("PORT", "8080"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
BAMBU_PRINTER = os.environ.get("BAMBU_PRINTER", "Bambu Lab P1S 0.4 nozzle")

# Editable columns. id, uuid, bambu_id and timestamps are managed by the server.
FIELDS = [
    "barcode", "brand", "material", "color_name", "color_hex", "finish",
    "td", "quantity", "weight_g", "nozzle_min", "nozzle_max",
    "bed_min", "bed_max", "location", "added_by", "notes",
]
NUMERIC = {"td": float, "quantity": int, "weight_g": int, "nozzle_min": int,
           "nozzle_max": int, "bed_min": int, "bed_max": int}

# Bambu Studio base presets that user presets inherit from.
BAMBU_BASE = {
    "PLA": "Generic PLA @base",
    "PETG": "Generic PETG @base",
    "TPU": "Generic TPU @base",
    "ABS": "Generic ABS @base",
    "ASA": "Generic ASA @base",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS filaments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    barcode     TEXT,
    brand       TEXT NOT NULL,
    material    TEXT NOT NULL,
    color_name  TEXT NOT NULL,
    color_hex   TEXT NOT NULL DEFAULT '#808080',
    finish      TEXT NOT NULL DEFAULT 'standard',
    td          REAL,
    quantity    INTEGER NOT NULL DEFAULT 1,
    weight_g    INTEGER,
    nozzle_min  INTEGER,
    nozzle_max  INTEGER,
    bed_min     INTEGER,
    bed_max     INTEGER,
    location    TEXT,
    added_by    TEXT,
    notes       TEXT,
    uuid        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_barcode ON filaments(barcode);
"""


# ---------- German colour names ----------
# Manufacturer colour names stay as printed on the spool; the German name is
# derived for display ("Dark Green / Dunkelgrün"). Unknown words are kept.
COLOR_WORDS_DE = {
    "black": "schwarz", "white": "weiss", "grey": "grau", "gray": "grau",
    "red": "rot", "green": "grün", "blue": "blau", "yellow": "gelb",
    "orange": "orange", "purple": "violett", "violet": "violett", "pink": "rosa",
    "brown": "braun", "beige": "beige", "silver": "silber", "gold": "gold",
    "bronze": "bronze", "copper": "kupfer", "transparent": "transparent",
    "clear": "klar", "natural": "natur", "cyan": "cyan", "magenta": "magenta",
    "teal": "petrol", "turquoise": "türkis", "navy": "marineblau", "olive": "oliv",
    "cream": "creme", "ivory": "elfenbein", "charcoal": "anthrazit",
    "matte": "matt", "silk": "seide", "glossy": "glänzend", "metallic": "metallic",
    "multicolor": "mehrfarbig", "multicolour": "mehrfarbig", "gradient": "verlauf",
    "rainbow": "regenbogen", "unknown": "unbekannt", "color": "farbe", "colour": "farbe",
}
# Joined with the following colour into one German word: "Dark Green" -> "Dunkelgrün"
COLOR_PREFIXES_DE = {"dark": "dunkel", "light": "hell", "pale": "blass",
                     "deep": "tief", "sky": "himmel", "lime": "limetten"}


# Whole names where word-by-word would read badly
COLOR_PHRASES_DE = {"unknown color": "Unbekannte Farbe", "unknown colour": "Unbekannte Farbe",
                    "multicolor gradient": "Farbverlauf", "matte white": "Mattweiss",
                    "matte black": "Mattschwarz"}


def color_name_de(name):
    if name.lower() in COLOR_PHRASES_DE:
        return COLOR_PHRASES_DE[name.lower()]
    words = name.split()
    out, i = [], 0
    while i < len(words):
        word = words[i].lower()
        nxt = words[i + 1].lower() if i + 1 < len(words) else None
        if word in COLOR_PREFIXES_DE and nxt in COLOR_WORDS_DE:
            out.append((COLOR_PREFIXES_DE[word] + COLOR_WORDS_DE[nxt]).capitalize())
            i += 2
            continue
        out.append(COLOR_WORDS_DE[word].capitalize() if word in COLOR_WORDS_DE else words[i])
        i += 1
    german = " ".join(out)
    return None if german.lower() == name.lower() else german


def color_label(name):
    """Both languages when they differ: "Dark Green / Dunkelgrün"."""
    german = color_name_de(name)
    return f"{name} / {german}" if german else name


# ---------- database ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)
        empty = conn.execute("SELECT COUNT(*) FROM filaments").fetchone()[0] == 0
    seed_file = APP_DIR / "seed.json"
    if empty and seed_file.exists():
        for item in json.loads(seed_file.read_text(encoding="utf-8")):
            insert_filament(clean(item))
        print(f"Seeded database from {seed_file}")


def clean(data):
    """Keep known fields, convert numbers, normalise text. Raises ValueError."""
    out = {}
    for key in FIELDS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, str):
            value = value.strip()
        if value in ("", None):
            out[key] = None
        elif key in NUMERIC:
            out[key] = NUMERIC[key](value)
        else:
            out[key] = str(value)
    if out.get("material"):
        out["material"] = out["material"].upper()
    if out.get("color_hex") and not re.fullmatch(r"#[0-9a-fA-F]{6}", out["color_hex"]):
        raise ValueError("Farbe muss das Format #RRGGBB haben")
    return out


def insert_filament(data):
    for required in ("brand", "material", "color_name"):
        if not data.get(required):
            raise ValueError(f"{required} ist ein Pflichtfeld")
    data = {k: v for k, v in data.items() if v is not None}
    data.update(uuid="{%s}" % uuid.uuid4(), created_at=now(), updated_at=now())
    cols = ", ".join(data)
    marks = ", ".join("?" * len(data))
    with db() as conn:
        cur = conn.execute(f"INSERT INTO filaments ({cols}) VALUES ({marks})",
                           list(data.values()))
        return cur.lastrowid


def update_filament(fid, data):
    if not data:
        return
    data["updated_at"] = now()
    sets = ", ".join(f"{k} = ?" for k in data)
    with db() as conn:
        conn.execute(f"UPDATE filaments SET {sets} WHERE id = ?", [*data.values(), fid])


def all_filaments():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM filaments ORDER BY material, brand, color_name").fetchall()
    return [with_label(dict(r)) for r in rows]


def with_label(item):
    item["color_label"] = color_label(item["color_name"])
    return item


def get_filament(fid):
    with db() as conn:
        row = conn.execute("SELECT * FROM filaments WHERE id = ?", (fid,)).fetchone()
    return with_label(dict(row)) if row else None


def find_by_barcode(code):
    """Exact match, plus a brand/material hint from entries with the same
    7-digit GS1 company prefix (same manufacturer)."""
    with db() as conn:
        row = conn.execute("SELECT * FROM filaments WHERE barcode = ?", (code,)).fetchone()
        hint = None
        if not row and len(code) >= 8 and code.isdigit():
            similar = conn.execute(
                "SELECT brand, material, nozzle_min, nozzle_max, bed_min, bed_max "
                "FROM filaments WHERE barcode LIKE ? ORDER BY updated_at DESC LIMIT 1",
                (code[:7] + "%",)).fetchone()
            hint = dict(similar) if similar else None
    return (with_label(dict(row)) if row else None), hint


# ---------- exports ----------

def export_hueforge():
    """HueForge filament library format: {"Filaments": [...]}.
    Entries without a TD value are skipped because HueForge needs it."""
    items = []
    for f in all_filaments():
        if f["td"] is None or f["quantity"] < 1:
            continue
        items.append({
            "Brand": f["brand"],
            "Color": f["color_hex"].lower(),
            "Name": f["color_label"],
            "Owned": True,
            "Transmissivity": f["td"],
            "Type": f["material"],
            "uuid": f["uuid"],
        })
    return json.dumps({"Filaments": items}, indent=4, ensure_ascii=False)


def bambu_preset(brand, material, sample):
    """One Bambu Studio user preset per brand + material.
    Colours are chosen per AMS slot, so they don't need separate presets."""
    name = f"{brand} {material} @{BAMBU_PRINTER}"
    # Deterministic id ("P" + 7 hex) so re-imports update instead of duplicating.
    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"{brand}|{material}").hex
    preset = {
        "type": "filament",
        "from": "User",
        "instantiation": "true",
        "name": name,
        "inherits": BAMBU_BASE.get(material, "Generic PLA @base"),
        "filament_settings_id": [name],
        "filament_id": "P" + digest[:7],
        "filament_vendor": [brand],
        "filament_type": [material],
        "default_filament_colour": [sample["color_hex"]],
        "compatible_printers": [BAMBU_PRINTER],
    }
    if sample["nozzle_min"] and sample["nozzle_max"]:
        nozzle = str(round((sample["nozzle_min"] + sample["nozzle_max"]) / 2))
        preset.update(
            nozzle_temperature=[nozzle],
            nozzle_temperature_initial_layer=[nozzle],
            nozzle_temperature_range_low=[str(sample["nozzle_min"])],
            nozzle_temperature_range_high=[str(sample["nozzle_max"])],
        )
    if sample["bed_min"] and sample["bed_max"]:
        bed = str(round((sample["bed_min"] + sample["bed_max"]) / 2))
        for key in ("hot_plate_temp", "hot_plate_temp_initial_layer",
                    "textured_plate_temp", "textured_plate_temp_initial_layer"):
            preset[key] = [bed]
    return name, preset


def export_bambu_zip():
    groups = {}
    for f in all_filaments():
        groups.setdefault((f["brand"], f["material"]), f)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for (brand, material), sample in groups.items():
            name, preset = bambu_preset(brand, material, sample)
            safe = re.sub(r"[^\w@. -]", "_", name)
            zf.writestr(f"{safe}.json", json.dumps(preset, indent=4))
    return buf.getvalue()


def export_csv():
    rows = all_filaments()
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return buf.getvalue()


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "FilamentDB/1.0"

    # --- helpers ---
    def send(self, status, body=b"", ctype="application/json", filename=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, status=HTTPStatus.OK):
        self.send(status, json.dumps(obj))

    def error(self, status, message):
        self.send_json({"error": message}, status)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def authorized(self):
        if not APP_PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                if secrets.compare_digest(pw, APP_PASSWORD):
                    return True
            except ValueError:
                pass
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="FilamentDB"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def route_id(self, prefix):
        match = re.fullmatch(prefix + r"(\d+)", self.path)
        return int(match.group(1)) if match else None

    # --- routes ---
    def do_GET(self):
        if not self.authorized():
            return
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self.send(200, (APP_DIR / "static/index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/filaments":
            self.send_json(all_filaments())
        elif path.startswith("/api/barcode/"):
            code = path.removeprefix("/api/barcode/")
            match, hint = find_by_barcode(code)
            self.send_json({"match": match, "hint": hint})
        elif path.startswith("/api/lookup/"):
            code = path.removeprefix("/api/lookup/")
            if not re.fullmatch(r"\d{8,14}", code):
                return self.error(HTTPStatus.BAD_REQUEST, "ungültiger Barcode")
            self.send_json(lookup.lookup(code))
        elif path == "/export/hueforge.json":
            self.send(200, export_hueforge(), filename="FilamentDB_HueForge.json")
        elif path == "/export/bambu.zip":
            self.send(200, export_bambu_zip(), "application/zip", "FilamentDB_BambuStudio.zip")
        elif path == "/export/backup.csv":
            self.send(200, export_csv(), "text/csv; charset=utf-8", "FilamentDB_backup.csv")
        else:
            self.error(HTTPStatus.NOT_FOUND, "nicht gefunden")

    def do_POST(self):
        if not self.authorized():
            return
        if self.path != "/api/filaments":
            return self.error(HTTPStatus.NOT_FOUND, "nicht gefunden")
        try:
            new_id = insert_filament(clean(self.read_json()))
        except (ValueError, json.JSONDecodeError) as exc:
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        self.send_json(get_filament(new_id), HTTPStatus.CREATED)

    def do_PUT(self):
        if not self.authorized():
            return
        fid = self.route_id("/api/filaments/")
        if fid is None or not get_filament(fid):
            return self.error(HTTPStatus.NOT_FOUND, "nicht gefunden")
        try:
            update_filament(fid, clean(self.read_json()))
        except (ValueError, json.JSONDecodeError) as exc:
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        self.send_json(get_filament(fid))

    def do_DELETE(self):
        if not self.authorized():
            return
        fid = self.route_id("/api/filaments/")
        if fid is None:
            return self.error(HTTPStatus.NOT_FOUND, "nicht gefunden")
        with db() as conn:
            conn.execute("DELETE FROM filaments WHERE id = ?", (fid,))
        self.send_json({"deleted": fid})

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}")


def main():
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    cert, key = os.environ.get("TLS_CERT"), os.environ.get("TLS_KEY")
    scheme = "http"
    if cert and key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    print(f"FilamentDB on {scheme}://0.0.0.0:{PORT}, data in {DB_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
