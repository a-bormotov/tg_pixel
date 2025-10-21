import os
import csv
import psycopg2

SQL_FILE = "db2_probe.sql"
OUT_CSV = "raw_data.csv"   # перезапишет существующий raw_data.csv — это ок для проверки

def main():
    sql = ""
    with open(SQL_FILE, "r", encoding="utf-8") as f:
        sql = f.read()

    conn = psycopg2.connect(
        host=os.environ["DB2_HOST"],
        port=int(os.environ.get("DB2_PORT", "5432")),
        dbname=os.environ["DB2_NAME"],
        user=os.environ["DB2_USER"],
        password=os.environ["DB2_PASSWORD"],
        application_name="gh-actions-db2-probe"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            colnames = [desc[0] for desc in cur.description]

        # Пишем CSV
        with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(colnames)
            writer.writerows(rows)

        print(f"Wrote {OUT_CSV} with {len(rows)} rows")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
