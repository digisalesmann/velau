# FX Single-Pair Trading Bot (XAU/USD Example)

## Overview
A robust, AI-powered trading bot focused on a single FX pair (e.g., XAU/USD). Integrates live news, sentiment analysis, technicals, and supports live trading via MT5 and Deriv API. Modular, scalable, and designed for premium effectiveness.

## Structure
- `core/` — Trading logic, strategies, risk management
- `ai/` — NLP, sentiment analysis, news classification
- `news/` — News API integration, fetchers, parsers
- `brokers/` — MT5 and Deriv API connectors
- `users/` — User management, account linking, copy trading
- `config/` — Settings, credentials, pair configs
- `tests/` — Unit/integration tests

## Features
- Real-time news ingestion and AI sentiment
- Technical + volatility analysis
- Dynamic risk management
- MT5 execution (primary), Deriv API bridge (optional)
- Copy trading model

## Next Steps
- Implement news ingestion and AI modules
- Build core trading logic
- Integrate broker APIs
- Add user/copy trading features
