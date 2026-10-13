#!/bin/sh
# Build + install (needs sudo), then make the panel right-click menu use the Task Manager menu plugin.
set -e
cd "$(dirname "$0")"
cmake -S plugin -B plugin/build -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release -Wno-dev >/dev/null
cmake --build plugin/build
sudo cmake --install plugin/build

rc=~/.config/plasma-org.kde.plasma.desktop-appletsrc
key="RightButton;NoModifier"
if [ "$(kreadconfig6 --file "$rc" --group ActionPlugins --group 1 --key "$key")" != org.kde.ktaskmanagermenu ]; then
    [ -e "$rc.bak-ktaskmanager" ] || cp "$rc" "$rc.bak-ktaskmanager"
    # plasmashell rewrites its config on exit, so edit while it's stopped. [ActionPlugins][1] = panels.
    systemctl --user stop plasma-plasmashell
    kwriteconfig6 --file "$rc" --group ActionPlugins --group 1 --key "$key" org.kde.ktaskmanagermenu
    systemctl --user start plasma-plasmashell
fi
echo "Done. Right-click the panel -> Task Manager. Undo: restore $rc.bak-ktaskmanager"
