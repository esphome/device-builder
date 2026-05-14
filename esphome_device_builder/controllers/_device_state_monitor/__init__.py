"""Device connectivity monitor — mDNS browser + ping fallback.

Tracks online/offline state for the configured devices, with mDNS as
the primary source (event-driven) and ICMP ping as a periodic fallback
for devices that aren't broadcasting their service. MQTT observations
are also welcomed via :meth:`apply` for devices that opt into MQTT
discovery. The monitor calls back into the owning controller whenever
a state actually changes; controllers stay free of zeroconf / icmplib
/ aiomqtt details.

Source precedence (highest first): ``mdns`` > ``mqtt`` > ``ping``. A
lower-priority source can never override the state set by a higher one.
"""

from __future__ import annotations

# Re-exports. Redundant-alias form marks these as intentional
# re-exports (PEP 484) so existing
# ``from ..controllers._device_state_monitor import X`` callers
# (including tests that reach for private constants and module
# helpers) keep working unchanged across the split arc.
from .controller import _MDNS_REFRESH_PADDING_SECONDS as _MDNS_REFRESH_PADDING_SECONDS
from .controller import DeviceStateMonitor as DeviceStateMonitor
from .controller import _decode_txt_bytes_to_sorted_pairs as _decode_txt_bytes_to_sorted_pairs
from .controller import device_name_from_service as device_name_from_service
