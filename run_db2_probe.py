import os
import csv
import psycopg2
from sshtunnel import SSHTunnelForwarder

SQL_FILE = "db2_probe.sql"
OUT_CSV = "raw_data.csv"

def main():
    # Параметры DB2 (удалённая БД — до неё идём через SSH-туннель)
    db_host = os.environ["DB2_HOST"]              # хост БД с точки зрения SSH-сервера (часто 127.0.0.1)
    db_port = int(os.environ.get("DB2_PORT", "5432"))
    db_name = os.environ["DB2_NAME"]
    db_user = os.environ["DB2_USER"]
    db_pass = os.environ["DB2_PASSWORD"]

    # Параметры SSH
    ssh_host = os.environ["SSH_HOST"]
    ssh_port = int(os.environ.get("SSH_PORT", "22"))
    ssh_user = os.environ["SSH_USER"]
    ssh_key_path = os.environ.get("SSH_KEY_PATH", os.path.expanduser("~/.ssh/id_rsa"))
    ssh_key_password = os.environ.get("SSH_KEY_PASSWORD")  # если ключ с паролем, иначе пусто

    with open(SQL_FILE, "r", encoding="utf-8") as f:
        sql = f.read()

    # Стартуем туннель: локальный порт -> (db_host:db_port) на удалённой стороне
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
            application_name="gh-actions-db2-probe-ssh"
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
                colnames = [d[0] for d in cur.description]

            # Пишем CSV
            with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(colnames)
                w.writerows(rows)

            print(f"Wrote {OUT_CSV} with {len(rows)} rows")
        finally:
            conn.close()
    finally:
        tunnel.stop()

if __name__ == "__main__":
    main()
