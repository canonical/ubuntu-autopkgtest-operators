#!/usr/bin/env python3
"""Run request app in local debug mode for testing."""

from request.app import app

if __name__ == "__main__":
    app.run(host="127.0.0.1", debug=True)
