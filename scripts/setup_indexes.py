from storage.mongo_client import get_db

db = get_db()

db.harris_bond.create_index([("spn",1),("case_number",1),("group",1)], unique=True, sparse=True)
db.harris_misfel.create_index([("spn",1),("case_number",1),("group",1)], unique=True, sparse=True)
db.harris_nafiling.create_index([("spn",1),("case_number",1),("group",1)], unique=True, sparse=True)

print("Indexes created successfully")
