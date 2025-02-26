#!/bin/bash
uvicorn $APP_MODULE --host 0.0.0.0 --port $APP_INTERNAL_PORT --workers $THREAD_WORKERS $ADDITIONAL_ARGS

# Debugging options
# if [ "$RUN_MODE" = "dev" ]; then
#     echo "Running in development mode with debugger..."
#     python -m debugpy --listen 0.0.0.0:$APP_INTERNAL_DEBUG_PORT --wait-for-client -m \
#     uvicorn $APP_MODULE --host 0.0.0.0 --port $APP_INTERNAL_PORT --workers $THREAD_WORKERS $ADDITIONAL_ARGS
# else
#     echo "Running in production mode..."
#     uvicorn $APP_MODULE --host 0.0.0.0 --port $APP_INTERNAL_PORT --workers $THREAD_WORKERS $ADDITIONAL_ARGS
# fi
