import os
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()  # this will find .env in the current folder

uri = os.getenv("MONGO_URI")
dbn = os.getenv("MONGO_DB", "warrantdb")

client = MongoClient(uri, serverSelectionTimeoutMS=5000)
print("Connected to cluster, MongoDB version:", client.server_info()["version"])
print("Using DB:", dbn)
print("Collections so far:", client[dbn].list_collection_names())