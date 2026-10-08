import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("MILVUS_URI", "http://localhost:19530")
os.environ.setdefault("ARK_API_KEY", "test-key")
os.environ.setdefault("APP_ENV", "test")

from app.config import Settings

# 测试不读开发者的 .env。
#
# Settings 配置了 env_file=".env"，于是构造 Settings(...) 时，没显式传的字段会从
# 本地 .env 取 —— 「测试通过」就取决于开发者机器上的 .env 内容。实测踩到：把部署
# 配置改成 12h/1d 后，5 个测试立刻变红，而代码本身没有任何问题。
#
# 置空 env_file 让测试回到「代码默认值 + conftest 设的环境变量」，与本地配置无关。
Settings.model_config["env_file"] = None

# 但置空 env_file 只挡住 Settings 自己那条路径：`import pymilvus` 会在导入时用
# python-dotenv 把 backend/.env 灌进 os.environ，之后 Settings() 又从环境变量读到
# 同一份本机配置（实测 TRADING_SYMBOLS / MARKET_TIMEFRAMES / USE_ZILLIZ 全部泄漏，
# 7 个测试变红）。所以每轮测试前把环境恢复到 conftest 建好的干净快照。
_CLEAN_ENV = dict(os.environ)


@pytest.fixture(autouse=True)
def _isolate_environment() -> None:
    os.environ.clear()
    os.environ.update(_CLEAN_ENV)
