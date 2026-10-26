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
<title>SPM – Server Project Manager</title>
<link rel="icon" href="favicon.ico" sizes="any">
<link rel="icon" type="image/png" sizes="32x32" href="icon-32.png">
<link rel="icon" type="image/png" sizes="192x192" href="icon-192.png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
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
  h1 { font-size: 22px; margin: 0 0 4px; display: flex; align-items: center; gap: 12px; }
  h1 img { width: 48px; height: 48px; flex: none; }
  .sub { color: var(--muted); margin: 0 0 24px; }
  .download {display: flex; flex-wrap: wrap; align-items: center; gap: 12px 20px; background: var(--card);
              border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 24px; }
  .btn { display: inline-block; background: var(--l3); color: #ffffff; text-decoration: none; font-weight: 600;
         padding: 10px 18px; border-radius: 6px; white-space: nowrap; }
  .btn:hover, .btn:focus-visible { background: var(--l4); }
  @media (prefers-color-scheme: dark) { .btn { background: #238636; } .btn:hover, .btn:focus-visible { background: #2ea043; } }
  .download ul { margin: 0; padding-left: 18px; }
  .download .how { color: var(--muted); font-size: 13px; flex-basis: 100%; margin: 0; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 28px; }
  .tile { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
  .tile b { display: block; font-size: 28px; line-height: 1.2; }
  .tile span { color: var(--muted); font-size: 13px; }
  h2 { font-size: 16px; margin: 0 0 10px; }
  .scroll { overflow-x: auto; padding-bottom: 6px; align-items: center; }
  .heat { display: grid; grid-template-rows: 14px repeat(7, 11px); grid-auto-flow: column;
          grid-auto-columns: 11px; gap: 3px; width: max-content;
          margin: 0 auto; }  /* na střed; užší okno = posouvání do strany (.scroll) */
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
  .lead { margin: 8px 0 20px; }
  .features { margin: 0 0 12px; padding-left: 22px; max-width: 820px; }
  .features li { margin: 4px 0; }
  .more { margin: 0 0 36px; }
  a { color: #0969da; }
  @media (prefers-color-scheme: dark) { a { color: #4493f8; } }
  .stats { border-top: 1px solid var(--border); padding-top: 24px; }
</style>
</head>
<body>
<main>
  <h1><img src="icon-96.png" alt="" width="48" height="48">SPM – Server Project Manager</h1>
  <p class="lead">Server Project Manager (SPM) je <b>lightweight PDM</b> systém běžící lokálně na zařízeních uživatelů. Vytváří <b>jednoduchý prostor pro spolupráci a sdílení dat</b> projektů mezi uživateli bez nutnosti instalace jakékoliv serverové strany. Nevyžaduje žádné zvláštní síťové požadavky, kromě standardního síťového úložiště. Aplikace je navržena tak, aby nebylo nutné ji instalovat. Jedná se tedy o jednoduchý spustitelný .exe soubor, který po nakopírování do počítačů uživatelů okamžitě funguje. Systém dále nevyžaduje tvorbu žádných uživatelských účtů a komplikované nastavování, kromě standardního nastavení přístupů na síťovém disku. Aplikace funguje stylem <b>Plug &amp; play</b>.</p>
  
 <section class="stats">
  <h2>Statistika používání</h2>

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
  </section>
  <div class="download">
    <a class="btn" href="<?= $h(SPM_DOWNLOAD_URL) ?>">Stáhnout SPM <?= $h(SPM_LATEST_VERSION) ?> (zip)</a>
<?php $notes = array_filter(array_map('trim', explode("\n", SPM_RELEASE_NOTES))); ?>
<?php if ($notes): ?>
    <ul>
<?php foreach ($notes as $line): ?>
      <li><?= $h($line) ?></li>
<?php endforeach; ?>
    </ul>
<?php endif; ?>
    <p class="how">Zip rozbalte do libovolné složky a spusťte <code>SPM\SPM.exe</code> – instalace není potřeba.
      Při aktualizaci rozbalte zip přes stávající složku SPM (nemažte ji – je v ní vaše nastavení).</p>
  </div>

  <h2>Základní funkce</h2>
  <ul class="features">
    <li><b>Přehledný seznam projektů.</b> Projekty představují složky na síťovém disku s možností vyhledávání.</li>
    <li><b>Dynamické zobrazování stavu projektů.</b> Který uživatel na čem pracuje.</li>
    <li><b>Automatické kopírování dat do lokálního úložiště pro spolehlivější práci s daty.</b></li>
    <li><b>Uzamykání dat na síťovém úložišti.</b> Funguje jen v rámci aplikace. Zabraňuje práci více lidí na jednom souboru.</li>
    <li><b>Vytváření historie práce na projektu s možností komentářů změn při každém uvolnění práce zpět na síťové úložiště.</b></li>
    <li><b>Možnost zpětně obnovit stav projektu, pokud existují příslušné zálohy.</b> Ve stylu GIT, ale není to GIT. :)</li>
    <li><b>Automatické skripty.</b> Systém umožňuje při odevzdání spustit předem připravený skript, který může se soubory téměř libovolně manipulovat. Vytvářet archivy, kopírovat nebo přesouvat data, zrcadlit data do jiné složky. Také umožňuje spouštění libovolného skriptu v PC (.bat, .exe atd...) a vytváření .bat souborů při každém běhu skriptu. Akripty umožňuji i POST metodu pro volání API jiných aplikací.</li>
    <li><b>Automatické aktualizace nastavení jednotlivých instancí aplikace.</b> Zajišťuje, aby všechny instance aplikace pracovaly identicky. Hlavně v rámci vytváření záloh pro obnovy a zrcadlení souborů.</li>
    <li><b>Systém poznámek k projektu, nebo ke konkrétnímu souboru s možností přiřazení poznámky konkrétnímu uživateli</b>. Dále možno vytvářet konverzace jako vlákno pod poznámkou.</li>
	<li><b>Vzkazy mezi uživateli. </b> Možnost zanechat jinému uživateli stejného síťového disku vzkaz, který se mu zobrazí jakmile aplikaci použije.</li>
  </ul>
</main>
</body>
</html>
