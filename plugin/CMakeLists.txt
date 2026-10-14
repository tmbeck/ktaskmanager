cmake_minimum_required(VERSION 3.16)
project(ktaskmanager VERSION 0.1.1 LANGUAGES CXX)
set(CMAKE_AUTOMOC ON)
set(CMAKE_CXX_STANDARD 20)
find_package(Qt6 REQUIRED COMPONENTS Core Gui)
find_package(Plasma REQUIRED)
find_package(KF6Config REQUIRED)
find_package(KF6CoreAddons REQUIRED)

add_library(org.kde.ktaskmanagermenu MODULE taskmanagermenu.cpp)
set_target_properties(org.kde.ktaskmanagermenu PROPERTIES PREFIX "")
target_link_libraries(org.kde.ktaskmanagermenu Plasma::Plasma KF6::ConfigCore KF6::CoreAddons Qt6::Gui)

install(TARGETS org.kde.ktaskmanagermenu DESTINATION ${CMAKE_INSTALL_PREFIX}/${QT6_INSTALL_PLUGINS}/plasma/containmentactions)  # lib64 or lib/<triplet> on other distros
install(PROGRAMS ../ktaskmanager.py DESTINATION ${CMAKE_INSTALL_PREFIX}/bin RENAME ktaskmanager)
install(PROGRAMS ../ktaskmanager-enable-panel-menu DESTINATION ${CMAKE_INSTALL_PREFIX}/bin)
install(FILES ../ktaskmanager.desktop DESTINATION ${CMAKE_INSTALL_PREFIX}/share/applications)
