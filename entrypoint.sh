#!/bin/sh
set -e
chown -R mediatriage /data
exec gosu mediatriage "$@"
