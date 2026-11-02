# SPM – Server Project Manager

Jednoduchý PDM pro spolupráci na projektech ve složkách na běžném síťovém úložišti. Nepotřebuje
server, databázový stroj ani uživatelské účty – stačí síťový disk s právy zápisu a `SPM.exe`
nakopírovaný na počítače uživatelů (bez instalace, *plug & play*).

Na jednom projektu může pracovat víc lidí současně – zamyká se po jednotlivých souborech.
Každý pracuje v místní kopii projektu; změny se na server dostanou odevzdáním (s komentářem
a zálohou předchozí verze).

- Web: <https://spm.vopravil.cz> (stažení aktuální verze)
- Zdrojový kód: <https://github.com/OndrejVopravil/SPM_ServerProjectManager_2>
- Podpora: ondrej@vopravil.cz

## Práce se soubory

Hlavní akce jsou jen dvě (F3 nebo dvojklik na název projektu podle stavu spustí tu vhodnou):

- **Převzít** – zamkne vybrané soubory k úpravě a stáhne jejich aktuální verzi. U projektu, na
  kterém zatím nepracuji, ho zároveň aktivuje: stáhne projekt do místního úložiště (ostatní
  soubory jen pro čtení, případně se nestahují vůbec – „Nestahovat neaktivované soubory“).
  Existující místní složka se předtím odsune do `<projekt>_puvodni_<datum>`. Bez vybraného
  souboru převzít nejde.
- **Odevzdat** – nahraje změny (upravené, nové, přejmenované, smazané) s komentářem a uvolní je.
  Na projektu je předem zaškrtnuté vše, ve stromu souborů jen označené. Když po odevzdání
  nedržím žádný soubor a nic neodevzdaného nezůstalo, práce na projektu skončí (projekt se
  deaktivuje a místní kopie se smaže). U složky „Jen místně“ Odevzdat vytvoří **nový projekt
  na serveru** (publikování).
- **Aktualizovat** – stáhne soubory, které mezitím odevzdali ostatní.
- **Zahodit neodevzdané změny a uvolnit zámky** (pravé tlačítko) – ukončí práci bez nahrání.

Další chování:

- **Otevřít** volný soubor, který nemám převzatý, ho na pozadí převezme (i s aktivací projektu)
  a otevře; cizí soubory se otevírají jen pro čtení.
- Nová složka nebo vložení souborů do projektu, na kterém nepracuji, ho po potvrzení aktivuje
  bez zámků a bez stahování.
- Nové soubory může vytvářet kdokoliv; patří tomu, kdo je vytvořil.
- Přejmenování a přesuny se při odevzdání rozpoznají (hash + podobnost) a zachová se historie.
- Soubory otevřené v jiném programu (CAD…) se před odevzdáním ohlásí (Restart Manager Windows).
- Nepovolené znaky v názvech (diakritika, mezery, `( ) & , . ; = ^`) se hlásí a takové soubory
  nejdou odevzdat.

## Rozhraní

- **Záložky** – každá záložka = jedna síťová složka s projekty a k ní místní úložiště. Záložka
  **Složka** zobrazí obyčejnou složku mimo spolupráci (kopírování, přesuny, ZIP, hledání; složky
  projektů SPM jsou v ní chráněné).
- **Dva panely** vedle sebe (jako Total Commander) – v každém jiná záložka; soubory mezi nimi
  lze kopírovat a přetahovat.
- **Seznam projektů** – stav (volný / aktivovaný mnou / jinými / Jen místně), kdo na projektu
  pracuje a od kdy (aktivace starší než 30 dní je zvýrazněná), komentář projektu, oblíbené.
  Pravé tlačítko: historie, obnova ze zálohy, zálohy a koš, archivace, přejmenování, kopírování
  názvu a místní cesty, odebrání zámků uživatele, smazání složky „Jen místně“ (do Koše)…
- **Strom souborů** – kdo soubor drží, zrcadlo, poznámky; hledání souborů a složek (`*` = cokoliv,
  bez ohledu na velikost písmen a diakritiku), rozbalit / sbalit, rozbalit aktivní složky.
  Kopírovat / Vyjmout / Vložit a přetahování myší (i do a z Průzkumníka; dočasné kopie se
  připraví, až si o soubory řekne jiný program).
- **Historie** projektu i jednotlivých souborů (časová osa odevzdání) a **obnova ze zálohy**
  k libovolnému odevzdání do nové místní složky.
- **Poznámky** k projektu a souborům (Markdown, adresát, zmínky `@uživatel`, `#projekt`,
  vyřízení) a **Vzkazy** mezi uživateli nevázané na projekt; vzkaz zůstává otevřený, dokud ho
  autor neoznačí jako dokončený (i když ho adresát už vyřídil), dokončené se po čase archivují.
- **Skripty** – vlastní jednoduchý jazyk spouštěný při odevzdání (`BEFORE` / po nahrání):
  zrcadlení do projektu jiné záložky (`MIRROR`), kopie, ZIP, textové soubory, dotazy na
  uživatele… Editor s nápovědou, hledáním (Ctrl+F, F3) a zkušebním během. Změny jazyka musí
  zůstat zpětně kompatibilní se staršími skripty.
- **Archivace** dokončených projektů do zipu v `<síťová složka>/.spm/archiv` (jen oprávnění
  uživatelé) a obnova z archivu.
- **Zálohy a koš** – kolik místa zabírají zálohy a koš projektů, úklid; zbytky nedokončených
  operací v síťové složce.
- **Úklid** – neaktivní uživatelé (30 dní) jsou v nabídkách zašedlí, záznamy uživatelů se po roce
  mažou samy; staré místní složky `_puvodni_` / `_obnova_` lze přesunout do Koše.

## Společné nastavení a statistika

- `<síťová složka>/spm_config.json` – pokud existuje, řídí nastavení všech uživatelů dané síťové
  složky (zálohy, ignorované soubory, kontrola názvů, archivace, oprávnění…); v aplikaci jsou
  taková nastavení zamčená.
- Aplikace jednou denně po spuštění odešle anonymní údaj (hash uživatele a počítače, verze) na
  <https://spm.vopravil.cz> a zjistí, zda je k dispozici nová verze. Statistiku lze vypnout
  v Nastavení nebo pro celou firmu v `spm_config.json` (`"usage_report": false`); upozornění na
  novou verzi funguje i s vypnutou statistikou. Web statistiky je ve složce `web/`.

## Spuštění

Dvojklikem na **`SPM.bat`**. Při prvním spuštění skript sám vytvoří `.venv` a nainstaluje závislosti.

Ručně (Python 3.13):

```
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m spm
```

Testy: `.venv\Scripts\python.exe -m pytest -q tests`

## Samostatná verze (bez Pythonu)

`build.bat` vytvoří `dist\SPM\SPM.exe` a zip `dist\SPM-<verze>.zip` (~30 MB). Zip stačí na cílovém
PC rozbalit do složky s právem zápisu (např. `C:\SPM`, ne `Program Files`) a spustit `SPM.exe`.
Nastavení se ukládá do `settings.json` vedle `SPM.exe`, vlastní skripty do `scripts\`.
Aktualizace = rozbalit nový zip přes stávající složku (nastavení zůstane).

## Struktura na serveru

```
\\server\projekty\                 síťová složka s projekty (jedna záložka)
    spm_config.json                společné nastavení (volitelné)
    .spm_users\<login>.json        adresář uživatelů (jméno, přezdívka, počítač, verze SPM)
    .spm\vzkazy\...                vzkazy mezi uživateli (_archiv = dokončené)
    .spm\archiv\*.zip              archivované projekty
    .spm\scripts\*.spm             síťové skripty (CALL SERVER)
    ProjektA\
        .spm\project.db            SQLite – soubory, zámky, sezení, odevzdání, změny, poznámky,
                                   zrcadla, běhy skriptů
        .spm\active\*.json         kdo má projekt aktivovaný (pro rychlé zobrazení v seznamu)
        .spm\write.lock            existuje jen během krátkého zápisu do databáze
        .spm\comment.json          komentář projektu
        .spm\backup\...            předchozí verze: soubor.backup01, .backup02, … (posledních N)
        .spm\trash\<sezení>\       soubory smazané během daného sezení
        ...soubory projektu...
```

V místním úložišti: místní kopie projektů (`<projekt>\.spm\manifest.json` – z jaké verze pochází),
složky `<projekt>_puvodni_<datum>` (odsunuté při aktivaci) a `<projekt>_obnova_<datum>`
(obnova ze zálohy), `.spm_scripts\` (nedokončené běhy skriptů) a `.spm_notes\`.

## Moduly

| Modul | Účel |
|---|---|
| `spm/sync.py` | aktivace, převzetí, odevzdání, aktualizace, deaktivace, publikování, zálohy, koš |
| `spm/db.py` | schéma a přístup k databázi projektu |
| `spm/lock.py` | značky aktivních uživatelů a zámek zápisu do databáze (`O_CREAT \| O_EXCL`) |
| `spm/manifest.py` | místní záznam, z jaké verze na serveru pochází místní kopie |
| `spm/scanner.py` | procházení složek, SHA-256, bezpečné kopírování přes dočasný soubor |
| `spm/matcher.py` | detekce přejmenovaných souborů (hash + podobnost názvu/velikosti/složky) |
| `spm/names.py` | nepovolené znaky v názvech |
| `spm/file_locks.py` | které programy mají soubory otevřené (Restart Manager) |
| `spm/restore.py` | obnova projektu ze záloh k zvolenému odevzdání |
| `spm/archive.py` | archivace projektů do zipu a obnova z archivu |
| `spm/storage.py` | místo zabrané zálohami a košem, úklid, zbytky nedokončených operací |
| `spm/mirror.py` | zrcadlení souborů do projektu jiné záložky (příkaz `MIRROR`) |
| `spm/notes.py`, `spm/messages.py`, `spm/mentions.py` | poznámky k projektu, vzkazy, zmínky |
| `spm/comment.py` | komentář projektu |
| `spm/users.py`, `spm/identity.py` | adresář uživatelů síťové složky, identita uživatele a počítače |
| `spm/config.py`, `spm/shared_config.py` | místní `settings.json` a společný `spm_config.json` |
| `spm/folder_ops.py`, `spm/folder_zones.py`, `spm/zip_ops.py` | záložka Složka, chráněné oblasti, ZIP |
| `spm/recycle.py` | mazání do Koše Windows |
| `spm/usage.py` | anonymní statistika a kontrola nové verze |
| `spm/scripting/` | jazyk skriptů: rozbor (`lang`), provádění (`runtime`), uložení (`store`), nápověda |
| `spm/gui/` | PySide6 rozhraní |
| `tools/make_icon.py` | ikona aplikace (`--web` = ikony webu) |
| `web/` | PHP web se statistikou používání a stažením aktuální verze |

## Licence

Copyright © 2026 Ondřej Vopravil, všechna práva vyhrazena. Aplikaci lze bezplatně používat
(i komerčně) a šířit v nezměněné podobě; je poskytována tak, jak je, bez záruk – podmínky
v `LICENSE.txt`. Licence součástí třetích stran (Qt / PySide6 – LGPL-3.0,
Python, PyInstaller) jsou ve složce `licenses\`.
