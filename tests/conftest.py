import os

# Before the app is imported: no rate limiting in the general suite (tests/test_production.py
# exercises the limiter on its own app), plain-text logs.
os.environ.setdefault("RATE_LIMIT_PER_MINUTE", "0")
os.environ.setdefault("LOG_FORMAT", "text")
