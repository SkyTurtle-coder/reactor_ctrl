bind = "127.0.0.1:5000"
# The runtime scheduler, manual reconciler, and recipe reconciler are
# in-process workers with an in-memory command queue. Running multiple
# Gunicorn processes would start duplicate schedulers against the same DB
# rows, which is not safe in production until the runtime is externalized.
workers = 1
threads = 4
timeout = 120
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
capture_output = True
