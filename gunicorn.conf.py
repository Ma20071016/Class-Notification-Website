"""One process preserves the app's in-memory login/registration limits."""

bind = "127.0.0.1:8000"
workers = 1
worker_class = "gthread"
threads = 4
# Allow a bounded local Word-to-PDF conversion to finish.
timeout = 90
graceful_timeout = 90
keepalive = 5
accesslog = "-"
errorlog = "-"
capture_output = True
# ProxyFix in app.py handles forwarding only when explicitly enabled.
forwarded_allow_ips = ""
