import os

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("MILVUS_URI", "http://localhost:19530")
os.environ.setdefault("ARK_API_KEY", "test-key")
os.environ.setdefault("APP_ENV", "test")

from app.config import Settings  # noqa: E402

# 测试不读开发者的 .env。
#
# Settings 配置了 env_file=".env"，于是构造 Settings(...) 时，没显式传的字段会从
# 本地 .env 取 —— 「测试通过」就取决于开发者机器上的 .env 内容。实测踩到：把部署
# 配置改成 12h/1d 后，5 个测试立刻变红，而代码本身没有任何问题。
#
# 置空 env_file 让测试回到「代码默认值 + conftest 设的环境变量」，与本地配置无关。
Settings.model_config["env_file"] = None
