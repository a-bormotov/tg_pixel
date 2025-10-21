import os, sys, csv, tempfile, io
import psycopg2
from sshtunnel import SSHTunnelForwarder

SQL_FILE = os.environ.get("SQL_FILE", "data.sql")
USER_SQL_FILE = os.environ.get("USER_SQL_FILE", "user_data.sql")
RAW_CSV = os.environ.get("OUTPUT_CSV", "raw_data.csv")
RESULT_CSV = "result_data.csv"
BLACKLIST_FILE = "black_list.csv"

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

def run_sql_via_ssh(db_host, db_port, db_name, db_user, db_pass, ssh_host, ssh_port, ssh_user, ssh_key_path, ssh_key_password, sql_text):
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
            application_name="sleepagotchi-run-query",
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

# ---------- main ----------
def main():
    # ---- SSH ----
    ssh_host = need("SSH_HOST")
    ssh_port = as_int("SSH_PORT", 22)
    ssh_user = need("SSH_USER")
    ssh_key = need("SSH_KEY")
    ssh_key_password = os.getenv("SSH_KEY_PASSWORD")
    key_path = write_key(ssh_key)

    # ---- DB2 (events) ----
    db2_host = need("DB2_HOST")
    db2_port = as_int("DB2_PORT", 5432)
    db2_name = need("DB2_NAME")
    db2_user = need("DB2_USER")
    db2_pass = clean_password(need("DB2_PASS"))

    # ---- DB1 (main) ----
    db1_host = need("DB1_HOST")
    db1_port = as_int("DB1_PORT", 5432)
    db1_name = need("DB1_NAME")
    db1_user = need("DB1_USER")
    db1_pass = clean_password(need("DB1_PASS"))

    # ---- Load SQL ----
    for file in [SQL_FILE, USER_SQL_FILE]:
        if not os.path.exists(file):
            print(f"[config error] Missing SQL file: {file}", file=sys.stderr)
            sys.exit(2)

    with open(SQL_FILE, "r", encoding="utf-8") as f:
        sql_data = f.read()
    with open(USER_SQL_FILE, "r", encoding="utf-8") as f:
        sql_user = f.read()

    # ---------- Step 1: run data.sql on DB2 ----------
    print("[1] Query DB2 (events)...")
    cols2, rows2 = run_sql_via_ssh(
        db2_host, db2_port, db2_name, db2_user, db2_pass,
        ssh_host, ssh_port, ssh_user, key_path, ssh_key_password,
        sql_data
    )

    with open(RAW_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(cols2); w.writerows(rows2)
    print(f"[1] wrote {RAW_CSV}: {len(rows2)} rows")

    # ---------- Step 2: get userIds ----------
    try:
        uid_idx = [c.lower() for c in cols2].index("userid")
    except ValueError:
        print("[error] Column 'userId' not found in result.", file=sys.stderr)
        sys.exit(2)
    user_ids = [str(r[uid_idx]) for r in rows2]

    # ---------- Step 3: blacklist ----------
    blacklist = set()
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
            rdr = csv.DictReader(f)
            for row in rdr:
                v = (row.get("userid") or "").strip()
                if v: blacklist.add(v)
    filtered_ids = [uid for uid in user_ids if uid not in blacklist]
    print(f"[2] blacklist filtered: {len(user_ids)} -> {len(filtered_ids)}")

    if not filtered_ids:
        print("[2] no players left after blacklist, stopping")
        return

    # ---------- Step 4: build user_data.sql query ----------
    # user_data.sql может содержать запрос вроде:
    # SELECT id::text AS userId,
    #   CASE WHEN username IS NULL OR username='' OR username='Secret Dino'
    #        THEN id::text ELSE username END AS username
    # FROM users WHERE id::text IN ({IDS})
    id_list = ",".join(f"'{i}'" for i in filtered_ids)
    sql_user_final = sql_user.replace("{IDS}", id_list)

    # ---------- Step 5: run user_data.sql on DB1 ----------
    print("[3] Query DB1 (usernames)...")
    cols1, rows1 = run_sql_via_ssh(
        db1_host, db1_port, db1_name, db1_user, db1_pass,
        ssh_host, ssh_port, ssh_user, key_path, ssh_key_password,
        sql_user_final
    )
    id_to_name = {str(r[0]): r[1] for r in rows1}

    # ---------- Step 6: build result_data.csv ----------
    def idx(name):
        try: return [c.lower() for c in cols2].index(name)
        except ValueError: return None
    idx_score = idx("score")
    idx_green = idx("green")
    idx_cards = idx("heroesmatched")

    records = []
    for r in rows2:
        uid = str(r[uid_idx])
        if uid in blacklist: continue
        username = id_to_name.get(uid, uid)
        score = float(r[idx_score]) if idx_score is not None and r[idx_score] is not None else 0.0
        green = int(r[idx_green]) if idx_green is not None and r[idx_green] is not None else 0
        cards = int(r[idx_cards]) if idx_cards is not None and r[idx_cards] is not None else 0
        records.append((username, score, green, cards, uid))

    records.sort(key=lambda x: (x[1], x[2], x[3]), reverse=True)

    with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Rank","Username","Score","Green","Cards","NFT","PIXEL","USD (ref.)"])
        for i, (username, score, green, cards, uid) in enumerate(records, start=1):
            w.writerow([i, username, score, green, cards, "-", "-", "-"])

    print(f"[4] wrote {RESULT_CSV}: {len(records)} rows (sorted by Score desc)")

if __name__ == "__main__":
    main()
