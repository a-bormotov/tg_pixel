import os, sys, csv, tempfile
import psycopg2
from sshtunnel import SSHTunnelForwarder

SQL_FILE = os.environ.get("SQL_FILE", "data.sql")
OUT_CSV  = os.environ.get("OUTPUT_CSV", "raw_data.csv")

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
    # убираем случайные переводы строк/кавычки из секрета
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

def main():
    # ---- SSH ----
    ssh_host = need("SSH_HOST")          # dbproxy.sleepagotchi.com
    ssh_port = as_int("SSH_PORT", 22)
    ssh_user = need("SSH_USER")          # ec2-user
    ssh_key  = need("SSH_KEY")           # весь приватный ключ (текст)
    ssh_key_password = os.getenv("SSH_KEY_PASSWORD")  # если ключ с паролем

    # ---- DB2 (как видит её SSH-хост) ----
    db_host = need("DB2_HOST")           # 10.10.0.29
    db_port = as_int("DB2_PORT", 5432)   # 5432
    db_name = need("DB2_NAME")           # lite
    db_user = need("DB2_USER")           # ro_user
    db_pass = clean_password(need("DB2_PASS"))

    # ---- SQL ----
    if not os.path.exists(SQL_FILE):
        print(f"[config error] {SQL_FILE} not found in repo root", file=sys.stderr)
        sys.exit(2)
    with open(SQL_FILE, "r", encoding="utf-8") as f:
        sql = f.read()

    key_path = write_key(ssh_key)

    # 1) SSH-туннель
    print(f"[ssh] {ssh_user}@{ssh_host}:{ssh_port} -> {db_host}:{db_port}")
    tunnel = SSHTunnelForwarder(
        (ssh_host, ssh_port),
        ssh_username=ssh_user,
        ssh_pkey=key_path,
        ssh_private_key_password=ssh_key_password,
        remote_bind_address=(db_host, db_port),
    )
    tunnel.start()
    print(f"[ssh] tunnel up on local_port={tunnel.local_bind_port}")

    try:
        # 2) Подключение к Postgres через туннель
        print(f"[db] connect 127.0.0.1:{tunnel.local_bind_port} db={db_name} user={db_user}")
        conn = psycopg2.connect(
            host="127.0.0.1",
            port=tunnel.local_bind_port,
            dbname=db_name,
            user=db_user,
            password=db_pass,
            application_name="run-query-db2-ssh",
        )
        try:
            # 2a) проверка соединения
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                print(f"[db] SELECT 1 -> {cur.fetchone()[0]} (ok)")

            # 3) выполняем data.sql и сохраняем CSV
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]

            with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(cols)
                w.writerows(rows)

            print(f"[ok] wrote {OUT_CSV} ({len(rows)} rows)")
        finally:
            conn.close()
    finally:
        tunnel.stop()

if __name__ == "__main__":
    main()
