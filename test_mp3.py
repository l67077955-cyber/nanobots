import chromadb
c1 = chromadb.PersistentClient(path='/root/.nanobot/mempalace/palace')
print("c1 created")
c2 = chromadb.PersistentClient(path='/root/.nanobot/mempalace/palace')
print("c2 created")
