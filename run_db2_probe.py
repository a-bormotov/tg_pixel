import os
import csv
import sys
import psycopg2

try:
    from sshtunnel import SSHTunnelForwarder
except Exception:
    SSHTunnelForwarder = None  # поставится в workflow

SQL_FILE = "db2_probe.sql"
OUT_CSV = "raw_data.csv"

TRUES = {"1", "true", "yes", "on", "y"}

def env_or(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v not in (None, "") else default

def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default

def want_ssh(flag_name: str) -> bool:
    v = os.getenv(flag_name)
    return (v or "").strip().lower() in TRUES

def run_query_via_conn(conn, sql: str):
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    print(f"Wrote {OUT_CSV} with {len(rows)} rows")

def main():
    # --- DB2 (на той стороне, куда втыкается SSH) ---
    db_host = env_or("DB2_HOST")
    db_port = env_int("DB2_PORT", 5432)
    db_name = env_or("DB2_NAME")
    db_user = env_or("DB2_USER")
    db_pass = env_or("DB2_PASSWORD")

    missing = [k for k, v in {
        "DB2_HOST": db_host, "DB2_NAME": db_name, "DB2_USER": db_user, "DB2_PASSWORD": db_pass
    }.items() if not v]
    if missing:
        print(f"[config error] Missing required DB2 env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)

    with open(SQL_FILE, "r", encoding="utf-8") as f:
        sql = f.read()

    if want_ssh("USE_SSH_DB2"):
        if SSHTunnelForwarder is None:
            print("[config error] sshtunnel is not installed", file=sys.stderr)
            sys.exit(2)

        # Используем профиль SSH2_* (по твоим секретам)
        ssh_host = env_or("SSH2_HOST")
        ssh_port = env_int("SSH2_PORT", 22)
        ssh_user = env_or("SSH2_USER")
        ssh_key_path = env_or("SSH2_KEY_PATH", os.path.expanduser("~/.ssh/id_rsa"))
        ssh_key_password = env_or("SSH2_KEY_PASSWORD")

        miss_ssh = [k for k, v in {
            "SSH2_HOST": ssh_host, "SSH2_USER": ssh_user
        }.items() if not v]
        if miss_ssh:
            print(f"[config error] Missing SSH2 vars: {', '.join(miss_ssh)}", file=sys.stderr)
            sys.exit(2)
        if not os.path.exists(ssh_key_path) or os.path.getsize(ssh_key_path) == 0:
            print(f"[config error] SSH key missing or empty at {ssh_key_path}", file=sys.stderr)
            sys.exit(2)

        tunnel = SSHTunnelForwarder(
            (ssh_host, ssh_port),
            ssh_username=ssh_user,
            ssh_pkey=ssh_key_path,
            ssh_private_key_password=ssh_key_password,
            remote_bind_address=(db_host, db_port)
        )
        tunnel.start()
        try:
            conn = psycopg2.connect(
                host="127.0.0.1",
                port=tunnel.local_bind_port,
                dbname=db_name,
                user=db_user,
                password=db_pass,
                application_name="gh-actions-db2-probe-ssh2"
            )
            try:
                run_query_via_conn(conn, sql)
            finally:
                conn.close()
        finally:
            tunnel.stop()
    else:
        # Прямое подключение (если USE_SSH_DB2 не выставлен)
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            dbname=db_name,
            user=db_user,
            password=db_pass,
            application_name="gh-actions-db2-probe-direct"
        )
        try:
            run_query_via_conn(conn, sql)
        finally:
            conn.close()

if __name__ == "__main__":
    main()
