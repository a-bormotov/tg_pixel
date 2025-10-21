import os, sys, csv, tempfile, io
import psycopg2
from sshtunnel import SSHTunnelForwarder

# ---- файлы ----
SQL_FILE       = os.environ.get("SQL_FILE", "data.sql")         # первый запрос (DB2)
USER_SQL_FILE  = os.environ.get("USER_SQL_FILE", "user_data.sql")# второй запрос (DB1)
RAW_CSV        = os.environ.get("OUTPUT_CSV", "raw_data.csv")
RESULT_CSV     = "result_data.csv"
BLACKLIST_FILE = "black_list.csv"  # столбец: userid

# ============== helpers ==============
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

def run_sql_via_ssh(db_host, db_port, db_name, db_user, db_pass,
                    ssh_host, ssh_port, ssh_user, ssh_key_path, ssh_key_password,
                    sql_text):
    """Выполнить SQL через SSH-туннель и вернуть (cols, rows)."""
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

def esc_sql_str(s: str) -> str:
    """Экранировать одинарные кавычки для включения строки в SQL."""
    return s.replace("'", "''")

# ============== main ==============
def main():
    # ---- SSH (общие) ----
    ssh_host = need("SSH_HOST")
    ssh_port = as_int("SSH_PORT", 22)
    ssh_user = need("SSH_USER")
    ssh_key  = need("SSH_KEY")
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

    # ---- SQL-файлы ----
    if not os.path.exists(SQL_FILE):
        print(f"[config error] Missing SQL file: {SQL_FILE}", file=sys.stderr); sys.exit(2)
    if not os.path.exists(USER_SQL_FILE):
        print(f"[config error] Missing SQL file: {USER_SQL_FILE}", file=sys.stderr); sys.exit(2)

    with open(SQL_FILE, "r", encoding="utf-8") as f:
        sql_data = f.read()
    with open(USER_SQL_FILE, "r", encoding="utf-8") as f:
        sql_user_template = f.read()

    # ===== 1) DB2: выполняем data.sql -> raw_data.csv =====
    print("[1] Query DB2 (events)...")
    cols2, rows2 = run_sql_via_ssh(
        db2_host, db2_port, db2_name, db2_user, db2_pass,
        ssh_host, ssh_port, ssh_user, key_path, ssh_key_password,
        sql_data
    )
    with open(RAW_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(cols2); w.writerows(rows2)
    print(f"[1] wrote {RAW_CSV}: {len(rows2)} rows")

    # извлечём userId из результата
    try:
        uid_idx = [c.lower() for c in cols2].index("userid")
    except ValueError:
        print("[error] Column 'userId' not found in data.sql result.", file=sys.stderr)
        sys.exit(2)
    user_ids = [str(r[uid_idx]) for r in rows2]

    # ===== 2) Чёрный список =====
    blacklist = set()
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
            rdr = csv.DictReader(f)
            hdrs = [h.strip().lower() for h in (rdr.fieldnames or [])]
            if "userid" in hdrs:
                for row in rdr:
                    v = (row.get("userid") or "").strip()
                    if v: blacklist.add(v)
            else:
                print("[warn] black_list.csv has no 'userid' header; skipping blacklist filtering")
    else:
        print("[info] black_list.csv not found; skipping blacklist filtering")

    filtered_ids = [uid for uid in user_ids if uid not in blacklist]
    print(f"[2] blacklist filtered: {len(user_ids)} -> {len(filtered_ids)}")

    if not filtered_ids:
        # пустой result_data.csv с заголовком
        with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Rank","Username","Score","Green","Cards","NFT","PIXEL","USD (ref.)"])
        print("[stop] no players left after blacklist; wrote empty result_data.csv")
        return

    # ===== 3) DB1: собираем usernames по user_data.sql =====
    # Поддерживаем два формата шаблона:
    #   A) ... WHERE id::text IN ({IDS})
    #   B) WITH ids(id, ord) AS ( VALUES %s ) ... ORDER BY ids.ord
    if "{IDS}" in sql_user_template:
        id_list = ",".join(f"'{esc_sql_str(i)}'" for i in filtered_ids)
        sql_user_final = sql_user_template.replace("{IDS}", id_list)
    elif "%s" in sql_user_template:
        values_rows = ",".join(f"('{esc_sql_str(uid)}',{ord_})" for ord_, uid in enumerate(filtered_ids, start=1))
        sql_user_final = sql_user_template % values_rows
    else:
        print("[config error] user_data.sql must contain either {IDS} or %s placeholder", file=sys.stderr)
        sys.exit(2)

    print("[3] Query DB1 (usernames with filters in user_data.sql)...")
    cols1, rows1 = run_sql_via_ssh(
        db1_host, db1_port, db1_name, db1_user, db1_pass,
        ssh_host, ssh_port, ssh_user, key_path, ssh_key_password,
        sql_user_final
    )
    # ожидаем колонки: userId, username, ord (ord опционален — для сортировки)
    id_to_name = {}
    ord_map = {}
    c_lower = [c.lower() for c in cols1]
    try:
        c_uid = c_lower.index("userid")
        c_unm = c_lower.index("username")
    except ValueError:
        print("[error] user_data.sql must return columns 'userId' and 'username'", file=sys.stderr)
        sys.exit(2)
    c_ord = c_lower.index("ord") if "ord" in c_lower else None

    for r in rows1:
        uid = str(r[c_uid])
        id_to_name[uid] = r[c_unm]
        if c_ord is not None:
            ord_map[uid] = r[c_ord]

    # ===== 4) Сборка result_data.csv =====
    def idx(name):
        try: return [c.lower() for c in cols2].index(name)
        except ValueError: return None
    idx_score = idx("score")
    idx_green = idx("green")
    idx_cards = idx("heroesmatched")  # из data.sql

    records = []
    for r in rows2:
        uid = str(r[uid_idx])
        if uid in blacklist:  # ещё раз на всякий случай
            continue
        username = id_to_name.get(uid, uid)
        score = float(r[idx_score]) if idx_score is not None and r[idx_score] is not None else 0.0
        green = int(r[idx_green]) if idx_green is not None and r[idx_green] is not None else 0
        cards = int(r[idx_cards]) if idx_cards is not None and r[idx_cards] is not None else 0
        order_key = ord_map.get(uid, 0)
        records.append((username, score, green, cards, uid, order_key))

    # сортируем по Score убыв., затем Green, затем Cards (и ord — если есть)
    records.sort(key=lambda x: (x[1], x[2], x[3], x[5]), reverse=True)

    with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Rank","Username","Score","Green","Cards","NFT","PIXEL","USD (ref.)"])
        for i, (username, score, green, cards, uid, _) in enumerate(records, start=1):
            w.writerow([i, username, score, green, cards, "-", "-", "-"])

    print(f"[4] wrote {RESULT_CSV}: {len(records)} rows (sorted by Score desc)")

if __name__ == "__main__":
    main()
