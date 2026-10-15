"""采集层配置项的默认值。

默认值就是系统实际会用的值 —— 测试锁住它们，免得改配置时无声地改变运行行为。

**没有「启用开关」是刻意的**：部署模型是「选哪些服务起来」，一个
STREAMS_ENABLED=false 只会让「容器起了但什么都没干」成为可能。而且它和
compose 的 restart: unless-stopped 会形成重启循环 —— 进程正常退出（exit 0）
也会被拉起来。想不跑采集就别启动那个服务。
"""

from app.config import Settings


def test_stream_settings_defaults():
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        ZILLIZ_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
    )

    assert settings.streams_ws_url == "wss://ws-contract.weex.com/v3/ws/private"
    assert settings.streams_reconnect_initial_seconds == 1.0
    assert settings.streams_reconnect_max_seconds == 60.0
    assert settings.streams_rescan_seconds == 300.0
    assert settings.streams_reconcile_seconds == 300.0


def test_there_is_no_stream_enable_switch():
    """这个开关被删掉了，别再加回来。

    它和 `restart: unless-stopped` 组合会变成重启循环：main() 打条日志正常
    退出（exit 0），Docker 对退出码 0 同样会重启。
    """

    settings = Settings(
        DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
        ZILLIZ_URI="http://localhost:19530",
        ARK_API_KEY="test-key",
    )

    assert not hasattr(settings, "streams_enabled")
