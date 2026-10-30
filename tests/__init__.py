import logging

# Keep the application's log lines out of the test output; assertLogs still sees them.
logging.getLogger("powerout").addHandler(logging.NullHandler())
