CONTRACT_INFO_RESPONSE = {
    "assets": [{"asset": "USDT", "marginAvailable": True}],
    "rateLimits": [],
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "displaySymbol": "BTCUSDT",
            "pricePrecision": 1,
            "quantityPrecision": 6,
            "contractVal": 0.000001,
            "minLeverage": 1,
            "maxLeverage": 408,
            "minOrderSize": 0.0001,
            "maxOrderSize": 10000,
        }
    ],
}

KLINES_RESPONSE = [[1700000000000, "1", "2", "0.5", "1.5", "10", 1700000299999, "15", 3, "5", "7.5"]]

TICKER_RESPONSE = {
    "symbol": "BTCUSDT",
    "lastPrice": "1.5",
    "bidPrice": "1.4",
    "askPrice": "1.6",
    "volume": "100",
    "closeTime": 1700000299999,
}

DEMO_BALANCE_RESPONSE = [
    {
        "asset": "SUSDT",
        "balance": "10000",
        "availableBalance": "9950",
        "frozen": "50",
        "unrealizePnl": "10",
    }
]

DEMO_POSITION_RESPONSE = [
    {
        "id": 123,
        "asset": "SUSDT",
        "symbol": "BTCSUSDT",
        "side": "LONG",
        "leverage": "3",
        "size": "0.01",
        "openValue": "15",
        "marginSize": "5",
        "unrealizePnl": "0.5",
        "liquidatePrice": "0",
    }
]

ORDER_ACCEPTED_RESPONSE = {
    "orderId": "702345678901234567",
    "clientOrderId": "alpha-0001",
    "success": True,
    "errorCode": "",
    "errorMessage": "",
}

ORDER_INFO_RESPONSE = {
    "orderId": 702345678901234567,
    "symbol": "BTCSUSDT",
    "side": "BUY",
    "positionSide": "LONG",
    "status": "FILLED",
    "type": "MARKET",
    "time": 1700000000000,
    "price": "1.5",
    "origQty": "0.01",
    "executedQty": "0.01",
    "avgPrice": "1.5",
    "updateTime": 1700000001000,
    "clientOrderId": "alpha-0001",
    "timeInForce": "GTC",
    "reduceOnly": False,
}

TRADE_RESPONSE = [
    {
        "id": 801234567890123456,
        "orderId": 702345678901234567,
        "symbol": "BTCSUSDT",
        "buyer": True,
        "commission": "0.001",
        "commissionAsset": "SUSDT",
        "maker": False,
        "price": "1.5",
        "qty": "0.01",
        "quoteQty": "0.015",
        "realizedPnl": "0",
        "side": "BUY",
        "positionSide": "LONG",
        "time": 1700000001000,
    }
]
