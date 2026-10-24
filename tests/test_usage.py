"""Anonymní hlášení používání a upozornění na novou verzi (spm.vopravil.cz) – bez sítě:
server simuluje místní HTTP server."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from spm import usage


@pytest.fixture
def fake_server():
    """Místní server, který odpovídá jako api/ping.php a pamatuje si, co dostal."""
    received = []
    reply = {"latest": "0.2.0", "url": "https://spm.vopravil.cz/download/SPM-0.2.0.zip", "notes": "Nové\nOpravy"}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append(json.loads(body))
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/api/ping.php", received, reply
    server.shutdown()


def test_install_id_is_stable_and_anonymous():
    a = usage.install_id("Alice", "pc-a")
    assert a == usage.install_id(" alice ", "PC-A")                    # bez ohledu na velikost písmen
    assert len(a) == 64 and "alice" not in a.lower()
    assert a != usage.install_id("alice", "PC-B") and a != usage.install_id("bob", "PC-A")


def test_payload_and_versions():
    assert usage.payload(True, "a", "b", "0.1.27") == {"version": "0.1.27", "id": usage.install_id("a", "b")}
    assert usage.payload(False, "a", "b", "0.1.27") == {"version": "0.1.27"}   # vypnuto: jen dotaz na verzi
    assert usage.is_newer("0.1.28", "0.1.27") and usage.is_newer("0.2.0", "0.1.99")
    assert usage.is_newer("0.1.10", "0.1.9")                          # čísla, ne text
    assert not usage.is_newer("0.1.27", "0.1.27") and not usage.is_newer("nesmysl", "0.1.0")


def test_ping_round_trip(fake_server):
    url, received, reply = fake_server
    info = usage.real_ping(True, "alice", "PC-A", "0.1.27", url=url)
    assert info == usage.UpdateInfo("0.2.0", reply["url"], "Nové\nOpravy")
    assert received[-1] == {"version": "0.1.27", "id": usage.install_id("alice", "PC-A")}
    usage.real_ping(False, "alice", "PC-A", "0.1.27", url=url)
    assert received[-1] == {"version": "0.1.27"}                       # bez id
    reply["url"] = "https://zlo.example.com/SPM.zip"                   # odkaz jen z vlastní domény
    assert usage.real_ping(True, "a", "b", "0.1.27", url=url).url == usage.STATS_URL
    assert usage.real_ping(True, "a", "b", "0.1.27", url="http://127.0.0.1:1/x", timeout=1) is None


def test_install_steps(tmp_path):
    assert "zdrojového kódu" in usage.install_steps(None, "0.2.0")[0]
    steps = usage.install_steps(tmp_path / "Programy" / "SPM", "0.2.0")
    assert "SPM-0.2.0.zip" in steps[0] and f"do složky {tmp_path / 'Programy'}" in steps[2]
    assert "settings.json" in steps[2] and not any("síťové" in s for s in steps)
    steps = usage.install_steps(tmp_path / "Nastroje", "0.2.0")
    assert "obsah složky SPM" in steps[2]
    steps = usage.install_steps(Path(r"\\server\nastroje\SPM"), "0.2.0")
    assert any("síťové složky" in s for s in steps)                    # ostatní musí SPM zavřít


# --- hlavní okno ---------------------------------------------------------------------------

@pytest.fixture
def window(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from spm import config
    from spm.config import Settings
    from spm.gui.main_window import MainWindow
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    app = QApplication.instance() or QApplication([])
    (tmp_path / "server").mkdir()
    w = MainWindow(Settings(server_root=str(tmp_path / "server"), local_root=str(tmp_path / "local")))
    yield w, app
    w._usage_check.wait()
    w._messages_check.wait()
    w.close()
    app.processEvents()


def test_update_notice_and_skip(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    w, app = window
    calls = []
    monkeypatch.setattr(usage, "ping", lambda enabled, login, pc, version, **k: calls.append(enabled)
                        or usage.UpdateInfo("9.9.9", "https://spm.vopravil.cz/download/SPM-9.9.9.zip", "Novinka"))
    shown = []

    def exec_(box):
        shown.append(box.text())
        box._choice = "Připomenout později"
        return 0
    monkeypatch.setattr(QMessageBox, "exec", exec_)
    monkeypatch.setattr(QMessageBox, "clickedButton",
                        lambda box: next(b for b in box.buttons() if b.text() == box._choice))
    w.check_usage(force=True)
    w._usage_check.wait()
    app.processEvents()
    app.processEvents()
    assert calls == [True]                                             # hlášení zapnuté ve výchozím stavu
    assert not w.btn_update.isHidden() and "9.9.9" in w.btn_update.text()
    assert shown and "Novinka" in shown[0] and "SPM-9.9.9.zip" in shown[0] and "Instalace" in shown[0]
    w.check_usage()                                                    # dnes už hlášeno -> bez sítě
    assert calls == [True] and len(shown) == 1                         # dialog jen jednou za verzi
    monkeypatch.setattr(QMessageBox, "exec", lambda box: setattr(box, "_choice", "Tuto verzi přeskočit"))
    assert w.show_update() == "skip"
    assert w.btn_update.isHidden() and w.settings.ui["update.skip"] == "9.9.9"
    w._on_update_info(usage.UpdateInfo("9.9.9", "https://spm.vopravil.cz/x.zip"))
    assert w.btn_update.isHidden()                                     # přeskočená verze se nehlásí


def test_usage_disabled_by_setting(window, monkeypatch):
    w, app = window
    calls = []
    monkeypatch.setattr(usage, "ping", lambda enabled, *a, **k: calls.append(enabled))
    w.settings.usage_report = False                                    # Nastavení / spm_config.json
    assert not w.usage_enabled()
    w.check_usage(force=True)
    w._usage_check.wait()
    assert calls == [False]                                            # jen dotaz na verzi


def test_shared_config_usage_report():
    from spm import shared_config as sc
    from spm.config import Settings
    assert sc.parse('{"usage_report": false}') == {"usage_report": False}
    with pytest.raises(sc.SharedConfigError, match="usage_report"):
        sc.parse('{"usage_report": "ne"}')
    s = Settings()
    assert s.usage_report and sc.apply(s, {"usage_report": False}) and not s.usage_report
