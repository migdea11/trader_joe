#!/bin/bash
uvicorn $APP_MODULE --host 0.0.0.0 --port $APP_INTERNAL_PORT