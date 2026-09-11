from app.collectors.base import CollectorResult
from app.domain.schemas import Candle, MarketMicrostructure, MarketSnapshot
from app.exchange.base import ExchangeClient


class WeexCollector:
    """Collect public WEEX market data without coupling persistence to transport."""

    def __init__(self, client: ExchangeClient) -> None:
        self.client = client

    def collect_candles(
        self,
        symbols: tuple[str, ...] = ("BTC-USDT", "ETH-USDT"),
        timeframes: tuple[str, ...] = ("5m", "1h", "4h"),
        limit: int = 100,
    ) -> CollectorResult[Candle]:
        result: CollectorResult[Candle] = CollectorResult(source="weex-candles")
        for symbol in symbols:
            for timeframe in timeframes:
                try:
                    result.items.extend(self.client.get_candles(symbol, timeframe, limit))
                except Exception as exc:  # noqa: BLE001 - isolate failures per market
                    result.errors.append(f"{symbol}/{timeframe}: {exc}")
        return result

    def collect_snapshots(
        self,
        symbols: tuple[str, ...] = ("BTC-USDT", "ETH-USDT"),
    ) -> CollectorResult[MarketSnapshot]:
        result: CollectorResult[MarketSnapshot] = CollectorResult(source="weex-snapshots")
        for symbol in symbols:
            try:
                result.items.append(self.client.get_market_snapshot(symbol))
            except Exception as exc:  # noqa: BLE001 - isolate failures per market
                result.errors.append(f"{symbol}: {exc}")
        return result

    def collect_microstructures(
        self,
        symbols: tuple[str, ...] = ("BTC-USDT", "ETH-USDT"),
    ) -> CollectorResult[MarketMicrostructure]:
        """盘口 / 订单流 / 衍生品。失败模式独立于 ticker，所以单独一个结果对象。"""

        result: CollectorResult[MarketMicrostructure] = CollectorResult(source="weex-microstructure")
        for symbol in symbols:
            try:
                result.items.append(self.client.get_microstructure(symbol))
            except Exception as exc:  # noqa: BLE001 - isolate failures per market
                result.errors.append(f"{symbol}: {exc}")
        return result

    def collect(
        self,
        symbols: tuple[str, ...] = ("BTC-USDT", "ETH-USDT"),
        timeframes: tuple[str, ...] = ("5m", "1h", "4h"),
        limit: int = 100,
    ) -> tuple[CollectorResult[Candle], CollectorResult[MarketSnapshot]]:
        return self.collect_candles(symbols, timeframes, limit), self.collect_snapshots(symbols)
