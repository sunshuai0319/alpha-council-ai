"""采集层配置项的默认值。

默认值就是系统实际会用的值 —— 测试锁住它们，免得改配置时无声地改变运行行为。
"""

from app.config import Settings


def test_stream_settings_defaults():
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        ZILLIZ_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
    )

    assert settings.streams_enabled is True
    assert settings.streams_ws_url == "wss://ws-contract.weex.com/v3/ws/private"
    assert settings.streams_reconnect_initial_seconds == 1.0
    assert settings.streams_reconnect_max_seconds == 60.0
    assert settings.streams_rescan_seconds == 300.0
