<?php
// Společné funkce serveru statistiky SPM: databáze (SQLite) a zápis hlášení.

declare(strict_types=1);

const SPM_WEB = true;
require __DIR__ . '/config.php';

date_default_timezone_set(SPM_TIMEZONE);

function spm_db(): PDO
{
    static $db = null;
    if ($db !== null) {
        return $db;
    }
    if (!is_dir(SPM_DATA_DIR)) {
        mkdir(SPM_DATA_DIR, 0750, true);
    }
    $db = new PDO('sqlite:' . SPM_DATA_DIR . '/spm_usage.sqlite');
    $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
    $db->exec('PRAGMA busy_timeout = 5000');
    $db->exec('CREATE TABLE IF NOT EXISTS pings (
                   id TEXT NOT NULL, day TEXT NOT NULL, version TEXT NOT NULL DEFAULT "",
                   PRIMARY KEY (id, day))');
    $db->exec('CREATE TABLE IF NOT EXISTS installs (
                   id TEXT PRIMARY KEY, first_day TEXT NOT NULL, last_day TEXT NOT NULL,
                   days INTEGER NOT NULL DEFAULT 1)');
    $db->exec('CREATE TABLE IF NOT EXISTS new_by_ip (
                   day TEXT NOT NULL, ip TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
                   PRIMARY KEY (day, ip))');
    $db->exec('CREATE INDEX IF NOT EXISTS ix_pings_day ON pings(day)');
    return $db;
}

function spm_today(): string
{
    return date('Y-m-d');
}

function spm_days_ago(int $days): string
{
    return date('Y-m-d', strtotime("-{$days} days"));
}

/** Platné hlášení: id = 64 hex znaků (SHA-256), verze ve tvaru 0.1.27 (jinak prázdná). */
function spm_valid_id($id): bool
{
    return is_string($id) && preg_match('/^[0-9a-f]{64}$/', $id) === 1;
}

function spm_clean_version($version): string
{
    return is_string($version) && preg_match('/^\d{1,3}\.\d{1,3}\.\d{1,4}$/', $version) === 1 ? $version : '';
}

/**
 * Zapíše hlášení (jedno na instalaci a den). Nová instalace se zapíše, jen když z dané IP
 * adresy ani celkem za den nepřibylo moc nových (ochrana proti zaplnění falešnými daty).
 * IP adresa se neukládá – jen její hash s tajným řetězcem a dnem, smazaný po dvou dnech.
 * Vrací, zda se hlášení zapsalo.
 */
function spm_record(string $id, string $version, string $ip): bool
{
    if (!defined('SPM_SECRET') || strlen(SPM_SECRET) < 32) {
        error_log('SPM: chybí secret.php (SPM_SECRET) – hlášení se nezapisují');
        return false;
    }
    $db = spm_db();
    $day = spm_today();
    $db->beginTransaction();
    try {
        $db->prepare('DELETE FROM new_by_ip WHERE day < ?')->execute([spm_days_ago(2)]);
        $seen = $db->prepare('SELECT 1 FROM pings WHERE id = ? AND day = ?');
        $seen->execute([$id, $day]);
        if ($seen->fetchColumn()) {  // dnes už hlášené – jen aktuální verze
            $db->prepare('UPDATE pings SET version = ? WHERE id = ? AND day = ?')->execute([$version, $id, $day]);
            $db->commit();
            return true;
        }
        $known = $db->prepare('SELECT 1 FROM installs WHERE id = ?');
        $known->execute([$id]);
        if ($known->fetchColumn()) {
            $db->prepare('UPDATE installs SET last_day = ?, days = days + 1 WHERE id = ?')->execute([$day, $id]);
        } else {
            $ip_hash = hash('sha256', SPM_SECRET . '|' . $day . '|' . $ip);
            $per_ip = $db->prepare('SELECT count FROM new_by_ip WHERE day = ? AND ip = ?');
            $per_ip->execute([$day, $ip_hash]);
            $total = $db->prepare('SELECT COUNT(*) FROM installs WHERE first_day = ?');
            $total->execute([$day]);
            if ((int)$per_ip->fetchColumn() >= SPM_MAX_NEW_PER_IP || (int)$total->fetchColumn() >= SPM_MAX_NEW_PER_DAY) {
                $db->rollBack();
                return false;
            }
            $db->prepare('INSERT INTO new_by_ip(day, ip, count) VALUES (?, ?, 1)
                          ON CONFLICT(day, ip) DO UPDATE SET count = count + 1')->execute([$day, $ip_hash]);
            $db->prepare('INSERT INTO installs(id, first_day, last_day, days) VALUES (?, ?, ?, 1)')
               ->execute([$id, $day, $day]);
        }
        $db->prepare('INSERT INTO pings(id, day, version) VALUES (?, ?, ?)')->execute([$id, $day, $version]);
        $db->commit();
        return true;
    } catch (Throwable $e) {
        if ($db->inTransaction()) {
            $db->rollBack();
        }
        throw $e;
    }
}

/** Informace o aktuální verzi – odpověď aplikaci (upozornění na aktualizaci). */
function spm_version_info(): array
{
    return ['latest' => SPM_LATEST_VERSION, 'url' => SPM_DOWNLOAD_URL, 'notes' => SPM_RELEASE_NOTES];
}
