from fastapi import FastAPI, HTTPException
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from datetime import date
import os

app = FastAPI()

# Configure API key and secret
API_KEY = os.getenv('ALPACA_API_KEY')
API_SECRET = os.getenv('ALPACA_API_SECRET')
BASE_URL = "https://paper-api.alpaca.markets"  # or the live API URL

# Initialize Alpaca Data Client
client = StockHistoricalDataClient(API_KEY, API_SECRET)

@app.get("/stock/{symbol}/today")
def get_todays_data(symbol: str):
    try:
        # Prepare the date range for today
        today = date.today()

        # Create the request for stock bars
        request_params = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=today.isoformat()
            # end=today.isoformat()
        )

        # Fetch the stock bars data
        bars = client.get_stock_bars(request_params)

        # request_params = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        # quote = client.get_stock_latest_quote(request_params)
        # print(quote)

        # Prepare the response data
        # data = [{
        #     "time": bar.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
        #     "open": bar.open,
        #     "high": bar.high,
        #     "low": bar.low,
        #     "close": bar.close,
        #     "volume": bar.volume
        # } for bar in bars]

        return {"symbol": symbol, "data": bars}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))

# Add other routes as needed
