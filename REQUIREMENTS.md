1. Each run overwrites `telegram.csv` with unique jobs from that run only.
2. The script closes only tabs it opened and never closes Chrome.
3. Only messages from the last `TELEGRAM_SINCE_HOURS` hours are processed.
4. Sequential processing of sources in one active browser tab (one MCP stdio session) is an intentional architecture choice. It is not treated as a performance problem unless measurements show violation of the per-source time limit (120s hard timeout in `collect.py` `main()`).
