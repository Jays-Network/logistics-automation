"""One module per client (or client family) with special routing rules.

Each module owns its own sheet IDs and rules. To add a client: drop a new file
here, then register it in ../resolver.py (route-override / trigger-route
clients) or ../processor.py (clients with a custom per-row flow).
"""
