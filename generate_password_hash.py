#!/usr/bin/env python3
"""Generate a Werkzeug password hash for use in APP_PASSWORD_HASH.

Usage:
    python generate_password_hash.py
    python generate_password_hash.py mypassword

The output can be placed directly in your .env file:
    APP_PASSWORD_HASH=<paste output here>
"""
import sys
from werkzeug.security import generate_password_hash

if len(sys.argv) > 1:
    password = sys.argv[1]
else:
    import getpass
    password = getpass.getpass("Enter password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Error: passwords do not match.", file=sys.stderr)
        sys.exit(1)

if not password:
    print("Error: password must not be empty.", file=sys.stderr)
    sys.exit(1)

print(generate_password_hash(password))
