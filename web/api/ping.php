<?php
// Hlášení používání SPM (jednou denně z aplikace) a odpověď s aktuální verzí.
//   POST {"id": "<sha256>", "version": "0.1.27"} – zapíše se (anonymně) a vrátí verzi
//   POST {"version": "0.1.27"} nebo GET           – jen informace o aktuální verzi
// Odpověď: {"latest": "...", "url": "...", "notes": "..."}

declare(strict_types=1);

require __DIR__ . '/../lib.php';

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $raw = file_get_contents('php://input', false, null, 0, 2048);
    $data = json_decode($raw === false ? '' : $raw, true);
    if (is_array($data) && isset($data['id'])) {
        if (spm_valid_id($data['id'])) {
            try {
                spm_record($data['id'], spm_clean_version($data['version'] ?? ''),
                           (string)($_SERVER['REMOTE_ADDR'] ?? ''));
            } catch (Throwable $e) {
                error_log('SPM ping: ' . $e->getMessage());  // hlášení se nepovedlo – verze se pošle stejně
            }
        }
    }
} elseif ($_SERVER['REQUEST_METHOD'] !== 'GET') {
    http_response_code(405);
    exit;
}

echo json_encode(spm_version_info(), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
