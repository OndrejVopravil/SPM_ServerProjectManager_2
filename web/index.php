<?php
// Veřejná stránka statistiky používání SPM: aktivní instalace za 30 dní, graf aktivity
// po dnech (jako na GitHubu) a používané verze. Počítají se jen instalace, které se ozvaly
// aspoň ve SPM_REAL_USER_DAYS různých dnech (jednorázová falešná hlášení se nepočítají).

declare(strict_types=1);

require __DIR__ . '/lib.php';

header('Content-Type: text/html; charset=utf-8');

$db = spm_db();
$real = SPM_REAL_USER_DAYS;
$today = spm_today();
$from30 = spm_days_ago(29);

$q = $db->prepare('SELECT COUNT(DISTINCT p.id) FROM pings p JOIN installs i ON i.id = p.id
                   WHERE i.days >= ? AND p.day >= ?');
$q->execute([$real, $from30]);
$active30 = (int)$q->fetchColumn();
$q->execute([$real, $today]);
$activeToday = (int)$q->fetchColumn();
$q = $db->prepare('SELECT COUNT(*) FROM installs WHERE days >= ?');
$q->execute([$real]);
$total = (int)$q->fetchColumn();

// graf aktivity: 53 týdnů, sloupec = týden (pondělí nahoře)
$weeks = 53;
$end = new DateTimeImmutable($today);
$start = $end->modify('-' . ((int)$end->format('N') - 1) . ' days')->modify('-' . ($weeks - 1) . ' weeks');
$q = $db->prepare('SELECT p.day, COUNT(DISTINCT p.id) FROM pings p JOIN installs i ON i.id = p.id
                   WHERE i.days >= ? AND p.day >= ? GROUP BY p.day');
$q->execute([$real, $start->format('Y-m-d')]);
$perDay = [];
foreach ($q->fetchAll(PDO::FETCH_NUM) as [$day, $count]) {
    $perDay[$day] = (int)$count;
}
$max = max([1] + array_values($perDay));

function level(int $count, int $max): int
{
    if ($count <= 0) {
        return 0;
    }
    return min(4, (int)ceil(4 * $count / $max));
}

// verze: poslední hlášená verze každé aktivní instalace za 30 dní
$q = $db->prepare('SELECT version, COUNT(*) FROM (
                       SELECT p.id, (SELECT version FROM pings x WHERE x.id = p.id ORDER BY day DESC LIMIT 1) AS version
                       FROM pings p JOIN installs i ON i.id = p.id
                       WHERE i.days >= ? AND p.day >= ? GROUP BY p.id)
                   GROUP BY version ORDER BY COUNT(*) DESC');
$q->execute([$real, $from30]);
$versions = $q->fetchAll(PDO::FETCH_NUM);

$months = ['led', 'úno', 'bře', 'dub', 'kvě', 'čvn', 'čvc', 'srp', 'zář', 'říj', 'lis', 'pro'];
$h = fn(string $s): string => htmlspecialchars($s, ENT_QUOTES, 'UTF-8');
?>
<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SPM – statistika používání</title>
<style>
  :root { --bg: #ffffff; --fg: #1f2328; --muted: #656d76; --card: #f6f8fa; --border: #d0d7de;
          --l0: #ebedf0; --l1: #9be9a8; --l2: #40c463; --l3: #30a14e; --l4: #216e39; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #0d1117; --fg: #e6edf3; --muted: #8d96a0; --card: #161b22; --border: #30363d;
            --l0: #161b22; --l1: #0e4429; --l2: #006d32; --l3: #26a641; --l4: #39d353; }
  }
  body { margin: 0; background: var(--bg); color: var(--fg);
         font: 15px/1.5 "Segoe UI", system-ui, sans-serif; }
  main { max-width: 980px; margin: 0 auto; padding: 24px 16px 48px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 24px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 28px; }
  .tile { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
  .tile b { display: block; font-size: 28px; line-height: 1.2; }
  .tile span { color: var(--muted); font-size: 13px; }
  h2 { font-size: 16px; margin: 0 0 10px; }
  .scroll { overflow-x: auto; padding-bottom: 6px; }
  .heat { display: grid; grid-template-rows: 14px repeat(7, 11px); grid-auto-flow: column;
          grid-auto-columns: 11px; gap: 3px; width: max-content; }
  .heat .m { font-size: 10px; color: var(--muted); white-space: nowrap; overflow: visible; }
  .heat i { display: block; width: 11px; height: 11px; border-radius: 2px; background: var(--l0); }
  .heat i.l1 { background: var(--l1); } .heat i.l2 { background: var(--l2); }
  .heat i.l3 { background: var(--l3); } .heat i.l4 { background: var(--l4); }
  .heat i.out { visibility: hidden; }
  .legend { display: flex; align-items: center; gap: 3px; color: var(--muted); font-size: 12px; margin: 8px 0 28px; }
  .legend i { width: 11px; height: 11px; border-radius: 2px; display: inline-block; }
  table { border-collapse: collapse; min-width: 260px; }
  td, th { text-align: left; padding: 4px 16px 4px 0; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 600; font-size: 13px; }
  .note { color: var(--muted); font-size: 13px; margin-top: 28px; }
</style>
</head>
<body>
<main>
  <h1>SPM – Server Project Manager</h1>
  <p class="sub">Anonymní statistika používání (instalace = uživatel na jednom počítači)</p>

  <div class="tiles">
    <div class="tile"><b><?= $active30 ?></b><span>aktivních instalací za 30 dní</span></div>
    <div class="tile"><b><?= $activeToday ?></b><span>aktivních dnes</span></div>
    <div class="tile"><b><?= $total ?></b><span>instalací celkem</span></div>
    <div class="tile"><b><?= $h(SPM_LATEST_VERSION) ?></b><span>aktuální verze</span></div>
  </div>

  <h2>Aktivita po dnech</h2>
  <div class="scroll">
    <div class="heat" role="img" aria-label="Počet aktivních instalací po dnech za poslední rok">
<?php
for ($w = 0; $w < $weeks; $w++) {
    $monday = $start->modify("+{$w} weeks");
    $label = ($w === 0 || (int)$monday->format('j') <= 7) ? $months[(int)$monday->format('n') - 1] : '';
    echo '      <span class="m">' . $h($label) . "</span>\n";
    for ($d = 0; $d < 7; $d++) {
        $day = $monday->modify("+{$d} days");
        $key = $day->format('Y-m-d');
        if ($key > $today) {
            echo "      <i class=\"out\"></i>\n";
            continue;
        }
        $count = $perDay[$key] ?? 0;
        $title = $day->format('j. n. Y') . ': ' . $count . ($count === 1 ? ' instalace' : ' instalací');
        echo '      <i class="l' . level($count, $max) . '" title="' . $h($title) . "\"></i>\n";
    }
}
?>
    </div>
  </div>
  <div class="legend">méně <i style="background:var(--l0)"></i><i style="background:var(--l1)"></i><i
      style="background:var(--l2)"></i><i style="background:var(--l3)"></i><i style="background:var(--l4)"></i> více</div>

  <h2>Používané verze (posledních 30 dní)</h2>
<?php if ($versions): ?>
  <table>
    <tr><th>Verze</th><th>Instalací</th></tr>
<?php foreach ($versions as [$version, $count]): ?>
    <tr><td><?= $h($version !== '' ? $version : 'neznámá') ?></td><td><?= (int)$count ?></td></tr>
<?php endforeach; ?>
  </table>
<?php else: ?>
  <p class="sub">Zatím žádná data.</p>
<?php endif; ?>

  <p class="note">Aplikace jednou denně odešle jen hash uživatelského jména a názvu počítače a číslo
    verze – žádná jména, cesty ani obsah projektů. IP adresa se neukládá. Počítají se instalace, které
    se ozvaly aspoň v <?= $real ?> různých dnech. Odesílání lze vypnout v nastavení aplikace nebo pro
    celou firmu ve <code>spm_config.json</code> (<code>"usage_report": false</code>).</p>
</main>
</body>
</html>
