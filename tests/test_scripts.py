"""Skripty odevzdání: jazyk, příkazy, MIRROR a napojení na odevzdání."""
import json
import re
import stat
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from spm import db as dbm
from spm.config import Settings
from spm.db import ProjectDB
from spm.scripting import lang, store
from spm.scripting.lang import ScriptSyntaxError
from spm.scripting.model import ScriptFile, TabInfo
from spm.scripting.runtime import ScriptEnv, compile_mask, map_path, run_scripts, select
from spm.sync import ProjectManager, SyncError

NOW = datetime(2026, 10, 16, 9, 5, 7)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
    path.write_text(text, encoding="utf-8")


def files_in(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and ".spm" not in p.parts)


@pytest.fixture
def env(tmp_path):
    o, w = tmp_path / "O", tmp_path / "W"
    proj = o / "058768"
    write(proj / "dily" / "nc_codes" / "E01.nc", "E01 v1")
    write(proj / "dily" / "nc_codes" / "E02.nc", "E02 v1")
    write(proj / "dily" / "nc_codes" / "popis.txt", "popis")
    write(proj / "dily" / "A" / "1234_hridel.h", "hlavicka")
    write(proj / "stare" / "nc_codes" / "X.nc", "stary")
    w.mkdir()

    def pm(root, user):
        return ProjectManager(Settings(server_root=str(root), local_root=str(tmp_path / f"l_{root.name}_{user}")),
                              user=user, pc=f"PC-{user}")

    alice = pm(o, "alice")
    alice.activate("058768")
    plan = alice.plan_upload("058768")
    alice.deactivate(plan, plan.default_assignments(), "")
    alice_w, bob_w = pm(w, "alice"), pm(w, "bob")
    tabs = [TabInfo("o", "Nástrojárna", "O-server", str(o), alice.settings.local_root),
            TabInfo("w", "Výroba", "", str(w), alice_w.settings.local_root)]
    pms = {"o": alice, "w": alice_w}

    def make(files=None, action="submit", comment="komentář", commit_id=7, project="058768"):
        if files is None:
            files = alice.preview_files(project)
        return ScriptEnv(alice, project, action, files, comment, commit_id, tabs[0], tabs,
                         lambda t: pms[t.ws_id], nick="Alča", now=NOW)

    class Env:
        pass
    e = Env()
    e.alice, e.alice_w, e.bob_w, e.o, e.w, e.tmp, e.make = alice, alice_w, bob_w, o, w, tmp_path, make
    return e


def run(env_obj, text, dry=False, name="test.spm"):
    report = run_scripts(env_obj, [(name, text)], dry=dry)
    return report


def ok(report):
    assert report.ok, report.text
    return report


# --- jazyk ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, line, message", [
    ('COPI a TO "x"', 1, "Neznámý příkaz"),
    ('SET a = 1\nIF a == 1\nLOG "x"', 2, "IF bez END"),
    ("ELSE", 1, "ELSE bez"),
    ("END", 1, "END bez"),
    ('FOR f IN commit\nLOG "x"', 1, "FOR bez END"),
    ('WRITE "a.txt"\nradek', 1, "WRITE bez END"),
    ('ASK "Pokračovat?"', 1, "je funkce"),
    ('FORM "x"\nINPUT a "A"', 1, "FORM bez END"),
    ('FORM "x"\nEND', 1, "bez položek"),
    ('FORM "x"\nCOPY a TO "b"\nEND', 2, "jen INPUT, CHOOSE nebo ASK"),
    ('FORM "x"\nINPUT a "A"\nASK A "B"\nEND', 3, "dvakrát"),
    ('FORM "x"\nCHOOSE a "A" "b"\nEND', 2, "FROM"),
    ('CALL', 1, "neúplný"),
    ("SET to = 1", 1, "klíčové slovo"),
    ("IF a = 1\nEND", 1, "=="),
    ('LOG "neukončený', 1, "uvozovka"),
    ('LOG "{COUNT(commit "*.nc)}"', 1, "uvozovka"),
    ('FILES x = commit "*.nc" STATUS novy', 1, "Neznámý stav"),
    ('COPY commit TO "x" KEEP 1 FLAT', 1, "jen jedno"),
    ('\n\nCOPY commit "x"', 3, "TO"),
])
def test_syntax_errors_with_line(text, line, message):
    with pytest.raises(ScriptSyntaxError) as e:
        lang.parse_script(text, "s.spm")
    assert e.value.line == line and message in str(e.value) and "s.spm" in str(e.value)


def test_parse_structure():
    script = lang.parse_script('''# komentář
FILES nc = commit "**/*.nc" STATUS changed, unchanged   # za příkazem
if count(nc) > 0 and not exists "x"
    copy nc to "{WORK}/a" keep 2 name "{file.stem}.bak"
else
    log "nic"
end
WRITE "a.bat" ENCODING ANSI LF
    @echo off
      echo "{PROJECT}"
END
''')
    files, cond, write_cmd = script.body
    assert files.expr.statuses == ["added", "modified", "renamed", "deleted", "unchanged"]
    assert isinstance(cond, lang.IfCmd) and len(cond.then) == 1 and len(cond.otherwise) == 1
    copy = cond.then[0]
    assert copy.mapping.kind == "keep" and copy.mapping.keep == 2 and copy.rename.raw == "{file.stem}.bak"
    assert write_cmd.lines == ["@echo off", '  echo "{PROJECT}"'] and write_cmd.encoding == "cp1250"
    assert write_cmd.newline == "\n"


def test_masks():
    m = compile_mask("*.nc")
    assert m("E01.nc") and m("a/b/E01.NC") and not m("a/E01.nc.bak")
    m = compile_mask("**/CAM/*.nc;*.mpf")
    assert m("x/CAM/a.nc") and m("CAM/a.nc") and m("q/z.MPF") and not m("x/CAM/sub/a.nc")
    m = compile_mask("Dily/**;!Dily/stare/**")
    assert m("Dily/a.h") and m("Dily/x/y.h") and not m("Dily/stare/a.h") and not m("Jine/a.h")
    m = compile_mask("*hridel*")
    assert m("A/1234_hridel.h") and not m("hridel/a.h")
    assert compile_mask("!*.txt")("a.nc") and not compile_mask("!*.txt")("a.txt")


def test_select_renamed_by_old_and_new_name():
    files = [ScriptFile("a/E01.nc", "renamed", "1", "a/E01.old"),
             ScriptFile("a/E02.txt", "renamed", "2", "a/E02.nc"),
             ScriptFile("a/E03.nc", "renamed", "3", "a/E03.nc.bak"),
             ScriptFile("a/E04.nc", "renamed", "4", "b/E04.nc"),
             ScriptFile("a/E05.nc", "deleted", "5")]
    got = {f.file_id: (f.status, f.path) for f in select(files, "*.nc", None)}
    assert got == {"1": ("added", "a/E01.nc"), "2": ("deleted", "a/E02.nc"),
                   "3": ("added", "a/E03.nc"), "4": ("renamed", "a/E04.nc"),
                   "5": ("deleted", "a/E05.nc")}
    assert [f.file_id for f in select(files, None, ["deleted"])] == ["5"]


def test_map_path():
    rel = "Zakazka/Dily/A/x.h"
    assert map_path(rel, lang.Mapping()) == rel
    assert map_path(rel, lang.Mapping("base"), "zakazka/dily") == "A/x.h"
    assert map_path(rel, lang.Mapping("keep", keep=2)) == "Dily/A/x.h"
    assert map_path(rel, lang.Mapping("keep", keep=0)) == "x.h"
    assert map_path(rel, lang.Mapping("keep", keep=9)) == rel
    with pytest.raises(ValueError, match="BASE"):
        map_path(rel, lang.Mapping("base"), "Jine")


# --- hodnoty a proměnné -----------------------------------------------------------------

def test_variables_functions_and_properties(env):
    report = ok(run(env.make(), r'''
LOG "{USER}|{PC}|{NICK}|{PROJECT}|{COMMENT}|{COMMIT_ID}|{ACTION}|{TAB}|{SERVER_NAME}"
LOG "{DATE}|{TIME}|{YY}|{YYYY}|{MM}|{DD}|{HH}|{MIN}|{SS}"
SET h = ONE(commit "*hridel*")
LOG "{h.name}|{h.stem}|{h.ext}|{h.folder}|{h.parent(2)}|{h.dir}|{h.path}|{h.status}"
PARSE h.name "{zak}_{nazev}.h"
LOG "{zak}-{nazev}-{UPPER(nazev)}-{LOWER("AB")}"
SET kod = MATCH(h.name, "^(\d+)")
LOG "kod={kod} count={COUNT(commit "*.nc")} first={FIRST(commit "*.nc").name} last={LAST(commit "*.nc").name}"
LOG "{REPLACE(h.name, "HRIDEL", "osa")} {{literal}} {TAB:Výroba} {TAB("o-server")}"
SET p = "C:\data\x.zip"
LOG "{p.stem}|{p.ext}|{p.folder}"
'''))
    log = report.log
    assert "alice|PC-alice|Alča|058768|komentář|7|submit|Nástrojárna|" in log
    assert "261016|0905|26|2026|10|16|09|05|07" in log
    assert "1234_hridel.h|1234_hridel|.h|A|dily|dily/A|dily/A/1234_hridel.h|unchanged" in log
    assert "1234-hridel-HRIDEL-ab" in log
    assert "kod=1234 count=3 first=E01.nc last=X.nc" in log
    assert f"1234_osa.h {{literal}} {env.w} {env.o}" in log
    assert "x|.zip|data" in log


def test_conditions(env):
    report = ok(run(env.make(), '''
SET a = "10"
IF a > 9 AND a < 11 AND "Abc" == "aBC" AND "abc" CONTAINS "B" AND "abc" STARTS "a" AND "abc" ENDS "c"
    LOG "ano1"
END
IF "x.nc" LIKE "*.NC" OR NOT EXISTS "neexistuje.txt"
    LOG "ano2"
END
IF COUNT(commit "*.pdf")
    LOG "spatne"
ELSE
    LOG "ano3"
END
FOR f IN commit "*.nc" STATUS unchanged
    IF f.folder == "nc_codes" AND f.parent(2) != "stare"
        LOG "nc {f.name}"
    END
END
'''))
    assert [l for l in report.log if l.startswith(("ano", "nc", "spatne"))] == \
        ["ano1", "ano2", "ano3", "nc E01.nc", "nc E02.nc"]


@pytest.mark.parametrize("text, message", [
    ('LOG "{neznama}"', "Neznámá proměnná"),
    ('SET PROJECT = "x"', "vestavěná"),
    ('LOG "{commit}"', "COMMIT_ID"),
    ('SET x = ONE(commit "*.nc")', "více souborů"),
    ('SET x = FIRST(commit "*.pdf")', "prázdná"),
    ('PARSE "abc" "{a}_{b}"', "neodpovídá"),
    ('LOG "{TAB:Neni}"', "Záložka „Neni“ neexistuje"),
    ('STOP "Chybí program {PROJECT}"', "STOP: Chybí program 058768"),
    ('SET x = 1\nIF commit == 1\nEND', "COUNT"),
])
def test_runtime_errors(env, text, message):
    report = run(env.make(), text)
    assert not report.ok and message in report.error
    assert "test.spm, řádek" in report.error


def test_exit_ends_script_and_next_script_runs(env):
    report = run_scripts(env.make(), [("a.spm", 'LOG "a1"\nEXIT\nLOG "a2"'), ("b.spm", 'LOG "b1"')])
    assert report.ok and "a1" in report.log and "a2" not in report.log and "b1" in report.log


def test_syntax_error_stops_before_any_action(env):
    out = env.tmp / "out"
    report = run_scripts(env.make(), [("a.spm", f'MKDIR "{out}"'), ("b.spm", "NESMYSL")])
    assert not report.ok and "b.spm, řádek 1" in report.error and not out.exists()


# --- soubory ---------------------------------------------------------------------------

def test_copy_zip_unzip(env):
    out = env.tmp / "out"
    ok(run(env.make(), f'''
FILES nc = commit "dily/**/*.nc"
COPY nc TO "{out}/keep" KEEP 1
COPY nc TO "{out}/flat" FLAT NAME "{{file.stem}}_{{DATE}}{{file.ext}}"
COPY nc TO "{out}/base" BASE "dily"
COPY commit "*.h" TO "{out}/proj"
ZIP nc TO "{{WORK}}/nc.zip" KEEP 1
COPY "{{WORK}}/nc.zip" TO "{out}"
UNZIP "{out}/nc.zip" TO "{out}/rozbaleno"
ZIP "{out}/keep" TO "{out}/slozka.zip"
'''))
    assert files_in(out / "keep") == ["nc_codes/E01.nc", "nc_codes/E02.nc"]
    assert files_in(out / "flat") == ["E01_261016.nc", "E02_261016.nc"]
    assert files_in(out / "base") == ["nc_codes/E01.nc", "nc_codes/E02.nc"]
    assert files_in(out / "proj") == ["dily/A/1234_hridel.h"]
    assert files_in(out / "rozbaleno") == ["nc_codes/E01.nc", "nc_codes/E02.nc"]
    with zipfile.ZipFile(out / "slozka.zip") as zf:
        assert sorted(zf.namelist()) == ["nc_codes/E01.nc", "nc_codes/E02.nc"]
    assert (out / "keep" / "nc_codes" / "E01.nc").read_text(encoding="utf-8") == "E01 v1"
    copy = out / "keep" / "nc_codes" / "E01.nc"
    assert copy.stat().st_mode & stat.S_IWRITE                     # kopie není jen pro čtení


def test_zip_duplicate_names_and_empty(env):
    dup = env.make([ScriptFile("a/x.nc", "unchanged", "1"), ScriptFile("b/x.nc", "unchanged", "2")])
    report = run(dup, f'ZIP commit "*.nc" TO "{env.tmp}/x.zip" FLAT')
    assert not report.ok and "stejnou cestu" in report.error
    report = ok(run(env.make(), f'ZIP commit "*.pdf" TO "{env.tmp}/prazdny.zip"'))
    assert not (env.tmp / "prazdny.zip").exists() and any("nic k zabalení" in l for l in report.log)


def test_write_append_rename_replace_delete(env, monkeypatch):
    from spm import recycle
    out = env.tmp / "out"
    binned = []
    monkeypatch.setattr(recycle, "to_recycle_bin", lambda paths: binned.extend(paths))
    ok(run(env.make(), f'''
WRITE "{out}/spust.bat" ENCODING ANSI
    @echo off
    echo Žluťoučký {{PROJECT}}
END
APPEND "{out}/spust.bat" ENCODING ANSI
    echo další
END
WRITE "{out}/unix.txt" LF
a
b
END
MKDIR "{out}/nc_v1"
WRITE "{out}/nc_v1/E01_v1.nc"
x
END
REPLACE NAMES IN "{out}" "_V1" "_v2"
RENAME "{out}/unix.txt" TO "linux.txt"
WRITE "{{WORK}}/tmp.txt"
x
END
DELETE "{{WORK}}/tmp.txt"
DELETE "{out}/linux.txt"
DELETE "{out}/neni.txt"
'''))
    assert (out / "spust.bat").read_bytes() == "@echo off\r\necho Žluťoučký 058768\r\necho další\r\n".encode("cp1250")
    assert files_in(out) == ["linux.txt", "nc_v2/E01_v2.nc", "spust.bat"]
    assert (out / "linux.txt").read_bytes() == b"a\nb\n"
    assert binned == [out / "linux.txt"]                                # mimo {WORK} do Koše


def test_write_unencodable_character(env):
    report = run(env.make(), f'WRITE "{env.tmp}/a.txt" ENCODING ASCII\nčau\nEND')
    assert not report.ok and "kódování" in report.error


def test_writing_into_tab_folders_is_refused(env):
    for target in (env.o / "jine", env.w / "058768", Path(env.alice.settings.local_root) / "x"):
        report = run(env.make(), f'COPY commit "*.h" TO "{target}"')
        assert not report.ok and "MIRROR" in report.error, target
    report = run(env.make(), f'WRITE "{env.w}/a.txt"\nx\nEND')
    assert not report.ok and "nesmí" in report.error


def test_relative_paths_go_to_work_and_work_is_removed(env):
    report = ok(run(env.make(), 'WRITE "a/b.txt"\nx\nEND\nLOG "{WORK}"\nIF EXISTS "a/b.txt"\nLOG "je"\nEND'))
    assert "je" in report.log
    work = Path(report.log[report.log.index("je") - 1])
    assert work.name.startswith("spm_skript_") and not work.exists()


def test_dry_run_writes_nothing(env):
    out = env.tmp / "out"
    report = ok(run(env.make(), f'''
ZIP commit "*.h" TO "{{WORK}}/h.zip"
COPY "{{WORK}}/h.zip" TO "{out}"
UNZIP "{out}/h.zip" TO "{out}/x"
WRITE "{out}/a.txt"
x
END
RENAME "{out}/a.txt" TO "b.txt"
MIRROR commit "*.nc" TO "{{TAB:Výroba}}"
''', dry=True))
    assert not out.exists() and not (env.w / "058768").exists()
    assert all(l.startswith("(zkouška)") for l in report.log[1:] if not l.startswith("=="))
    assert any("MIRROR: 3 soubory" in l for l in report.log)


# --- MIRROR ---------------------------------------------------------------------------------

def mirror_changes(proj: Path):
    with ProjectDB.for_project(proj) as db:
        commits = [c for c in db.commits() if c["kind"] == dbm.MIRROR]
        return [(ch["change_type"], ch["new_path"] or ch["old_path"])
                for c in commits for ch in db.changes(c["commit_id"])]


MIRROR_NC = 'MIRROR commit "dily/**/*.nc" TO "{TAB:Výroba}"'


def submit(e, edit, comment="zmena"):
    """Aktivace, úprava v místní kopii, deaktivace – vrací ScriptEnv s odevzdanými soubory."""
    e.alice.activate("058768")
    edit(e.alice.local_dir("058768"))
    plan = e.alice.plan_upload("058768")
    res = e.alice.deactivate(plan, plan.default_assignments(), comment)
    return e.make(res.script_files, action="deactivate", comment=comment, commit_id=res.commit_id)


def test_initial_mirror_creates_project_and_locks_files(env):
    ok(run(env.make(), MIRROR_NC))
    w = env.w / "058768"
    assert files_in(w) == ["dily/nc_codes/E01.nc", "dily/nc_codes/E02.nc"]
    assert sorted(mirror_changes(w)) == [("added", "dily/nc_codes/E01.nc"), ("added", "dily/nc_codes/E02.nc")]
    entries = {e.path: e for e in env.bob_w.file_entries("058768")}
    assert entries["dily/nc_codes/E01.nc"].mirror == str(env.o / "058768")
    assert entries["dily/nc_codes/E01.nc"].mirror_server_name == ""
    with pytest.raises(SyncError, match="zrcadlo"):
        env.bob_w.activate("058768", lock_paths=["dily/nc_codes/E01.nc"])
    with ProjectDB.for_project(w) as db:
        assert "Zrcadleno z" in db.commits()[0]["comment"] and "komentář" in db.commits()[0]["comment"]
    # podruhé beze změny – žádný záznam
    ok(run(env.make(), MIRROR_NC))
    with ProjectDB.for_project(w) as db:
        assert len([c for c in db.commits() if c["kind"] == dbm.MIRROR]) == 1
    assert not list(w.rglob("*.spmtmp"))


def test_changes_renames_and_deletions_follow_source(env):
    ok(run(env.make(), MIRROR_NC))

    def edit(local):
        write(local / "dily/nc_codes/E01.nc", "E01 v2")
        (local / "dily/nc_codes/E02.nc").rename(local / "dily/nc_codes/E02b.nc")
        write(local / "dily/nc_codes/E03.nc", "E03")
        (local / "dily/nc_codes/popis.txt").unlink()
    e = submit(env, edit)
    statuses = {f.path: f.status for f in e.files}
    assert statuses == {"dily/A/1234_hridel.h": "unchanged", "dily/nc_codes/E01.nc": "modified",
                        "dily/nc_codes/E02b.nc": "renamed", "dily/nc_codes/E03.nc": "added",
                        "dily/nc_codes/popis.txt": "deleted", "stare/nc_codes/X.nc": "unchanged"}
    report = ok(run(e, MIRROR_NC))
    w = env.w / "058768"
    assert files_in(w) == ["dily/nc_codes/E01.nc", "dily/nc_codes/E02b.nc", "dily/nc_codes/E03.nc"]
    assert (w / "dily/nc_codes/E01.nc").read_text(encoding="utf-8") == "E01 v2"
    assert list((w / ".spm" / "backup").rglob("E01.nc.backup01"))
    assert any("nové: 1, změněné: 1, přejmenované: 1" in l for l in report.log)

    e = submit(env, lambda local: (local / "dily/nc_codes/E03.nc").unlink())
    ok(run(e, MIRROR_NC))
    assert not (w / "dily/nc_codes/E03.nc").exists()
    assert list((w / ".spm" / "trash").rglob("E03.nc"))

    # přejmenování na název mimo masku = v cíli smazán
    e = submit(env, lambda local: (local / "dily/nc_codes/E02b.nc").rename(local / "dily/nc_codes/E02b.bak"))
    ok(run(e, MIRROR_NC))
    assert files_in(w) == ["dily/nc_codes/E01.nc"]


def test_deleted_folder_removed_from_mirror_with_status_changed(env):
    """Smazaná složka se soubory: STATUS changed zahrnuje i smazané, zrcadlo je v cíli smaže."""
    script = '''FILES nc = commit "**/nc_codes/*.nc" STATUS changed
IF COUNT(nc) > 0
    MIRROR nc TO "{TAB:Výroba}/{PROJECT}/NCFiles"
    LOG "Zrcadleno souborů: {COUNT(nc)}"
END
'''
    e = submit(env, lambda local: write(local / "dily/nc_codes/E01.nc", "E01 v2"))
    ok(run(e, script.replace("STATUS changed", "")))                  # první zrcadlení všeho
    target = env.w / "058768" / "NCFiles"
    assert files_in(target) == ["dily/nc_codes/E01.nc", "dily/nc_codes/E02.nc", "stare/nc_codes/X.nc"]

    def drop(local):
        import shutil
        for path in (local / "dily" / "nc_codes").rglob("*"):
            path.chmod(stat.S_IWRITE | stat.S_IREAD)
        shutil.rmtree(local / "dily" / "nc_codes")
    e = submit(env, drop)
    report = ok(run(e, script))
    assert files_in(target) == ["stare/nc_codes/X.nc"]
    assert "Zrcadleno souborů: 2" in report.log and "MIRROR hotovo: smazané: 2" in report.log
    assert list((env.w / "058768" / ".spm" / "trash").rglob("E01.nc"))


def source_targets(e) -> dict[str, list[tuple[str, str]]]:
    """Cesta ve zdroji -> [(cílový projekt, cesta v cíli)] podle označení ve zdroji."""
    return {x.path: [(Path(t.label).name, t.path) for t in x.mirror_targets]
            for x in e.alice.file_entries("058768") if x.mirror_targets}


def test_source_files_marked_as_mirrored(env):
    ok(run(env.make(), MIRROR_NC))
    assert source_targets(env) == {"dily/nc_codes/E01.nc": [("058768", "dily/nc_codes/E01.nc")],
                                   "dily/nc_codes/E02.nc": [("058768", "dily/nc_codes/E02.nc")]}
    e01 = next(x for x in env.alice.file_entries("058768") if x.path.endswith("E01.nc"))
    assert Path(e01.mirror_targets[0].label) == env.w / "058768"
    # druhý cíl s jiným mapováním – soubor má dva cíle
    ok(run(env.make(), 'MIRROR commit "**/E01.nc" TO "{TAB:Výroba}/CAM/NC" FLAT'))
    assert sorted(source_targets(env)["dily/nc_codes/E01.nc"]) == \
        [("058768", "dily/nc_codes/E01.nc"), ("CAM", "NC/E01.nc")]

    def edit(local):
        (local / "dily/nc_codes/E01.nc").rename(local / "dily/nc_codes/E01b.nc")
        (local / "dily/nc_codes/E02.nc").unlink()
    e = submit(env, edit)
    ok(run(e, MIRROR_NC))
    marks = source_targets(env)
    # přejmenovaný: cesta v cíli se změnila; do druhého cíle CAM se tentokrát nezrcadlil –
    # označení zmizelo (kopie v CAM neodpovídá); smazaný už označený není
    assert marks["dily/nc_codes/E01b.nc"] == [("058768", "dily/nc_codes/E01b.nc")]
    assert "dily/nc_codes/E02.nc" not in marks


MIRROR_IF_ASKED = '''IF ASK("Zrcadlit?")
    MIRROR commit "dily/**/*.nc" TO "{TAB:Výroba}"
END
'''


def test_skipped_mirror_removes_marks_of_changed_files(env):
    ok(run(env.make(), MIRROR_NC))
    ok(run(env.make(), 'MIRROR commit "**/E01.nc" TO "{TAB:Výroba}/CAM" FLAT'))

    def edit(local):
        write(local / "dily/nc_codes/E01.nc", "E01 v2")
    e = submit(env, edit)                                   # E01 změněn, E02 beze změny
    e.ui = FakeUI(ask=False)                                # uživatel zrcadlení odmítne
    report = ok(run(e, MIRROR_IF_ASKED))
    marks = source_targets(env)
    assert "dily/nc_codes/E01.nc" not in marks              # kopie v cílech je stará
    assert marks["dily/nc_codes/E02.nc"] == [("058768", "dily/nc_codes/E02.nc")]
    assert any("u 2 změněných souborů odebráno" in l for l in report.log)   # dva cíle
    # znovu, se zrcadlením jen do jednoho cíle: označení se vrátí jen u něj
    e.ui = FakeUI(ask=True)
    ok(run(e, MIRROR_IF_ASKED))
    assert source_targets(env)["dily/nc_codes/E01.nc"] == [("058768", "dily/nc_codes/E01.nc")]


def target_entries(env) -> dict:
    return {x.path: x for x in env.bob_w.file_entries("058768")}


def test_excluded_changed_file_is_stale_in_target(env):
    """Změněný soubor, který skript vynechal: ve zdroji ztratí značku, v cíli je neaktuální."""
    ok(run(env.make(), MIRROR_NC))
    assert not any(x.mirror_stale for x in target_entries(env).values())

    def edit(local):
        write(local / "dily/nc_codes/E01.nc", "E01 v2")
        (local / "dily/nc_codes/E02.nc").unlink()
    e = submit(env, edit)
    report = ok(run(e, 'MIRROR commit "dily/**/*.nc;!*E01.nc;!*E02.nc" TO "{TAB:Výroba}"'))
    assert any("neaktuální" in l for l in report.log)
    assert source_targets(env) == {}
    entries = target_entries(env)
    e01, e02 = entries["dily/nc_codes/E01.nc"], entries["dily/nc_codes/E02.nc"]
    assert (e01.mirror_stale.reason, e01.mirror_stale.by) == ("changed", "alice")
    assert e02.mirror_stale.reason == "deleted"                    # v cíli zůstal
    assert (env.w / "058768/dily/nc_codes/E01.nc").read_text(encoding="utf-8") == "E01 v1"
    # další zrcadlení souboru příznak smaže
    ok(run(e, MIRROR_NC))
    entries = target_entries(env)
    assert "dily/nc_codes/E02.nc" not in entries
    assert entries["dily/nc_codes/E01.nc"].mirror_stale is None
    assert (env.w / "058768/dily/nc_codes/E01.nc").read_text(encoding="utf-8") == "E01 v2"


def test_stale_mirror_cleared_when_content_already_current(env):
    """Neaktuální kopie, kterou další MIRROR ověří beze změny obsahu (např. opakovaný běh)."""
    ok(run(env.make(), MIRROR_NC))
    e = submit(env, lambda local: write(local / "dily/nc_codes/E01.nc", "E01 v2"))
    ok(run(e, MIRROR_NC))                                           # zrcadleno
    e.ui = FakeUI(ask=False)
    ok(run(e, MIRROR_IF_ASKED))                                     # přeskočeno – neaktuální
    assert target_entries(env)["dily/nc_codes/E01.nc"].mirror_stale is not None
    e.ui = FakeUI(ask=True)
    ok(run(e, MIRROR_IF_ASKED))                                     # obsah v cíli už odpovídá
    assert target_entries(env)["dily/nc_codes/E01.nc"].mirror_stale is None


def test_release_stale_mirrored_file(env):
    ok(run(env.make(), MIRROR_NC))
    e = submit(env, lambda local: write(local / "dily/nc_codes/E01.nc", "E01 v2"))
    ok(run(e, 'MIRROR commit "dily/**/*.nc;!*E01.nc" TO "{TAB:Výroba}"'))
    # aktuální E02 se neuvolní, neaktuální E01 ano
    assert env.bob_w.release_mirrored("058768", ["dily/nc_codes/E01.nc", "dily/nc_codes/E02.nc"]) == 1
    entries = target_entries(env)
    assert entries["dily/nc_codes/E01.nc"].mirror is None and entries["dily/nc_codes/E01.nc"].free
    assert entries["dily/nc_codes/E02.nc"].mirror is not None
    with ProjectDB.for_project(env.w / "058768") as db:
        assert "Uvolněno ze zrcadla" in db.commits()[0]["comment"]
    env.bob_w.activate("058768", lock_paths=["dily/nc_codes/E01.nc"])  # už jde převzít
    # zdroj ho znovu zrcadlí – drží ho bob, takže konflikt (nic se nepřepíše)
    assert not run(e, MIRROR_NC).ok
    assert (env.w / "058768/dily/nc_codes/E01.nc").read_text(encoding="utf-8") == "E01 v1"


def test_stale_target_not_in_tabs_only_warns(env):
    ok(run(env.make(), MIRROR_NC))
    e = submit(env, lambda local: write(local / "dily/nc_codes/E01.nc", "E01 v2"))
    e.tabs = e.tabs[:1]                                             # cíl není mezi záložkami
    report = ok(run(e, 'LOG "nic"'))
    assert any("není mezi vašimi záložkami" in l for l in report.log)
    assert "dily/nc_codes/E01.nc" not in source_targets(env)
    assert target_entries(env)["dily/nc_codes/E01.nc"].mirror_stale is None


def test_stopped_or_broken_script_removes_marks(env):
    ok(run(env.make(), MIRROR_NC))
    e = submit(env, lambda local: write(local / "dily/nc_codes/E01.nc", "E01 v2"))
    assert not run(e, 'STOP "ne"\n' + MIRROR_NC).ok
    assert list(source_targets(env)) == ["dily/nc_codes/E02.nc"]
    ok(run(e, MIRROR_NC))
    e2 = submit(env, lambda local: write(local / "dily/nc_codes/E01.nc", "E01 v3"))
    assert not run(e2, "NESMYSL").ok                        # chyba zápisu – skript neběží
    assert list(source_targets(env)) == ["dily/nc_codes/E02.nc"]
    ok(run(e2, MIRROR_NC, dry=True))                        # zkušební běh nic nemění
    assert list(source_targets(env)) == ["dily/nc_codes/E02.nc"]


def test_conflicted_file_not_marked(env):
    write(env.w / "058768" / "dily/nc_codes/E01.nc", "verze z W")
    env.bob_w.activate("058768")
    assert not run(env.make(), MIRROR_NC).ok
    assert list(source_targets(env)) == ["dily/nc_codes/E02.nc"]


def test_unchanged_file_missing_in_target_is_restored(env):
    ok(run(env.make(), MIRROR_NC))
    w = env.w / "058768"
    (w / "dily/nc_codes/E02.nc").unlink()
    ok(run(env.make(), MIRROR_NC))
    assert (w / "dily/nc_codes/E02.nc").read_text(encoding="utf-8") == "E02 v1"


def test_mirror_mapping_into_other_project_and_folder(env):
    ok(run(env.make(), 'MIRROR commit "*.nc" STATUS unchanged TO "{TAB:Výroba}/CAM_{PROJECT}/NC" KEEP 1'))
    target = env.w / "CAM_058768"
    assert files_in(target) == ["NC/nc_codes/E01.nc", "NC/nc_codes/E02.nc", "NC/nc_codes/X.nc"]
    # změna mapování = přesun v cíli
    ok(run(env.make(), 'MIRROR commit "dily/**/*.nc" TO "{TAB:Výroba}/CAM_{PROJECT}/NC" FLAT'))
    assert files_in(target) == ["NC/E01.nc", "NC/E02.nc", "NC/nc_codes/X.nc"]


def test_mirror_errors(env):
    dup = env.make([ScriptFile("a/x.nc", "unchanged", "1"), ScriptFile("b/x.nc", "unchanged", "2")])
    report = run(dup, 'MIRROR commit "*.nc" TO "{TAB:Výroba}" FLAT')
    assert not report.ok and "stejnou cestu" in report.error
    report = run(env.make(), f'MIRROR commit "*.h" TO "{env.tmp / "jinam"}"')
    assert not report.ok and "neleží v žádné záložce" in report.error
    report = run(env.make(), 'MIRROR commit "*.h" TO "{TAB:Nástrojárna}"')
    assert not report.ok and "sám do sebe" in report.error


def test_existing_target_files_adopted_or_overwritten_with_backup(env):
    w = env.w / "058768"
    write(w / "dily/nc_codes/E01.nc", "E01 v1")          # stejný obsah, mimo evidenci
    write(w / "dily/nc_codes/E02.nc", "jina verze")      # jiný obsah
    write(w / "jen_na_W.txt", "w")
    report = ok(run(env.make(), MIRROR_NC))
    assert any("převzaté: 1" in l for l in report.log)
    assert (w / "dily/nc_codes/E02.nc").read_text(encoding="utf-8") == "E02 v1"
    assert list((w / ".spm" / "backup").rglob("E02.nc.backup01"))
    assert (w / "jen_na_W.txt").exists()


def test_file_held_on_target_fails_script(env):
    w = env.w / "058768"
    write(w / "dily/nc_codes/E01.nc", "verze z W")
    env.bob_w.activate("058768")
    report = run(env.make(), MIRROR_NC)
    assert not report.ok and "E01.nc" in report.error and "bob" in report.error
    assert (w / "dily/nc_codes/E01.nc").read_text(encoding="utf-8") == "verze z W"
    assert (w / "dily/nc_codes/E02.nc").exists()                      # zbytek proběhl
    env.bob_w.cancel_activation("058768")
    ok(run(env.make(), MIRROR_NC))                                     # opakování dokončí


def test_forbidden_name_on_target_not_mirrored(env):
    env.alice.settings.check_forbidden_chars = False
    e = submit(env, lambda local: write(local / "dily/nc_codes/Výkres 1.nc", "x"))
    report = run(e, MIRROR_NC)
    assert not report.ok and "Výkres 1.nc" in report.error and "znaky" in report.error
    assert not (env.w / "058768" / "dily/nc_codes/Výkres 1.nc").exists()


def test_source_identity_and_legacy_key(env):
    ok(run(env.make(), MIRROR_NC))
    w = env.w / "058768"
    with ProjectDB.for_project(w) as db:              # stav z dřívější verze: klíč = cesta
        db.conn.execute("UPDATE mirror_files SET source = ?", (str(env.o / "058768"),))
        db.conn.execute("DELETE FROM mirror_sources")
        db.conn.commit()
    report = ok(run(env.make(), MIRROR_NC))
    assert any("MIRROR hotovo: beze změny" in l for l in report.log)
    with ProjectDB.for_project(env.o / "058768") as db:
        pid = db.project_id()
    with ProjectDB.for_project(w) as db:
        assert {s for s, _ in db.mirrored().values()} == {pid}


def test_permission_error_reported(env, monkeypatch):
    from spm import mirror as mirror_mod

    def denied(src, dst):
        raise PermissionError(13, "Přístup byl odepřen")
    monkeypatch.setattr(mirror_mod, "copy_with_hash", denied)
    report = run(env.make(), MIRROR_NC)
    assert not report.ok and "oprávnění" in report.error


# --- odevzdání ---------------------------------------------------------------------------

def test_submit_without_changes_has_no_script_files(env):
    env.alice.activate("058768")
    plan = env.alice.plan_upload("058768")
    res = env.alice.deactivate(plan, plan.default_assignments(), "")
    assert res.script_files is None and res.local_deleted


def test_partial_submit_contains_only_submitted_files(env):
    env.alice.activate("058768", lock_paths=["dily/nc_codes/E01.nc", "dily/nc_codes/E02.nc"])
    local = env.alice.local_dir("058768")
    write(local / "dily/nc_codes/E01.nc", "E01 v2")
    plan = env.alice.plan_upload("058768")
    e01 = next(bf.file_id for bf, _ in plan.modified)
    res = env.alice.upload(plan, plan.default_assignments(), "jen E01", selected={e01})
    assert [(f.path, f.status) for f in res.script_files] == [("dily/nc_codes/E01.nc", "modified")]
    assert not res.ended and res.commit_id
    # deaktivace: E01 už odevzdaný – jen zbytek (E02 beze změny)
    plan = env.alice.plan_upload("058768")
    res = env.alice.deactivate(plan, plan.default_assignments(), "")
    assert res.script_files is None                                    # bez změn skripty neběží


def test_keep_local_until_scripts_finish(env):
    env.alice.activate("058768")
    local = env.alice.local_dir("058768")
    write(local / "dily/nc_codes/E01.nc", "E01 v2")
    plan = env.alice.plan_upload("058768")
    res = env.alice.upload(plan, plan.default_assignments(), "zmena", end_session=True, keep_local=True)
    assert res.ended and res.cleanup_pending and not res.local_deleted and local.exists()
    # skript čte z místní kopie (odevzdaná verze)
    e = env.make(res.script_files, action="deactivate", commit_id=res.commit_id)
    report = ok(run(e, 'LOG "{ONE(commit \"*E01*\").full}"'))
    assert str(local) in report.log[-1]
    assert env.alice.finish_local("058768") and not local.exists()


def test_publish_returns_files_for_scripts(env):
    local = Path(env.alice.settings.local_root) / "NOVY"
    write(local / "a.nc", "a")
    write(local / "b" / "c.h", "c")
    res = env.alice.publish("NOVY", "první")
    assert [(f.path, f.status) for f in res.script_files] == [("a.nc", "added"), ("b/c.h", "added")]
    assert res.commit_id and all(f.file_id for f in res.script_files)
    e = env.make(res.script_files, action="publish", project="NOVY")
    ok(run(e, 'MIRROR commit TO "{TAB:Výroba}"'))                       # čte ze serveru
    assert files_in(env.w / "NOVY") == ["a.nc", "b/c.h"]


def test_record_script_run(env):
    env.alice.record_script_run("058768", 1, "submit", ["a.spm", "b.spm"], False, "log\nchyba")
    with ProjectDB.for_project(env.o / "058768") as db:
        runs = db.script_runs()
    assert len(runs) == 1 and runs[0]["scripts"] == "a.spm, b.spm" and runs[0]["ok"] == 0
    assert runs[0]["log"] == "log\nchyba" and runs[0]["user"] == "alice"


def test_pending_roundtrip_and_status(env):
    root = Path(env.alice.settings.local_root)
    files = [ScriptFile("a.nc", "renamed", "1", "b.nc", "abc", 3), ScriptFile("x", "deleted", "2")]
    store.save_pending(root, store.PendingRun("058768", "deactivate", "k", 5, files, ["s.spm"],
                                              error="chyba", cleanup=True))
    loaded = store.load_pending(root, "058768")
    assert loaded.files == files and loaded.cleanup and loaded.commit_id == 5 and loaded.error == "chyba"
    status = next(p for p in env.alice.list_projects() if p.name == "058768")
    assert status.script_pending
    assert all(p.name != store.PENDING_DIR for p in env.alice.list_projects())   # není „Jen místně“
    store.clear_pending(root, "058768")
    assert not env.alice.project_status("058768").script_pending


def test_script_files_store(tmp_path, monkeypatch):
    from spm import config
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    assert store.list_scripts() == []
    store.save_script("b.spm", 'LOG "b"')
    store.save_script("A.spm", 'LOG "a"')
    (store.scripts_dir() / "poznamka.txt").write_text("x")
    assert store.list_scripts() == ["A.spm", "b.spm"]
    store.rename_script("b.spm", "c.spm")
    assert store.load_script("c.spm") == 'LOG "b"'
    with pytest.raises(FileExistsError):
        store.rename_script("c.spm", "A.spm")
    assert store.normalize_name(" x ") == "x.spm" and store.validate_name("a/b.spm")
    assert store.validate_name(".spm") and store.validate_name("ok.spm") is None
    assert lang.parse_script(store.TEMPLATE, "sablona")      # šablona nového skriptu je platná
    assert lang.parse_script(store.NETWORK_TEMPLATE, "sit")
    net = tmp_path / "srv" / ".spm" / "scripts"
    assert store.network_dir(tmp_path / "srv") == net and store.list_scripts(net) == []
    store.save_script("x.spm", "EXIT", net)
    assert store.list_scripts(net) == ["x.spm"] and store.load_script("x.spm", net) == "EXIT"
    assert store.list_scripts() == ["A.spm", "c.spm"]                 # místní beze změny


# --- dialogy (etapa 2) -----------------------------------------------------------------

class FakeUI:
    """Odpovědi na dialogy skriptu; zaznamenává, na co se skript ptal."""

    def __init__(self, ask=True, input="vstup", choose=0, form=None, cancel=False):
        self.answers = {"ask": ask, "input": input, "choose": choose, "form": form}
        self.cancel = cancel
        self.asked = []

    def _reply(self, kind, *args):
        self.asked.append((kind,) + args)
        if self.cancel and kind != "ask":
            return None
        answer = self.answers[kind]
        return answer(*args) if callable(answer) else answer

    def ask(self, title, text):
        return self._reply("ask", title, text)

    def input(self, title, prompt, default):
        return self._reply("input", title, prompt, default)

    def choose(self, title, prompt, options, default):
        return self._reply("choose", title, prompt, options, default)

    def form(self, title, text, fields):
        return self._reply("form", title, text, fields)


def with_ui(env, ui, **kw):
    e = env.make(**kw)
    e.ui = ui
    return e


def test_ask_input_choose(env):
    ui = FakeUI(ask=True, input=lambda t, p, d: d + "-X", choose=lambda t, p, o, d: o.index("DMU80") if "DMU80" in o else 0)
    report = ok(run(with_ui(env, ui), '''
IF ASK("Pokračovat s {PROJECT}?")
    LOG "ano"
END
SET zak = INPUT("Zakázka", "{PROJECT}")
SET stroj = CHOOSE("Stroj", "DMU50", "DMU80")
SET prog = CHOOSE("Program", commit "*.nc" STATUS unchanged)
LOG "{zak}|{stroj}|{prog.name}"
'''))
    assert "ano" in report.log and "058768-X|DMU80|E01.nc" in report.log
    kinds = [a[0] for a in ui.asked]
    assert kinds == ["ask", "input", "choose", "choose"]
    assert ui.asked[0][1] == "Skript test.spm – projekt 058768" and ui.asked[0][2] == "Pokračovat s 058768?"
    assert ui.asked[3][3] == ["dily/nc_codes/E01.nc", "dily/nc_codes/E02.nc", "stare/nc_codes/X.nc"]
    assert any(l.startswith("ASK „Pokračovat s 058768?“: ano") for l in report.log)


def test_ask_no_and_one_offers_choice(env):
    ui = FakeUI(ask=False, choose=1)
    report = ok(run(with_ui(env, ui), '''
IF NOT ASK("Zrcadlit?")
    LOG "ne"
END
SET f = ONE(commit "dily/**/*.nc")
LOG "{f.name}"
'''))
    assert "ne" in report.log and "E02.nc" in report.log
    assert ui.asked[1][2] == "Odpovídá více souborů – vyberte jeden:"


@pytest.mark.parametrize("text, message", [
    ('SET a = INPUT("Zakázka")', "Zadání „Zakázka“ bylo zrušeno"),
    ('SET a = CHOOSE("Stroj", "A", "B")', "Výběr „Stroj“ byl zrušen"),
    ('FORM "Předání"\nINPUT a "A"\nEND', "Formulář „Předání“ byl zrušen"),
])
def test_cancelled_dialog_stops_script(env, text, message):
    report = run(with_ui(env, FakeUI(cancel=True)), text)
    assert not report.ok and message in report.error


def test_dialog_without_window_is_error(env):
    report = run(env.make(), 'IF ASK("x")\nEND')
    assert not report.ok and "neběží v okně" in report.error
    report = run(env.make(), 'SET f = ONE(commit "*.nc")')                # bez okna: jako dřív
    assert not report.ok and "více souborů" in report.error


def test_form(env):
    def fill(title, text, fields):
        assert text == "Předání 058768"
        by_name = {f["name"]: f for f in fields}
        assert by_name["zak"] == {"kind": "INPUT", "name": "zak", "label": "Zakázka",
                                  "default": "058768", "options": []}
        assert by_name["stroj"]["options"] == ["DMU50", "DMU80"] and by_name["stroj"]["default"] == 1
        assert by_name["prog"]["options"][0] == "dily/nc_codes/E01.nc"
        assert by_name["mail"]["default"] is True and by_name["tisk"]["default"] is False
        return {"zak": "123", "stroj": 0, "prog": 1, "mail": False, "tisk": True}
    report = ok(run(with_ui(env, FakeUI(form=fill)), '''
FORM "Předání {PROJECT}"
    INPUT zak "Zakázka" DEFAULT "{PROJECT}"
    CHOOSE stroj "Stroj" FROM "DMU50", "DMU80" DEFAULT "dmu80"
    CHOOSE prog "Program" FROM commit "*.nc"
    ASK mail "Poslat e-mail" DEFAULT 1
    ASK tisk "Tisk"
END
LOG "{zak}|{stroj}|{prog.name}|{mail}|{tisk}"
'''))
    assert "123|DMU50|E02.nc|0|1" in report.log
    assert any(l.startswith("FORM „Předání 058768“: zak = 123") for l in report.log)


# --- CALL -------------------------------------------------------------------------------

def test_call_server_project_and_local_share_variables(env, tmp_path, monkeypatch):
    from spm import config
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    store.save_script("spolecne.spm", 'SET cil = "{WORK}/{PROJECT}"\nLOG "server {kdo}"',
                      store.network_dir(env.o))
    store.save_script("projekt.spm", 'LOG "projekt"\nEXIT\nLOG "nikdy"',
                      store.network_dir(env.o / "058768"))
    store.save_script("pomocny.spm", 'SET z_pomocneho = "ano"')
    report = ok(run(env.make(), '''
SET kdo = "místní"
CALL SERVER "spolecne"
CALL PROJECT "projekt.spm"
CALL "pomocny"
LOG "cil={cil} z={z_pomocneho}"
'''))
    log = report.log
    assert "server místní" in log and "projekt" in log and "nikdy" not in log
    assert any(l.startswith("cil=") and l.endswith("058768 z=ano") for l in log)
    assert any(l.startswith("CALL ") and l.endswith("spolecne.spm") for l in log)


def test_call_errors(env, tmp_path, monkeypatch):
    from spm import config
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    net = store.network_dir(env.o)
    report = run(env.make(), 'CALL SERVER "neni"')
    assert not report.ok and "neexistuje" in report.error and "test.spm, řádek 1" in report.error
    store.save_script("vadny.spm", 'LOG "a"\nCOPI', net)
    report = run(env.make(), 'LOG "x"\nCALL SERVER "vadny"')
    assert not report.ok and "test.spm, řádek 2" in report.error and "chyba na řádku 2" in report.error
    store.save_script("stop.spm", 'LOG "a"\nSTOP "ze sítě"', net)
    report = run(env.make(), 'CALL SERVER "stop"')
    assert not report.ok and report.error.startswith("stop.spm, řádek 2: STOP: ze sítě")
    store.save_script("smycka.spm", 'CALL SERVER "smycka"', net)
    report = run(env.make(), 'CALL SERVER "smycka"')
    assert not report.ok and "příliš hluboké" in report.error


# --- BEFORE (před odevzdáním) ------------------------------------------------------------

from spm.scripting.runtime import ScriptSession  # noqa: E402
from spm.sync import selection_files  # noqa: E402

ZIP_BEFORE = '''BEFORE
    FILES nc = commit "dily/**/*.nc" STATUS changed
    SET archiv = "Export/{PROJECT}.zip"
    ZIP nc TO "{LOCAL_DIR}/{archiv}" KEEP 1
END
LOG "po odevzdání: {archiv} {COMMIT_ID}"
MIRROR commit "**/Export/*.zip" TO "{TAB:Výroba}"
'''


def activate_and_edit(e):
    e.alice.activate("058768")
    local = e.alice.local_dir("058768")
    write(local / "dily/nc_codes/E01.nc", "E01 v2")
    return local


def two_phases(e, script, selected=None, end_session=True, ui=None):
    """Odevzdání jako v aplikaci: BEFORE -> převzetí vytvořených souborů -> nahrání -> zbytek."""
    pm = e.alice
    plan = pm.plan_upload("058768")
    assignments = plan.default_assignments()
    env = e.make(selection_files(plan, assignments, selected), commit_id=None)
    env.ui = ui
    session = ScriptSession(env, [("s.spm", script)])
    assert session.run_before(), session.report.text
    plan2, selected2, added = pm.adopt_script_outputs("058768", plan, selected,
                                                      activated=session.interp.activated)
    res = pm.upload(plan2, assignments, "zmena", selected2, end_session)
    session.run_after(res.script_files, res.commit_id)
    return session.report, res, added


def test_before_creates_file_that_is_submitted_and_mirrored(env):
    local = activate_and_edit(env)
    report, res, added = two_phases(env, ZIP_BEFORE)
    assert report.ok, report.text
    assert added == ["Export/058768.zip"]
    statuses = {f.path: f.status for f in res.script_files}
    assert statuses["Export/058768.zip"] == "added" and statuses["dily/nc_codes/E01.nc"] == "modified"
    assert (env.o / "058768" / "Export" / "058768.zip").exists()            # odevzdán na server
    assert files_in(env.w / "058768") == ["Export/058768.zip"]               # a zrcadlen
    assert f"po odevzdání: Export/058768.zip {res.commit_id}" in report.log  # proměnná z BEFORE
    with zipfile.ZipFile(env.o / "058768" / "Export" / "058768.zip") as zf:
        assert zf.namelist() == ["nc_codes/E01.nc"]
    assert not local.exists()                                              # deaktivace uklidila


def before_session(e, script, ui=None):
    plan = e.alice.plan_upload("058768")
    env = e.make(selection_files(plan, plan.default_assignments(), None), commit_id=None)
    env.ui = ui
    return plan, ScriptSession(env, [("s.spm", script)])


def test_before_overwrite_of_unlocked_file_asks(env):
    activate_and_edit(env)
    two_phases(env, ZIP_BEFORE)                                   # zip je v projektu
    zip_on_server = (env.o / "058768" / "Export" / "058768.zip").read_bytes()

    # nová aktivace jen s E01 – zip mám jen pro čtení (nezamčený)
    def reactivate():
        env.alice.activate("058768", lock_paths=["dily/nc_codes/E01.nc"])
        write(env.alice.local_dir("058768") / "dily/nc_codes/E01.nc", "E01 v3")

    reactivate()
    plan, session = before_session(env, ZIP_BEFORE)               # bez okna – nelze se zeptat
    assert not session.run_before() and "neaktivovaného souboru Export/058768.zip" in session.report.error
    assert "ACTIVATE" in session.report.error
    plan, session = before_session(env, ZIP_BEFORE, FakeUI(ask=False))   # uživatel odmítne
    assert not session.run_before() and "odmítnut" in session.report.error
    assert (env.o / "058768" / "Export" / "058768.zip").read_bytes() == zip_on_server
    ui = FakeUI(ask=True)                                         # uživatel souhlasí
    report, res, added = two_phases(env, ZIP_BEFORE, ui=ui)
    assert report.ok, report.text
    assert "nemáte aktivovaný" in ui.asked[0][2] and "Export/058768.zip" in ui.asked[0][2]
    assert added == ["Export/058768.zip"]
    assert {f.path: f.status for f in res.script_files}["Export/058768.zip"] == "modified"
    assert list((env.o / "058768" / ".spm" / "backup").rglob("058768.zip.backup01"))


def test_before_cannot_overwrite_file_held_by_other(env):
    bob = ProjectManager(Settings(server_root=str(env.o), local_root=str(env.tmp / "l_bob")),
                         user="bob", pc="PC-bob")
    bob.activate("058768", lock_paths=["dily/A/1234_hridel.h"])
    env.alice.activate("058768", lock_paths=["dily/nc_codes/E01.nc"])
    write(env.alice.local_dir("058768") / "dily/nc_codes/E01.nc", "E01 v2")
    plan, session = before_session(env, 'BEFORE\n  WRITE "{LOCAL_DIR}/dily/A/1234_hridel.h"\n  x\n  END\nEND',
                                   FakeUI(ask=True))
    assert not session.run_before() and "drží bob" in session.report.error
    assert (env.o / "058768" / "dily/A/1234_hridel.h").read_text(encoding="utf-8") == "hlavicka"


def test_run_changing_unlocked_file_is_refused(env, tmp_path):
    env.alice.activate("058768", lock_paths=["dily/nc_codes/E01.nc"])
    write(env.alice.local_dir("058768") / "dily/nc_codes/E01.nc", "E01 v2")
    target = env.alice.local_dir("058768") / "dily" / "A" / "1234_hridel.h"
    prog = py_script(tmp_path, "import sys, os\nos.chmod(sys.argv[1], 0o666)\n"
                               "open(sys.argv[1], 'w').write('zmeneno programem')\n")
    plan, session = before_session(env, f'BEFORE\n  RUN "{PY}" "{prog}" "{target}"\nEND')
    assert session.run_before(), session.report.text
    with pytest.raises(SyncError, match="(?s)nemáte aktivované.*ACTIVATE"):
        env.alice.adopt_script_outputs("058768", plan, None, activated=session.interp.activated)
    assert (env.o / "058768" / "dily/A/1234_hridel.h").read_text(encoding="utf-8") == "hlavicka"


TEST_TXT = """BEFORE
    IF ACTIVATE("{LOCAL_DIR}/test.txt")
        LOG "aktivován"
    END
    FILES txt = commit "test.txt"
    IF COUNT(txt) > 0
        APPEND "{LOCAL_DIR}/test.txt" LF
            další {COMMENT}
        END
    ELSE
        WRITE "{LOCAL_DIR}/test.txt" LF
            první
        END
    END
END
"""


def test_activate_then_append_or_write(env):
    # 1. test.txt v projektu není – ACTIVATE vrátí 0, vytvoří se nový
    activate_and_edit(env)
    report, res, added = two_phases(env, TEST_TXT)
    assert report.ok, report.text
    assert "aktivován" not in report.log and any("v projektu není" in l for l in report.log)
    assert (env.o / "058768" / "test.txt").read_text(encoding="utf-8") == "první\n"

    # 2. test.txt existuje, nemám ho aktivovaný; mezitím ho bob změnil na serveru
    env.alice.activate("058768", lock_paths=["dily/nc_codes/E01.nc"])
    bob = ProjectManager(Settings(server_root=str(env.o), local_root=str(env.tmp / "l_bob")),
                         user="bob", pc="PC-bob")
    bob.activate("058768", lock_paths=["test.txt"])
    write(bob.local_dir("058768") / "test.txt", "první\nbob\n")
    bplan = bob.plan_upload("058768")
    bob.deactivate(bplan, bplan.default_assignments(), "bob")
    write(env.alice.local_dir("058768") / "dily/nc_codes/E01.nc", "E01 v3")
    plan = env.alice.plan_upload("058768")
    e01 = next(bf.file_id for bf, lf in plan.modified if lf.path.endswith("E01.nc"))
    report, res, added = two_phases(env, TEST_TXT, selected={e01}, end_session=False)
    assert report.ok, report.text
    assert "aktivován" in report.log and "test.txt" in added
    # připsáno k aktuální verzi ze serveru (bobova změna zůstala)
    assert (env.o / "058768" / "test.txt").read_text(encoding="utf-8") == "první\nbob\ndalší komentář\n"
    assert {f.path: f.status for f in res.script_files}["test.txt"] == "modified"
    entries = {e.path: e for e in env.alice.file_entries("058768")}
    assert entries["test.txt"].owner is None                      # odevzdáním se uvolnil


def test_activate_cases(env, tmp_path):
    bob = ProjectManager(Settings(server_root=str(env.o), local_root=str(env.tmp / "l_bob")),
                         user="bob", pc="PC-bob")
    bob.activate("058768", lock_paths=["dily/A/1234_hridel.h"])
    env.alice.activate("058768", lock_paths=["dily/nc_codes/E01.nc"])
    write(env.alice.local_dir("058768") / "dily/nc_codes/E01.nc", "E01 v2")
    plan, session = before_session(env, """BEFORE
    LOG "neni={ACTIVATE("neni.txt")} drzi={ACTIVATE("dily/A/1234_hridel.h")} moje={ACTIVATE("dily/nc_codes/E01.nc")}"
    SET e02 = "{SERVER_DIR}/dily/nc_codes/E02.nc"
    LOG "volny={ACTIVATE(e02)}"
    ACTIVATE "{LOCAL_DIR}/stare/nc_codes/X.nc"
    LOG "pocet={COUNT(commit STATUS unchanged)}"
END""")
    assert session.run_before(), session.report.text
    log = session.report.log
    assert "neni=0 drzi=0 moje=1" in log and "volny=1" in log and "pocet=2" in log
    assert any("drží ho bob" in l for l in log)
    assert sorted(session.interp.activated) == ["dily/nc_codes/E02.nc", "stare/nc_codes/X.nc"]
    entries = {e.path: e for e in env.alice.file_entries("058768")}
    assert entries["dily/nc_codes/E02.nc"].mine and entries["stare/nc_codes/X.nc"].mine


@pytest.mark.parametrize("script, message", [
    ('ACTIVATE "test.txt"', "jen v bloku BEFORE"),
    ('BEFORE\n  ACTIVATE "C:/jinde/test.txt"\nEND', "neleží v projektu"),
    ('BEFORE\n  ACTIVATE "../x.txt"\nEND', "neplatná cesta"),
])
def test_activate_errors(env, script, message):
    activate_and_edit(env)
    report = run(env.make(), script)
    assert not report.ok and message in report.error


def test_dry_run_does_not_lock_or_ask(env):
    activate_and_edit(env)
    two_phases(env, ZIP_BEFORE)
    env.alice.activate("058768", lock_paths=["dily/nc_codes/E01.nc"])
    write(env.alice.local_dir("058768") / "dily/nc_codes/E01.nc", "E01 v3")
    ui = FakeUI(ask=True)
    e = env.make()
    e.ui = ui
    report = ok(run(e, ZIP_BEFORE.replace("MIRROR", "#MIRROR") + 'BEFORE\n  ACTIVATE "stare/nc_codes/X.nc"\nEND\n',
                    dry=True))
    assert ui.asked == []
    assert any("zeptá na aktivaci" in l for l in report.log) and any("nezamčeno" in l for l in report.log)
    entries = {x.path: x for x in env.alice.file_entries("058768")}
    assert not entries["Export/058768.zip"].mine and not entries["stare/nc_codes/X.nc"].mine


def test_partial_submit_adds_created_file_to_selection(env):
    activate_and_edit(env)
    local = env.alice.local_dir("058768")
    write(local / "dily/nc_codes/E02.nc", "E02 v2")               # změněný, ale neodevzdávaný
    plan = env.alice.plan_upload("058768")
    e01 = next(bf.file_id for bf, lf in plan.modified if lf.path.endswith("E01.nc"))
    report, res, added = two_phases(env, ZIP_BEFORE, selected={e01}, end_session=False)
    assert report.ok, report.text
    assert sorted(f.path for f in res.script_files) == ["Export/058768.zip", "dily/nc_codes/E01.nc"]
    with zipfile.ZipFile(env.o / "058768" / "Export" / "058768.zip") as zf:
        assert zf.namelist() == ["nc_codes/E01.nc"]                 # BEFORE viděl jen výběr


@pytest.mark.parametrize("script, message", [
    ('BEFORE\n  DELETE "{LOCAL_DIR}/dily/A/1234_hridel.h"\nEND', "jen vytvářet a přepisovat"),
    ('BEFORE\n  WRITE "{LOCAL_DIR}/.spm/x.txt"\n  x\n  END\nEND', "servisní složky"),
    ('WRITE "{LOCAL_DIR}/x.txt"\nx\nEND', "jen v bloku BEFORE"),
    ('BEFORE\n  CALL "m"\nEND', "MIRROR nelze použít v BEFORE"),
    ('BEFORE\n  STOP "ne"\nEND\nLOG "po"', "STOP: ne"),
])
def test_before_restrictions(env, tmp_path, monkeypatch, script, message):
    from spm import config
    monkeypatch.setattr(config, "APP_DIR", tmp_path / "app")
    store.save_script("m.spm", 'MIRROR commit TO "{TAB:Výroba}"')
    activate_and_edit(env)
    report = run(env.make(), script)
    assert not report.ok and message in report.error
    assert "po" not in report.log


def test_exit_in_before_skips_rest_of_script(env):
    report = ok(run(env.make(), 'BEFORE\n  LOG "a"\n  EXIT\nEND\nLOG "b"'))
    assert "a" in report.log and "b" not in report.log


def test_dry_run_shows_files_created_in_before(env):
    activate_and_edit(env)
    report = ok(run(env.make(), ZIP_BEFORE, dry=True))
    assert not (env.alice.local_dir("058768") / "Export").exists()
    assert any("MIRROR: 1 soubor" in l for l in report.log)
    assert any("Export/058768.zip (added)" in l for l in report.log)


def test_session_vars_roundtrip(env):
    session = ScriptSession(env.make(), [("s.spm", 'BEFORE\n  SET a = "x"\n  SET f = FIRST(commit)\n'
                                                   '  FILES m = commit "*.nc"\nEND\n'
                                                   'LOG "{a} {f.name} {COUNT(m)}"')])
    assert session.run_before()
    data = json.loads(json.dumps(session.export_vars()))            # přežije uložení do souboru
    again = ScriptSession(env.make(), [("s.spm", 'BEFORE\n  STOP "nespouštět"\nEND\n'
                                                 'LOG "{a} {f.name} {COUNT(m)}"')])
    again.import_vars(data)
    assert again.run_after()
    assert "x 1234_hridel.h 3" in again.report.log


# --- RUN, POST, SECRET (etapa 3) ------------------------------------------------------------

import sys  # noqa: E402

PY = sys.executable


def py_script(tmp_path, code: str) -> Path:
    path = tmp_path / "prog.py"
    path.write_text(code, encoding="utf-8")
    return path


def test_run_program_with_result(env, tmp_path):
    prog = py_script(tmp_path, "import sys, os\nprint('argumenty:', sys.argv[1:])\n"
                               "print('slozka:', os.path.basename(os.getcwd()))\nprint('Žluťoučký')\n"
                               "sys.exit(3)\n")
    report = ok(run(env.make(), f'''
FILES h = commit "*.h"
RUN "{PY}" "{prog}" "a b" h "{{PROJECT}}" -> vysledek
LOG "kód={{vysledek.code}}"
IF vysledek.output CONTAINS "Žluťoučký" AND vysledek.output STARTS "argumenty"
    LOG "výstup ok"
END
'''))
    assert "kód=3" in report.log and "výstup ok" in report.log
    out = "\n".join(report.log)
    assert "'a b'" in out and "1234_hridel.h" in out and "'058768'" in out
    assert "slozka: spm_skript_" in out                       # pracovní složka = {WORK}


def test_run_failure_without_variable_and_missing_program(env, tmp_path):
    prog = py_script(tmp_path, "print('chyba vstupu')\nraise SystemExit(2)\n")
    report = run(env.make(), f'RUN "{PY}" "{prog}"')
    assert not report.ok and "kódem 2" in report.error and "chyba vstupu" in report.error
    report = run(env.make(), 'RUN "neexistujici_program_xyz.exe"')
    assert not report.ok and "nebyl nalezen" in report.error


def test_run_timeout_cwd_nowait_and_dry(env, tmp_path):
    slow = py_script(tmp_path, "import time\ntime.sleep(5)\n")
    report = run(env.make(), f'RUN "{PY}" "{slow}" WAIT 1')
    assert not report.ok and "neskončil do 1 s" in report.error
    (tmp_path / "jinde").mkdir()
    cwd = py_script(tmp_path, "import os\nopen('vystup.txt','w').write(os.getcwd())\n")
    ok(run(env.make(), f'RUN "{PY}" "{cwd}" IN "{tmp_path / "jinde"}"'))
    assert (tmp_path / "jinde" / "vystup.txt").exists()
    report = ok(run(env.make(), f'RUN "{PY}" "{slow}" NOWAIT'))
    assert any("NOWAIT" in l for l in report.log)
    report = ok(run(env.make(), f'RUN "{PY}" "{cwd}" IN "{tmp_path / "neni"}" -> r\nLOG "k={{r.code}}"',
                    dry=True))
    assert "k=0" in report.log and any("nespustil" in l for l in report.log)


def test_run_in_before_creates_project_file(env, tmp_path):
    activate_and_edit(env)
    gen = py_script(tmp_path, "import sys, pathlib\np = pathlib.Path(sys.argv[1])\n"
                              "p.parent.mkdir(parents=True, exist_ok=True)\np.write_text('report')\n")
    script = f'BEFORE\n    RUN "{PY}" "{gen}" "{{LOCAL_DIR}}/Report/r.txt"\nEND\n'
    report, res, added = two_phases(env, script)
    assert report.ok, report.text
    assert added == ["Report/r.txt"] and (env.o / "058768" / "Report" / "r.txt").exists()


@pytest.mark.parametrize("text, message", [
    ('RUN "x" NOWAIT -> r', "NOWAIT nelze"),
    ('RUN "x" WAIT', "neúplný"),
    ('RUN "x" -> ', "neúplný"),
    ('POST "http://x"', "POST bez END"),
    ('POST "http://x"\n  a b\nEND', "="),
    ('POST "http://x" HEADER "a"\nEND', "neúplný"),
])
def test_run_post_syntax_errors(text, message):
    with pytest.raises(ScriptSyntaxError, match=message):
        lang.parse_script(text)


@pytest.fixture
def http_server():
    import http.server
    import threading
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            received.append({"path": self.path, "headers": dict(self.headers),
                             "body": json.loads(self.rfile.read(length).decode("utf-8"))})
            status = 500 if self.path == "/chyba" else 200
            body = json.dumps({"id": 42, "polozky": [{"nazev": "Ř1"}], "ok": True}, ensure_ascii=False)
            if status == 500:
                body = "Interní chyba"
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", received
    server.shutdown()
    server.server_close()


def test_post_json_and_response(env, http_server):
    url, received = http_server
    e = env.make()
    e.secrets = {"cam": "tajny-klic-123"}
    report = ok(run(e, f'''
FILES nc = commit "dily/**/*.nc"
POST "{url}/api" HEADER "Authorization" "Bearer {{SECRET:cam}}" TIMEOUT 5 -> odpoved
    projekt = PROJECT
    "počet" = COUNT(nc)
    soubory = nc
    prvni = FIRST(nc)
END
LOG "stav={{odpoved.status}} id={{odpoved.json("id")}} nazev={{odpoved.json("polozky.0.nazev")}} ok={{odpoved.json("ok")}} chybi=[{{odpoved.json("neni.nic")}}]"
LOG "klíč je {{SECRET:cam}}"
'''))
    req = received[0]
    assert req["path"] == "/api" and req["headers"]["Authorization"] == "Bearer tajny-klic-123"
    assert req["body"] == {"projekt": "058768", "počet": 2,
                           "soubory": ["dily/nc_codes/E01.nc", "dily/nc_codes/E02.nc"],
                           "prvni": "dily/nc_codes/E01.nc"}
    assert "stav=200 id=42 nazev=Ř1 ok=1 chybi=[]" in report.log
    assert "klíč je •••" in report.log
    assert "tajny-klic-123" not in report.text                 # tajná hodnota se nevypíše nikde


def test_post_errors(env, http_server):
    url, _ = http_server
    report = run(env.make(), f'POST "{url}/chyba"\nEND')
    assert not report.ok and "chybu 500" in report.error and "Interní chyba" in report.error
    report = ok(run(env.make(), f'POST "{url}/chyba" -> r\nEND\nLOG "s={{r.status}} {{r.body}}"'))
    assert "s=500 Interní chyba" in report.log
    report = run(env.make(), 'POST "http://127.0.0.1:1/x" TIMEOUT 2\nEND')
    assert not report.ok and "spojení" in report.error
    report = run(env.make(), 'POST "ftp://x"\nEND')
    assert not report.ok and "http://" in report.error
    report = run(env.make(), f'POST "{url}/x" -> r\nEND\nLOG "{{r}}"')
    assert not report.ok and "{x.status}" in report.error
    report = run(env.make(), f'POST "{url}/x" -> r\nEND\nLOG "{{r.code}}"')
    assert not report.ok and "nemá vlastnost .code" in report.error


def test_post_dry_run_sends_nothing(env, http_server):
    url, received = http_server
    report = ok(run(env.make(), f'POST "{url}/x" -> r\n  a = 1\nEND\nLOG "s={{r.status}}"', dry=True))
    assert received == [] and "s=200" in report.log


def test_secret_missing_and_masked_in_errors(env):
    report = run(env.make(), 'LOG "{SECRET:neni}"')
    assert not report.ok and "není nastavená" in report.error
    e = env.make()
    e.secrets = {"Cam": "heslo42"}
    report = run(e, 'SET k = SECRET("cam")\nSTOP "klíč {k} nefunguje"')
    assert not report.ok and "heslo42" not in report.error and "klíč ••• nefunguje" in report.error


def test_records_survive_pending_vars(env, tmp_path):
    prog = py_script(tmp_path, "print('ahoj')\n")
    session = ScriptSession(env.make(), [("s.spm", f'BEFORE\n  RUN "{PY}" "{prog}" -> r\nEND\nLOG "{{r.code}} {{r.output}}"')])
    assert session.run_before()
    again = ScriptSession(env.make(), [("s.spm", 'LOG "{r.code} {r.output}"')])
    again.import_vars(json.loads(json.dumps(session.export_vars())))
    assert again.run_after() and "0 ahoj" in again.report.log


# --- počítání a FORMAT -------------------------------------------------------------------

def test_arithmetic(env):
    report = ok(run(env.make(), '''
SET a = 2 + 3 * 4
SET b = (2 + 3) * 4
SET c = -b + 1 - -2
SET d = 7 / 2
SET e = 8 / 2
SET f = 10 / 3
SET g = 1.5 * 2
SET n = COUNT(commit "*.nc") * 10 - 1
LOG "{a}|{b}|{c}|{d}|{e}|{f}|{g}|{n}|{a * 2}|{(a + b) / 2}"
'''))
    assert "14|20|-17|3,5|4|3,333333|3|29|28|17" in report.log


def test_numbers_from_text(env):
    ui = FakeUI(input="2,5")
    report = ok(run(with_ui(env, ui), '''
SET x = INPUT("Koeficient")
PARSE "pos_015.h" "pos_{p}.h"
SET y = x * 2 + p
LOG "{y}|{p + 0}"
IF p == 15 AND p < 100 AND "9" < "10"
    LOG "číselně"
END
IF "abc" < "abd"
    LOG "abecedně"
END
'''))
    assert "20|15" in report.log and "číselně" in report.log and "abecedně" in report.log


def test_format(env):
    report = ok(run(env.make(), '''
SET a = 7 FORMAT "000"
SET b = 3.14159 FORMAT "0.00"
SET c = 3.05 FORMAT "00,0"
SET d = 2.5 FORMAT "0"
SET e = 1234.5 FORMAT "00.0"
SET f = -0.001 FORMAT "0.00"
LOG "{a}|{b}|{c}|{d}|{e}|{f}|{FORMAT(COUNT(commit), "00")}|{FORMAT("2,345", "0,00")}|{a + 1}"
'''))
    assert "007|3.14|03,1|3|1234.5|0.00|05|2,35|8" in report.log


def test_format_stays_with_variable(env):
    ui = FakeUI(input="7")
    session = ScriptSession(with_ui(env, ui), [("s.spm", '''
BEFORE
    SET a = 1 FORMAT "000"
    SET a = a + 1
    LOG "a: {a}"
END
SET a = a * 10
LOG "po: {a}"
SET a = INPUT("Číslo")
LOG "zadáno: {a}"
SET a = "text"
LOG "text: {a}"
SET a = 2.5 FORMAT "0,0"
SET a = a * 3
LOG "nový formát: {a}"
SET b = a
LOG "jiná proměnná: {b}"
''')])
    assert session.run_before()
    data = json.loads(json.dumps(session.export_vars()))       # formát přežije i opakovaný běh
    assert session.run_after(), session.report.text
    log = session.report.log
    assert "a: 002" in log and "po: 020" in log and "zadáno: 007" in log
    assert "text: text" in log and "nový formát: 7,5" in log and "jiná proměnná: 7,5" in log
    again = ScriptSession(env.make(), [("s.spm", 'SET a = 5\nLOG "znovu: {a}"')])
    again.import_vars({"s.spm": {k: v for k, v in data["s.spm"].items()}})
    assert again.run_after() and "znovu: 005" in again.report.log


@pytest.mark.parametrize("text, message", [
    ('SET a = 1 / 0', "Dělení nulou"),
    ('SET a = "abc" + 1', "„abc“ není číslo"),
    ('SET a = commit + 1', "množina není číslo"),
    ('SET a = 5 FORMAT "#.##"', "maska"),
    ('SET a = "x" FORMAT "0"', "není číslo"),
    ('LOG "{FORMAT(1)}"', "2 argumenty"),
])
def test_arithmetic_errors(env, text, message):
    report = run(env.make(), text)
    assert not report.ok and message in report.error


@pytest.mark.parametrize("text, message", [
    ("SET a = (1 + 2", "„)“"),
    ("SET a = 1 +", "neúplný"),
    ("SET a = * 2", "Očekává se hodnota"),
    ('SET a = 1 FORMAT 2', "maska formátu"),
])
def test_arithmetic_syntax_errors(text, message):
    with pytest.raises(ScriptSyntaxError, match=re.escape(message)):
        lang.parse_script(text)
