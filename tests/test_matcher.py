from spm.db import DbFile
from spm.matcher import suggest_renames
from spm.scanner import FileInfo


def dbf(fid, path, size=1000, sha="x"):
    return DbFile(fid, path, size, 0.0, sha)


def fi(path, size=1000, sha="y"):
    return FileInfo(path, size, 0.0, sha)


def test_exact_hash_wins():
    missing = [dbf("1", "a/report.pdf", sha="h1")]
    new = [fi("a/report_v2.pdf", sha="zz"), fi("b/something.pdf", sha="h1")]
    [s] = suggest_renames(missing, new)
    assert s.new_path == "b/something.pdf" and s.same_content


def test_similar_name_matches_modified_file():
    [s] = suggest_renames([dbf("1", "doc/zprava.docx", 1000)], [fi("doc/zprava_final.docx", 1100)])
    assert s.new_path == "doc/zprava_final.docx" and s.score >= 0.6


def test_unrelated_files_not_matched():
    assert suggest_renames([dbf("1", "doc/zprava.docx", 1000)], [fi("doc/faktura.xlsx", 50)]) == []


def test_each_file_assigned_once():
    missing = [dbf("1", "x/plan.dwg", 1000), dbf("2", "x/plan_b.dwg", 1000)]
    new = [fi("x/plan_c.dwg", 1000)]
    assert len(suggest_renames(missing, new)) == 1
