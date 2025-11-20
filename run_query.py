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

    # ---- DB1 (main: users + users_additional) ----
    db1_host = need("DB1_HOST")
    db1_port = as_int("DB1_PORT", 5432)
    db1_name = need("DB1_NAME")
    db1_user = need("DB1_USER")
    db1_pass = clean_password(need("DB1_PASS"))

    # ---- DB2 (events + vipHistory) ----
    db2_host = need("DB2_HOST")
    db2_port = as_int("DB2_PORT", 5432)
    db2_name = need("DB2_NAME")
    db2_user = need("DB2_USER")
    db2_pass = clean_password(need("DB2_PASS"))

    # ---- SQL files ----
    for f in [SQL_FILE, USER_SQL_FILE]:
        if not os.path.exists(f):
            print(f"[config error] Missing SQL file: {f}", file=sys.stderr)
            sys.exit(2)

    with open(SQL_FILE, "r", encoding="utf-8") as f:
        sql_data_tpl = f.read()
    with open(USER_SQL_FILE, "r", encoding="utf-8") as f:
        sql_user = f.read()

    # ===== 1) DB1 -> участники (vip2/vip3) =====
    print("[1] Query DB1 (users_additional + users)...")
    cols_users, rows_users = run_sql_via_ssh(
        db1_host, db1_port, db1_name, db1_user, db1_pass,
        ssh_host, ssh_port, ssh_user, key_path, ssh_key_password,
        sql_user
    )
    if not rows_users:
        print("[1] No VIP participants found (vip2/vip3).")
        with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ["Rank","Username","Score","PurpleStones",
                 "Cards rare","Cards epic","Cards legendary",
                 "NFT","PIXEL","USD (ref.)","userId"]
            )
        return

    lower_users = [c.lower() for c in cols_users]
    try:
        idx_uid_u = lower_users.index("userid")
    except ValueError:
        print("[error] Column 'userId' not found in user_data.sql result.", file=sys.stderr)
        sys.exit(2)

    idx_username_u = lower_users.index("username") if "username" in lower_users else None
    idx_vip_u      = lower_users.index("viplevel") if "viplevel" in lower_users else None

    id_to_name = {}
    id_to_vip  = {}
    vip_ids = []

    for r in rows_users:
        uid = str(r[idx_uid_u])
        vip_ids.append(uid)
        if idx_username_u is not None and r[idx_username_u] is not None:
            id_to_name[uid] = str(r[idx_username_u])
        else:
            id_to_name[uid] = uid
        if idx_vip_u is not None and r[idx_vip_u] is not None:
            id_to_vip[uid] = str(r[idx_vip_u])

    print(f"[1] Participants (vip2/vip3): {len(vip_ids)}")

    # ===== 2) Подставляем IDs в data.sql (DB2) =====
    if "{IDS}" not in sql_data_tpl:
        print("[config error] data.sql must contain {IDS} placeholder.", file=sys.stderr)
        sys.exit(2)

    values_rows = ",".join(
        f"('{esc_sql_str(uid)}')" for uid in vip_ids
    )
    sql_data_final = sql_data_tpl.replace("{IDS}", values_rows)

    # ===== 3) DB2 -> raw_data.csv (purpleStones + карты) =====
    print("[2] Query DB2 (events + vipHistory)...")
    cols2, rows2 = run_sql_via_ssh(
        db2_host, db2_port, db2_name, db2_user, db2_pass,
        ssh_host, ssh_port, ssh_user, key_path, ssh_key_password,
        sql_data_final
    )
    with open(RAW_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols2)
        w.writerows(rows2)
    print(f"[2] wrote {RAW_CSV}: {len(rows2)} rows")

    if not rows2:
        print("[2] No rows from data.sql; nothing to rank.")
        with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ["Rank","Username","Score","PurpleStones",
                 "Cards rare","Cards epic","Cards legendary",
                 "NFT","PIXEL","USD (ref.)","userId"]
            )
        return

    # userId index
    try:
        uid_idx = [c.lower() for c in cols2].index("userid")
    except ValueError:
        print("[error] Column 'userId' not found in data.sql result.", file=sys.stderr)
        sys.exit(2)

    # ===== 4) Blacklist =====
    user_ids_all = [str(r[uid_idx]) for r in rows2]
    blacklist = set()
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
            rdr = csv.DictReader(f)
            for row in rdr:
                v = (row.get("userid") or "").strip()
                if v:
                    blacklist.add(v)
    filtered_ids = [u for u in user_ids_all if u not in blacklist]
    print(f"[3] blacklist filtered: {len(user_ids_all)} -> {len(filtered_ids)}")

    if not filtered_ids:
        with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ["Rank","Username","Score","PurpleStones",
                 "Cards rare","Cards epic","Cards legendary",
                 "NFT","PIXEL","USD (ref.)","userId"]
            )
        print("[stop] no players left; wrote empty result_data.csv")
        return

    # ===== 5) NFT multipliers =====
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
            print(f"[3.5] nft_data loaded: {len(nft_mult)} multipliers")
        except Exception as e:
            print(f"[warn] failed to read {NFT_FILE}: {e}")
    else:
        print("[info] nft_data.csv not found; NFT multiplier defaults to 1.0")

    # ===== 6) Build result_data.csv =====
    def idx(name):
        try:
            return [c.lower() for c in cols2].index(name)
        except ValueError:
            return None

    idx_score      = idx("score")
    idx_purple     = idx("purplestones")
    idx_cards_tot  = idx("heroesmatched")
    idx_cards_rare = idx("cardsrare")
    idx_cards_epic = idx("cardsepic")
    idx_cards_leg  = idx("cardslegendary")

    records = []
    for ord_key, r in enumerate(rows2, start=1):
        uid = str(r[uid_idx])
        if uid in blacklist:
            continue

        username = id_to_name.get(uid, uid)

        base_score = float(r[idx_score]) if idx_score is not None and r[idx_score] is not None else 0.0
        purple = int(r[idx_purple]) if idx_purple is not None and r[idx_purple] is not None else 0

        cards_total = int(r[idx_cards_tot]) if idx_cards_tot is not None and r[idx_cards_tot] is not None else 0
        c_rare = int(r[idx_cards_rare]) if idx_cards_rare is not None and r[idx_cards_rare] is not None else 0
        c_epic = int(r[idx_cards_epic]) if idx_cards_epic is not None and r[idx_cards_epic] is not None else 0
        c_leg = int(r[idx_cards_leg]) if idx_cards_leg is not None and r[idx_cards_leg] is not None else 0

        mult = float(nft_mult.get(uid, 1.0))
        final_score = base_score * mult

        records.append((username, final_score, purple, cards_total,
                        c_rare, c_epic, c_leg,
                        uid, ord_key, mult))

    # sort by adjusted score, then purpleStones, then total cards, then ord
    records.sort(key=lambda x: (x[1], x[2], x[3], x[8]), reverse=True)
    records = records[:TOP_N]

    # write CSV
    with open(RESULT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "Rank","Username","Score","PurpleStones",
            "Cards rare","Cards epic","Cards legendary",
            "NFT","PIXEL","USD (ref.)","userId"
        ])
        for i, (username, score, purple, cards_total,
                c_rare, c_epic, c_leg,
                uid, _, mult) in enumerate(records, start=1):
            nft_col = f"{mult:.1f}" if uid in nft_mult else "-"
            w.writerow([
                i, username, f"{score:.2f}", purple,
                c_rare, c_epic, c_leg,
                nft_col, "-", "-", uid
            ])

    print(f"[4] wrote {RESULT_CSV}: {len(records)} rows (top {TOP_N}, NFT multipliers applied)")

if __name__ == "__main__":
    main()
