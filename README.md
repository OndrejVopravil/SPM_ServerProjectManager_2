# SPM – Server Project Manager

Správa projektů ve složkách na síťovém úložišti: projekt se **aktivuje** (zamkne a zkopíruje na místní disk),
upravuje se lokálně a při **deaktivaci** se na server nahrají jen změněné soubory s povinným komentářem.

## Spuštění

Dvojklikem na **`SPM.bat`**. Při prvním spuštění skript sám vytvoří `.venv` a nainstaluje závislosti.

Ručně:

```
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m spm
```

Testy: `.venv\Scripts\python.exe -m pytest tests`

## Samostatná verze (bez Pythonu)

`build.bat` vytvoří `dist\SPM\SPM.exe` a zip `dist\SPM-<verze>.zip` (~30 MB). Zip stačí na cílovém
PC rozbalit do složky s právem zápisu (např. `C:\SPM`, ne `Program Files`) a spustit `SPM.exe`.
Nastavení se ukládá do `settings.json` vedle `SPM.exe`.

## Struktura na serveru

```
\\server\projekty\ProjektA\
    .spm\project.db        SQLite – soubory, sezení (kdo/kdy/komentář), změny
    .spm\lock.json         existuje, jen když je projekt aktivován
    .spm\backup\...        předchozí verze: soubor.backup01, .backup02, … (posledních N)
    .spm\trash\<sezení>\   soubory smazané během daného sezení
    ...soubory projektu...
```

## Moduly

| Modul | Účel |
|---|---|
| `spm/sync.py` | aktivace, návrh a provedení deaktivace, zálohy, koš |
| `spm/matcher.py` | detekce přejmenovaných souborů (hash + podobnost názvu/velikosti/složky) |
| `spm/db.py` | schéma a přístup k databázi projektu |
| `spm/lock.py` | atomický zámek projektu (`O_CREAT \| O_EXCL`) |
| `spm/scanner.py` | procházení složek, SHA-256, bezpečné kopírování přes dočasný soubor |
| `spm/config.py` | nastavení v `settings.json` ve složce aplikace (vedle `SPM.bat`) |
| `spm/gui/` | PySide6 rozhraní |
