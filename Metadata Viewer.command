#!/bin/bash
# Double-click me in Finder to launch the viewer.
exec /usr/bin/env python3 "$(dirname "$0")/metadata_viewer.py" "$@"
