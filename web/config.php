<?php
// Nastavení serveru statistiky SPM (https://spm.vopravil.cz).
// Při vydání nové verze upravte SPM_LATEST_VERSION, SPM_DOWNLOAD_URL a SPM_RELEASE_NOTES
// a nahrajte zip do složky download/.

declare(strict_types=1);

if (!defined('SPM_WEB')) {
    http_response_code(403);
    exit;
}

// --- aktuální verze aplikace (odpověď na hlášení – upozornění na aktualizaci) ---
const SPM_LATEST_VERSION = '0.1.27';
const SPM_DOWNLOAD_URL = 'https://spm.vopravil.cz/download/SPM-0.1.27.zip';
const SPM_RELEASE_NOTES = "Převzít zaeviduje soubory, které v evidenci chybí.\n"
    . "Anonymní statistika používání a upozornění na novou verzi.";

// --- data ---
// Složka s databází (SQLite). Nejlépe mimo veřejnou složku webu; když to hosting neumožní,
// zůstane v data/ (přístup zakazuje data/.htaccess).
define('SPM_DATA_DIR', __DIR__ . '/data');

// Tajný řetězec pro hash IP adres (omezení zneužití) je v secret.php – ten se do gitu
// nedává (.gitignore), nahrává se jen na hosting. Chybí-li, hlášení se nezapisují.
if (is_file(__DIR__ . '/secret.php')) {
    require __DIR__ . '/secret.php';
}

// --- ochrana proti falešným datům ---
const SPM_MAX_NEW_PER_IP = 20;     // nových instalací z jedné IP adresy za den
const SPM_MAX_NEW_PER_DAY = 500;   // nových instalací celkem za den
const SPM_REAL_USER_DAYS = 2;      // za skutečného uživatele se počítá až po N různých dnech

const SPM_TIMEZONE = 'Europe/Prague';
