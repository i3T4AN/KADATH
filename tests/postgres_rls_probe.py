"""Manual live probe for the organism knowledge-bucket RLS boundary."""
import os

import psycopg

connection = psycopg.connect(os.environ["KADATH_DATABASE_URL"])
cursor = connection.cursor()
cursor.execute("SET ROLE kadath_agent")
cursor.execute("SET LOCAL kadath.agent_id='agent-a'")
cursor.execute("SAVEPOINT own_bucket")
cursor.execute("INSERT INTO knowledge(run_id,epoch,agent_id,kind,payload_json,published_at) VALUES('rls-proof',0,'agent-a','proof','{}',NOW())")
cursor.execute("ROLLBACK TO SAVEPOINT own_bucket")
cursor.execute("SAVEPOINT memory_control")
memory_control_rejected = False
try:
    cursor.execute("SELECT * FROM memory_links")
except psycopg.errors.InsufficientPrivilege:
    memory_control_rejected = True
    cursor.execute("ROLLBACK TO SAVEPOINT memory_control")
cursor.execute("SAVEPOINT denied_bucket")
rejected = False
try:
    cursor.execute("INSERT INTO knowledge(run_id,epoch,agent_id,kind,payload_json,published_at) VALUES('rls-proof',0,'agent-b','proof','{}',NOW())")
except psycopg.errors.InsufficientPrivilege:
    rejected = True
    cursor.execute("ROLLBACK TO SAVEPOINT denied_bucket")
connection.rollback()
connection.close()
print({"cross_agent_write_rejected": rejected, "memory_control_rejected": memory_control_rejected})
if not rejected or not memory_control_rejected: raise SystemExit(1)
