#!/bin/sh
# THE THREE SCALARS ARE QUOTED, $ADDITIONAL_ARGS IS NOT, and that asymmetry is the whole content of
# this file (tj-xc6nfv, when `make lint` learned to read shell). APP_MODULE comes from the image
# (Dockerfile's ENV), APP_INTERNAL_PORT from .env.default and SERVICE_WORKERS from the service's
# .env.default; all three are single words the stack guarantees -- docker-compose.yaml's healthcheck
# already reads os.environ['APP_INTERNAL_PORT'] and raises KeyError without it -- so quoting them
# changes nothing the stack can produce and stops a value with a space or a glob from becoming
# several arguments. ADDITIONAL_ARGS is the opposite case: it is an ARGUMENT LIST ('--reload' in the
# dev stage, UNSET in prod), so it must stay unquoted to word-split and to vanish when unset.
# Quoting it would pass uvicorn one empty argument in prod and break startup.
# shellcheck disable=SC2086  # $ADDITIONAL_ARGS is a word-split argument list, per the paragraph above
uvicorn "$APP_MODULE" --host 0.0.0.0 --port "$APP_INTERNAL_PORT" --workers "$SERVICE_WORKERS" $ADDITIONAL_ARGS
