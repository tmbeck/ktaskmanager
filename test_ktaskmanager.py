import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtGui import QStandardItemModel
from ktaskmanager import KEY, app_members, hugepages, nice_ceiling, sync

assert nice_ceiling(0, 100e3) == 100e3
assert nice_ceiling(130e3, 100e3) == 200e3
assert nice_ceiling(3e6, 1) == 5e6
assert nice_ceiling(10, 1) == 10

model = QStandardItemModel()
root = model.invisibleRootItem()
cell = lambda t: (t, t, None)
sync(root, {1: [cell("a")], 2: [cell("b")]})
sync(root, {2: [cell("B")], 3: [cell("c")]})  # 1 removed, 2 updated, 3 added
assert sorted((root.child(r).data(KEY), root.child(r).text()) for r in range(root.rowCount())) == [(2, "B"), (3, "c")]
assert hugepages({}) == "None"
assert hugepages({"HugePages_Total": 32 * 1024, "HugePages_Free": 30 * 1024, "Hugepagesize": 2**30}) == "2/32 × 1 GB"
assert hugepages({"HugePages_Total": 512 * 1024, "HugePages_Free": 0, "Hugepagesize": 2 * 2**20}) == "512/512 × 2 MB"
# Dolphin (1) with a helper (2) that opened Okular (3, own window) and its child (4): Okular isn't Dolphin's
kids = {1: [2], 2: [3], 3: [4]}
assert sorted(app_members(1, kids, {1, 3})) == [1, 2]
assert sorted(app_members(3, kids, {1, 3})) == [3, 4]
print("ok")
