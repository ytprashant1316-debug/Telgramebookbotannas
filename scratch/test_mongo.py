import pymongo

MONGO_URI = "mongodb+srv://soniprashant671_db_user:sC0oFGksHNHf8kIz@cluster0.j5hwlec.mongodb.net/?appName=Cluster0"

try:
    print("Connecting to MongoDB Atlas...")
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    # Trigger connection
    print("Databases list:", client.list_database_names())
    db = client["annas_downloader"]
    col = db["settings"]
    print("Document count:", col.count_documents({}))
    print("Success!")
except Exception as e:
    print("Error:", e)
