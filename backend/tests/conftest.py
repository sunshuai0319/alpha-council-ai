import os


os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("MILVUS_URI", "http://localhost:19530")
os.environ.setdefault("ARK_API_KEY", "test-key")
