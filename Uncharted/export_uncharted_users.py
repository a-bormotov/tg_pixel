# Uncharted/export_uncharted_users.py

import os, sys, csv, tempfile, json
import psycopg2
from sshtunnel import SSHTunnelForwarder
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ---------- helpers ----------
def need(name: str) -> str:
    v = os.getenv(name, "")
    if not v.strip():
        print(f"[config error] Missing env: {name}", file=sys.stderr)
        sys.exit(2)
    return v

def as_int(name: str, default: int) -> int:
    v = os.getenv(name, "").strip()
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        print(f"[config error] Env {name} must be integer, got: {v!r}", file=sys.stderr)
        sys.exit(2)

def clean_password(v: str) -> str:
    v = v.replace("\r", "").replace("\n", "")
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    return v

def write_key(key_text: str) -> str:
    fd, path = tempfile.mkstemp(prefix="id_", suffix=".key")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key_text)
    os.chmod(path, 0o600)
    return path

def run_sql_via_ssh(db_host, db_port, db_name, db_user, db_pass,
                    ssh_host, ssh_port, ssh_user, ssh_key_path, ssh_key_password,
                    sql_text):
    tunnel = SSHTunnelForwarder(
        (ssh_host, ssh_port),
        ssh_username=ssh_user,
        ssh_pkey=ssh_key_path,
        ssh_private_key_password=ssh_key_password,
        remote_bind_address=(db_host, db_port),
    )
    tunnel.start()
    try:
        conn = psycopg2.connect(
            host="127.0.0.1",
            port=tunnel.local_bind_port,
            dbname=db_name,
            user=db_user,
            password=db_pass,
            application_name="uncharted-users-export",
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql_text)
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
            return cols, rows
        finally:
            conn.close()
    finally:
        tunnel.stop()

# ---- Google Sheets ----

def get_sheets_service():
    sa_json = need("GOOGLE_SERVICE_ACCOUNT_JSON")
    sa_info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    service = build("sheets", "v4", credentials=creds)
    return service

def upload_to_sheets(headers, rows):
    spreadsheet_id = need("GOOGLE_SHEETS_UNCHARTED_ID")
    sheet_range = os.getenv("GOOGLE_SHEETS_UNCHARTED_RANGE", "Sheet1!A1")

    values = [headers]
    for row in rows:
        processed = []
        for v in row:
            if v is None:
                processed.append("")
            else:
                processed.append(str(v))
        values.append(processed)

    body = {"values": values}
    service = get_sheets_service()

    # clear old data
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=sheet_range,
    ).execute()

    # write new data
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=sheet_range,
        valueInputOption="RAW",
        body=body,
    ).execute()

    print(f"[sheets] Uploaded {len(rows)} rows to {spreadsheet_id} at {sheet_range}")

# ---------- main ----------
def main():
    base_dir = os.path.dirname(__file__)

    # ---- SSH ----
    ssh_host = need("SSH_HOST")
    ssh_port = as_int("SSH_PORT", 22)
    ssh_user = need("SSH_USER")
    ssh_key  = need("SSH_KEY")
    ssh_key_password = os.getenv("SSH_KEY_PASSWORD")
    key_path = write_key(ssh_key)

    # ---- DB1 (используем как в твоём run_query.py) ----
    db1_host = need("DB1_HOST")
    db1_port = as_int("DB1_PORT", 5432)
    db1_name = need("DB1_NAME")
    db1_user = need("DB1_USER")
    db1_pass = clean_password(need("DB1_PASS"))

    # ---- SQL file ----
    sql_path = os.environ.get("SQL_FILE") or os.path.join(base_dir, "data.sql")
    if not os.path.exists(sql_path):
        print(f"[config error] Missing SQL file: {sql_path}", file=sys.stderr)
        sys.exit(2)

    with open(sql_path, "r", encoding="utf-8") as f:
        sql_text = f.read()

    print("[1] Query DB1 for Uncharted users...")
    cols, rows = run_sql_via_ssh(
        db1_host, db1_port, db1_name, db1_user, db1_pass,
        ssh_host, ssh_port, ssh_user, key_path, ssh_key_password,
        sql_text
    )
    print(f"[1] Got {len(rows)} rows")

    # ---- локальный CSV в папке Uncharted ----
    out_csv = os.path.join(base_dir, "UnchartedUsers.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    print(f"[2] Wrote CSV: {out_csv}")

    # ---- загрузка в Google Sheets ----
    if rows:
        upload_to_sheets(cols, rows)
    else:
        print("[sheets] No rows, skip upload")

if __name__ == "__main__":
    main()
