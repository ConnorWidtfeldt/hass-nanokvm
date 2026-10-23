"""Constants for NanoKVM."""

from datetime import timedelta

DOMAIN = "nanokvm"
CONF_FINGERPRINT = "fingerprint"
MIN_VERSION = "2.5.2"
STATUS_INTERVAL = timedelta(seconds=5)
CONFIG_INTERVAL = timedelta(seconds=60)
UPDATE_INTERVAL = timedelta(hours=6)
