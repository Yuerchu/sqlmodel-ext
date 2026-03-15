"""Internal utility functions."""
from datetime import datetime, timezone

now = lambda: datetime.now(timezone.utc)
now_date = lambda: datetime.now(timezone.utc).date()
