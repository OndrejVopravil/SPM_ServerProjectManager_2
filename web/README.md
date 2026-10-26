# Server statistiky SPM (https://spm.vopravil.cz)

Primitivní PHP server pro anonymní hlášení používání aplikace SPM a upozornění na novou verzi.
Do balíku s aplikací (`SPM.exe`) se nedává.

## Obsah

| Soubor | Účel |
|---|---|
| `api/ping.php` | Hlášení z aplikace (jednou denně): `POST {"id": "<sha256>", "version": "0.1.27"}`. Odpovídá aktuální verzí: `{"latest", "url", "notes"}`. Bez `id` (vypnutá statistika) nebo `GET` = jen verze. |
| `index.php` | Veřejná stránka statistiky – aktivní instalace za 30 dní, graf aktivity po dnech, používané verze. |
| `config.php` | Aktuální verze, odkaz ke stažení, popis změn, limity. |
| `secret.php` | Tajný řetězec `SPM_SECRET` pro hash IP adres – **není v gitu** (`.gitignore`), nahrává se jen na hosting. |
| `lib.php` | Databáze (SQLite) a zápis hlášení. |
| `data/` | Databáze `spm_usage.sqlite` (vznikne sama). Přístup z webu zakazuje `data/.htaccess`. |
| `download/` | Balíky `SPM-<verze>.zip` ke stažení. |
| `favicon.ico`, `icon-*.png`, `apple-touch-icon.png` | Ikona aplikace pro web (favicona, telefony, nadpis stránky). Vyrábí je `tools\make_icon.py --web` ze stejné kresby jako ikonu SPM.exe. |

## Nasazení

1. Požadavky: PHP 8.1+ s rozšířením `pdo_sqlite`, Apache (kvůli `.htaccess`; u jiného serveru
   zakažte přístup k `data/`, `config.php` a `lib.php` jinak).
2. Nahrajte obsah složky `web/` do kořene webu `spm.vopravil.cz`.
3. Nahrajte i `secret.php` (není v gitu). Když chybí, vytvořte ho podle vzoru níže s náhodným
   řetězcem z `php -r "echo bin2hex(random_bytes(32));"` – bez něj se hlášení nezapisují.
4. Složka `data/` musí být zapisovatelná pro PHP. Pokud to hosting dovolí, přesuňte ji mimo
   veřejnou složku webu a upravte `SPM_DATA_DIR` v `config.php`.
5. Kontrola: `https://spm.vopravil.cz/api/ping.php` vrátí JSON s verzí,
   `https://spm.vopravil.cz/config.php` i `secret.php` vrátí 403.

Vzor `secret.php`:

```php
<?php
declare(strict_types=1);
if (!defined('SPM_WEB')) { http_response_code(403); exit; }
const SPM_SECRET = '<64 náhodných hex znaků>';
```

## Vydání nové verze

1. `build.bat` vytvoří `dist\SPM-<verze>.zip` – nahrajte ho do `download/`.
2. V `config.php` upravte `SPM_LATEST_VERSION`, `SPM_DOWNLOAD_URL` a `SPM_RELEASE_NOTES`
   (řádky popisu se v aplikaci zobrazí jako odrážky).
3. Aplikace upozorní uživatele při nejbližší denní kontrole.

## Ochrana proti falešným datům

Úplně zabránit podvrženým hlášením nejde (aplikace je veřejná). Omezení:

- kontrola formátu (`id` = 64 hex znaků, verze `0.1.27`), zpráva max. 2 kB,
- jedno hlášení na instalaci a den,
- z jedné IP adresy nejvýš `SPM_MAX_NEW_PER_IP` nových instalací za den, celkem nejvýš
  `SPM_MAX_NEW_PER_DAY` (IP adresa se neukládá – jen její hash s tajným řetězcem a dnem,
  smaže se po dvou dnech),
- statistika počítá jen instalace, které se ozvaly aspoň v `SPM_REAL_USER_DAYS` různých dnech.

Podvržená data lze smazat přímo v databázi (`installs`, `pings`).

## Vyzkoušení na místním počítači

```
php -d extension=pdo_sqlite -S 127.0.0.1:8765 -t web
```
