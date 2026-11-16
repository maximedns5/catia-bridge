"""The link between a hosted app and CATIA on the user's PC.

The app runs in the cloud and cannot open a connection to the PC, so the
bridge calls out: it pairs once with a short code, then long-polls the app's
server for jobs, runs each one against the running CATIA over COM, and posts
the answer back. Jobs carry data (a feature tree), never code.

    config.py   where the link's address and token live on the PC
    client.py   the HTTP calls (claim, next, result) and the polling loop
    jobs.py     the methods a job may name, and how each runs
    relay.py    a reference server for the app side (development and tests)

The protocol is in docs/app-link.md.
"""

PROTOCOL = 1
# Device routes on the app's server, relative to the address the bridge keeps.
DEVICE_PATH = "/api/catia-link/device"
# A server answering `next` with this header moves the bridge to that address.
SERVER_HEADER = "X-CATIA-Link-Server"
