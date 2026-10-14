#!/bin/sh
# Build + install from source (needs sudo), then turn on the panel's Task Manager menu.
set -e
cd "$(dirname "$0")"
cmake -S plugin -B plugin/build -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release -Wno-dev >/dev/null
cmake --build plugin/build
sudo cmake --install plugin/build
ktaskmanager-enable-panel-menu
