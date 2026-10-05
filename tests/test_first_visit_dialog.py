"""First-visit dialog: dismissing it must not crash, and must fall back to
the quiet choice. Uses a real QMessageBox built exactly as the shipping code
builds it, so the Qt behaviour under test is the real one."""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication, QMessageBox
app = QApplication([])

fails = 0
def check(name, cond):
    global fails
    fails += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}")


def build(with_fix: bool):
    m = QMessageBox()
    m.setIcon(QMessageBox.Icon.Question)
    m.setText("First visit")
    b_all = m.addButton("📥  All personal messages (PN + PY)",
                        QMessageBox.ButtonRole.YesRole)
    b_new = m.addButton("⚡  New messages only (PN)",
                        QMessageBox.ButtonRole.NoRole)
    if with_fix:
        m.setEscapeButton(b_new)
    return m, b_all, b_new


# ── The bug, reproduced against the pre-fix construction ─────────────
m, b_all, b_new = build(with_fix=False)
check("without the fix Qt assigns no escape button", m.escapeButton() is None)
m.reject()
crashed = False
try:
    m.clickedButton().text()          # exactly what the old code did
except AttributeError:
    crashed = True
check("without the fix, dismissing crashes on .text()", crashed)

# ── With the fix ─────────────────────────────────────────────────────
m, b_all, b_new = build(with_fix=True)
check("escape button is 'New messages only'", m.escapeButton() is b_new)
m.reject()
clicked = m.clickedButton()
full = (clicked is b_all)              # exactly what the new code does
check("dismissing does not crash", True)
check("dismissing means new-only, not all", full is False)

# ── Explicit clicks still mean what they say ─────────────────────────
for label, btn, want_full in (("All", "b_all", True), ("New", "b_new", False)):
    m, b_all, b_new = build(with_fix=True)
    target = b_all if btn == "b_all" else b_new
    target.click()
    check(f"clicking '{label}' gives full={want_full}",
          (m.clickedButton() is b_all) is want_full)

print(f"\n{7 - fails}/7 passed")
sys.exit(1 if fails else 0)
