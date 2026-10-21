"""Přehled aktivity nad časovou osou: období (den / týden / měsíc), počty po uživatelích,
skok na období, vazba na filtry."""
import os
from datetime import date, datetime, timedelta

import pytest

from spm.gui.activity_strip import ActivityStrip, bucket_label, buckets


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def iso(d: date, h: int = 10) -> str:
    return datetime(d.year, d.month, d.day, h).isoformat(timespec="seconds")


def test_buckets_by_width():
    today = date.today()
    recs = [(iso(today), "alice"), (iso(today), "bob"), (iso(today - timedelta(days=3)), "alice")]
    unit, items = buckets(recs, 400)
    assert unit == "day" and len(items) == 4
    assert items[-1].users == {"alice": 1, "bob": 1} and items[0].total == 1 and items[1].total == 0
    long_ago = [(iso(today - timedelta(days=200)), "alice")] + recs
    unit, items = buckets(long_ago, 400)                       # 200 dní se do 80 sloupečků nevejde
    assert unit == "week" and sum(b.total for b in items) == 4
    unit, items = buckets([(iso(today - timedelta(days=2000)), "a")] + recs, 200)
    assert unit == "month" and "20" in bucket_label(unit, items[0])
    assert buckets([], 300) == ("day", [])


def test_strip_in_timeline_jumps_and_follows_filter(app, tmp_path):
    from spm.config import Settings
    from spm.db import ProjectDB
    from spm.gui.timeline_dialog import TimelineDialog
    from spm.sync import ProjectManager
    proj = tmp_path / "server" / "P"
    proj.mkdir(parents=True)
    (proj / "a.txt").write_text("a")
    pm = ProjectManager(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "l")),
                        user="alice", pc="PC")
    for i in range(3):
        pm.activate("P")
        (pm.local_dir("P") / "a.txt").chmod(0o666)
        (pm.local_dir("P") / "a.txt").write_text(f"a{i}")
        plan = pm.plan_upload("P")
        pm.deactivate(plan, plan.default_assignments(), f"verze {i}")
    with ProjectDB.for_project(proj) as db:                    # odevzdání v různých dnech
        ids = [c["commit_id"] for c in db.commits()]
        with db.conn:
            for n, cid in enumerate(ids):
                db.conn.execute("UPDATE commits SET created_at = ? WHERE commit_id = ?",
                                (iso(date.today() - timedelta(days=5 * n)), cid))
        dlg = TimelineDialog(db, "P", me="alice")
        dlg.resize(1000, 600)
        dlg.show()
        app.processEvents()
        strip = dlg.activity
        assert sum(b.total for b in strip.items) == len(ids)
        target = date.today() - timedelta(days=10)
        dlg.jump_to(target, target)
        assert dlg.selected_commit()["created_at"].startswith(target.isoformat())
        dlg.search.setText("verze 0")                          # filtr -> přehled jen z nalezených
        assert sum(b.total for b in strip.items) == 1
        dlg.close()
