import sqlite3
import time

from django.db import OperationalError, connection


def run_with_sqlite_lock_retry(operation):
    for attempt in range(3):
        try:
            return operation()
        except OperationalError as exc:
            cause = exc.__cause__
            error_code = getattr(cause, "sqlite_errorcode", None)
            primary_code = (
                error_code & 0xFF
                if error_code is not None
                else None
            )
            is_sqlite_lock = (
                connection.vendor == "sqlite"
                and primary_code in (
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                )
            )
            if not is_sqlite_lock or attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))
