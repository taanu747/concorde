import sqlite3
c = sqlite3.connect(":memory:")
c.execute("CREATE TABLE t (lat REAL, lon REAL)")
c.execute("INSERT INTO t VALUES (42.1234, -71.1234)")
print(c.execute("SELECT ROUND(CAST(lat AS NUMERIC), 3) as r_lat FROM t GROUP BY r_lat").fetchall())
