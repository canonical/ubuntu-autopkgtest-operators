#!/usr/bin/env python3

"""Run results app as CGI script"""

from wsgiref.handlers import CGIHandler

from private_results.app import app

if __name__ == "__main__":
    app.config["DEBUG"] = True
    CGIHandler().run(app)
