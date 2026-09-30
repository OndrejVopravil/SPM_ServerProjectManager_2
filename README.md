# SPM – Server Project Manager

Správa projektů ve složkách na síťovém úložišti. Na jednom projektu může pracovat víc lidí současně –
zamyká se po jednotlivých souborech:

- **Aktivovat** – stáhne celý projekt a zamkne vybrané soubory k úpravě (ostatní jsou jen pro čtení).
- **Odevzdat** – nahraje vybrané změny (s komentářem) a uvolní je; projekt zůstává aktivovaný.
- **Aktualizovat** – stáhne soubory, které mezitím odevzdali ostatní.
- **Převzít** – zamkne další volné soubory k úpravě.
- **Deaktivovat** – odevzdá vše, uvolní všechny zámky a ukončí aktivaci.

Nové soubory může vytvářet kdokoliv; patří tomu, kdo je vytvořil.

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
    .spm\project.db        SQLite – soubory, zámky souborů, sezení, odevzdání (kdo/kdy/komentář), změny
    .spm\active\*.json     kdo má projekt aktivovaný (pro rychlé zobrazení v seznamu)
    .spm\write.lock        existuje jen během krátkého zápisu do databáze
    .spm\backup\...        předchozí verze: soubor.backup01, .backup02, … (posledních N)
    .spm\trash\<sezení>\   soubory smazané během daného sezení
    ...soubory projektu...
```

## Moduly

| Modul | Účel |
|---|---|
| `spm/sync.py` | aktivace, odevzdání, aktualizace, převzetí, deaktivace, zálohy, koš |
| `spm/manifest.py` | místní záznam, z jaké verze na serveru pochází místní kopie |
| `spm/names.py` | nepovolené znaky v názvech |
| `spm/matcher.py` | detekce přejmenovaných souborů (hash + podobnost názvu/velikosti/složky) |
| `spm/db.py` | schéma a přístup k databázi projektu |
| `spm/lock.py` | značky aktivních uživatelů a zámek zápisu do databáze (`O_CREAT \| O_EXCL`) |
| `spm/scanner.py` | procházení složek, SHA-256, bezpečné kopírování přes dočasný soubor |
| `spm/config.py` | nastavení v `settings.json` ve složce aplikace (vedle `SPM.bat`) |
| `spm/gui/` | PySide6 rozhraní |
