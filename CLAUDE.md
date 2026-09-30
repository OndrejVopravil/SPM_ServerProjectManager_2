# SPM – pokyny pro práci na projektu

- Komunikace, texty v UI a komentáře v kódu jsou česky.
- Python spouštět vždy přes `.venv\Scripts\python.exe` (`python` v PATH je interpret Cimatronu).
- Testy: `.venv\Scripts\python.exe -m pytest -q tests`

## Git a vydání

- **Commit se dělá výhradně na výslovný pokyn uživatele.** Nikdy necommitovat z vlastní iniciativy.
- Každý commit = zároveň nový balíček s .exe. Postup:
  1. spustit testy – při selhání necommitovat a oznámit to,
  2. zvýšit verzi v `spm/__init__.py` (`__version__`): standardně poslední číslo
     (0.1.0 -> 0.1.1), jiné navýšení jen na pokyn uživatele; změna verze je součástí commitu,
  3. `git add` + `git commit`,
  4. spustit `build.bat` (výstup `dist\SPM\SPM.exe` a `dist\SPM-<verze>.zip`),
  5. ověřit, že `dist\SPM\SPM.exe` nastartuje, a oznámit hash commitu a cestu k zipu.
- `.bat` soubory musí mít konce řádků CRLF a bez diakritiky (jinak je cmd špatně přečte).
