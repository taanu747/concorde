import csv
import psycopg2
import psycopg2.extras
import os

DATABASE_URL = os.environ.get("DATABASE_URL")

def upload():
    if not DATABASE_URL:
        print("Please set DATABASE_URL")
        return
        
    conn = psycopg2.connect(DATABASE_URL)
    cursor = conn.cursor()
    
    print("Creating table aircraft_metadata...", flush=True)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS aircraft_metadata (
            icao24 TEXT PRIMARY KEY,
            registration TEXT,
            model TEXT,
            typecode TEXT,
            operator TEXT
        )
    ''')
    conn.commit()
    
    print("Uploading data...", flush=True)
    with open('aircraftDatabase.csv', 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        count = 0
        batch = []
        for row in reader:
            icao = row.get('icao24', '').strip().lower()
            if icao:
                batch.append((
                    icao,
                    row.get('registration', ''),
                    row.get('model', ''),
                    row.get('typecode', ''),
                    row.get('operator', '')
                ))
            if len(batch) >= 10000:
                psycopg2.extras.execute_values(
                    cursor,
                    '''
                    INSERT INTO aircraft_metadata (icao24, registration, model, typecode, operator)
                    VALUES %s
                    ON CONFLICT (icao24) DO NOTHING
                    ''',
                    batch
                )
                conn.commit()
                count += len(batch)
                print(f"Uploaded {count} records...", flush=True)
                batch = []
        if batch:
            psycopg2.extras.execute_values(
                cursor,
                '''
                INSERT INTO aircraft_metadata (icao24, registration, model, typecode, operator)
                VALUES %s
                ON CONFLICT (icao24) DO NOTHING
                ''',
                batch
            )
            conn.commit()
            
    print("Done!", flush=True)

if __name__ == "__main__":
    upload()
