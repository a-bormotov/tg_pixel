import os, sys, csv, tempfile
import psycopg2
from sshtunnel import SSHTunnelForwarder

SQL_FILE       = os.environ.get("SQL_FILE", "data.sql")
USER_SQL_FILE  = os.environ.get("USER_SQL_FILE", "user_data.sql")
RAW_CSV        = os.environ.get("OUTPUT_CSV", "raw_data.csv")
RESULT_CSV     = "result_data.csv"
BLACKLIST_FILE = "black_list.csv"   # столбец: userid
NFT_FILE       = "nft_data.csv"     # столбцы: userId, multiplier
TOP_N          = int(os.environ.get("TOP_N", "3000"))

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
    return s.replace("'", "''")

# ---------- main ----------
def main():
    # ---- SSH ----
    ssh_host = need("SSH_HOST")
    ssh_port = as_int("SSH_PORT", 22)
    ssh_user = need("SSH_USER")
    ssh_key  = need("SSH_KEY")
    ssh_key_password = os.getenv("SSH_KEY_PASSWORD")
    key_path = write_key(ssh_key)

    # ---- DB2 ----
    db2_host = need("DB2_HOST")
    db2_port = as_int("DB2_PORT", 5432)
    db2_name = need("DB2_NAME")
    db2_user = need("DB2_USER")
    db2_pass = clean_password(need("DB2_PASS"))

    # ---- DB1 ----
    db1_host = need("DB1_HOST")
    db1_port = as_int("DB1_PORT", 5432)
    db1_name = need("DB1_NAME")
    db1_user = need("DB1_USER")
    db1_pass = clean_password(need("DB1_PASS"))

    # ---- SQL ----
    for f in [SQL_FILE, USER_SQL_FILE]:
        if not os.path.exists(f):
            print(f"[config error] Missing SQL file: {f}", file=sys.stderr)
            sys.exit(2)
    with open(SQL_FILE, "r", encoding="utf-8") as f:
        sql_data = f.read()
    with open(USER_SQL_FILE, "r", encoding="utf-8") as f:
        sql_user_tpl = f.read()

    # ===== 1) DB2 -> raw_data.csv =====
    print("[1] Query DB2 (events)...")
    cols2, rows2 = run_sql_via_ssh(
        db2_host, db2_port, db2_name, db2_user, db2_pass,
        ssh_host, ssh_port, ssh_user, key_path, ssh_key_password,
        sql_data
    )
    with open(RAW_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols2)
        w.writerows(rows2)
    print(f"[1] wrote {RAW_CSV}: {len(rows2)} rows")

    # userId index
    try:
        uid_idx = [c.lower() for c in cols2].index("userid")
    except ValueError:
        print("[error] Column 'userId' not found.", file=sys.stderr)
        sys.exit(2)
    user_ids = [str(r[uid_idx]) for r in rows2]

    # ===== 2) Blacklist =====
    blacklist = set()
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
            rdr = csv.DictReader(f)
            for row in rdr:
                v = (row.get("userid") or "").strip()
                if v:
                    blacklist.add(v)
    filtered_ids = [u for u in user_ids if u not in blacklist]
    print(f"[2] blacklist filtered: {len(user_ids)} -> {len(filtered_ids)}")

    # ===== 2.5) NFT multipliers (and eligibility) =====
    nft_mult = {}
    if os.path.exists(NFT_FILE):
        try:
            with open(NFT_FILE, "r", encoding="utf-8") as f:
                rdr = csv.DictReader(f)
                for row in rdr:
                    uid = (row.get("userId") or row.get("userid") or "").strip()
                    m_raw = (row.get("multiplier") or "").strip()
                    if not uid or not m_raw:
                        continue
                    try:
                        m = float(m_raw)
                        if m <= 0:
                            continue
                        nft_mult[uid] = m
                    except Exception:
                        continue
            print(f"[2.5] nft_data loaded: {len(nft_mult)} multipliers")
        except Exception as e:
            print(f"[warn] failed to read {NFT_FILE}: {e}")
    else:
        print("[info] nft_data.csv not found; no eligible players by NFT rule")

    before = len(filtered_ids)
    filtered_ids = [u for u in filtered_ids if u in nft_mult]
    print(f"[2.6] nft eligibility filtered: {before} -> {len(filtered_ids)}")

    if not filtered_ids:
        with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ["Rank","Username","Score","Points",
                 "Gacha rare","Gacha epic","Gacha legendary",
                 "NFT","PIXEL","USD (ref.)","userId"]
            )
        print("[stop] no eligible players left; wrote empty result_data.csv")
        return

    # ===== 3) DB1 -> usernames =====
    if "{IDS}" in sql_user_tpl:
        id_list = ",".join(f"'{esc_sql_str(i)}'" for i in filtered_ids)
        sql_user_final = sql_user_tpl.replace("{IDS}", id_list)
    elif "%s" in sql_user_tpl:
        values_rows = ",".join(
            f"('{esc_sql_str(uid)}',{ord_})"
            for ord_, uid in enumerate(filtered_ids, start=1)
        )
        sql_user_final = sql_user_tpl % values_rows
    else:
        print("[config error] user_data.sql must contain {IDS} or %s", file=sys.stderr)
        sys.exit(2)

    print("[3] Query DB1 (usernames)...")
    cols1, rows1 = run_sql_via_ssh(
        db1_host, db1_port, db1_name, db1_user, db1_pass,
        ssh_host, ssh_port, ssh_user, key_path, ssh_key_password,
        sql_user_final
    )
    c_lower = [c.lower() for c in cols1]
    c_uid = c_lower.index("userid")
    c_unm = c_lower.index("username")
    c_ord = c_lower.index("ord") if "ord" in c_lower else None

    id_to_name = {str(r[c_uid]): r[c_unm] for r in rows1}
    ord_map = {str(r[c_uid]): r[c_ord] for r in rows1 if c_ord is not None}

    # ===== 4) Build result_data.csv =====
    cols2_lower = [c.lower() for c in cols2]

    def idx(name: str):
        try:
            return cols2_lower.index(name)
        except ValueError:
            return None

    idx_score = idx("score")
    idx_pts   = idx("points")
    idx_r     = idx("rare")
    idx_e     = idx("epic")
    idx_l     = idx("legendary")

    records = []
    for r in rows2:
        uid = str(r[uid_idx])
        if uid in blacklist:
            continue
        if uid not in nft_mult:
            continue  # eligibility

        username = id_to_name.get(uid, uid)

        base_score = float(r[idx_score]) if idx_score is not None and r[idx_score] is not None else 0.0
        points = int(r[idx_pts]) if idx_pts is not None and r[idx_pts] is not None else 0

        gacha_rare = int(r[idx_r]) if idx_r is not None and r[idx_r] is not None else 0
        gacha_epic = int(r[idx_e]) if idx_e is not None and r[idx_e] is not None else 0
        gacha_leg  = int(r[idx_l]) if idx_l is not None and r[idx_l] is not None else 0
        gacha_total = gacha_rare + gacha_epic + gacha_leg

        mult = float(nft_mult[uid])
        final_score = base_score * mult
        order_key = ord_map.get(uid, 0)

        records.append((username, final_score, points, gacha_total,
                        gacha_rare, gacha_epic, gacha_leg,
                        uid, order_key, mult))

    # sort by adjusted score, then points, then total gacha, then ord
    records.sort(key=lambda x: (x[1], x[2], x[3], x[8]), reverse=True)
    records = records[:TOP_N]

    with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "Rank","Username","Score","Points",
            "Gacha rare","Gacha epic","Gacha legendary",
            "NFT","PIXEL","USD (ref.)","userId"
        ])
        for i, (username, score, points, _gacha_total,
                gacha_rare, gacha_epic, gacha_leg,
                uid, _, mult) in enumerate(records, start=1):
            w.writerow([
                i, username, score, points,
                gacha_rare, gacha_epic, gacha_leg,
                f"{mult:.1f}", "-", "-", uid
            ])

    print(f"[4] wrote {RESULT_CSV}: {len(records)} rows (top {TOP_N}, NFT multipliers applied)")

if __name__ == "__main__":
    main()
