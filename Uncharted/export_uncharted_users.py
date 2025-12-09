# Uncharted/export_uncharted_users.py

import os
import json
import csv
from contextlib import closing
from sshtunnel import SSHTunnelForwarder
import psycopg2
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ---- утилиты окружения ----

def need(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value

def as_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value is not None else default

def clean_password(p: str) -> str:
    # на случай, если пароль с \n и т.п.
    return p.replace("\\n", "\n") if p else p

def write_key(key_str: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "tmp_ssh_key")
    with open(path, "w", encoding="utf-8") as f:
        f.write(key_str)
    os.chmod(path, 0o600)
    return path

# ---- работа с БД ----

def fetch_data_from_db():
    # SSH
    ssh_host = need("SSH_HOST")
    ssh_port = as_int("SSH_PORT", 22)
    ssh_user = need("SSH_USER")
    ssh_key = need("SSH_KEY")
    ssh_key_password = os.getenv("SSH_KEY_PASSWORD")

    key_path = write_key(ssh_key)

    # DB1 (main)
    db1_host = need("DB1_HOST")
    db1_port = as_int("DB1_PORT", 5432)
    db1_name = need("DB1_NAME")
    db1_user = need("DB1_USER")
    db1_pass = clean_password(need("DB1_PASS"))

    sql_path = os.path.join(os.path.dirname(__file__), "data.sql")
    with open(sql_path, "r", encoding="utf-8") as f:
        query = f.read()

    with SSHTunnelForwarder(
        (ssh_host, ssh_port),
        ssh_username=ssh_user,
        ssh_pkey=key_path,
        ssh_private_key_password=ssh_key_password,
        remote_bind_address=(db1_host, db1_port),
        local_bind_address=("127.0.0.1", 0),
    ) as tunnel:
        local_port = tunnel.local_bind_port

        conn = psycopg2.connect(
            host="127.0.0.1",
            port=local_port,
            dbname=db1_name,
            user=db1_user,
            password=db1_pass,
        )
        with closing(conn), conn.cursor() as cur:
            cur.execute(query)
            colnames = [d[0] for d in cur.description]
            rows = cur.fetchall()

    return colnames, rows

# ---- CSV (локальный файл в репо) ----

def save_csv(headers, rows):
    out_path = os.path.join(os.path.dirname(__file__), "UnchartedUsers.csv")
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(row)
    print(f"Saved CSV to {out_path}")

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

    # Готовим данные: первая строка — заголовки
    values = [headers]
    for row in rows:
        # превращаем все значения в что-то сериализуемое
        processed = []
        for v in row:
            if v is None:
                processed.append("")
            else:
                processed.append(str(v))
        values.append(processed)

    body = {"values": values}

    service = get_sheets_service()

    # очищаем старые данные
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=sheet_range,
    ).execute()

    # записываем новые
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=sheet_range,
        valueInputOption="RAW",
        body=body,
    ).execute()

    print(f"Uploaded {len(rows)} rows to Google Sheets {spreadsheet_id} at {sheet_range}")

# ---- main ----

def main():
    headers, rows = fetch_data_from_db()
    save_csv(headers, rows)          # локальный CSV в папке Uncharted
    upload_to_sheets(headers, rows)  # заливка в Google Sheets

if __name__ == "__main__":
    main()
