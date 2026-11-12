"""Alpaca venue identity and defaults.

Venue and unsupported-operation message follow nautilus_trader@d53849b
``examples/live/_template/constants.py``; ALPACA is the paper runners' venue
(``src/sextant/paper/native.py:108``). A config names the credential environment
variable, never its value (``scripts/keys.sh:26-31``). The ``sip`` default
follows ``src/sextant/paper/alpaca_feed.py:60`` at 1252eb9f.
"""

from nautilus_trader.model import Venue

VENUE = Venue.from_str("ALPACA")
NOT_IMPLEMENTED = "This operation is not implemented by the Alpaca adapter"

DEFAULT_FEED = "sip"
