"""Constants for the ESPHome Device Builder."""

__version__ = "0.0.0"

DEFAULT_PORT = 6052
DEFAULT_HOST = "0.0.0.0"

# Trusted TCP site for HA Ingress. Bound only when ``--ha-addon`` is set,
# on the supervisor's docker bridge network, and bypasses the password
# gate (the supervisor has already authenticated the request).
DEFAULT_INGRESS_PORT = 8099
