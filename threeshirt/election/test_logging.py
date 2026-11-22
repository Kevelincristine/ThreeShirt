import logging
import sys
from io import StringIO
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings


@override_settings(DEBUG=False)
class ApplicationLoggingTests(SimpleTestCase):
    def test_errors_from_request_and_application_loggers_reach_stderr(self):
        for logger_name in ("django.request", "election", "accounts"):
            with self.subTest(logger=logger_name):
                logger = logging.getLogger(logger_name)
                handler = next(
                    handler
                    for handler in logger.handlers
                    if isinstance(handler, logging.StreamHandler)
                )
                self.assertIs(handler.stream, sys.stderr)
                output = StringIO()

                with patch.object(handler, "stream", output):
                    try:
                        raise RuntimeError(
                            f"synthetic logging diagnostic: {logger_name}"
                        )
                    except RuntimeError:
                        logger.exception("Synthetic exception for logging test.")

                self.assertIn("Traceback (most recent call last)", output.getvalue())
                self.assertIn(
                    f"synthetic logging diagnostic: {logger_name}",
                    output.getvalue(),
                )
