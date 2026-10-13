import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtGui import QStandardItemModel
from ktaskmanager import KEY, nice_ceiling, sync

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
print("ok")
