#!/usr/bin/env python3

"""Run request app as CGI script."""

from wsgiref.handlers import CGIHandler

from request.app import app

if __name__ == "__main__":
    app.config["DEBUG"] = True
    CGIHandler().run(app)
