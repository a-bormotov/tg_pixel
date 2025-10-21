import os, sys, csv, tempfile
import psycopg2

try:
    from sshtunnel import SSHTunnelForwarder
except Exception:
    SSHTunnelForwarder = None

SQL_FILE = os.environ.get("SQL_FILE", "db2_probe.sql")
OUT_CSV  = os.environ.get("OUTPUT_CSV", "raw_data.csv")
TRUES = {"1","true","yes","on","y"}

def b(v): return (v or "").strip()
def t(v): return b(v).lower() in TRUES
def i(v, d): 
    v = b(v)
    try: return int(v) if v else d
    except: return d
def strip_nl(v): return v.rstrip("\r\n") if v else v

def write_key_from_env(key_env) -> str | None:
    key = os.environ.get(key_env)
    if not b(key): return None
    # сохранить приватный ключ во временный файл
    fd, path = tempfile.mkstemp(prefix="id_", suffix=".key")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    os.chmod(path, 0o600)
    return path

def run_sql(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(cols); w.writerows(rows)
    print(f"[ok] wrote {OUT_CSV}: {len(rows)} rows")

def main():
    # DB2 creds (как у тебя в секретах)
    db_host = b(os.environ.get("DB2_HOST"))
    db_port = i(os.environ.get("DB2_PORT"), 5432)
    db_name = b(os.environ.get("DB2_NAME"))
    db_user = b(os.environ.get("DB2_USER"))
    db_pass = strip_nl(os.environ.get("DB2_PASSWORD"))

    missing = [k for k,v in {
        "DB2_HOST":db_host,"DB2_NAME":db_name,"DB2_USER":db_user,"DB2_PASSWORD":db_pass
    }.items() if not v]
    if missing:
        print(f"[config] Missing: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)

    with open(SQL_FILE, "r", encoding="utf-8") as f: sql = f.read()

    use_ssh = t(os.environ.get("USE_SSH_DB2"))
    print(f"[probe] use_ssh={use_ssh} db={db_user}@{db_host}:{db_port}/{db_name}")

    if use_ssh:
        if SSHTunnelForwarder is None:
            print("[config] sshtunnel not installed", file=sys.stderr); sys.exit(2)
        ssh_host = b(os.environ.get("SSH2_HOST"))
        ssh_port = i(os.environ.get("SSH2_PORT"), 22)
        ssh_user = b(os.environ.get("SSH2_USER"))
        # ключ из ENV (как в твоих секретах) -> во временный файл:
        ssh_key_path = write_key_from_env("SSH2_PRIVATE_KEY")
        if not all([ssh_host, ssh_user, ssh_key_path]):
            print("[config] SSH2_HOST/SSH2_USER/SSH2_PRIVATE_KEY required", file=sys.stderr)
            sys.exit(2)
        ssh_pass = os.environ.get("SSH2_KEY_PASSWORD")  # если нужен

        tunnel = SSHTunnelForwarder(
            (ssh_host, ssh_port),
            ssh_username=ssh_user,
            ssh_pkey=ssh_key_path,
            ssh_private_key_password=ssh_pass,
            remote_bind_address=(db_host, db_port),
        )
        tunnel.start()
        print(f"[probe] ssh up on local_port={tunnel.local_bind_port}")
        try:
            conn = psycopg2.connect(
                host="127.0.0.1",
                port=tunnel.local_bind_port,
                dbname=db_name,
                user=db_user,
                password=db_pass,
                application_name="db2-probe-ssh",
            )
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;"); cur.fetchone()
                run_sql(conn, sql)
            finally:
                conn.close()
        finally:
            tunnel.stop()
    else:
        conn = psycopg2.connect(
            host=db_host, port=db_port, dbname=db_name, user=db_user, password=db_pass,
            application_name="db2-probe-direct",
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;"); cur.fetchone()
            run_sql(conn, sql)
        finally:
            conn.close()

if __name__ == "__main__":
    main()
