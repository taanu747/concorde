import sqlite3
import psycopg2
import psycopg2.extras
import os
import sys

SQLITE_DB = 'aircraft_history.db'
# We fetch this from env or hardcode for now
DATABASE_URL = "postgresql://postgres.moajobbeudcqaiwjmnsq:mNwcHcBsR1XkQiLe@aws-0-us-east-1.pooler.supabase.com:6543/postgres"

def migrate():
    if not os.path.exists(SQLITE_DB):
        print(f"Error: {SQLITE_DB} not found!")
        sys.exit(1)
        
    print("Connecting to local SQLite database...")
    sqlite_conn = sqlite3.connect(SQLITE_DB)
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_cursor = sqlite_conn.cursor()
    
    print("Connecting to Supabase PostgreSQL database...")
    try:
        pg_conn = psycopg2.connect(DATABASE_URL)
        pg_cursor = pg_conn.cursor()
    except Exception as e:
        print(f"PostgreSQL Connection Error: {e}")
        sys.exit(1)
        
    # Count rows
    sqlite_cursor.execute("SELECT COUNT(*) FROM aircraft_history")
    total_rows = sqlite_cursor.fetchone()[0]
    print(f"Found {total_rows} records in SQLite database to migrate.")
    
    if total_rows == 0:
        print("Nothing to migrate!")
        return
        
    # Query all rows (excluding id so Postgres can auto-increment it safely)
    sqlite_cursor.execute("SELECT hex, callsign, lat, lon, altitude, timestamp, heading FROM aircraft_history")
    
    batch = []
    count = 0
    print("Migrating data...", flush=True)
    
    while True:
        row = sqlite_cursor.fetchone()
        if not row:
            break
            
        batch.append((
            row['hex'], 
            row['callsign'], 
            row['lat'], 
            row['lon'], 
            row['altitude'], 
            row['timestamp'], 
            row['heading']
        ))
        
        if len(batch) >= 10000:
            psycopg2.extras.execute_values(
                pg_cursor,
                '''
                INSERT INTO aircraft_history (hex, callsign, lat, lon, altitude, timestamp, heading)
                VALUES %s
                ''',
                batch
            )
            pg_conn.commit()
            count += len(batch)
            print(f"Migrated {count} / {total_rows} records...", flush=True)
            batch = []
            
    # Process remaining records
    if batch:
        psycopg2.extras.execute_values(
            pg_cursor,
            '''
            INSERT INTO aircraft_history (hex, callsign, lat, lon, altitude, timestamp, heading)
            VALUES %s
            ''',
            batch
        )
        pg_conn.commit()
        count += len(batch)
        print(f"Migrated {count} / {total_rows} records...", flush=True)
        
    print("Migration complete!", flush=True)
    
    sqlite_conn.close()
    pg_conn.close()

if __name__ == "__main__":
    migrate()
