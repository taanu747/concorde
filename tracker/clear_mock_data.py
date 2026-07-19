import psycopg2
import os

DATABASE_URL = "postgresql://postgres.moajobbeudcqaiwjmnsq:mNwcHcBsR1XkQiLe@aws-0-us-east-1.pooler.supabase.com:6543/postgres"

def clear_db():
    try:
        print("Connecting to Supabase...")
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        
        print("Clearing historical mock data...")
        cursor.execute('TRUNCATE TABLE aircraft_history')
        
        print("Clearing live map payload...")
        cursor.execute("UPDATE latest_payload SET payload = '{}' WHERE id = 1")
        
        conn.commit()
        print("Mock data completely erased!")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    clear_db()
