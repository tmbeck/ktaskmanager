#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""KDE Task Manager — a task manager for Plasma styled after the Windows one (PyQt6 + psutil).

Options > Style switches between the Windows 2000, Windows 7 and Windows 10 looks, and KDE Native
(the Windows 10 layout drawn with your Plasma theme).
"""
import atexit
import configparser
import glob
import json
import math
import re
import os
import shlex
import socket
import subprocess
import sys
import time
from collections import deque
from functools import lru_cache
from types import SimpleNamespace

import psutil
from PyQt6.QtCore import QPointF, QProcess, QRectF, QSettings, QSize, QSortFilterProxyModel, QStandardPaths, Qt, QTimer
from PyQt6.QtDBus import QDBusConnection, QDBusInterface
from PyQt6.QtGui import (QActionGroup, QColor, QFont, QIcon, QKeySequence, QPainter, QPainterPath, QPalette, QPen, QPixmap,
                         QPolygonF, QStandardItem, QStandardItemModel)
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
                             QInputDialog, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
                             QSizePolicy, QStackedWidget, QTableView, QTabWidget, QTreeView, QVBoxLayout, QWidget)

APP_NAME = "KDE Task Manager"
HISTORY = 60
SORT = Qt.ItemDataRole.UserRole
KEY = Qt.ItemDataRole.UserRole + 1
RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
AUTOSTART = os.path.expanduser("~/.config/autostart")


# ---------------------------------------------------------------- helpers

def fmt_uptime(s):
    s = int(s)
    return f"{s // 86400}:{s // 3600 % 24:02}:{s // 60 % 60:02}:{s % 60:02}"


def fmt_hms(s):
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02}:{s % 60:02}"


def fmt_bits(bps):
    return f"{bps / 1e9:.1f} Gbps" if bps >= 1e9 else f"{bps / 1e6:.1f} Mbps" if bps >= 1e6 else f"{bps / 1e3:.0f} Kbps"


def fmt_rate(Bps):
    return f"{Bps / 2**20:.1f} MB/s" if Bps >= 2**20 else f"{Bps / 2**10:.0f} KB/s"


def gb(n):
    return f"{n / 2**30:.1f} GB"


def hugepages(m):
    """'used/total × size' for the preallocated hugetlb pool; meminfo() scaled the page counts by 1024."""
    total = m.get("HugePages_Total", 0) // 1024
    if not total:
        return "None"
    used = total - m["HugePages_Free"] // 1024
    size = m["Hugepagesize"]
    return f"{used}/{total} × {size // 2**30} GB" if size >= 2**30 else f"{used}/{total} × {size // 2**20} MB"


def nice_ceiling(m, floor):
    """Smallest 1/2/5 x 10^k >= m (at least floor) — graph autoscale."""
    if m <= floor:
        return floor
    k = 10 ** math.floor(math.log10(m))
    return next(s * k for s in (1, 2, 5, 10) if s * k >= m)


def read_desktop(path):
    c = configparser.ConfigParser(interpolation=None, strict=False)
    c.optionxform = str
    try:
        c.read(path, encoding="utf-8")
        return dict(c["Desktop Entry"])
    except (configparser.Error, KeyError, UnicodeDecodeError):
        return {}


@lru_cache(maxsize=None)
def desktop_app(desktop_id):
    path = QStandardPaths.locate(QStandardPaths.StandardLocation.GenericDataLocation, f"applications/{desktop_id}.desktop")
    e = read_desktop(path) if path else {}
    return e.get("Name"), e.get("Icon")


@lru_cache(maxsize=None)
def app_names():
    """Executable basename -> friendly app name, from installed .desktop files (stands in for 'file description')."""
    idx = {}
    for d in QStandardPaths.standardLocations(QStandardPaths.StandardLocation.ApplicationsLocation):
        for f in glob.glob(f"{d}/*.desktop"):
            e = read_desktop(f)
            try:
                exe = shlex.split(e.get("Exec", ""))
            except ValueError:
                continue
            if exe and e.get("Name"):
                idx.setdefault(os.path.basename(exe[0]), e["Name"])
    return idx


@lru_cache(maxsize=None)
def package_of(path):
    """Owning package (stands in for 'Publisher')."""
    try:
        out = subprocess.run(["pacman", "-Qqo", path], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.splitlines()[0] if out else ""


def disks_of(dev):
    """Whole disks under a block device, through partitions, LUKS and LVM: '/dev/mapper/root' -> {'nvme0n1'}."""
    sysfs = os.path.realpath(f"/sys/class/block/{os.path.basename(os.path.realpath(dev))}")
    if not os.path.exists(sysfs):
        return set()
    slaves = os.listdir(f"{sysfs}/slaves") if os.path.isdir(f"{sysfs}/slaves") else []
    if slaves:
        return set().union(*(disks_of(f"/dev/{x}") for x in slaves))
    return {os.path.basename(os.path.dirname(sysfs) if os.path.exists(f"{sysfs}/partition") else sysfs)}


def meminfo():
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            out[k] = int(v.split()[0]) * 1024
    return out


def physical_disks():
    out = []
    for d in sorted(os.listdir("/sys/block")):
        if d.startswith(("loop", "ram", "zram", "sr", "dm-", "md")):
            continue
        try:
            if int(open(f"/sys/block/{d}/size").read()):
                out.append(d)
        except OSError:
            pass
    return out


@lru_cache(maxsize=None)
def lscpu():
    info = {}

    def walk(entries):
        for e in entries:
            info[e["field"].rstrip(":")] = e.get("data")
            walk(e.get("children", []))
    try:
        walk(json.loads(subprocess.run(["lscpu", "-J"], capture_output=True, text=True).stdout)["lscpu"])
    except (OSError, ValueError, KeyError):
        pass
    return info


_kwin = {}


def kwin_windows():
    """Open windows via KWin D-Bus (works on Wayland): [{uuid, caption, name, icon, pid}]."""
    if not _kwin:
        bus = QDBusConnection.sessionBus()
        _kwin["runner"] = QDBusInterface("org.kde.KWin", "/WindowsRunner", "org.kde.krunner1", bus)
        _kwin["kwin"] = QDBusInterface("org.kde.KWin", "/KWin", "org.kde.KWin", bus)
    args = _kwin["runner"].call("Match", "window").arguments()
    out = []
    for mid, text, icon, *_ in (args[0] if args else []):
        if mid.startswith("0_"):
            info = _kwin["kwin"].call("getWindowInfo", mid[2:]).arguments()
            info = info[0] if info and isinstance(info[0], dict) else {}
            name, app_icon = desktop_app(info["desktopFile"]) if info.get("desktopFile") else (None, None)
            out.append(dict(uuid=mid[2:], caption=text, pid=info.get("pid"), name=name or text,
                            icon=QIcon.fromTheme(app_icon or icon or "application-x-executable")))
    return out


def kwin_window_action(uuid, action):
    # WindowsRunner match ids are "<action>_<uuid>": 0 activate, 1 close, 2 minimize, 3 maximize
    if not _kwin:
        kwin_windows()
    _kwin["runner"].call("Run", f"{action}_{uuid}", "")


def app_members(pid, kids, stop):
    """pid plus descendants, not descending into `stop` (other apps' windows, e.g. an Okular that Dolphin opened)."""
    out, stack = [], [pid]
    while stack:
        x = stack.pop()
        out.append(x)
        stack.extend(k for k in kids.get(x, []) if k not in stop)
    return out


def end_process(parent, pid, tree=False, confirm=True, app=False):
    try:
        p = psutil.Process(pid)
        name = p.name()
    except psutil.Error:
        return
    if confirm:
        msg = (f"Do you want to end the process{' tree' if tree else ''} of '{name}'?\n\n"
               "If an open program is associated with this process, it will close and you will lose any unsaved data. "
               "If you end a system process, it might result in system instability.")
        if QMessageBox.warning(parent, APP_NAME, msg, QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok,
                               QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Ok:
            return
    if app:  # Win10 "End task" on an app: the app and its helpers, not other apps' windows started from it
        kids = {}
        for q in psutil.process_iter(["ppid"]):
            kids.setdefault(q.info["ppid"], []).append(q.pid)
        victims = []
        for x in reversed(app_members(pid, kids, {w["pid"] for w in kwin_windows()})):  # children first, parent last
            try:
                victims.append(psutil.Process(x))
            except psutil.Error:
                pass
    else:
        victims = (p.children(recursive=True) if tree else []) + [p]
    for v in victims:
        try:
            v.terminate()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            QMessageBox.critical(parent, "Unable to Terminate Process", "The operation could not be completed.\n\nAccess is denied.")
            return

    def kill_leftovers():  # Windows doesn't wait politely: escalate to SIGKILL if SIGTERM was ignored.
        for v in victims:
            try:
                v.kill()
            except psutil.Error:
                pass
    QTimer.singleShot(3000, kill_leftovers)


def open_file_location(pid):
    try:
        QProcess.startDetached("xdg-open", [os.path.dirname(psutil.Process(pid).exe())])
    except psutil.Error:
        pass


AMD_ENGINES = {"gfx": "3D", "compute": "Compute", "dec": "Video Decode", "enc": "Video Encode", "copy": "Copy", "dma": "Copy",
               "vcn": "Video Codec", "jpeg": "Video JPEG"}


@lru_cache(maxsize=None)
def gpus():
    """GPUs in display order: NVIDIA (nvidia-smi) then AMD (sysfs). [{kind, key, name, driver, bus}]"""
    out = []
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=index,name,driver_version,pci.bus_id", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            idx, name, drv, bus = (x.strip() for x in line.split(","))
            out.append(dict(kind="nvidia", key=idx, name=name, driver=drv, bus=bus.lower()[4:]))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    for dev in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
        if not os.path.exists(f"{dev}/gpu_busy_percent"):
            continue
        bus = os.path.basename(os.path.realpath(dev))
        try:
            lspci = subprocess.run(["lspci", "-mm", "-s", bus], capture_output=True, text=True, timeout=5).stdout.split('"')[5]
            m = re.search(r"\[(.*)\]", lspci)
            name = f"AMD {m.group(1) if m else lspci}"
        except (OSError, subprocess.SubprocessError, IndexError):
            name = "AMD GPU"
        try:
            drv = open(f"{dev}/driver/module/version").read().strip()
        except OSError:
            drv = os.uname().release  # amdgpu ships with the kernel
        out.append(dict(kind="amd", key=dev, name=name, driver=drv, bus=bus))
    return out


def read_num(path, default=0):
    try:
        return int(open(path).read().split()[0])
    except (OSError, ValueError, IndexError):
        return default


class Helpers:
    """Long-running metric sources, read in the background without blocking the UI:
    KDE's ksgrd_network_helper (per-process network, runs with cap_net_raw) and nvidia-smi (GPU totals and per-process).
    A missing source just leaves its columns blank."""
    NET_HELPER = "/usr/lib/ksysguard/ksgrd_network_helper"

    def __init__(self):
        self._net, self._net_next, self._net_ts, self._net_at = {}, {}, None, 0.0
        self.nv_proc, self.nv, self.amd_prev, self.procs = {}, {}, {}, []
        if os.access(self.NET_HELPER, os.X_OK):
            # it only flushes per line on a tty; stdbuf makes it line-buffered into our pipe
            self.start(["stdbuf", "-oL", self.NET_HELPER], self.on_net)
        if any(g["kind"] == "nvidia" for g in gpus()):
            self.start(["nvidia-smi", "pmon", "-s", "u", "-d", "1"], self.on_pmon)
            self.start(["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu",
                        "--format=csv,noheader,nounits", "-lms", "1000"], self.on_nvquery)
        QApplication.instance().aboutToQuit.connect(self.stop)
        atexit.register(self.stop)  # also when the event loop never ran (tests)

    def stop(self):
        # disconnect first: output arriving during interpreter teardown would hit a dead Python slot and abort
        for p in self.procs:
            p.readyReadStandardOutput.disconnect()
            p.kill()
            p.waitForFinished(1000)
        self.procs = []

    def start(self, cmd, handler):
        p = QProcess(QApplication.instance())
        buf = [""]

        def read():
            *lines, buf[0] = (buf[0] + bytes(p.readAllStandardOutput()).decode(errors="replace")).split("\n")
            for line in lines:
                try:
                    handler(line.strip())
                except (ValueError, IndexError):
                    pass  # ignore a malformed line rather than lose the stream
        p.readyReadStandardOutput.connect(read)
        p.start(cmd[0], cmd[1:])
        self.procs.append(p)

    def on_net(self, line):
        # "HH:MM:SS" alone, or "HH:MM:SS|PID|<pid>|IN|<bytes>|OUT|<bytes>" for each process with traffic that second
        ts = line.split("|")[0]
        if ts != self._net_ts:
            self._net, self._net_next, self._net_ts, self._net_at = self._net_next, {}, ts, time.monotonic()
        f = line.split("|")
        if len(f) == 7 and f[1] == "PID":
            self._net_next[int(f[2])] = (int(f[4]), int(f[6]))

    def net(self):
        """pid -> bits/s (in + out) over the last full second."""
        if time.monotonic() - self._net_at > 3:
            return {}
        return {pid: (i + o) * 8 for pid, (i, o) in self._net.items()}

    def on_pmon(self, line):
        # "# gpu pid type sm mem ..." header, then "    0  15372  C  12  3 ... llama-server"
        if line and not line.startswith("#"):
            gpu, pid, typ, sm = line.split()[:4]
            if pid != "-":
                self.nv_proc[int(pid)] = (gpu, typ, 0.0 if sm == "-" else float(sm), time.monotonic())

    def on_nvquery(self, line):
        idx, util, used, total, temp = (x.strip() for x in line.split(","))
        self.nv[idx] = dict(util=float(util), used=int(used) * 2**20, total=int(total) * 2**20, temp=temp)

    def gpu_totals(self):
        out = []
        for g in gpus():
            if g["kind"] == "nvidia":
                out.append(self.nv.get(g["key"], dict(util=0.0, used=0, total=0, temp="")))
            else:
                d = g["key"]
                temp = read_num(next(iter(glob.glob(f"{d}/hwmon/hwmon*/temp1_input")), ""), None)
                out.append(dict(util=float(read_num(f"{d}/gpu_busy_percent")), used=read_num(f"{d}/mem_info_vram_used"),
                                total=read_num(f"{d}/mem_info_vram_total"), temp=str(temp // 1000) if temp else ""))
        return out

    def gpu_procs(self, pids):
        """pid -> (utilization %, 'GPU n - engine'), from nvidia-smi pmon and DRM fdinfo (AMD, own processes only)."""
        now, order = time.monotonic(), {(g["kind"], g["key"] if g["kind"] == "nvidia" else g["bus"]): i
                                         for i, g in enumerate(gpus())}
        out = {}
        for pid, (gpu, typ, sm, t) in list(self.nv_proc.items()):
            if now - t > 3:
                del self.nv_proc[pid]
            else:
                out[pid] = (sm, f"GPU {order.get(('nvidia', gpu), gpu)} - {'3D' if 'G' in typ else 'Compute'}")
        if not any(k == "amd" for k, _ in order):
            return out
        cur = {}
        for pid in pids:
            try:
                fds = os.listdir(f"/proc/{pid}/fd")
            except OSError:
                continue
            seen = set()
            for fd in fds:
                try:
                    if not os.readlink(f"/proc/{pid}/fd/{fd}").startswith("/dev/dri/"):
                        continue
                    kv = dict(l.split(":", 1) for l in open(f"/proc/{pid}/fdinfo/{fd}").read().splitlines() if ":" in l)
                except OSError:
                    continue
                cid, bus = kv.get("drm-client-id", "").strip(), kv.get("drm-pdev", "").strip()
                if ("amd", bus) not in order or cid in seen:
                    continue  # several fds can share one DRM client
                seen.add(cid)
                for k, v in kv.items():
                    if k.startswith("drm-engine-") and v.strip().endswith("ns"):
                        cur[(pid, cid, bus, k[11:])] = int(v.split()[0])
        for key, ns in cur.items():
            if key in self.amd_prev:
                pns, pt = self.amd_prev[key]
                pct = min(100.0, (ns - pns) / max(now - pt, 1e-3) / 1e7)
                pid, _, bus, eng = key
                if pct > out.get(pid, (-1,))[0]:
                    out[pid] = (pct, f"GPU {order[('amd', bus)]} - {AMD_ENGINES.get(eng, eng)}")
        self.amd_prev = {k: (ns, now) for k, ns in cur.items()}
        return out


_helpers = []


def helpers():
    if not _helpers:
        _helpers.append(Helpers())
    return _helpers[0]


class Sampler:
    """One snapshot of system-wide rates per tick, shared by every view."""

    def __init__(self):
        self.disks = physical_disks()
        self.t = time.monotonic()
        self.disk = psutil.disk_io_counters(perdisk=True) or {}
        self.net = psutil.net_io_counters(pernic=True)
        self.peak_commit = 0
        psutil.cpu_percent(percpu=True)

    def sample(self):
        now = time.monotonic()
        dt = max(now - self.t, 1e-3)
        s = SimpleNamespace()
        s.cpu = psutil.cpu_percent(percpu=True)
        s.cpu_total = sum(s.cpu) / len(s.cpu)
        s.vm, s.swap, s.mem = psutil.virtual_memory(), psutil.swap_memory(), meminfo()
        self.peak_commit = max(self.peak_commit, s.mem["Committed_AS"])
        s.peak_commit = self.peak_commit
        s.handles = int(open("/proc/sys/fs/file-nr").read().split()[0])  # allocated file handles, system-wide
        procs = list(psutil.process_iter(["num_threads"]))
        s.nprocs, s.nthreads = len(procs), sum(p.info["num_threads"] or 0 for p in procs)
        disk = psutil.disk_io_counters(perdisk=True) or {}
        s.disks = {}
        for d in self.disks:
            if d in disk and d in self.disk:
                a, b = self.disk[d], disk[d]
                ops = b.read_count + b.write_count - a.read_count - a.write_count
                s.disks[d] = dict(active=min(100.0, (b.busy_time - a.busy_time) / (dt * 10)),
                                  read=(b.read_bytes - a.read_bytes) / dt, write=(b.write_bytes - a.write_bytes) / dt,
                                  resp=(b.read_time + b.write_time - a.read_time - a.write_time) / ops if ops > 0 else 0)
        net, stats = psutil.net_io_counters(pernic=True), psutil.net_if_stats()
        s.nets = {}
        for nic, st in stats.items():
            if nic != "lo" and nic in net and nic in self.net:
                a, b = self.net[nic], net[nic]
                s.nets[nic] = dict(sent=(b.bytes_sent - a.bytes_sent) * 8 / dt, recv=(b.bytes_recv - a.bytes_recv) * 8 / dt,
                                   speed=st.speed, up=st.isup)
        s.gpus = helpers().gpu_totals()
        self.t, self.disk, self.net = now, disk, net
        return s


# ---------------------------------------------------------------- painted icons

def _pix(size=16):
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    return pm, p


@lru_cache(maxsize=None)
def uac_shield():
    """The Vista/7 UAC shield: blue/yellow quarters."""
    pm, p = _pix()
    path = QPainterPath()
    path.moveTo(8, 1)
    path.cubicTo(10, 2.5, 12.5, 3, 14.5, 3)
    path.cubicTo(14.5, 9, 12, 13, 8, 15)
    path.cubicTo(4, 13, 1.5, 9, 1.5, 3)
    path.cubicTo(3.5, 3, 6, 2.5, 8, 1)
    p.setClipPath(path)
    blue, yellow = QColor(28, 86, 180), QColor(248, 200, 30)
    p.fillRect(0, 0, 8, 8, blue)
    p.fillRect(8, 8, 8, 8, blue)
    p.fillRect(8, 0, 8, 8, yellow)
    p.fillRect(0, 8, 8, 8, yellow)
    p.setClipping(False)
    p.setPen(QPen(QColor(40, 50, 80), 0.8))
    p.drawPath(path)
    p.end()
    return QIcon(pm)


@lru_cache(maxsize=None)
def details_icon(up):
    """Win10 'Fewer details' / 'More details' circled chevron."""
    pm, p = _pix(18)
    p.setPen(QPen(QColor(110, 110, 110), 1))
    p.drawEllipse(QRectF(1, 1, 16, 16))
    y0, y1 = (11, 7) if up else (7, 11)
    p.drawPolyline(QPolygonF([QPointF(5, y0), QPointF(9, y1), QPointF(13, y0)]))
    p.end()
    return QIcon(pm)


@lru_cache(maxsize=None)
def arrow_png(up, color="#97a5b5"):
    """Tiny sort-arrow image for Win7's header (QSS needs a file)."""
    d = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"arrow_{'up' if up else 'down'}.png")
    pm, p = _pix(9)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawPolygon(QPolygonF([QPointF(0.5, 6), QPointF(4.5, 2), QPointF(8.5, 6)] if up else
                            [QPointF(0.5, 2), QPointF(4.5, 6), QPointF(8.5, 2)]))
    p.end()
    pm.save(path)
    return path


# ---------------------------------------------------------------- shared widgets

def sync(parent, rows, key_col=0, right_cols=()):
    """Update a QStandardItem's children in place. rows: {key: [(text, sort, icon[, bg]), ...]}. Returns {key: item}."""
    for r in reversed(range(parent.rowCount())):
        if parent.child(r, key_col).data(KEY) not in rows:
            parent.removeRow(r)
    existing = {parent.child(r, key_col).data(KEY): r for r in range(parent.rowCount())}
    for key, cells in rows.items():
        if key not in existing:
            items = [QStandardItem() for _ in cells]
            items[key_col].setData(key, KEY)
            for c in right_cols:
                items[c].setTextAlignment(RIGHT)
            parent.appendRow(items)
            existing[key] = parent.rowCount() - 1
        r = existing[key]
        for c, (text, sort, icon, *bg) in enumerate(cells):
            it = parent.child(r, c)
            if it.text() != text:
                it.setText(text)
            if it.data(SORT) != sort:
                it.setData(sort, SORT)
            if icon is not None and it.icon().isNull():
                it.setIcon(icon)
            if bg:
                it.setBackground(bg[0])
    return {k: parent.child(r, key_col) for k, r in existing.items()}


def setup_view(view, headers, proxy=None):
    """Shared model/proxy/selection setup for QTableView and QTreeView."""
    view.model_ = QStandardItemModel(0, len(headers))
    view.model_.setHorizontalHeaderLabels(headers)
    view.proxy = proxy or QSortFilterProxyModel()
    view.proxy.setSourceModel(view.model_)
    view.proxy.setSortRole(SORT)
    view.setModel(view.proxy)
    view.setSortingEnabled(True)
    view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    (view.header() if isinstance(view, QTreeView) else view.horizontalHeader()).setStretchLastSection(True)
    view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)


def selected_key(view):
    idx = view.selectionModel().selectedRows(0)
    if not idx:
        return None
    src = view.proxy.mapToSource(idx[0])
    return view.model_.itemFromIndex(src.siblingAtColumn(view.key_col)).data(KEY)


class Table(QTableView):
    def __init__(self, headers, key_col=0, right_cols=(), header=None, row_height=4):
        super().__init__()
        if header:
            self.setHorizontalHeader(header)
        setup_view(self, headers)
        self.key_col, self.right_cols = key_col, right_cols
        self.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + row_height)
        self.setShowGrid(False)
        self.setWordWrap(False)

    def sync(self, rows):
        sync(self.model_.invisibleRootItem(), rows, self.key_col, self.right_cols)

    def selected_key(self):
        return selected_key(self)

    def widths(self, *ws):
        for c, w in enumerate(ws):
            self.horizontalHeader().resizeSection(c, w)


def bar(*widgets, left=()):
    """A row of buttons (right-aligned) as a widget, so Win10 can move it into its footer."""
    w = QWidget()
    row = QHBoxLayout(w)
    row.setContentsMargins(0, 0, 0, 0)
    for x in left:
        row.addWidget(x)
    row.addStretch()
    for x in widgets:
        row.addWidget(x)
    return w


def button(text, slot, icon=None):
    b = QPushButton(text)
    if icon:
        b.setIcon(icon)
    b.clicked.connect(slot)
    return b


def link(text, slot, icon=None):
    b = QPushButton(text)
    b.setFlat(True)
    b.setObjectName("link")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if icon:
        b.setIcon(icon)
    b.clicked.connect(slot)
    return b


class ColumnChooser:
    """Right-click a header to show/hide columns; also the 'Select Columns' dialog. Remembers visible columns per view."""

    def __init__(self, view, key, default_visible, on_change=None):
        self.view, self.key, self.on_change = view, key, on_change
        self.settings = QSettings("ktaskmanager", "ktaskmanager")
        self.header = view.header() if isinstance(view, QTreeView) else view.horizontalHeader()
        self.names = [view.model_.headerData(c, Qt.Orientation.Horizontal) for c in range(view.model_.columnCount())]
        saved, known = self.settings.value(key), self.settings.value(key + "Known")
        as_set = lambda v: set(v if isinstance(v, list) else [v]) if v else set()
        # columns that didn't exist when the choice was saved get their default visibility
        self.visible = (as_set(saved) | (set(default_visible) - as_set(known))) if saved else set(default_visible)
        self.header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.header.customContextMenuRequested.connect(self.menu)
        self.apply()

    def shown(self, col):
        return col == 0 or self.names[col] in self.visible  # the name column can't be hidden, as on Windows

    def apply(self):
        for c in range(len(self.names)):
            self.header.setSectionHidden(c, not self.shown(c))

    def set_visible(self, visible):
        self.visible = set(visible)
        self.settings.setValue(self.key, [n for n in self.names if n in self.visible])
        self.settings.setValue(self.key + "Known", self.names)
        self.apply()
        if self.on_change:
            self.on_change()

    def menu(self, pos):
        m = QMenu(self.header)
        for n in self.names[1:]:
            a = m.addAction(n)
            a.setCheckable(True)
            a.setChecked(n in self.visible)
            a.toggled.connect(lambda on, n=n: self.set_visible(self.visible | {n} if on else self.visible - {n}))
        m.addSeparator()
        m.addAction("Select columns...", self.dialog)
        m.exec(self.header.mapToGlobal(pos))

    def dialog(self):
        d = QDialog(self.view.window())
        d.setWindowTitle("Select Columns")
        lay = QVBoxLayout(d)
        lay.addWidget(QLabel("Select the columns that will appear on this page."))
        boxes = {}
        for n in self.names[1:]:
            boxes[n] = QCheckBox(n)
            boxes[n].setChecked(n in self.visible)
            lay.addWidget(boxes[n])
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        lay.addWidget(bb)
        if d.exec():
            self.set_visible(n for n, b in boxes.items() if b.isChecked())


def priority_name(nice):
    return ("Realtime" if nice <= -20 else "High" if nice <= -10 else "Above Normal" if nice < 0 else "Normal" if nice == 0
            else "Below Normal" if nice < 10 else "Low")


# per field: psutil attrs it needs, display width
PROC_FIELDS = dict(name=(["name"], 130), pid=([], 55), user=([], 90), status=(["status"], 75), cpu=(["cpu_percent"], 40),
                   cputime=(["cpu_times"], 70), mem=(["memory_info"], 150), ws=(["memory_info"], 130),
                   threads=(["num_threads"], 60), handles=(["num_fds"], 60), nice=(["nice"], 90),
                   ioread=(["io_counters"], 70), iowrite=(["io_counters"], 70), path=(["exe"], 220),
                   cmd=(["cmdline"], 300), desc=(["name", "cmdline"], 200))
NUMERIC = {"pid", "cpu", "cputime", "mem", "ws", "threads", "handles", "ioread", "iowrite"}


class Processes(QWidget):
    """Process list: Win2000/Win7 Processes tab and Win10 Details tab.

    cols: [(header, field, shown_by_default)]; only the visible columns' data is fetched.
    all_users: "checkbox" (XP-style), "button" (Win7 UAC button) or "always".
    """

    def __init__(self, cols, key, all_users="always", end_text="&End Process", header=None, confirm=True):
        super().__init__()
        self.cols, self.confirm = cols, confirm
        self.table = Table([h for h, *_ in cols], right_cols=tuple(i for i, (_, f, _) in enumerate(cols) if f in NUMERIC),
                           header=header)
        self.table.widths(*(PROC_FIELDS[f][1] for _, f, _ in cols))
        self.table.customContextMenuRequested.connect(self.menu)
        self.chooser = ColumnChooser(self.table, key, [h for h, _, on in cols if on], self.refresh)
        self.end_btn = button(end_text, lambda: self.end(False))
        self.show_all = all_users == "always"
        left = []
        if all_users == "checkbox":
            self.all_box = QCheckBox("&Show processes from all users")
            self.all_box.toggled.connect(self.set_all)
            left = [self.all_box]
        elif all_users == "button":
            self.all_box = button("&Show processes from all users", lambda: self.set_all(True), uac_shield())
            left = [self.all_box]
        lay = QVBoxLayout(self)
        lay.addWidget(self.table)
        self.bar = bar(self.end_btn, left=left)
        lay.addWidget(self.bar)
        self.me = psutil.Process().username()
        self.ncpu = psutil.cpu_count() or 1

    def set_all(self, on):
        self.show_all = on
        if isinstance(self.all_box, QPushButton):
            self.all_box.setEnabled(not on)
        self.refresh()

    def value(self, f, pid, i, names):
        if f == "name":
            return i["name"], i["name"].lower()
        if f == "pid":
            return str(pid), pid
        if f == "user":
            return i["username"] or "", i["username"] or ""
        if f == "status":
            s = "Suspended" if i["status"] == psutil.STATUS_STOPPED else "Running"
            return s, s
        if f == "cpu":
            cpu = round((i["cpu_percent"] or 0) / self.ncpu)
            return f"{cpu:02}", cpu
        if f == "cputime":
            ct = sum(i["cpu_times"][:2]) if i["cpu_times"] else 0
            return fmt_hms(ct), ct
        if f in ("mem", "ws"):
            mi = i["memory_info"]
            n = 0 if not mi else mi.rss - mi.shared if f == "mem" else mi.rss  # private ~ rss minus shared pages
            return f"{n // 1024:,} K", n
        if f in ("threads", "handles"):
            n = i["num_threads" if f == "threads" else "num_fds"]
            return ("", -1) if n is None else (f"{n:,}", n)
        if f == "nice":
            return ("", 99) if i["nice"] is None else (priority_name(i["nice"]), i["nice"])
        if f in ("ioread", "iowrite"):
            io = i["io_counters"]
            n = (io.read_count if f == "ioread" else io.write_count) if io else -1
            return ("", -1) if n < 0 else (f"{n:,}", n)
        if f == "path":
            return i["exe"] or "", i["exe"] or ""
        cmd = i["cmdline"] or []
        if f == "cmd":
            return " ".join(cmd), " ".join(cmd)
        desc = names.get(i["name"]) or (names.get(os.path.basename(cmd[0])) if cmd else None) or " ".join(cmd)
        return desc, desc

    def refresh(self):
        shown = [self.chooser.shown(c) for c in range(len(self.cols))]
        attrs = {"username"} | {a for (_, f, _), on in zip(self.cols, shown) if on for a in PROC_FIELDS[f][0]}
        rows, names = {}, app_names()
        for p in psutil.process_iter(list(attrs)):
            if not self.show_all and p.info["username"] != self.me:
                continue
            rows[p.pid] = [(*(self.value(f, p.pid, p.info, names) if on else ("", 0)), None)
                           for (_, f, _), on in zip(self.cols, shown)]
        self.table.sync(rows)

    def end(self, tree):
        if (pid := self.table.selected_key()) is not None:
            end_process(self, pid, tree, self.confirm)
            self.refresh()

    def set_priority(self, pid, nice):
        try:
            psutil.Process(pid).nice(nice)
        except psutil.AccessDenied:
            QMessageBox.critical(self, "Unable to Change Priority", "The operation could not be completed.\n\nAccess is denied.")
        except psutil.NoSuchProcess:
            pass

    def menu(self, pos):
        pid = self.table.selected_key()
        if pid is None:
            return
        m = QMenu(self)
        m.addAction("&Open File Location", lambda: open_file_location(pid))
        m.addSeparator()
        m.addAction("&End Process", lambda: self.end(False))
        m.addAction("End Process &Tree", lambda: self.end(True))
        m.addSeparator()
        pri = m.addMenu("Set &Priority")
        try:
            cur = psutil.Process(pid).nice()
        except psutil.Error:
            cur = None
        for name, nice in (("&Realtime", -20), ("&High", -10), ("&Above Normal", -5), ("&Normal", 0), ("&Below Normal", 5),
                           ("&Low", 19)):
            a = pri.addAction(name, lambda n=nice: self.set_priority(pid, n))
            a.setCheckable(True)
            a.setChecked(cur == nice)
        m.exec(self.table.viewport().mapToGlobal(pos))


class Services(QWidget):
    slow = True  # spawns systemctl: refresh on tab switch / F5 only

    def __init__(self, header=None):
        super().__init__()
        self.table = Table(["Name", "PID", "Description", "Status", "Group"], right_cols=(1,), header=header)
        self.table.widths(200, 55, 300, 70)
        self.table.customContextMenuRequested.connect(self.menu)
        QVBoxLayout(self).addWidget(self.table)

    def refresh(self):
        units = json.loads(subprocess.run(["systemctl", "list-units", "--type=service", "--all", "-o", "json"],
                                          capture_output=True, text=True).stdout or "[]")
        show = subprocess.run(["systemctl", "show", "*.service", "-p", "Id,MainPID,Slice"], capture_output=True, text=True).stdout
        extra = {}
        for block in show.split("\n\n"):
            kv = dict(l.split("=", 1) for l in block.splitlines() if "=" in l)
            extra[kv.get("Id")] = kv
        rows = {}
        for u in units:
            x = extra.get(u["unit"], {})
            pid = int(x.get("MainPID") or 0)
            status = "Running" if u["sub"] == "running" else "Stopped" if u["active"] == "inactive" else u["sub"].capitalize()
            name = u["unit"].removesuffix(".service")
            group = x.get("Slice", "").removesuffix(".slice")
            rows[u["unit"]] = [(name, name.lower(), None), (str(pid) if pid else "", pid, None),
                               (u["description"], u["description"], None), (status, status, None), (group, group, None)]
        self.table.sync(rows)

    def ctl(self, verb):
        # polkit prompts for authentication when needed
        if unit := self.table.selected_key():
            subprocess.run(["systemctl", verb, unit])
            self.refresh()

    def menu(self, pos):
        if not self.table.selected_key():
            return
        m = QMenu(self)
        m.addAction("&Start Service", lambda: self.ctl("start"))
        m.addAction("S&top Service", lambda: self.ctl("stop"))
        m.addAction("&Restart Service", lambda: self.ctl("restart"))
        m.exec(self.table.viewport().mapToGlobal(pos))


def logoff():
    QProcess.startDetached("qdbus6", ["org.kde.LogoutPrompt", "/LogoutPrompt", "promptLogout"])


class Users7(QWidget):
    def __init__(self):
        super().__init__()
        self.table = Table(["User", "ID", "Status", "Client Name", "Session"], right_cols=(1,))
        self.table.widths(120, 40, 70, 110)
        lay = QVBoxLayout(self)
        lay.addWidget(self.table)
        disconnect, send = QPushButton("&Disconnect"), QPushButton("Send &Message...")
        disconnect.setEnabled(False)
        send.setEnabled(False)
        lay.addWidget(bar(disconnect, button("&Logoff", logoff), send))

    def refresh(self):
        rows = {}
        for n, u in enumerate(psutil.users(), start=1):
            session = "Console" if not u.host else u.terminal or ""
            rows[(u.name, u.terminal)] = [(u.name, u.name, QIcon.fromTheme("user-identity")), (str(n), n, None),
                                          ("Active", 0, None), (u.host or "", u.host or "", None), (session, session, None)]
        self.table.sync(rows)


# ---------------------------------------------------------------- Windows 2000 / 7 widgets

GREEN, GRID7, BLACK = QColor(0, 255, 0), QColor(0, 128, 64), QColor(0, 0, 0)


class Graph7(QWidget):
    """Scrolling green-on-black history graph. Values 0..100 (or autoscaled)."""

    def __init__(self, autoscale=False, color=GREEN):
        super().__init__()
        self.data, self.autoscale, self.color, self.ticks = deque([0.0] * HISTORY, HISTORY), autoscale, color, 0
        self.setMinimumSize(16, 16)

    def push(self, v):
        self.data.append(v)
        self.ticks += 1
        self.update()

    def top(self):
        return nice_ceiling(max(self.data), 0.5) if self.autoscale else 100.0

    def paintEvent(self, _):
        p = QPainter(self)
        w, h, cell = self.width(), self.height(), 12
        p.fillRect(self.rect(), BLACK)
        p.setPen(QPen(GRID7, 1))
        x = w - (self.ticks * w / HISTORY) % cell
        while x > 0:
            p.drawLine(int(x), 0, int(x), h)
            x -= cell
        for y in range(h - cell, 0, -cell):
            p.drawLine(0, y, w, y)
        top, n = self.top(), len(self.data)
        p.setPen(QPen(self.color, 1))
        p.drawPolyline(QPolygonF([QPointF(w * i / (n - 1), h - 1 - (h - 2) * min(v, top) / top) for i, v in enumerate(self.data)]))
        if self.autoscale:
            p.setPen(GREEN)
            p.drawText(4, 12, f"{top:g} %")


class Meter(QWidget):
    """LED bar meter with a caption underneath."""

    def __init__(self):
        super().__init__()
        self.value, self.text = 0.0, ""
        self.setFixedWidth(70)
        self.setMinimumHeight(70)

    def set(self, value, text):
        self.value, self.text = value, text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), BLACK)
        bar_h = h - 20
        rows = bar_h // 3
        lit = round(rows * self.value / 100)
        for r in range(rows):
            y = bar_h - (r + 1) * 3 + 4
            c = GREEN if r < lit else GRID7
            p.fillRect(14, y, w // 2 - 15, 2, c)
            p.fillRect(w // 2 + 1, y, w // 2 - 15, 2, c)
        p.setPen(GREEN)
        p.drawText(0, h - 17, w, 15, Qt.AlignmentFlag.AlignCenter, self.text)


def boxed(title, w):
    box = QGroupBox(title)
    QVBoxLayout(box).addWidget(w)
    return box


class Applications(QWidget):
    def __init__(self):
        super().__init__()
        self.table = Table(["Task", "Status"])
        self.table.widths(280)
        self.table.doubleClicked.connect(lambda: self.run(0))
        self.table.customContextMenuRequested.connect(self.menu)
        lay = QVBoxLayout(self)
        lay.addWidget(self.table)
        lay.addWidget(bar(button("&End Task", lambda: self.run(1)), button("&Switch To", lambda: self.run(0)),
                          button("&New Task...", lambda: self.window().new_task())))

    def refresh(self):
        self.table.sync({w["uuid"]: [(w["caption"], w["caption"].lower(), w["icon"]), ("Running", 0, None)]
                         for w in kwin_windows()})

    def run(self, action):
        if uuid := self.table.selected_key():
            kwin_window_action(uuid, action)
            if action == 0 and self.window().minimize_on_use.isChecked():
                self.window().showMinimized()

    def menu(self, pos):
        if not self.table.selected_key():
            return
        m = QMenu(self)
        m.addAction("&Switch To", lambda: self.run(0))
        m.addAction("&Minimize", lambda: self.run(2))
        m.addAction("Ma&ximize", lambda: self.run(3))
        m.addSeparator()
        m.addAction("&End Task", lambda: self.run(1))
        m.exec(self.table.viewport().mapToGlobal(pos))


class Performance7(QWidget):
    """Win7 Performance tab, or Win2000's when classic=True (different labels, units and memory line colour)."""

    def __init__(self, classic=False):
        super().__init__()
        self.classic = classic
        self.cpu_meter, self.mem_meter = Meter(), Meter()
        ncpu = psutil.cpu_count() or 1
        self.cpu_graphs = [Graph7() for _ in range(ncpu)]
        self.cpu_one = Graph7()
        self.mem_graph = Graph7(color=QColor(255, 255, 0) if classic else QColor(42, 143, 230))

        self.per_cpu_box = QWidget()
        cpu_grid = QGridLayout(self.per_cpu_box)
        cpu_grid.setContentsMargins(0, 0, 0, 0)
        cpu_grid.setSpacing(4)
        cols = (ncpu + 1) // 2 if ncpu > 4 else ncpu
        for i, g in enumerate(self.cpu_graphs):
            cpu_grid.addWidget(g, i // cols, i % cols)
        cpu_hist = QWidget()
        hl = QVBoxLayout(cpu_hist)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.addWidget(self.per_cpu_box)
        hl.addWidget(self.cpu_one)

        grid = QGridLayout()
        grid.addWidget(boxed("CPU Usage", self.cpu_meter), 0, 0)
        grid.addWidget(boxed("CPU Usage History", cpu_hist), 0, 1)
        grid.addWidget(boxed("MEM Usage" if classic else "Memory", self.mem_meter), 1, 0)
        grid.addWidget(boxed("Memory Usage History" if classic else "Physical Memory Usage History", self.mem_graph), 1, 1)
        grid.setColumnStretch(1, 1)

        self.labels = {}

        def stats(title, names):
            box = QGroupBox(title)
            g = QGridLayout(box)
            g.setVerticalSpacing(0)
            for r, n in enumerate(names):
                g.addWidget(QLabel(n), r, 0)
                self.labels[f"{title}/{n}"] = QLabel()
                self.labels[f"{title}/{n}"].setAlignment(Qt.AlignmentFlag.AlignRight)
                g.addWidget(self.labels[f"{title}/{n}"], r, 1)
            return box

        bottom = QGridLayout()
        if classic:
            bottom.addWidget(stats("Totals", ["Handles", "Threads", "Processes"]), 0, 0)
            bottom.addWidget(stats("Physical Memory (K)", ["Total", "Available", "System Cache"]), 0, 1)
            bottom.addWidget(stats("Commit Charge (K)", ["Total", "Limit", "Peak"]), 1, 0)
            bottom.addWidget(stats("Kernel Memory (K)", ["Total", "Paged", "Nonpaged"]), 1, 1)
        else:
            bottom.addWidget(stats("Physical Memory (MB)", ["Total", "Cached", "Available", "Free"]), 0, 0)
            bottom.addWidget(stats("Kernel Memory (MB)", ["Paged", "Nonpaged"]), 1, 0)
            bottom.addWidget(stats("System", ["Handles", "Threads", "Processes", "Up Time", "Commit (MB)"]), 0, 1)
            bottom.addWidget(bar(left=[button("&Resource Monitor...", lambda: QProcess.startDetached("plasma-systemmonitor", []),
                                              uac_shield())]), 1, 1, Qt.AlignmentFlag.AlignTop)
        bottom.setColumnStretch(0, 1)
        bottom.setColumnStretch(1, 1)
        lay = QVBoxLayout(self)
        lay.addLayout(grid, 1)
        lay.addLayout(bottom)

    def set_per_cpu(self, on):
        self.per_cpu_box.setVisible(on)
        self.cpu_one.setVisible(not on)

    def update_from(self, s):
        for g, v in zip(self.cpu_graphs, s.cpu):
            g.push(v)
        self.cpu_one.push(s.cpu_total)
        self.cpu_meter.set(s.cpu_total, f"{round(s.cpu_total)} %")
        vm, m = s.vm, s.mem
        commit, limit = m["Committed_AS"], m["CommitLimit"]
        cached = m["Cached"] + m["Buffers"]
        paged, nonpaged = m["SReclaimable"], m["SUnreclaim"]
        if self.classic:
            self.mem_graph.push(commit / limit * 100)
            self.mem_meter.set(commit / limit * 100, f"{commit // 1024}K")
            k = lambda n: str(n // 1024)
            v = {"Totals/Handles": str(s.handles), "Totals/Threads": str(s.nthreads), "Totals/Processes": str(s.nprocs),
                 "Physical Memory (K)/Total": k(vm.total), "Physical Memory (K)/Available": k(vm.available),
                 "Physical Memory (K)/System Cache": k(cached), "Commit Charge (K)/Total": k(commit),
                 "Commit Charge (K)/Limit": k(limit), "Commit Charge (K)/Peak": k(s.peak_commit),
                 "Kernel Memory (K)/Total": k(paged + nonpaged), "Kernel Memory (K)/Paged": k(paged),
                 "Kernel Memory (K)/Nonpaged": k(nonpaged)}
        else:
            used = vm.total - vm.available
            self.mem_graph.push(vm.percent)
            self.mem_meter.set(vm.percent, f"{used / 2**30:.2f} GB" if used >= 2**30 else f"{used // 2**20} MB")
            mb = lambda n: str(n // 2**20)
            v = {"Physical Memory (MB)/Total": mb(vm.total), "Physical Memory (MB)/Cached": mb(cached),
                 "Physical Memory (MB)/Available": mb(vm.available), "Physical Memory (MB)/Free": mb(vm.free),
                 "Kernel Memory (MB)/Paged": mb(paged), "Kernel Memory (MB)/Nonpaged": mb(nonpaged),
                 "System/Handles": str(s.handles), "System/Threads": str(s.nthreads), "System/Processes": str(s.nprocs),
                 "System/Up Time": fmt_uptime(time.time() - psutil.boot_time()), "System/Commit (MB)": f"{mb(commit)} / {mb(limit)}"}
        for key, l in self.labels.items():
            l.setText(v[key])


class Networking(QWidget):
    def __init__(self):
        super().__init__()
        self.graphs = {}
        self.graph_box = QHBoxLayout()
        self.table = Table(["Adapter Name", "Network Utilization", "Link Speed", "State"], right_cols=(1, 2))
        self.table.widths(200, 140, 90)
        lay = QVBoxLayout(self)
        lay.addLayout(self.graph_box, 1)
        lay.addWidget(self.table, 1)

    def update_from(self, s):
        rows = {}
        for nic, n in s.nets.items():
            # ponytail: Wi-Fi reports speed 0 on Linux; assume 1 Gbps so the graph still moves
            util = min(100.0, (n["sent"] + n["recv"]) / ((n["speed"] or 1000) * 1e6) * 100)
            if nic not in self.graphs:
                self.graphs[nic] = Graph7(autoscale=True)
                self.graph_box.addWidget(boxed(nic, self.graphs[nic]))
            self.graphs[nic].push(util)
            state = "Connected" if n["up"] else "Disconnected"
            speed = f"{n['speed'] // 1000} Gbps" if n["speed"] >= 1000 else f"{n['speed']} Mbps" if n["speed"] else "Unknown"
            rows[nic] = [(nic, nic, None), (f"{util:.2f} %", util, None), (speed, n["speed"], None), (state, state, None)]
        self.table.sync(rows)


# ---------------------------------------------------------------- Windows 10 widgets

CPU10, MEM10, DISK10, NET10 = QColor(17, 125, 187), QColor(139, 18, 174), QColor(77, 166, 12), QColor(167, 79, 1)
# Colours used by the Win10-layout widgets; Win10 uses these, the native style derives them from the KDE palette.
WIN10_THEME = SimpleNamespace(base=QColor(255, 255, 255), text=QColor(0, 0, 0), grey=QColor(112, 112, 112),
                              sep=QColor(229, 229, 229), header_text=QColor(76, 96, 122), accent=QColor(30, 100, 180),
                              heat_lo=QColor(255, 244, 196), heat_hi=QColor(255, 167, 62))
T = WIN10_THEME


def blend(c, other, t):
    return QColor(*(round(a + (b - a) * t) for a, b in zip(c.getRgb()[:3], other.getRgb()[:3])))


def heat(frac):
    return blend(T.heat_lo, T.heat_hi, max(0.0, min(1.0, frac)))


def font(pt, weight=None):
    f = QFont(QApplication.font())
    f.setPointSizeF(pt)
    if weight:
        f.setWeight(weight)
    return f


class Header10(QHeaderView):
    """Win10 header: text bottom-left, sort chevron top-centre, big 'NN%' totals over resource columns."""

    def __init__(self, tall=False):
        super().__init__(Qt.Orientation.Horizontal)
        self.totals, self.tall = {}, tall
        self.setSectionsClickable(True)
        self.setSortIndicatorShown(True)
        self.setHighlightSections(False)

    def sizeHint(self):
        s = super().sizeHint()
        s.setHeight(48 if self.tall else 30)
        return s

    def paintSection(self, p, rect, col):
        p.save()
        pct = self.totals.get(col)
        p.fillRect(rect, T.base if pct is None or pct < 50 else heat((pct - 50) / 50))
        p.setPen(T.sep)
        p.drawLine(rect.topRight(), rect.bottomRight())
        r = rect.adjusted(6, 2, -6, -4)
        text = str(self.model().headerData(col, Qt.Orientation.Horizontal) or "")
        if pct is not None:
            p.setFont(font(12.5))
            p.setPen(T.text)
            p.drawText(r, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, f"{round(pct)}%")
            p.setFont(self.font())
            p.setPen(T.header_text)
            p.drawText(r, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, text)
        else:
            p.setPen(T.header_text)
            p.drawText(r, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, text)
        if self.sortIndicatorSection() == col:
            cx, y = rect.center().x(), rect.top() + 3
            up = self.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QPen(T.grey, 1))
            p.drawPolyline(QPolygonF([QPointF(cx - 4, y + 4), QPointF(cx, y), QPointF(cx + 4, y + 4)] if up else
                                     [QPointF(cx - 4, y), QPointF(cx, y + 4), QPointF(cx + 4, y)]))
        p.restore()


def heat_cell(text, value, frac):
    return (text, value, None, heat(frac))


def pct10(v):
    return "0%" if v < 0.05 else f"{v:.1f}%"


def mb10(n):
    return f"{n / 2**20:.1f} MB"


def mbps10(bps):
    return "0 Mbps" if bps < 0.05e6 else f"{bps / 1e6:.1f} Mbps"


def rate10(n):
    return "0 MB/s" if n < 0.05 * 2**20 else f"{n / 2**20:.1f} MB/s"


class GroupsFirst(QSortFilterProxyModel):
    """Keep the Apps / Background / System groups in fixed order regardless of sort direction."""

    def lessThan(self, a, b):
        if not a.parent().isValid():
            return (a.row() < b.row()) != (self.sortOrder() == Qt.SortOrder.DescendingOrder)
        return super().lessThan(a, b)


class Processes10(QWidget):
    GROUPS = ("Apps", "Background processes", "System processes")
    HEAD = ["Name", "Type", "Status", "PID", "Process name", "Command line", "CPU", "Memory", "Disk", "Network", "GPU",
            "GPU engine"]
    RES = (6, 7, 8, 9, 10)  # CPU, Memory, Disk, Network, GPU: heat-shaded, with totals in the header

    def __init__(self):
        super().__init__()
        self.tree = QTreeView()
        self.header = Header10(tall=True)
        self.tree.setHeader(self.header)
        setup_view(self.tree, self.HEAD, GroupsFirst())
        self.tree.key_col = 0
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setIconSize(QSize(16, 16))
        self.tree.customContextMenuRequested.connect(self.menu)
        root = self.tree.model_.invisibleRootItem()
        self.groups = []
        for g in self.GROUPS:
            items = [QStandardItem(g)] + [QStandardItem() for _ in self.HEAD[1:]]
            items[0].setFont(font(11.5))
            items[0].setForeground(T.accent)
            for c in self.RES:
                items[c].setBackground(T.heat_lo)
            root.appendRow(items)
            self.groups.append(items[0])
        for c, w in enumerate((280, 130, 90, 60, 130, 250, 75, 90, 80, 80, 65)):
            self.tree.header().resizeSection(c, w)
        self.chooser = ColumnChooser(self.tree, "Windows 10/columns/processes",
                                     ["Status", "CPU", "Memory", "Disk", "Network", "GPU", "GPU engine"])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.tree)
        self.bar = bar(button("&End task", self.end))
        self.me = psutil.Process().username()
        self.ncpu = psutil.cpu_count() or 1
        self.io, self.t, self.expanded = {}, time.monotonic(), False

    def update_header(self, s):
        disk = max((d["active"] for d in s.disks.values()), default=0)
        # network: busiest adapter's share of its link speed (Wi-Fi reports no speed; assume 1 Gbps)
        net = max(((n["sent"] + n["recv"]) / ((n["speed"] or 1000) * 1e6) * 100 for n in s.nets.values() if n["up"]), default=0)
        gpu = max((g["util"] for g in s.gpus), default=0)
        self.header.totals = dict(zip(self.RES, (s.cpu_total, s.vm.percent, disk, net, gpu)))
        self.header.viewport().update()

    def refresh(self):
        now = time.monotonic()
        dt, self.t = max(now - self.t, 1e-3), now
        total = psutil.virtual_memory().total
        info, kids, io = {}, {}, {}
        for p in psutil.process_iter(["name", "username", "cpu_percent", "memory_info", "io_counters", "status", "ppid", "cmdline"]):
            i = p.info
            i["cpu"] = (i["cpu_percent"] or 0) / self.ncpu
            i["mem"] = (i["memory_info"].rss - i["memory_info"].shared) if i["memory_info"] else 0
            i["disk"] = 0.0
            if i["io_counters"]:
                io[p.pid] = i["io_counters"].read_bytes + i["io_counters"].write_bytes
                i["disk"] = max(0.0, (io[p.pid] - self.io.get(p.pid, io[p.pid])) / dt)
            info[p.pid] = i
            kids.setdefault(i["ppid"], []).append(p.pid)
        self.io = io
        net = helpers().net()
        gpu = helpers().gpu_procs([pid for pid, i in info.items() if i["username"] == self.me]) \
            if self.chooser.shown(10) or self.chooser.shown(11) else {}
        for pid, i in info.items():
            i["net"] = net.get(pid, 0)
            i["gpu"], i["engine"] = gpu.get(pid, (0.0, ""))

        def cells(name, icon, kind, pid, cpu, mem, disk, net, gpu, engine):
            i = info[pid]
            status = "Suspended" if i["status"] == psutil.STATUS_STOPPED else ""
            cmd = " ".join(i["cmdline"] or [])
            return [(name, name.lower(), icon), (kind, kind, None), (status, status, None), (str(pid), pid, None),
                    (i["name"], i["name"].lower(), None), (cmd, cmd, None), heat_cell(pct10(cpu), cpu, cpu / 25),
                    heat_cell(mb10(mem), mem, mem / total * 20), heat_cell(rate10(disk), disk, disk / 50e6),
                    heat_cell(mbps10(net), net, net / 1e8), heat_cell(pct10(gpu), gpu, gpu / 50), (engine, engine, None)]

        def proc_cells(pid, kind):
            i = info[pid]
            return cells(i["name"], None, kind, pid, *(i[k] for k in ("cpu", "mem", "disk", "net", "gpu", "engine")))

        # Apps: windowed processes plus their descendants, expandable like Win10's "Google Chrome (20)"
        apps, members, claimed = {}, {}, set()
        wins = kwin_windows()
        windowed = {w["pid"] for w in wins}
        for w in wins:
            pid = w["pid"]
            if pid not in info or pid in claimed:
                continue
            m = app_members(pid, kids, windowed | claimed)
            claimed.update(m)
            members[pid] = m
            name = w["name"] + (f" ({len(m)})" if len(m) > 1 else "")
            engine = next((info[x]["engine"] for x in m if info[x]["engine"]), "")
            apps[pid] = cells(name, w["icon"], "App", pid, *(sum(info[x][k] for x in m) for k in ("cpu", "mem", "disk", "net", "gpu")),
                              engine)
        rows = [apps, {}, {}]
        for pid, i in info.items():
            if pid not in claimed:
                mine = i["username"] == self.me
                rows[1 if mine else 2][pid] = proc_cells(pid, "Background process" if mine else "System process")
        right = (3, *self.RES)
        for g, r, name in zip(self.groups, rows, self.GROUPS):
            g.setText(f"{name} ({len(r)})")
            items = sync(g, r, 0, right_cols=right)
            if g is self.groups[0]:
                for pid, it in items.items():
                    m = members[pid]
                    sync(it, {x: proc_cells(x, "App") for x in m} if len(m) > 1 else {}, 0, right_cols=right)
        if not self.expanded:
            for g in self.groups:
                self.tree.expand(self.tree.proxy.mapFromSource(g.index()))
            self.expanded = True

    def end(self):
        pid = selected_key(self.tree)
        if isinstance(pid, int):
            # Win10 "End task" doesn't ask; on an app it ends the whole app
            item = self.tree.model_.itemFromIndex(self.tree.proxy.mapToSource(self.tree.currentIndex()).siblingAtColumn(0))
            end_process(self, pid, confirm=False, app=item.parent() is self.groups[0])

    def menu(self, pos):
        pid = selected_key(self.tree)
        if not isinstance(pid, int):
            return
        m = QMenu(self)
        m.addAction("&End task", self.end)
        m.addSeparator()
        m.addAction("&Open file location", lambda: open_file_location(pid))
        m.exec(self.tree.viewport().mapToGlobal(pos))


class Graph10(QWidget):
    """Win10 graph: white background, tinted grid, coloured line over a light fill, optional dashed 2nd series."""

    def __init__(self, color, autoscale_floor=None, grid=True, second=False):
        super().__init__()
        self.color, self.floor, self.grid = color, autoscale_floor, grid
        self.data = deque([0.0] * HISTORY, HISTORY)
        self.data2 = deque([0.0] * HISTORY, HISTORY) if second else None
        self.setMinimumSize(40, 30)

    def push(self, v, v2=0.0):
        self.data.append(v)
        if self.data2 is not None:
            self.data2.append(v2)
        self.update()

    def top(self):
        if self.floor is None:
            return 100.0
        return nice_ceiling(max(max(self.data), max(self.data2 or [0])), self.floor)

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), T.base)
        if self.grid:
            p.setPen(blend(self.color, T.base, 0.85))
            for i in range(1, 10):
                p.drawLine(0, h * i // 10, w, h * i // 10)
            for i in range(1, 20):
                p.drawLine(w * i // 20, 0, w * i // 20, h)
        top = self.top()
        pts = lambda d: [QPointF(w * i / (len(d) - 1), h - (h - 1) * min(v, top) / top) for i, v in enumerate(d)]
        line = pts(self.data)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(blend(self.color, T.base, 0.9))
        p.drawPolygon(QPolygonF(line + [QPointF(w, h), QPointF(0, h)]))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(self.color, 1))
        p.drawPolyline(QPolygonF(line))
        if self.data2 is not None:
            p.setPen(QPen(self.color, 1, Qt.PenStyle.DashLine))
            p.drawPolyline(QPolygonF(pts(self.data2)))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setPen(self.color)
        p.drawRect(0, 0, w - 1, h - 1)


class Composition(QWidget):
    """Win10 'Memory composition' bar: in use | cached (standby) | free."""

    def __init__(self):
        super().__init__()
        self.parts = (0, 0, 1)
        self.setFixedHeight(34)

    def set(self, used, cached, free):
        self.parts = (used, cached, free)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        w, h = self.width() - 1, self.height() - 1
        total = sum(self.parts) or 1
        x = 0.0
        for frac, c in zip(self.parts, (blend(MEM10, T.base, 0.75), blend(MEM10, T.base, 0.93), T.base)):
            dx = w * frac / total
            p.fillRect(QRectF(x, 0, dx, h), c)
            p.setPen(MEM10)
            p.drawLine(int(x + dx), 0, int(x + dx), h)
            x += dx
        p.drawRect(0, 0, w, h)


def label(text="", pt=None, grey=False, align=None, weight=None):
    l = QLabel(text)
    if pt:
        l.setFont(font(pt, weight))
    if grey:
        l.setStyleSheet(f"color: {T.grey.name()}")
    if align:
        l.setAlignment(align)
    return l


class Page10(QWidget):
    """One resource page on the Win10 Performance tab."""

    def __init__(self, title, subtitle, graphs, big_rows, small, extra=None):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 6, 16, 6)
        head = QHBoxLayout()
        head.addWidget(label(title, 22, weight=QFont.Weight.Light))
        head.addStretch()
        self.subtitle = label(subtitle, 13, align=RIGHT)
        head.addWidget(self.subtitle)
        lay.addLayout(head)
        self.tops = []
        for caption, g in graphs:
            row = QHBoxLayout()
            row.addWidget(label(caption, grey=True))
            row.addStretch()
            self.tops.append(label(grey=True))
            row.addWidget(self.tops[-1])
            lay.addLayout(row)
            lay.addWidget(g, 1)
            foot = QHBoxLayout()
            foot.addWidget(label("60 seconds", grey=True))
            foot.addStretch()
            foot.addWidget(label("0", grey=True))
            lay.addLayout(foot)
        if extra:
            lay.addWidget(label(extra[0], grey=True))
            lay.addWidget(extra[1])
        self.values = {}
        stats = QHBoxLayout()
        bigs = QGridLayout()
        bigs.setHorizontalSpacing(24)
        for r, names in enumerate(big_rows):
            for c, name in enumerate(names):
                cell = QVBoxLayout()
                cell.setSpacing(0)
                cell.addWidget(label(name, grey=True))
                self.values[name] = label(pt=15)
                cell.addWidget(self.values[name])
                bigs.addLayout(cell, r, c)
        bigs.setColumnStretch(3, 1)
        stats.addLayout(bigs, 3)
        form = QGridLayout()
        form.setVerticalSpacing(2)
        for r, name in enumerate(small):
            form.addWidget(label(name + ":", grey=True), r, 0)
            self.values[name] = label()
            form.addWidget(self.values[name], r, 1)
        form.setRowStretch(len(small), 1)
        stats.addLayout(form, 2)
        lay.addLayout(stats)

    def set(self, values):
        for k, v in values.items():
            self.values[k].setText(v)


class SideItem(QWidget):
    def __init__(self, title, color, autoscale_floor=None, second=False):
        super().__init__()
        self.graph = Graph10(color, autoscale_floor, grid=False, second=second)
        self.graph.setFixedSize(66, 46)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.addWidget(self.graph)
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addWidget(label(title, 13))
        self.sub = label(grey=True)
        text.addWidget(self.sub)
        text.addStretch()
        lay.addLayout(text, 1)


class Performance10(QWidget):
    def __init__(self, sampler):
        super().__init__()
        self.side = QListWidget()
        self.side.setObjectName("side")
        self.side.setFixedWidth(240)
        self.side.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pages = QStackedWidget()
        self.side.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.entries = []
        cpu = lscpu()

        g = Graph10(CPU10)
        ncpu = psutil.cpu_count() or 1
        cores = [Graph10(CPU10) for _ in range(ncpu)]
        cores_box = QWidget()
        cg = QGridLayout(cores_box)
        cg.setContentsMargins(0, 0, 0, 0)
        cg.setSpacing(3)
        cols = math.ceil(math.sqrt(ncpu))
        for i, c in enumerate(cores):
            cg.addWidget(c, i // cols, i % cols)
        cpu_stack = QStackedWidget()
        cpu_stack.addWidget(g)
        cpu_stack.addWidget(cores_box)
        settings = QSettings("ktaskmanager", "ktaskmanager")
        cpu_stack.setCurrentIndex(int(settings.value("Windows 10/perCpu", False, bool)))
        cpu_stack.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        def cpu_menu(pos):  # Win10: right-click > Change graph to
            menu = QMenu(self)
            m = menu.addMenu("Change graph &to")
            for i, name in enumerate(("&Overall utilization", "&Logical processors")):
                a = m.addAction(name, lambda i=i: (cpu_stack.setCurrentIndex(i), settings.setValue("Windows 10/perCpu", bool(i))))
                a.setCheckable(True)
                a.setChecked(cpu_stack.currentIndex() == i)
            menu.exec(cpu_stack.mapToGlobal(pos))
        cpu_stack.customContextMenuRequested.connect(cpu_menu)
        page = Page10("CPU", cpu.get("Model name", ""), [("% Utilization", cpu_stack)],
                      [["Utilization", "Speed"], ["Processes", "Threads", "Handles"], ["Up time"]],
                      ["Base speed", "Sockets", "Cores", "Logical processors", "Virtualization", "L1 cache", "L2 cache", "L3 cache"])
        maxmhz = float(cpu.get("CPU max MHz") or 0)
        cache = lambda k: (cpu.get(k) or "").split(" (")[0].replace("KiB", "KB").replace("MiB", "MB")
        page.set({"Base speed": f"{maxmhz / 1000:.2f} GHz" if maxmhz else "", "Sockets": cpu.get("Socket(s)", ""),
                  "Cores": str(psutil.cpu_count(logical=False)), "Logical processors": str(psutil.cpu_count()),
                  "Virtualization": "Enabled" if cpu.get("Virtualization") else "Not capable",
                  "L1 cache": cache("L1d cache"), "L2 cache": cache("L2 cache"), "L3 cache": cache("L3 cache")})
        page.tops[0].setText("100%")

        def upd_cpu(s, side, page, g):
            freq = psutil.cpu_freq()
            ghz = f"{freq.current / 1000:.2f} GHz" if freq else ""
            g[0].push(s.cpu_total)
            for c, v in zip(g[1:], s.cpu):
                c.push(v)
            side.graph.push(s.cpu_total)
            side.sub.setText(f"{round(s.cpu_total)}%  {ghz}")
            page.set({"Utilization": f"{round(s.cpu_total)}%", "Speed": ghz, "Processes": str(s.nprocs),
                      "Threads": str(s.nthreads), "Handles": str(s.handles),
                      "Up time": fmt_uptime(time.time() - psutil.boot_time())})
        self.add(SideItem("CPU", CPU10), page, [g, *cores], upd_cpu)

        g, comp = Graph10(MEM10), Composition()
        total = psutil.virtual_memory().total
        page = Page10("Memory", gb(total), [("Memory usage", g)],
                      [["In use", "Available"], ["Committed", "Cached"], ["Paged pool", "Non-paged pool"]],
                      ["Swap", "Huge pages", "Transparent huge pages"],
                      extra=("Memory composition", comp))
        page.tops[0].setText(gb(total))

        def upd_mem(s, side, page, g):
            vm, m = s.vm, s.mem
            used = vm.total - vm.available
            cached = m["Cached"] + m["Buffers"]
            g[0].push(vm.percent)
            side.graph.push(vm.percent)
            comp.set(used, cached, max(0, vm.total - used - cached))
            side.sub.setText(f"{used / 2**30:.1f}/{vm.total / 2**30:.1f} GB ({round(vm.percent)}%)")
            page.set({"In use": gb(used), "Available": gb(vm.available), "Cached": gb(cached),
                      "Committed": f"{m['Committed_AS'] / 2**30:.1f}/{m['CommitLimit'] / 2**30:.1f} GB",
                      "Paged pool": f"{m['SReclaimable'] // 2**20} MB", "Non-paged pool": f"{m['SUnreclaim'] // 2**20} MB",
                      "Swap": gb(s.swap.total), "Huge pages": hugepages(m), "Transparent huge pages": gb(m.get("AnonHugePages", 0))})
        self.add(SideItem("Memory", MEM10), page, [g], upd_mem)

        parts = psutil.disk_partitions()
        swaps = set().union(*(disks_of(l.split()[0]) for l in open("/proc/swaps").read().splitlines()[1:]))
        for i, d in enumerate(sampler.disks):
            act, rate = Graph10(DISK10), Graph10(DISK10, autoscale_floor=2**20, second=True)
            mine = [p for p in parts if d in disks_of(p.device)]
            mount = next((p.mountpoint for p in sorted(mine, key=lambda p: len(p.mountpoint))), "")
            model_path = f"/sys/block/{d}/device/model"
            model = open(model_path).read().strip() if os.path.exists(model_path) else d
            page = Page10(f"Disk {i}", model, [("Active time", act), ("Disk transfer rate", rate)],
                          [["Active time", "Average response time"], ["Read speed", "Write speed"]],
                          ["Capacity", "Formatted", "System disk", "Page file", "Type"])
            rot = open(f"/sys/block/{d}/queue/rotational").read().strip() == "1"
            formatted = 0
            for mnt in {p.device: p.mountpoint for p in mine}.values():  # once per filesystem, not per btrfs subvolume
                try:
                    formatted += psutil.disk_usage(mnt).total
                except OSError:  # e.g. mounted under another user's /run/media
                    pass
            page.set({"Capacity": gb(int(open(f"/sys/block/{d}/size").read()) * 512),
                      "Formatted": gb(formatted) if formatted else "", "Type": "HDD" if rot else "SSD",
                      "System disk": "Yes" if any(p.mountpoint == "/" for p in mine) else "No",
                      "Page file": "Yes" if d in swaps else "No"})
            page.tops[0].setText("100%")

            def upd_disk(s, side, page, g, d=d):
                x = s.disks.get(d)
                if not x:
                    return
                g[0].push(x["active"])
                g[1].push(x["read"], x["write"])
                page.tops[1].setText(fmt_rate(g[1].top()))
                side.graph.push(x["active"])
                side.sub.setText(f"{round(x['active'])}%")
                page.set({"Active time": f"{round(x['active'])}%", "Average response time": f"{x['resp']:.1f} ms",
                          "Read speed": fmt_rate(x["read"]), "Write speed": fmt_rate(x["write"])})
            self.add(SideItem(f"Disk {i} ({mount or d})", DISK10), page, [act, rate], upd_disk)

        addrs = psutil.net_if_addrs()
        for nic in sampler.net:
            if nic == "lo":
                continue
            kind = "Wi-Fi" if os.path.exists(f"/sys/class/net/{nic}/wireless") else "Ethernet"
            if not os.path.exists(f"/sys/class/net/{nic}/device"):
                kind = f"vEthernet ({nic})"  # virtual adapter, as Win10 labels Hyper-V switches
            g = Graph10(NET10, autoscale_floor=100e3, second=True)
            page = Page10(kind, nic, [("Throughput", g)], [["Send", "Receive"]],
                          ["Adapter name", "Connection type", "IPv4 address", "IPv6 address"])
            fam = lambda f: next((a.address.split("%")[0] for a in addrs.get(nic, []) if a.family == f), "")
            page.set({"Adapter name": nic, "Connection type": kind.split(" ")[0], "IPv4 address": fam(socket.AF_INET),
                      "IPv6 address": fam(socket.AF_INET6)})

            def upd_net(s, side, page, g, nic=nic):
                n = s.nets.get(nic)
                if not n or not n["up"]:
                    side.sub.setText("Not connected")
                    return
                g[0].push(n["recv"], n["sent"])
                side.graph.push(n["recv"], n["sent"])
                page.tops[0].setText(fmt_bits(g[0].top()))
                unit, div = ("Mbps", 1e6) if max(n["sent"], n["recv"]) >= 1e6 else ("Kbps", 1e3)
                side.sub.setText(f"S: {n['sent'] / div:.1f} R: {n['recv'] / div:.1f} {unit}")
                page.set({"Send": fmt_bits(n["sent"]), "Receive": fmt_bits(n["recv"])})
            self.add(SideItem(kind, NET10, autoscale_floor=100e3, second=True), page, [g], upd_net)

        for i, gpu in enumerate(gpus()):
            util, mem = Graph10(CPU10), Graph10(CPU10)
            page = Page10(f"GPU {i}", gpu["name"], [("Utilization", util), ("Dedicated GPU memory usage", mem)],
                          [["Utilization", "Dedicated GPU memory"], ["GPU temperature"]],
                          ["Driver version", "Physical location"])
            dom, bus, rest = gpu["bus"].split(":") if gpu["bus"].count(":") == 2 else ("", "0", "0.0")
            dev, fn = rest.split(".")
            page.set({"Driver version": gpu["driver"],
                      "Physical location": f"PCI bus {int(bus, 16)}, device {int(dev, 16)}, function {int(fn, 16)}"})
            page.tops[0].setText("100%")

            def upd_gpu(s, side, page, g, i=i, name=gpu["name"]):
                x = s.gpus[i]
                g[0].push(x["util"])
                g[1].push(x["used"] / x["total"] * 100 if x["total"] else 0)
                page.tops[1].setText(gb(x["total"]))
                side.graph.push(x["util"])
                side.sub.setText(f"{name}\n{round(x['util'])}%" + (f" ({x['temp']} °C)" if x["temp"] else ""))
                page.set({"Utilization": f"{round(x['util'])}%",
                          "Dedicated GPU memory": f"{x['used'] / 2**30:.1f}/{x['total'] / 2**30:.1f} GB",
                          "GPU temperature": f"{x['temp']} °C" if x["temp"] else "—"})
            self.add(SideItem(f"GPU {i}", CPU10), page, [util, mem], upd_gpu)

        self.side.setCurrentRow(0)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.side)
        sep = QWidget()
        sep.setFixedWidth(1)
        sep.setStyleSheet(f"background: {T.sep.name()}")
        lay.addWidget(sep)
        lay.addWidget(self.pages, 1)
        self.bar = bar(left=[link("Open Resource Monitor", lambda: QProcess.startDetached("plasma-systemmonitor", []),
                                  QIcon.fromTheme("utilities-system-monitor"))])

    def add(self, side, page, graphs, fn):
        it = QListWidgetItem()
        it.setSizeHint(QSize(220, max(62, side.sizeHint().height())))
        self.side.addItem(it)
        self.side.setItemWidget(it, side)
        self.pages.addWidget(page)
        self.entries.append((side, page, graphs, fn))

    def update_from(self, s):
        for side, page, g, fn in self.entries:
            fn(s, side, page, g)


class AppHistory(QWidget):
    """Cumulative CPU time per app for the current user since boot (Win10 App history)."""
    slow = True

    def __init__(self):
        super().__init__()
        self.baseline = {}
        boot = time.strftime("%-m/%-d/%Y", time.localtime(psutil.boot_time()))
        self.since = label(f"Resource usage since {boot} for current user account.")
        top = QVBoxLayout()
        top.setContentsMargins(10, 10, 10, 6)
        top.setSpacing(2)
        top.addWidget(self.since)
        top.addWidget(link("Delete usage history", self.delete), alignment=Qt.AlignmentFlag.AlignLeft)
        self.table = Table(["Name", "CPU time"], right_cols=(1,), header=Header10(), row_height=12)
        self.table.widths(260)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(top)
        lay.addWidget(self.table)
        self.me = psutil.Process().username()

    def usage(self):
        names, out = app_names(), {}
        for p in psutil.process_iter(["name", "username", "cpu_times"]):
            if p.info["username"] == self.me and p.info["cpu_times"]:
                name = names.get(p.info["name"]) or p.info["name"]
                out[name] = out.get(name, 0) + sum(p.info["cpu_times"][:2])
        return out

    def delete(self):
        self.baseline = self.usage()
        self.since.setText(f"Resource usage since {time.strftime('%-m/%-d/%Y')} for current user account.")
        self.refresh()

    def refresh(self):
        self.table.sync({n: [(n, n.lower(), None), heat_cell(fmt_hms(t), t, t / 3600)]
                         for n, t in ((n, max(0, t - self.baseline.get(n, 0))) for n, t in self.usage().items())})


class Startup(QWidget):
    """XDG autostart entries; Disable writes Hidden=true into the user's ~/.config/autostart copy (as Plasma does)."""
    slow = True

    def __init__(self):
        super().__init__()
        self.bios = label(grey=True, align=RIGHT)
        self.table = Table(["Name", "Publisher", "Status", "Startup impact"], header=Header10(), row_height=12)
        self.table.widths(240, 170, 90)
        self.toggle_btn = button("&Disable", self.toggle)
        self.table.clicked.connect(self.update_button)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.addWidget(self.bios)
        lay.addWidget(self.table)
        self.bar = bar(self.toggle_btn)
        self.entries = {}

    def refresh(self):
        if not self.bios.text():
            # "Startup finished in 7.3s (firmware) + ..." — firmware time is the BIOS time
            out = subprocess.run(["systemd-analyze"], capture_output=True, text=True).stdout
            fw = out.split("(firmware)")[0].split()[-1] if "(firmware)" in out else None
            self.bios.setText(f"Last BIOS time:  {fw.rstrip('s')} seconds  " if fw else " ")
        self.entries = {}
        for d in ("/etc/xdg/autostart", AUTOSTART):  # user dir overrides system by filename
            for path in sorted(glob.glob(f"{d}/*.desktop")):
                e = read_desktop(path)
                if "KDE" in e.get("NotShowIn", "") or (e.get("OnlyShowIn") and "KDE" not in e["OnlyShowIn"]):
                    self.entries.pop(os.path.basename(path), None)
                    continue
                self.entries[os.path.basename(path)] = (e, path)
        rows = {}
        for f, (e, _) in self.entries.items():
            name = e.get("Name", f)
            status = "Disabled" if e.get("Hidden", "").lower() == "true" else "Enabled"
            try:
                exe = shlex.split(e.get("Exec", ""))[0]
            except (ValueError, IndexError):
                exe = ""
            exe_path = exe if exe.startswith("/") else (QStandardPaths.findExecutable(exe) if exe else "")
            pub = package_of(exe_path) if exe_path else ""
            rows[f] = [(name, name.lower(), QIcon.fromTheme(e.get("Icon", "application-x-executable"))), (pub, pub, None),
                       (status, status, None), ("Not measured", 0, None)]
        self.table.sync(rows)
        self.update_button()

    def disabled(self, f):
        return self.entries[f][0].get("Hidden", "").lower() == "true"

    def update_button(self):
        f = self.table.selected_key()
        self.toggle_btn.setEnabled(f in self.entries)
        self.toggle_btn.setText("&Enable" if f in self.entries and self.disabled(f) else "&Disable")

    def toggle(self):
        f = self.table.selected_key()
        if f not in self.entries:
            return
        lines = [l for l in open(self.entries[f][1], encoding="utf-8-sig").read().splitlines() if not l.startswith("Hidden=")]
        at = next((i + 1 for i, l in enumerate(lines) if l.strip() == "[Desktop Entry]"), None)
        if at is None:
            lines, at = ["[Desktop Entry]"] + lines, 1
        lines.insert(at, f"Hidden={'false' if self.disabled(f) else 'true'}")
        os.makedirs(AUTOSTART, exist_ok=True)
        with open(os.path.join(AUTOSTART, f), "w", encoding="utf-8") as out:
            out.write("\n".join(lines) + "\n")
        self.refresh()


class Users10(QWidget):
    def __init__(self):
        super().__init__()
        self.header = Header10(tall=True)
        self.table = Table(["User", "Status", "CPU", "Memory"], right_cols=(2, 3), header=self.header, row_height=12)
        self.table.widths(280, 90, 75)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.table)
        self.bar = bar(button("&Sign out", logoff))
        self.ncpu = psutil.cpu_count() or 1

    def update_header(self, s):
        self.header.totals = {2: s.cpu_total, 3: s.vm.percent}
        self.header.viewport().update()

    def refresh(self):
        vm = psutil.virtual_memory()
        users = {u.name: [0, 0.0, 0] for u in psutil.users()}
        for p in psutil.process_iter(["username", "cpu_percent", "memory_info"]):
            if p.info["username"] in users and p.info["memory_info"]:
                u = users[p.info["username"]]
                u[0] += 1
                u[1] += (p.info["cpu_percent"] or 0) / self.ncpu
                u[2] += p.info["memory_info"].rss - p.info["memory_info"].shared
        self.table.sync({n: [(f"{n} ({c})", n, QIcon.fromTheme("user-identity")), ("", "", None),
                             heat_cell(pct10(cpu), cpu, cpu / 25), heat_cell(mb10(mem), mem, mem / vm.total * 5)]
                         for n, (c, cpu, mem) in users.items()})


class Compact10(QWidget):
    """Win10 'Fewer details' view: just the apps."""

    def __init__(self):
        super().__init__()
        self.list = QListWidget()
        self.list.setIconSize(QSize(20, 20))
        self.list.itemDoubleClicked.connect(lambda _: self.switch())
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.menu)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.list)
        self.end_btn = button("&End task", self.end)
        self.wins = {}

    def refresh(self):
        wins = {w["uuid"]: w for w in kwin_windows()}
        if list(wins) != list(self.wins):
            cur = self.current()
            self.list.clear()
            for uuid, w in wins.items():
                it = QListWidgetItem(w["icon"], w["name"])
                it.setData(KEY, uuid)
                it.setSizeHint(QSize(0, 28))
                self.list.addItem(it)
                if cur and uuid == cur["uuid"]:
                    self.list.setCurrentItem(it)
        self.wins = wins

    def current(self):
        it = self.list.currentItem()
        return self.wins.get(it.data(KEY)) if it else None

    def switch(self):
        if w := self.current():
            kwin_window_action(w["uuid"], 0)

    def end(self):
        if (w := self.current()) and w["pid"]:
            end_process(self, w["pid"], confirm=False, app=True)

    def menu(self, pos):
        w = self.current()
        if not w:
            return
        win = self.window()
        m = QMenu(self)
        f = m.addAction("&Switch to", self.switch)
        bold = f.font()
        bold.setBold(True)
        f.setFont(bold)
        m.addAction("&End task", self.end)
        m.addSeparator()
        m.addAction("&Run new task", win.new_task)
        m.addAction(win.on_top)
        m.addSeparator()
        m.addAction("&Open file location", lambda: open_file_location(w["pid"]))
        m.exec(self.list.viewport().mapToGlobal(pos))


# ---------------------------------------------------------------- themes

def make_palette(window, highlight, hl_text="#ffffff"):
    p = QPalette()
    for role, c in ((QPalette.ColorRole.Window, window), (QPalette.ColorRole.WindowText, "#000000"),
                    (QPalette.ColorRole.Base, "#ffffff"), (QPalette.ColorRole.AlternateBase, "#f7f7f7"),
                    (QPalette.ColorRole.Text, "#000000"), (QPalette.ColorRole.Button, window),
                    (QPalette.ColorRole.ButtonText, "#000000"), (QPalette.ColorRole.Highlight, highlight),
                    (QPalette.ColorRole.HighlightedText, hl_text), (QPalette.ColorRole.ToolTipBase, "#ffffe1"),
                    (QPalette.ColorRole.ToolTipText, "#000000"), (QPalette.ColorRole.Link, "#0066cc"),
                    (QPalette.ColorRole.PlaceholderText, "#808080")):
        p.setColor(role, QColor(c))
    for role in (QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText, QPalette.ColorRole.Text):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor("#838383"))
    return p


QSS_2000 = """
QMenuBar { background: #d4d0c8; }
QMenuBar::item { padding: 2px 6px; background: transparent; }
QMenuBar::item:selected, QMenu::item:selected { background: #0a246a; color: white; }
QMenu { background: #d4d0c8; border: 2px outset #d4d0c8; }
QMenu::item { padding: 3px 24px 3px 20px; }
QTabWidget::pane { border: 2px outset #d4d0c8; top: -2px; }
QTabBar::tab { background: #d4d0c8; border: 2px outset #d4d0c8; border-bottom: none; padding: 2px 7px; margin-top: 2px; }
QTabBar::tab:selected { margin-top: 0; padding-bottom: 4px; margin-left: -2px; margin-right: -2px; }
QTabBar::tab:first:selected { margin-left: 0; }
QPushButton { background: #d4d0c8; border: 2px outset #d4d0c8; padding: 3px 8px; min-width: 66px; }
QPushButton:pressed { border-style: inset; }
QTableView, QTreeView { background: white; border: 2px inset #d4d0c8; selection-background-color: #0a246a; }
QHeaderView::section { background: #d4d0c8; border: 2px outset #d4d0c8; padding: 0 4px; }
QGroupBox { border: 2px groove #d4d0c8; margin-top: 7px; padding-top: 3px; }
QGroupBox::title { subcontrol-origin: margin; left: 7px; padding: 0 2px; }
QStatusBar::item { border: 1px inset #d4d0c8; }
QStatusBar QLabel { padding: 0 4px; }
"""


def qss_7():
    up, down = arrow_png(True), arrow_png(False)
    btn = "stop:0 #f2f2f2, stop:0.5 #ebebeb, stop:0.51 #dddddd, stop:1 #cfcfcf"
    hot = "stop:0 #eaf6fd, stop:0.5 #d9f0fc, stop:0.51 #bee6fd, stop:1 #a7d9f5"
    grad = "qlineargradient(x1:0, y1:0, x2:0, y2:1, {})".format
    return f"""
QMainWindow {{ background: #f0f0f0; }}
QMenuBar {{ background: {grad("stop:0 #ffffff, stop:1 #e5ecf5")}; border-bottom: 1px solid #b6bcc6; }}
QMenuBar::item {{ padding: 3px 7px; background: transparent; }}
QMenuBar::item:selected {{ background: {grad("stop:0 #f2f8fe, stop:1 #d3e5fa")}; border: 1px solid #a8c8ea; border-radius: 2px; }}
QMenu {{ background: #f0f0f0; border: 1px solid #979797; }}
QMenu::item {{ padding: 3px 28px; }}
QMenu::item:selected {{ background: {grad("stop:0 #f2f8fe, stop:1 #dbeafc")}; border: 1px solid #aecff7; border-radius: 3px; color: black; }}
QMenu::separator {{ height: 1px; background: #e0e0e0; margin: 2px 2px 2px 28px; }}
QTabWidget::pane {{ background: white; border: 1px solid #898c95; top: -1px; }}
QTabBar::tab {{ background: {grad(btn)}; border: 1px solid #898c95; border-bottom: none; padding: 2px 6px 3px 6px;
               margin-top: 2px; margin-right: -1px; border-top-left-radius: 2px; border-top-right-radius: 2px; }}
QTabBar::tab:hover:!selected {{ background: {grad(hot)}; border-color: #3c7fb1; }}
QTabBar::tab:selected {{ background: white; margin-top: 0; padding-bottom: 4px; margin-left: -2px; margin-right: -3px; }}
QTabBar::tab:first:selected {{ margin-left: 0; }}
QPushButton {{ border: 1px solid #707070; border-radius: 3px; padding: 3px 12px; min-width: 61px; min-height: 16px; background: {grad(btn)}; }}
QPushButton:hover {{ border-color: #3c7fb1; background: {grad(hot)}; }}
QPushButton:pressed {{ border-color: #2c628b; background: {grad("stop:0 #e5f4fc, stop:0.5 #c4e5f6, stop:0.51 #98d1ef, stop:1 #68b3db")}; }}
QPushButton:disabled {{ border-color: #adb2b5; background: #f4f4f4; color: #838383; }}
QTableView, QTreeView {{ background: white; border: 1px solid #828790; selection-background-color: transparent; outline: 0; }}
QTableView::item:hover {{ background: {grad("stop:0 #fafbfd, stop:1 #ebf3fd")}; border-top: 1px solid #b8d6fb; border-bottom: 1px solid #b8d6fb; }}
QTableView::item:selected {{ color: black; background: {grad("stop:0 #dcebfc, stop:1 #c1dbfc")};
               border-top: 1px solid #84acdd; border-bottom: 1px solid #84acdd; }}
QHeaderView::section {{ background: white; border: none; border-right: 1px solid #e3e8ee; padding: 6px 5px 3px 5px; }}
QHeaderView::section:hover {{ background: {grad("stop:0 #e3f7ff, stop:0.4 #e3f7ff, stop:0.41 #bde6fd, stop:1 #a6d9f4")}; border-right: 1px solid #93c9e3; }}
QHeaderView::up-arrow {{ image: url({up}); subcontrol-origin: padding; subcontrol-position: top center; top: -5px; }}
QHeaderView::down-arrow {{ image: url({down}); subcontrol-origin: padding; subcontrol-position: top center; top: -5px; }}
QGroupBox {{ border: 1px solid #d5dfe5; border-radius: 3px; margin-top: 8px; padding-top: 3px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 6px; padding: 0 2px; }}
QStatusBar {{ background: #f0f0f0; border-top: 1px solid #d7d7d7; }}
QStatusBar::item {{ border: none; border-right: 1px solid #c5c5c5; }}
QStatusBar QLabel {{ padding: 2px 4px; }}
"""


QSS_10 = """
QMainWindow, QStackedWidget, QTabWidget > QWidget { background: white; }
QMenuBar { background: white; }
QMenuBar::item { padding: 3px 7px; background: transparent; }
QMenuBar::item:selected { background: #e5f3ff; border: 1px solid #cce8ff; }
QMenu { background: #f2f2f2; border: 1px solid #cccccc; }
QMenu::item { padding: 4px 32px; }
QMenu::item:selected { background: #91c9f7; color: black; }
QMenu::separator { height: 1px; background: #d7d7d7; margin: 3px 0; }
QTabWidget::pane { border: none; border-top: 1px solid #d9d9d9; top: -1px; }
QTabBar::tab { background: white; border: 1px solid #d9d9d9; border-bottom: none; padding: 2px 6px 3px 6px; margin-top: 2px; margin-right: -1px; }
QTabBar::tab:hover:!selected { background: #d8eaf9; border-color: #c0dcf3; }
QTabBar::tab:selected { margin-top: 0; padding-bottom: 4px; margin-left: -2px; margin-right: -3px; }
QTabBar::tab:first:selected { margin-left: 0; }
QPushButton { background: #e1e1e1; border: 1px solid #adadad; padding: 3px 18px; min-width: 50px; min-height: 16px; }
QPushButton:hover { background: #e5f1fb; border-color: #0078d7; }
QPushButton:pressed { background: #cce4f7; border-color: #005499; }
QPushButton:disabled { background: #cccccc; border-color: #bfbfbf; color: #838383; }
QPushButton#link { background: transparent; border: none; padding: 2px 4px; color: #0066cc; text-align: left; }
QPushButton#link:hover { text-decoration: underline; }
QPushButton#details { background: transparent; border: none; padding: 2px 4px; color: #000000; text-align: left; }
QPushButton#details:hover { color: #0066cc; }
QTableView, QTreeView, QListWidget { background: white; border: none; selection-background-color: transparent; outline: 0; }
QTreeView::item { height: 26px; }
QTableView::item:hover, QTreeView::item:hover, QListWidget::item:hover { background: #e5f3ff; }
QTableView::item:selected, QTreeView::item:selected, QListWidget::item:selected { background: #cce8ff; color: black; }
QListWidget#side::item:selected:!active { background: #d9d9d9; }
QWidget#footer { background: white; }
"""


# ---------------------------------------------------------------- windows

class Base(QMainWindow):
    STYLE = RUN_TEXT = EXIT_TEXT = ""
    FONT = ("Segoe UI", 9)
    DEFAULT_TAB = 0
    DEFAULT_SIZE = QSize(600, 600)

    def __init__(self):
        global T
        app = QApplication.instance()
        if not NATIVE:  # remember KDE's own look before any Windows style replaces it
            NATIVE.update(style=app.style().name(), palette=QPalette(app.palette()), font=QFont(app.font()))
        self.apply_look(app)
        T = self.theme()
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon.fromTheme("utilities-system-monitor"))
        self.settings = QSettings("ktaskmanager", "ktaskmanager")
        self.sampler = Sampler()
        self.tabs = QTabWidget()
        self.tabs.setUsesScrollButtons(False)

        mb = self.menuBar()
        f = mb.addMenu("&File")
        f.addAction(self.RUN_TEXT, self.new_task)
        f.addSeparator()
        f.addAction(self.EXIT_TEXT, self.close)
        o = mb.addMenu("&Options")
        self.on_top = o.addAction("&Always on top")
        self.on_top.setCheckable(True)
        self.on_top.toggled.connect(self.set_on_top)
        self.minimize_on_use = o.addAction("&Minimize on use")
        self.minimize_on_use.setCheckable(True)
        o.addSeparator()
        style = o.addMenu("&Style")
        sg = QActionGroup(self)
        for name in STYLES:
            a = style.addAction(name, lambda name=name: self.switch_style(name))
            a.setCheckable(True)
            a.setChecked(name == self.STYLE)
            sg.addAction(a)
        v = mb.addMenu("&View")
        v.addAction("&Refresh now", lambda: self.tick(sample=False)).setShortcut(QKeySequence("F5"))
        speed = v.addMenu("&Update speed")
        group = QActionGroup(self)
        cur = self.settings.value("interval", 1000, int)
        for name, ms in (("&High", 500), ("&Normal", 1000), ("&Low", 4000), ("&Paused", 0)):
            a = speed.addAction(name, lambda ms=ms: self.set_interval(ms))
            a.setCheckable(True)
            a.setChecked(ms == cur)
            group.addAction(a)

        self.build(v)
        self.tabs.currentChanged.connect(lambda _: self.tick(sample=False))
        self.timer = QTimer(self, timeout=self.tick)
        self.set_interval(cur)
        self.restore_size()
        self.tabs.setCurrentIndex(self.settings.value(f"{self.STYLE}/tab", self.DEFAULT_TAB, int))
        self.on_top.setChecked(self.settings.value("onTop", False, bool))
        self.minimize_on_use.setChecked(self.settings.value("minimizeOnUse", True, bool))
        QTimer.singleShot(0, self.tick)

    def apply_look(self, app):
        app.setStyle("Fusion")
        app.setPalette(self.palette_())
        app.setFont(QFont(*self.FONT))
        app.setStyleSheet(self.qss())

    def theme(self):
        return WIN10_THEME

    def palette_(self):
        return make_palette("#f0f0f0", "#3399ff")

    def qss(self):
        return ""

    def geometry_key(self):
        return f"{self.STYLE}/geometry"

    def default_size(self):
        return self.DEFAULT_SIZE

    def restore_size(self):
        if geo := self.settings.value(self.geometry_key()):
            self.restoreGeometry(geo)
        else:
            self.resize(self.default_size())

    def build(self, view_menu):
        raise NotImplementedError

    def update_from(self, s):
        pass

    def current_view(self):
        return self.tabs.currentWidget()

    def tick(self, sample=True):
        if sample:
            self.update_from(self.sampler.sample())
        cur = self.current_view()
        if hasattr(cur, "refresh") and (not getattr(cur, "slow", False) or not sample):
            cur.refresh()

    def set_interval(self, ms):
        self.settings.setValue("interval", ms)
        self.timer.stop()
        if ms:
            self.timer.start(ms)

    def set_on_top(self, on):
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        self.show()

    def new_task(self):
        cmd, ok = QInputDialog.getText(self, "Create New Task", "Type the name of a program, folder, document, or Internet "
                                       "resource, and it will be opened for you.\n\nOpen:")
        if ok and cmd.strip():
            try:
                args = shlex.split(cmd)
            except ValueError:  # unbalanced quote, e.g. a path with an apostrophe
                args = [cmd]
            if not args or not QProcess.startDetached(args[0], args[1:]):
                QProcess.startDetached("xdg-open", [cmd])

    def switch_style(self, name):
        if name == self.STYLE:
            return
        self.settings.setValue("style", name)
        app = QApplication.instance()
        app.setQuitOnLastWindowClosed(False)  # don't quit in the gap between closing this window and showing the next
        self.close()
        app.win = STYLES[name]()
        app.win.show()
        app.setQuitOnLastWindowClosed(True)

    def closeEvent(self, e):
        s = self.settings
        s.setValue(self.geometry_key(), self.saveGeometry())
        s.setValue(f"{self.STYLE}/tab", self.tabs.currentIndex())
        s.setValue("onTop", self.on_top.isChecked())
        s.setValue("minimizeOnUse", self.minimize_on_use.isChecked())
        self.save()
        self.timer.stop()
        super().closeEvent(e)

    def save(self):
        pass


class Classic(Base):
    """Shared by Win2000 and Win7: buttons inside each tab page and a 3-pane status bar."""

    def finish_classic(self, v):
        # as in Win7/2000: View > Select Columns... (for the Processes page)
        v.addAction("&Select Columns...", lambda: (c := getattr(self.tabs.currentWidget(), "chooser", None)) and c.dialog())
        hist = v.addMenu("&CPU History")
        g = QActionGroup(self)
        per = self.settings.value("perCpu", True, bool)
        for name, on in (("&One Graph, All CPUs", False), ("One Graph &Per CPU", True)):
            a = hist.addAction(name, lambda on=on: self.perf.set_per_cpu(on))
            a.setCheckable(True)
            a.setChecked(on == per)
            g.addAction(a)
        self.perf.set_per_cpu(per)
        self.menuBar().addMenu("&Help").addAction("&About Task Manager", lambda: QMessageBox.about(
            self, f"About {APP_NAME}", f"{APP_NAME}\nA task manager for KDE Plasma."))
        self.status = [QLabel() for _ in range(3)]
        for l, stretch in zip(self.status, (2, 2, 3)):
            self.statusBar().addWidget(l, stretch)
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(6, 6, 6, 4)
        lay.addWidget(self.tabs)
        self.setCentralWidget(wrap)

    def save(self):
        self.settings.setValue("perCpu", self.perf.per_cpu_box.isVisibleTo(self.perf))


class Win2000(Classic):
    STYLE, RUN_TEXT, EXIT_TEXT = "Windows 2000", "&New Task (Run...)", "E&xit Task Manager"
    FONT = ("Tahoma", 8)
    DEFAULT_SIZE = QSize(470, 520)

    def palette_(self):
        return make_palette("#d4d0c8", "#0a246a")

    def qss(self):
        return QSS_2000

    def build(self, v):
        self.perf = Performance7(classic=True)
        procs = Processes([("Image Name", "name", 1), ("PID", "pid", 1), ("User Name", "user", 0), ("CPU", "cpu", 1),
                           ("CPU Time", "cputime", 1), ("Mem Usage", "ws", 1), ("Handles", "handles", 0),
                           ("Thread Count", "threads", 0), ("Base Priority", "nice", 0), ("I/O Reads", "ioread", 0),
                           ("I/O Writes", "iowrite", 0)], "Windows 2000/columns/processes")
        for w, name in ((Applications(), "Applications"), (procs, "Processes"), (self.perf, "Performance")):
            self.tabs.addTab(w, name)
        self.finish_classic(v)

    def update_from(self, s):
        self.perf.update_from(s)
        self.status[0].setText(f"Processes: {s.nprocs}")
        self.status[1].setText(f"CPU Usage: {round(s.cpu_total)}%")
        self.status[2].setText(f"Mem Usage: {s.mem['Committed_AS'] // 1024}K / {s.mem['CommitLimit'] // 1024}K")


class Win7(Classic):
    STYLE, RUN_TEXT, EXIT_TEXT = "Windows 7", "&New Task (Run...)", "E&xit Task Manager"
    DEFAULT_SIZE = QSize(560, 600)
    DEFAULT_TAB = 1

    def qss(self):
        return qss_7()

    def build(self, v):
        self.perf, self.net = Performance7(), Networking()
        procs = Processes([("Image Name", "name", 1), ("PID", "pid", 0), ("User Name", "user", 1), ("CPU", "cpu", 1),
                           ("CPU Time", "cputime", 0), ("Memory - Working Set", "ws", 0),
                           ("Memory (Private Working Set)", "mem", 1), ("Handles", "handles", 0), ("Threads", "threads", 0),
                           ("Base Priority", "nice", 0), ("I/O Reads", "ioread", 0), ("I/O Writes", "iowrite", 0),
                           ("Image Path Name", "path", 0), ("Command Line", "cmd", 0), ("Description", "desc", 1)],
                          "Windows 7/columns/processes", all_users="button")
        for w, name in ((Applications(), "Applications"), (procs, "Processes"), (Services(), "Services"),
                        (self.perf, "Performance"), (self.net, "Networking"), (Users7(), "Users")):
            self.tabs.addTab(w, name)
        self.finish_classic(v)

    def update_from(self, s):
        self.perf.update_from(s)
        self.net.update_from(s)
        self.status[0].setText(f"Processes: {s.nprocs}")
        self.status[1].setText(f"CPU Usage: {round(s.cpu_total)}%")
        self.status[2].setText(f"Physical Memory: {round(s.vm.percent)}%")


class Win10(Base):
    STYLE, RUN_TEXT, EXIT_TEXT = "Windows 10", "&Run new task", "E&xit"
    DEFAULT_SIZE = QSize(720, 640)

    def palette_(self):
        return make_palette("#ffffff", "#cce8ff", "#000000")

    def qss(self):
        return QSS_10

    def build(self, v):
        self.procs, self.perf, self.users = Processes10(), Performance10(self.sampler), Users10()
        details = Processes([("Name", "name", 1), ("PID", "pid", 1), ("Status", "status", 1), ("User name", "user", 1),
                             ("CPU", "cpu", 1), ("CPU time", "cputime", 0), ("Memory (private working set)", "mem", 1),
                             ("Memory (working set)", "ws", 0), ("Handles", "handles", 0), ("Threads", "threads", 0),
                             ("Base priority", "nice", 0), ("I/O reads", "ioread", 0), ("I/O writes", "iowrite", 0),
                             ("Image path name", "path", 0), ("Command line", "cmd", 0), ("Description", "desc", 1)],
                            "Windows 10/columns/details", end_text="&End task", header=Header10(), confirm=False)
        details.table.verticalHeader().setDefaultSectionSize(details.fontMetrics().height() + 6)
        for w, name in ((self.procs, "Processes"), (self.perf, "Performance"), (AppHistory(), "App history"),
                        (Startup(), "Startup"), (self.users, "Users"), (details, "Details"), (Services(Header10()), "Services")):
            w.layout().setContentsMargins(0, 0, 0, 0)
            self.tabs.addTab(w, name)

        # footer: "Fewer details" plus the current tab's button(s), on one line like Win10
        self.footer_stack = QStackedWidget()
        for i in range(self.tabs.count()):
            self.footer_stack.addWidget(getattr(self.tabs.widget(i), "bar", None) or QWidget())
        self.tabs.currentChanged.connect(self.footer_stack.setCurrentIndex)
        full = QWidget()
        fl = QVBoxLayout(full)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(0)
        fl.addWidget(self.tabs, 1)
        fl.addWidget(self.footer(details_icon(True), "Fewer details", self.footer_stack))

        self.compact = Compact10()
        small = QWidget()
        sl = QVBoxLayout(small)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        sl.addWidget(self.compact, 1)
        sl.addWidget(self.footer(details_icon(False), "More details", bar(self.compact.end_btn)))

        self.modes = QStackedWidget()
        self.modes.addWidget(full)
        self.modes.addWidget(small)
        self.setCentralWidget(self.modes)
        self.set_compact(self.settings.value(f"{self.STYLE}/compact", False, bool), resize=False)

    def footer(self, icon, text, right):
        w = QWidget()
        w.setObjectName("footer")
        row = QHBoxLayout(w)
        row.setContentsMargins(8, 8, 10, 8)
        b = QPushButton(icon, f"  {text}")
        b.setObjectName("details")
        b.setFlat(True)
        b.setIconSize(QSize(18, 18))
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.clicked.connect(lambda: self.set_compact(self.modes.currentIndex() == 0))
        row.addWidget(b)
        row.addWidget(right, 1)
        return w

    def geometry_key(self):
        return f"{self.STYLE}/{'compact' if self.modes.currentIndex() else 'full'}Geometry"

    def default_size(self):
        return QSize(360, 330) if self.modes.currentIndex() else self.DEFAULT_SIZE

    def set_compact(self, on, resize=True):
        if resize:
            self.settings.setValue(self.geometry_key(), self.saveGeometry())
        self.modes.setCurrentIndex(1 if on else 0)
        for i in range(self.modes.count()):  # a hidden page mustn't impose its minimum size
            pol = QSizePolicy.Policy.Preferred if i == self.modes.currentIndex() else QSizePolicy.Policy.Ignored
            self.modes.widget(i).setSizePolicy(pol, pol)
        self.menuBar().setVisible(not on)
        self.settings.setValue(f"{self.STYLE}/compact", on)
        if resize:
            self.modes.updateGeometry()
            self.layout().activate()  # recompute the minimum size before shrinking
            self.setMinimumSize(0, 0)
            self.restore_size()
            self.tick(sample=False)

    def current_view(self):
        return self.compact if self.modes.currentIndex() == 1 else self.tabs.currentWidget()

    def update_from(self, s):
        self.perf.update_from(s)
        self.procs.update_header(s)
        self.users.update_header(s)


class Native(Win10):
    """Win10's layout drawn entirely with the user's KDE style, colour scheme, font and accent colour."""
    STYLE, RUN_TEXT, EXIT_TEXT = "KDE Native", "&Run New Task…", "&Quit"

    def apply_look(self, app):
        app.setStyleSheet("")
        app.setStyle(NATIVE["style"])
        app.setPalette(NATIVE["palette"])
        app.setFont(NATIVE["font"])

    def theme(self):
        pal = NATIVE["palette"]
        base, text, hl = (pal.color(r) for r in (QPalette.ColorRole.Base, QPalette.ColorRole.Text, QPalette.ColorRole.Highlight))
        return SimpleNamespace(base=base, text=text, grey=blend(text, base, 0.45), sep=blend(base, text, 0.12),
                               header_text=blend(text, base, 0.2), accent=hl,
                               heat_lo=blend(base, hl, 0.06), heat_hi=blend(base, hl, 0.6))


NATIVE = {}
STYLES = {"Windows 2000": Win2000, "Windows 7": Win7, "Windows 10": Win10, "KDE Native": Native}


def main():
    app = QApplication(sys.argv)
    app.setDesktopFileName("ktaskmanager")
    app.setApplicationName("ktaskmanager")
    app.setApplicationDisplayName(APP_NAME)
    style = QSettings("ktaskmanager", "ktaskmanager").value("style", "Windows 7")
    app.win = STYLES.get(style, Win7)()
    app.win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
