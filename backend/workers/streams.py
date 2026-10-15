"""私有 WS 采集进程入口。

与 workers/scheduler.py 分开跑：采集是长连接，交易是 5 分钟一轮的短事务，
混在一个进程里会让 worker 的失败面扩大到网络长连接。
"""

import asyncio
import logging

from app.config import get_settings
from app.logging import configure_logging
from app.streams.listener import StreamListener

logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("private stream listener starting: url=%s", settings.streams_ws_url)
    asyncio.run(StreamListener(settings=settings).run_forever())


if __name__ == "__main__":
    main()
