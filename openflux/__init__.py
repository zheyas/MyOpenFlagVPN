"""OpenFlux — Python port.

A pluggable-transport L4 tunnel: a SOCKS5 client multiplexes reliable streams
over an exotic carrier (cups.online rooms, Yandex/Mail.ru docs, MAX calls, or a
plain TCP link) to an L4 exit node that re-dials the real destination.

See ``openflux.cli`` for the command-line entry point.
"""

__version__ = "0.1.0"
