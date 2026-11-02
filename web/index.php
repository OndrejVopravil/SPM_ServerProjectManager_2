<?php
// Veřejná stránka SPM: představení aplikace, stažení a statistika používání – aktivní instalace
// za 30 dní, graf aktivity po dnech (jako na GitHubu) a používané verze. Počítají se jen
// instalace, které se ozvaly aspoň ve SPM_REAL_USER_DAYS různých dnech.

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
$versionsTotal = max(1, array_sum(array_map(fn($v) => (int)$v[1], $versions)));

$months = ['led', 'úno', 'bře', 'dub', 'kvě', 'čvn', 'čvc', 'srp', 'zář', 'říj', 'lis', 'pro'];
$h = fn(string $s): string => htmlspecialchars($s, ENT_QUOTES, 'UTF-8');
$notes = array_filter(array_map('trim', explode("\n", SPM_RELEASE_NOTES)));
$github = 'https://github.com/OndrejVopravil/SPM_ServerProjectManager_2';
?>
<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SPM – Server Project Manager</title>
<meta name="description" content="Lightweight PDM bez serveru: zamykání souborů, historie, zálohy a skripty nad běžným síťovým diskem.">
<link rel="icon" href="favicon.ico" sizes="any">
<link rel="icon" type="image/png" sizes="32x32" href="icon-32.png">
<link rel="icon" type="image/png" sizes="192x192" href="icon-192.png">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<style>
  :root {
    --bg: #05080d; --bg2: #0a0f17; --panel: #0d141e; --line: #1b2636; --line2: #263449;
    --fg: #e6edf6; --muted: #8392a8; --dim: #55647a;
    --acc: #3ee089; --acc2: #2bb3ff; --warn: #ffb547; --red: #ff5d6c; --vio: #b18cff;
    --l0: #111a26; --l1: #0f3d29; --l2: #11683f; --l3: #23a35f; --l4: #3ee089;
    --mono: "JetBrains Mono", "Cascadia Code", Consolas, "SFMono-Regular", monospace;
    --sans: "Inter", "Segoe UI", system-ui, -apple-system, sans-serif;
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.6 var(--sans);
         -webkit-font-smoothing: antialiased; overflow-x: hidden; }
  a { color: var(--acc2); text-decoration: none; }
  a:hover { text-decoration: underline; }
  code, .mono { font-family: var(--mono); }
  code { font-size: .9em; background: #111a26; border: 1px solid var(--line); border-radius: 4px; padding: 1px 5px; }
  .wrap { max-width: 1160px; margin: 0 auto; padding: 0 24px; }

  /* --- navigace --- */
  nav { position: sticky; top: 0; z-index: 10; backdrop-filter: blur(12px);
        background: rgba(5, 8, 13, .72); border-bottom: 1px solid var(--line); }
  nav .wrap { display: flex; align-items: center; gap: 28px; height: 60px; }
  .brand { display: flex; align-items: center; gap: 10px; font-weight: 700; color: var(--fg); letter-spacing: .02em; }
  .brand img { width: 28px; height: 28px; }
  .brand small { font: 500 11px var(--mono); color: var(--acc); border: 1px solid #1d4f36;
                 background: #0b2418; padding: 1px 7px; border-radius: 99px; }
  nav .links { display: flex; gap: 22px; margin-left: auto; font-size: 14px; }
  nav .links a { color: var(--muted); }
  nav .links a:hover { color: var(--fg); text-decoration: none; }
  @media (max-width: 760px) { nav .links a.opt { display: none; } }

  /* --- hero --- */
  .hero { position: relative; padding: 96px 0 80px; overflow: hidden; border-bottom: 1px solid var(--line); }
  .hero::before { content: ""; position: absolute; inset: 0; pointer-events: none;
    background-image: linear-gradient(var(--line) 1px, transparent 1px), linear-gradient(90deg, var(--line) 1px, transparent 1px);
    background-size: 48px 48px; opacity: .45;
    mask-image: radial-gradient(ellipse 80% 70% at 50% 30%, #000 30%, transparent 75%);
    -webkit-mask-image: radial-gradient(ellipse 80% 70% at 50% 30%, #000 30%, transparent 75%); }
  .hero::after { content: ""; position: absolute; width: 900px; height: 900px; left: 50%; top: -520px;
    transform: translateX(-50%); pointer-events: none;
    background: radial-gradient(circle, rgba(62, 224, 137, .16), rgba(43, 179, 255, .06) 40%, transparent 70%); }
  .hero .wrap { position: relative; z-index: 1; display: grid; grid-template-columns: .9fr 1.1fr; gap: 48px; align-items: center; }
  @media (max-width: 960px) { .hero .wrap { grid-template-columns: 1fr; } .hero { padding: 64px 0 56px; } }
  .eyebrow { font: 500 12px var(--mono); color: var(--acc); letter-spacing: .14em; text-transform: uppercase;
             display: inline-flex; align-items: center; gap: 10px; }
  .eyebrow::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--acc);
                     box-shadow: 0 0 0 4px rgba(62, 224, 137, .15), 0 0 16px var(--acc); animation: pulse 2.4s infinite; }
  @keyframes pulse { 50% { box-shadow: 0 0 0 7px rgba(62, 224, 137, .05), 0 0 22px var(--acc); } }
  h1 { font-size: clamp(34px, 4.4vw, 56px); line-height: 1.04; margin: 18px 0 20px; letter-spacing: -.035em; font-weight: 800; }
  h1 .grad { background: linear-gradient(92deg, var(--acc) 0%, var(--acc2) 60%, var(--vio) 100%);
             -webkit-background-clip: text; background-clip: text; color: transparent; }
  .lead { color: #b3c0d2; font-size: 18px; max-width: 560px; margin: 0 0 32px; }
  .cta { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; }
  .btn { display: inline-flex; align-items: center; gap: 10px; padding: 13px 22px; border-radius: 10px; font-weight: 650;
         font-size: 15px; transition: transform .15s, box-shadow .15s, background .15s; white-space: nowrap; }
  .btn:hover { text-decoration: none; transform: translateY(-1px); }
  .btn.primary { background: linear-gradient(180deg, #4bf09a, #25c472); color: #03140b;
                 box-shadow: 0 0 0 1px #4bf09a inset, 0 10px 30px -8px rgba(62, 224, 137, .55); }
  .btn.primary:hover { box-shadow: 0 0 0 1px #6ff7b1 inset, 0 14px 38px -8px rgba(62, 224, 137, .75); }
  .btn.ghost { color: var(--fg); border: 1px solid var(--line2); background: rgba(13, 20, 30, .6); }
  .btn.ghost:hover { border-color: var(--muted); }
  .btn svg { width: 18px; height: 18px; }
  .specs { display: flex; flex-wrap: wrap; gap: 8px 22px; margin-top: 28px; font: 12.5px var(--mono); color: var(--muted); }
  .specs span::before { content: "✓ "; color: var(--acc); }

  /* --- terminál --- */
  .term { background: linear-gradient(180deg, #0c131d, #080d14); border: 1px solid var(--line2); border-radius: 14px;
          box-shadow: 0 40px 80px -30px rgba(0, 0, 0, .8), 0 0 0 1px rgba(255, 255, 255, .02) inset,
                      0 0 60px -20px rgba(43, 179, 255, .25); overflow: hidden; }
  .term .bar { display: flex; align-items: center; gap: 7px; padding: 11px 14px; border-bottom: 1px solid var(--line);
               font: 12px var(--mono); color: var(--dim); }
  .term .bar i { width: 11px; height: 11px; border-radius: 50%; background: #2a3646; }
  .term .bar i:nth-child(1) { background: #ff5f57; } .term .bar i:nth-child(2) { background: #febc2e; }
  .term .bar i:nth-child(3) { background: #28c840; }
  .term .bar span { margin-left: 10px; }
  .term pre { margin: 0; padding: 18px 20px 20px; font: 13px/1.75 var(--mono); color: #c7d3e3; overflow-x: auto; min-height: 330px; }
  .t-dim { color: var(--dim); } .t-acc { color: var(--acc); } .t-blue { color: var(--acc2); }
  .t-warn { color: var(--warn); } .t-vio { color: var(--vio); } .t-red { color: var(--red); }
  .cursor { display: inline-block; width: 8px; height: 15px; background: var(--acc); vertical-align: -2px; animation: blink 1s steps(1) infinite; }
  @keyframes blink { 50% { opacity: 0; } }

  /* --- náhled okna aplikace --- */
  .app { background: #11161d; border: 1px solid var(--line2); border-radius: 12px; overflow: hidden; font: 12.5px/1.45 "Segoe UI", system-ui, sans-serif;
         color: #d7dee8; box-shadow: 0 40px 80px -30px rgba(0, 0, 0, .8), 0 0 60px -20px rgba(43, 179, 255, .25); }
  .app-title { display: flex; align-items: center; gap: 8px; padding: 8px 12px; background: #0b0f15; border-bottom: 1px solid var(--line); color: var(--muted); font-size: 12px; }
  .app-tools { display: flex; flex-wrap: wrap; gap: 4px; padding: 6px 8px; border-bottom: 1px solid var(--line); }
  .tb { padding: 3px 9px; border-radius: 5px; color: var(--muted); }
  .tb.on { color: #e6edf6; background: #1a2230; border: 1px solid var(--line2); }
  .tb.sep { padding: 0; width: 1px; background: var(--line2); margin: 2px 4px; }
  .tb kbd { font: 10px var(--mono); color: var(--acc); }
  .app-tabs { display: flex; gap: 2px; padding: 6px 8px 0; border-bottom: 1px solid var(--line); }
  .tab { padding: 4px 12px; border: 1px solid transparent; border-bottom: 0; border-radius: 6px 6px 0 0; color: var(--muted); }
  .tab.act { color: #6fdc95; background: #11161d; border-color: var(--line2); font-weight: 600; margin-bottom: -1px; }
  .app-body { display: grid; grid-template-columns: .92fr 1.08fr; min-height: 270px; }
  .app-list { border-collapse: collapse; width: 100%; border-right: 1px solid var(--line); align-self: start; }
  .app-list th, .tr.th { color: var(--dim); font-weight: 600; font-size: 11.5px; text-align: left; }
  .app-list th, .app-list td { padding: 4px 8px; white-space: nowrap; border-bottom: 1px solid #161d27; }
  .app-list .sel td { background: #173a5c; }
  .app-tree { padding: 0 0 6px; }
  .tr { display: grid; grid-template-columns: 1fr 1fr; padding: 3.5px 8px; white-space: nowrap; border-bottom: 1px solid #161d27; }
  .tr span { overflow: hidden; text-overflow: ellipsis; }
  .tr.i1 span:first-child { padding-left: 22px; }
  .c-mine, .c-mine-t { color: #6fdc95; } .c-other { color: #6ea8f0; } .c-taken { color: #f07167; }
  .c-local, .c-new, .c-new-t { color: #7d8898; } .c-mirror { color: #b18cff; }
  .app-status { padding: 7px 10px; border-top: 1px solid var(--line); color: var(--muted); font-size: 11.5px; background: #0b0f15; }
  .app-status b { color: #d7dee8; }
  @media (max-width: 560px) { .app-body { grid-template-columns: 1fr; } .app-list { border-right: 0; border-bottom: 1px solid var(--line); } }

  /* --- sekce --- */
  section { padding: 96px 0; border-bottom: 1px solid var(--line); }
  .kicker { font: 500 12px var(--mono); color: var(--acc2); letter-spacing: .14em; text-transform: uppercase; }
  h2 { font-size: clamp(28px, 3.4vw, 40px); letter-spacing: -.025em; line-height: 1.15; margin: 12px 0 14px; }
  .sec-lead { color: var(--muted); max-width: 680px; font-size: 16.5px; margin: 0 0 48px; }

  /* metriky */
  .metrics { display: grid; grid-template-columns: repeat(4, 1fr); border: 1px solid var(--line); border-radius: 14px;
             background: var(--bg2); overflow: hidden; }
  @media (max-width: 760px) { .metrics { grid-template-columns: repeat(2, 1fr); } }
  .metric { padding: 26px 26px 22px; border-right: 1px solid var(--line); position: relative; }
  .metric:last-child { border-right: 0; }
  @media (max-width: 760px) { .metric:nth-child(2) { border-right: 0; } .metric:nth-child(-n+2) { border-bottom: 1px solid var(--line); } }
  .metric b { display: block; font: 700 40px/1.1 var(--mono); letter-spacing: -.03em; }
  .metric span { color: var(--muted); font-size: 13px; }
  .metric::before { content: attr(data-k); position: absolute; top: 12px; right: 16px; font: 10px var(--mono); color: var(--dim); letter-spacing: .1em; }

  /* architektura */
  .arch { background: var(--bg2); border: 1px solid var(--line); border-radius: 16px; padding: 28px; }
  .arch svg { width: 100%; height: auto; display: block; }
  @media (max-width: 760px) { .arch { overflow-x: auto; padding: 16px; } .arch svg { min-width: 760px; } }
  .flow { stroke-dasharray: 6 8; animation: flow 1.4s linear infinite; }
  .flow.rev { animation-direction: reverse; }
  @keyframes flow { to { stroke-dashoffset: -28; } }

  /* workflow */
  .steps { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; counter-reset: s; }
  @media (max-width: 900px) { .steps { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 520px) { .steps { grid-template-columns: 1fr; } }
  .step { background: var(--panel); border: 1px solid var(--line); border-radius: 14px; padding: 22px; position: relative; }
  .step::before { counter-increment: s; content: "0" counter(s); font: 600 12px var(--mono); color: var(--dim); }
  .step h3 { margin: 8px 0 8px; font-size: 18px; display: flex; align-items: center; gap: 10px; }
  .step h3 .k { font: 600 11px var(--mono); padding: 2px 7px; border-radius: 6px; border: 1px solid var(--line2); color: var(--muted); }
  .step p { margin: 0; color: var(--muted); font-size: 14px; }
  .step .bar { height: 3px; border-radius: 3px; margin-top: 18px; }

  /* funkce */
  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1px; background: var(--line);
          border: 1px solid var(--line); border-radius: 16px; overflow: hidden; }
  @media (max-width: 900px) { .grid { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 560px) { .grid { grid-template-columns: 1fr; } }
  .feat { background: var(--bg2); padding: 28px 26px; transition: background .2s; }
  .feat:hover { background: #0e1621; }
  .feat .ico { width: 38px; height: 38px; border-radius: 10px; display: grid; place-items: center; margin-bottom: 16px;
               border: 1px solid var(--line2); background: linear-gradient(180deg, #121c29, #0b121b); }
  .feat .ico svg { width: 20px; height: 20px; }
  .feat h3 { margin: 0 0 8px; font-size: 16.5px; }
  .feat p { margin: 0; color: var(--muted); font-size: 14px; }

  /* skript */
  .split { display: grid; grid-template-columns: .9fr 1.1fr; gap: 48px; align-items: center; }
  @media (max-width: 960px) { .split { grid-template-columns: 1fr; } }
  .checks { list-style: none; padding: 0; margin: 0; display: grid; gap: 12px; }
  .checks li { padding-left: 30px; position: relative; color: #b3c0d2; }
  .checks li::before { content: ""; position: absolute; left: 0; top: 4px; width: 18px; height: 18px; border-radius: 5px;
                       background: #0b2418 url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Cpath d='M4 8.5l2.5 2.5L12 5.5' fill='none' stroke='%233ee089' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") center/14px no-repeat;
                       border: 1px solid #1d4f36; }

  /* telemetrie */
  .telemetry { display: grid; grid-template-columns: 1fr; gap: 20px; }
  .card { background: var(--bg2); border: 1px solid var(--line); border-radius: 16px; padding: 24px; min-width: 0; }
  .card h3 { margin: 0 0 16px; font-size: 14px; font: 600 12px var(--mono); color: var(--muted); letter-spacing: .1em; text-transform: uppercase;
             display: flex; justify-content: space-between; }
  .scroll { overflow-x: auto; padding-bottom: 6px; }
  .heat { display: grid; grid-template-rows: 14px repeat(7, 12px); grid-auto-flow: column; grid-auto-columns: 12px;
          gap: 3px; width: max-content; margin: 0 auto; }
  .heat .m { font: 10px var(--mono); color: var(--dim); white-space: nowrap; }
  .heat i { display: block; width: 12px; height: 12px; border-radius: 3px; background: var(--l0); }
  .heat i.l1 { background: var(--l1); } .heat i.l2 { background: var(--l2); }
  .heat i.l3 { background: var(--l3); } .heat i.l4 { background: var(--l4); box-shadow: 0 0 8px rgba(62, 224, 137, .55); }
  .heat i.out { visibility: hidden; }
  .legend { display: flex; justify-content: flex-end; align-items: center; gap: 4px; color: var(--dim); font: 11px var(--mono); margin-top: 12px; }
  .legend i { width: 12px; height: 12px; border-radius: 3px; display: inline-block; }
  .ver { display: grid; grid-template-columns: 90px 1fr 50px; gap: 12px; align-items: center; font: 13px var(--mono); padding: 6px 0; }
  .ver .track { height: 8px; border-radius: 8px; background: var(--l0); overflow: hidden; }
  .ver .track b { display: block; height: 100%; border-radius: 8px; background: linear-gradient(90deg, var(--acc2), var(--acc)); }
  .ver .n { text-align: right; color: var(--muted); }
  .privacy { color: var(--muted); font-size: 13px; margin: 20px 0 0; max-width: 820px; }

  /* stažení */
  .dl { position: relative; overflow: hidden; border: 1px solid var(--line2); border-radius: 20px; padding: 48px;
        background: radial-gradient(ellipse at top right, rgba(62, 224, 137, .14), transparent 55%),
                    radial-gradient(ellipse at bottom left, rgba(43, 179, 255, .12), transparent 55%), var(--panel);
        display: grid; grid-template-columns: 1.1fr .9fr; gap: 40px; }
  @media (max-width: 860px) { .dl { grid-template-columns: 1fr; padding: 28px; } }
  .dl h2 { margin-top: 6px; }
  .release { font: 13px/1.7 var(--mono); color: #b3c0d2; background: #070b11; border: 1px solid var(--line); border-radius: 12px; padding: 18px 20px; }
  .release .hd { color: var(--dim); margin-bottom: 8px; }
  .release ul { margin: 0; padding-left: 0; list-style: none; }
  .release li::before { content: "+ "; color: var(--acc); }
  .how { color: var(--muted); font-size: 14px; margin: 18px 0 0; }

  footer { padding: 36px 0 56px; color: var(--dim); font: 12.5px var(--mono); }
  footer .wrap { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 12px; }

  .reveal { opacity: 0; transform: translateY(18px); transition: opacity .7s ease, transform .7s ease; }
  .reveal.in { opacity: 1; transform: none; }
  @media (prefers-reduced-motion: reduce) {
    .reveal { opacity: 1; transform: none; transition: none; }
    .flow, .cursor, .eyebrow::before { animation: none; }
  }
  @media (max-width: 520px) { .wrap { padding: 0 16px; } section { padding: 64px 0; } }
</style>
</head>
<body>

<nav>
  <div class="wrap">
    <a class="brand" href="#"><img src="icon-96.png" alt="" width="28" height="28">SPM <small>v<?= $h(SPM_LATEST_VERSION) ?></small></a>
    <div class="links">
      <a class="opt" href="#architektura">Architektura</a>
      <a class="opt" href="#funkce">Funkce</a>
      <a class="opt" href="#telemetrie">Statistika</a>
      <a href="<?= $h($github) ?>" target="_blank" rel="noopener">GitHub</a>
      <a href="#stazeni">Stáhnout</a>
    </div>
  </div>
</nav>

<header class="hero">
  <div class="wrap">
    <div>
      <div class="eyebrow">Serverless PDM · Windows</div>
      <h1>Správa projektových dat.<br><span class="grad">Bez serveru. Bez instalace.</span></h1>
      <p class="lead">SPM je lightweight PDM, které z běžného síťového disku udělá prostor pro týmovou práci:
        zamykání souborů, historie odevzdání, zálohy, obnova a automatizační skripty.
        Jeden <code>SPM.exe</code>, žádné účty, žádný databázový server ani služba.</p>
      <div class="cta">
        <a class="btn primary" href="<?= $h(SPM_DOWNLOAD_URL) ?>">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0l-5-5m5 5l5-5M4 21h16"/></svg>
          Stáhnout <?= $h(SPM_LATEST_VERSION) ?></a>
        <a class="btn ghost" href="<?= $h($github) ?>" target="_blank" rel="noopener">
          <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 .5a11.5 11.5 0 0 0-3.64 22.41c.58.1.79-.25.79-.56v-2c-3.2.7-3.88-1.54-3.88-1.54-.53-1.33-1.28-1.69-1.28-1.69-1.05-.72.08-.7.08-.7 1.16.08 1.77 1.19 1.77 1.19 1.03 1.77 2.7 1.26 3.36.96.1-.75.4-1.26.73-1.55-2.55-.29-5.24-1.28-5.24-5.69 0-1.26.45-2.29 1.19-3.1-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.17 1.18a11 11 0 0 1 5.77 0c2.2-1.49 3.17-1.18 3.17-1.18.63 1.59.23 2.76.11 3.05.74.81 1.19 1.84 1.19 3.1 0 4.42-2.7 5.4-5.26 5.68.41.36.78 1.06.78 2.14v3.17c0 .31.21.67.8.56A11.5 11.5 0 0 0 12 .5z"/></svg>
          Zdrojový kód</a>
      </div>
      <div class="specs"><span>Plug &amp; play</span><span>Zámky po souborech</span><span>SQLite na síťovém disku</span><span>SHA-256</span></div>
    </div>

    <div class="app" role="img" aria-label="Ukázka okna SPM: seznam projektů a strom souborů se stavem zámků">
      <div class="app-title"><img src="icon-32.png" alt="" width="14" height="14"> SPM – Server Project Manager</div>
      <div class="app-tools">
        <span class="tb on">Převzít…</span><span class="tb on">Odevzdat… <kbd>F3</kbd></span><span class="tb">Aktualizovat</span>
        <span class="tb sep"></span><span class="tb">Historie</span><span class="tb">Poznámky</span>
      </div>
      <div class="app-tabs"><span class="tab act">Konstrukce</span><span class="tab">Výroba</span><span class="tab">Složka</span></div>
      <div class="app-body">
        <table class="app-list">
          <tr><th>Projekt</th><th>Stav</th><th>Aktivováno</th></tr>
          <tr class="c-mine sel"><td>Forma_2417</td><td>Aktivováno mnou</td><td>já, Bobík</td></tr>
          <tr class="c-other"><td>Forma_2409</td><td>Pracují jiní</td><td>Jana</td></tr>
          <tr><td>Forma_2398</td><td>Volný</td><td></td></tr>
          <tr><td>Pripravek_A12</td><td>Volný</td><td></td></tr>
          <tr class="c-local"><td>Nabidka_88</td><td>Jen místně</td><td></td></tr>
          <tr><td>Forma_2371</td><td>Volný</td><td></td></tr>
        </table>
        <div class="app-tree">
          <div class="tr th"><span>Soubor</span><span>Drží</span></div>
          <div class="tr"><span>▾ 📁 elektrody</span><span></span></div>
          <div class="tr c-mine i1"><span>E01.elt</span><span>já</span></div>
          <div class="tr c-mine i1"><span>E02.elt</span><span>já</span></div>
          <div class="tr c-taken i1"><span>E03.elt</span><span>Bobík</span></div>
          <div class="tr c-new i1"><span>E04.elt</span><span>nový – neodevzdaný</span></div>
          <div class="tr"><span>▾ 📁 dily</span><span></span></div>
          <div class="tr i1"><span class="c-mine-t">deska_pevna.elt</span><span class="c-new-t">přejmenován</span></div>
          <div class="tr i1"><span>vodici_tyc.elt</span><span></span></div>
          <div class="tr c-mirror i1"><span>sestava.pdf</span><span></span></div>
          <div class="tr"><span>▸ 📁 vykresy</span><span></span></div>
        </div>
      </div>
      <div class="app-status">Aktivováno: já, Bobík · zamčené soubory – já: <b>2</b>, Bobík: <b>1</b> · volné: <b>3</b> · nové neodevzdané: <b>1</b></div>
    </div>
  </div>
</header>

<section id="telemetrie-kratce" style="padding:56px 0">
  <div class="wrap">
    <div class="metrics reveal">
      <div class="metric" data-k="30D"><b class="count" data-to="<?= $active30 ?>"><?= $active30 ?></b><span>aktivních instalací za 30 dní</span></div>
      <div class="metric" data-k="DNES"><b class="count" data-to="<?= $activeToday ?>"><?= $activeToday ?></b><span>aktivních dnes</span></div>
      <div class="metric" data-k="CELKEM"><b class="count" data-to="<?= $total ?>"><?= $total ?></b><span>instalací celkem</span></div>
      <div class="metric" data-k="LATEST"><b><?= $h(SPM_LATEST_VERSION) ?></b><span>aktuální verze</span></div>
    </div>
  </div>
</section>

<section id="architektura">
  <div class="wrap">
    <div class="kicker">// Architektura</div>
    <h2>Síťový disk je celý backend.</h2>
    <p class="sec-lead">Každý klient pracuje v místní kopii. Stav projektu – zámky, odevzdání, historie, poznámky – je
      v SQLite databázi přímo ve složce projektu. Zápis do databáze chrání atomický <code>write.lock</code>, soubory se kopírují
      přes dočasný soubor a změny se poznají podle SHA-256. Žádná služba, kterou by bylo nutné provozovat.</p>
    <div class="arch reveal">
      <svg viewBox="0 0 1080 380" role="img" aria-label="Schéma: klienti SPM s místními kopiemi synchronizují data se síťovou složkou">
        <defs>
          <linearGradient id="gA" x1="0" x2="1"><stop offset="0" stop-color="#3ee089"/><stop offset="1" stop-color="#2bb3ff"/></linearGradient>
          <filter id="glow"><feGaussianBlur stdDeviation="6"/></filter>
        </defs>
        <!-- klienti -->
        <g font-family="JetBrains Mono, Consolas, monospace" font-size="13">
          <?php foreach ([[40, 'PC-KONSTRUKCE', 'alice', '#3ee089', 'drží 3 soubory'], [40, 'PC-CNC', 'bob', '#2bb3ff', 'drží 1 soubor'], [40, 'PC-TECHNOLOG', 'jana', '#b18cff', 'jen čtení']] as $i => [$x, $pc, $u, $c, $st]): $y = 30 + $i * 120; ?>
          <rect x="<?= $x ?>" y="<?= $y ?>" width="250" height="88" rx="12" fill="#0d141e" stroke="#263449"/>
          <circle cx="<?= $x + 22 ?>" cy="<?= $y + 26 ?>" r="5" fill="<?= $c ?>"/>
          <text x="<?= $x + 36 ?>" y="<?= $y + 31 ?>" fill="#e6edf6" font-weight="700"><?= $pc ?></text>
          <text x="<?= $x + 18 ?>" y="<?= $y + 56 ?>" fill="#8392a8">SPM.exe · <?= $u ?></text>
          <text x="<?= $x + 18 ?>" y="<?= $y + 75 ?>" fill="<?= $c ?>" font-size="12">● <?= $st ?></text>
          <path class="flow<?= $i === 1 ? ' rev' : '' ?>" d="M<?= $x + 250 ?> <?= $y + 44 ?> C 400 <?= $y + 44 ?>, 420 190, 520 190" fill="none" stroke="<?= $c ?>" stroke-width="2" opacity=".85"/>
          <?php endforeach; ?>
        </g>
        <!-- síťová složka -->
        <rect x="520" y="60" width="300" height="260" rx="16" fill="#0a1019" stroke="url(#gA)" stroke-width="1.5"/>
        <rect x="520" y="60" width="300" height="260" rx="16" fill="none" stroke="#3ee089" opacity=".25" filter="url(#glow)"/>
        <g font-family="JetBrains Mono, Consolas, monospace" font-size="12.5">
          <text x="544" y="92" fill="#3ee089" font-weight="700" font-size="13.5">\\server\projekty\</text>
          <text x="544" y="122" fill="#8392a8">├─ spm_config.json</text>
          <text x="544" y="144" fill="#8392a8">├─ .spm\ vzkazy · archiv · scripts</text>
          <text x="544" y="166" fill="#e6edf6">└─ Forma_058768\</text>
          <text x="568" y="188" fill="#2bb3ff">├─ .spm\project.db</text>
          <text x="568" y="210" fill="#ffb547">├─ .spm\write.lock</text>
          <text x="568" y="232" fill="#8392a8">├─ .spm\backup\ · trash\</text>
          <text x="568" y="254" fill="#8392a8">├─ .spm\active\*.json</text>
          <text x="568" y="276" fill="#e6edf6">└─ …soubory projektu</text>
        </g>
        <!-- zrcadlo -->
        <path class="flow" d="M820 190 C 880 190, 880 120, 930 120" fill="none" stroke="#b18cff" stroke-width="2"/>
        <path class="flow" d="M820 230 C 880 230, 880 280, 930 280" fill="none" stroke="#ffb547" stroke-width="2"/>
        <g font-family="JetBrains Mono, Consolas, monospace" font-size="12">
          <rect x="930" y="88" width="130" height="64" rx="10" fill="#0d141e" stroke="#263449"/>
          <text x="946" y="114" fill="#b18cff" font-weight="700">MIRROR</text>
          <text x="946" y="136" fill="#8392a8">jiná záložka</text>
          <rect x="930" y="248" width="130" height="64" rx="10" fill="#0d141e" stroke="#263449"/>
          <text x="946" y="274" fill="#ffb547" font-weight="700">ARCHIV</text>
          <text x="946" y="296" fill="#8392a8">.zip projektu</text>
        </g>
      </svg>
    </div>
  </div>
</section>

<section id="workflow">
  <div class="wrap">
    <div class="kicker">// Workflow</div>
    <h2>Dvě akce. Celý životní cyklus.</h2>
    <p class="sec-lead">Převzít a Odevzdat pokrývají vše od aktivace projektu po jeho uvolnění. Když nic nedržíte
      a nic neodevzdaného nezůstalo, práce na projektu skončí automaticky.</p>
    <div class="steps">
      <div class="step reveal"><h3>Převzít <span class="k">F3</span></h3><p>Zamkne vybrané soubory a stáhne aktuální verzi. Projekt se aktivuje do místního úložiště.</p><div class="bar" style="background:var(--acc)"></div></div>
      <div class="step reveal"><h3>Pracovat</h3><p>Lokálně, rychle a spolehlivě – i v CAD. Ostatní soubory jen pro čtení, cizí zámky jsou vidět.</p><div class="bar" style="background:var(--acc2)"></div></div>
      <div class="step reveal"><h3>Odevzdat <span class="k">F3</span></h3><p>Změny, nové soubory, přejmenování i mazání s komentářem. Předchozí verze jde do zálohy.</p><div class="bar" style="background:var(--vio)"></div></div>
      <div class="step reveal"><h3>Historie</h3><p>Časová osa odevzdání a obnova projektu k odevzdání, ke kterému jsou zálohy – ve stylu Gitu, ale bez Gitu.</p><div class="bar" style="background:var(--warn)"></div></div>
    </div>
  </div>
</section>

<section id="funkce">
  <div class="wrap">
    <div class="kicker">// Funkce</div>
    <h2>Navrženo pro dílnu, ne pro IT oddělení.</h2>
    <p class="sec-lead">Vše, co tým potřebuje ke sdílení projektových dat – a nic, co by bylo nutné spravovat.</p>
    <?php
    $ico = fn(string $d, string $c) => '<div class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="' . $c . '" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' . $d . '</svg></div>';
    $features = [
        ['<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>', '#3ee089', 'Zámky po souborech', 'Na jednom projektu pracuje víc lidí současně. V seznamu je vidět, kdo na projektu pracuje, ve stromu kdo drží který soubor.'],
        ['<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 3"/>', '#2bb3ff', 'Historie a obnova', 'Každé odevzdání s komentářem, předchozí verze souborů v zálohách. Obnova projektu k vybranému odevzdání do nové místní složky.'],
        ['<path d="M8 6l-6 6 6 6M16 6l6 6-6 6M14 4l-4 16"/>', '#b18cff', 'Automatizační skripty', 'Vlastní jazyk spouštěný při odevzdání: MIRROR, kopie, ZIP, textové soubory, .bat, POST na API.'],
        ['<path d="M4 7h16M4 12h10M4 17h7"/><circle cx="18" cy="16" r="3"/>', '#ffb547', 'Rozpoznání přejmenování', 'Přesuny a přejmenování se rozpoznají podle hashe a podobnosti – historie souboru zůstane.'],
        ['<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>', '#3ee089', 'Poznámky a vzkazy', 'Markdown, adresáti, @zmínky a #projekty. Vlákna odpovědí a vyřízení přímo v kartě.'],
        ['<rect x="3" y="4" width="8" height="16" rx="2"/><rect x="13" y="4" width="8" height="16" rx="2"/>', '#2bb3ff', 'Dva panely', 'Jako Total Commander: dvě záložky vedle sebe, kopírování a přetahování i z Průzkumníka, hledání souborů a složek.'],
        ['<path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z"/><path d="M9 12l2 2 4-4"/>', '#b18cff', 'Bezpečné kopírování', 'Zápis přes dočasný soubor (přerušené kopírování nic nepoškodí), SHA-256 pro rozpoznání změn, před odevzdáním kontrola souborů otevřených v jiném programu.'],
        ['<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>', '#ffb547', 'Společné nastavení', '<code>spm_config.json</code> v síťové složce sjednotí zálohy, ignorované soubory i oprávnění všech klientů.'],
        ['<path d="M21 8v13H3V8M1 3h22v5H1zM10 12h4"/>', '#3ee089', 'Archivace', 'Dokončené projekty do zipu (jen oprávnění uživatelé), obnova z archivu, přehled a úklid záloh a koše.'],
    ];
    ?>
    <div class="grid">
<?php foreach ($features as [$d, $c, $t, $p]): ?>
      <div class="feat reveal"><?= $ico($d, $c) ?><h3><?= $h($t) ?></h3><p><?= $p ?></p></div>
<?php endforeach; ?>
    </div>
  </div>
</section>

<section id="skripty">
  <div class="wrap split">
    <div>
      <div class="kicker">// Skripty</div>
      <h2>Odevzdání, které pracuje za vás.</h2>
      <p class="sec-lead" style="margin-bottom:28px">Jednoduchý jazyk spouštěný při odevzdání (i před nahráním). Editor s nápovědou,
        hledáním a zkušebním během – bez programátora.</p>
      <ul class="checks">
        <li>Zrcadlení vybraných souborů do projektu jiné síťové složky</li>
        <li>ZIP balíčky, kopie, generované textové a .bat soubory</li>
        <li>Dotazy na uživatele a podmínky podle typu akce</li>
        <li>Volání externích API metodou POST</li>
        <li>Zpětně kompatibilní – staré skripty fungují i v nových verzích</li>
      </ul>
    </div>
    <div class="term reveal" aria-hidden="true">
      <div class="bar"><i></i><i></i><i></i><span>vyroba.spm</span></div>
<pre style="min-height:0"><span class="t-dim"># skript odevzdání – data pro výrobu</span>
<span class="t-vio">BEFORE</span>
    <span class="t-blue">IF</span> ACTION == <span class="t-warn">"deactivate"</span> <span class="t-blue">AND NOT</span> ASK(<span class="t-warn">"Předat projekt do výroby?"</span>)
        <span class="t-red">STOP</span> <span class="t-warn">"Odevzdání zrušeno"</span>
    <span class="t-blue">END</span>
<span class="t-vio">END</span>

<span class="t-blue">FILES</span> nc = commit <span class="t-warn">"**/PGMs_*/*.h"</span> <span class="t-blue">STATUS</span> changed
<span class="t-blue">IF</span> COUNT(nc) &gt; 0
    <span class="t-acc">MIRROR</span> nc <span class="t-blue">TO</span> <span class="t-warn">"{TAB:Výroba}/{PROJECT}/NCFiles"</span> <span class="t-blue">KEEP</span> 1
    <span class="t-acc">ZIP</span> nc <span class="t-blue">TO</span> <span class="t-warn">"P:/Predani/{PROJECT}_{DATE}.zip"</span>
    <span class="t-acc">POST</span> <span class="t-warn">"https://erp.firma.cz/api/predani"</span>
        projekt = PROJECT
        <span class="t-warn">"počet programů"</span> = COUNT(nc)
    <span class="t-blue">END</span>
<span class="t-blue">END</span></pre>
    </div>
  </div>
</section>

<section id="telemetrie">
  <div class="wrap">
    <div class="kicker">// Telemetrie</div>
    <h2>Otevřená statistika používání.</h2>
    <p class="sec-lead">Živá data z anonymních denních hlášení. Počítají se instalace, které se ozvaly aspoň
      v <?= $real ?> různých dnech.</p>
    <div class="telemetry">
      <div class="card reveal">
        <h3><span>Aktivita · 53 týdnů</span><span><?= $h($today) ?></span></h3>
        <div class="scroll">
          <div class="heat" role="img" aria-label="Počet aktivních instalací po dnech za poslední rok">
<?php
for ($w = 0; $w < $weeks; $w++) {
    $monday = $start->modify("+{$w} weeks");
    $label = ($w === 0 || (int)$monday->format('j') <= 7) ? $months[(int)$monday->format('n') - 1] : '';
    echo '            <span class="m">' . $h($label) . "</span>\n";
    for ($d = 0; $d < 7; $d++) {
        $day = $monday->modify("+{$d} days");
        $key = $day->format('Y-m-d');
        if ($key > $today) {
            echo "            <i class=\"out\"></i>\n";
            continue;
        }
        $count = $perDay[$key] ?? 0;
        $title = $day->format('j. n. Y') . ': ' . $count . ($count === 1 ? ' instalace' : ' instalací');
        echo '            <i class="l' . level($count, $max) . '" title="' . $h($title) . "\"></i>\n";
    }
}
?>
          </div>
        </div>
        <div class="legend">méně <i style="background:var(--l0)"></i><i style="background:var(--l1)"></i><i
            style="background:var(--l2)"></i><i style="background:var(--l3)"></i><i style="background:var(--l4)"></i> více</div>
      </div>
      <div class="card reveal">
        <h3><span>Používané verze · 30 dní</span><span><?= array_sum(array_map(fn($v) => (int)$v[1], $versions)) ?> instalací</span></h3>
<?php if ($versions): ?>
<?php foreach ($versions as [$version, $count]): ?>
        <div class="ver"><span><?= $h($version !== '' ? $version : 'neznámá') ?></span>
          <span class="track"><b style="width:<?= round(100 * (int)$count / $versionsTotal, 1) ?>%"></b></span>
          <span class="n"><?= (int)$count ?></span></div>
<?php endforeach; ?>
<?php else: ?>
        <p class="privacy" style="margin:0">Zatím žádná data.</p>
<?php endif; ?>
      </div>
    </div>
    <p class="privacy">Aplikace jednou denně odešle jen hash uživatelského jména a názvu počítače a číslo verze –
      žádná jména, cesty ani obsah projektů. IP adresa se neukládá. Odesílání lze vypnout v nastavení aplikace
      nebo pro celou firmu ve <code>spm_config.json</code> (<code>"usage_report": false</code>).</p>
  </div>
</section>

<section id="stazeni" style="border-bottom:0">
  <div class="wrap">
    <div class="dl reveal">
      <div>
        <div class="kicker">// Stažení</div>
        <h2>Rozbalit. Spustit. Hotovo.</h2>
        <p class="how" style="margin:0 0 26px">Zip rozbalte do libovolné složky s právem zápisu a spusťte
          <code>SPM\SPM.exe</code> – instalace není potřeba. Při aktualizaci rozbalte zip přes stávající
          složku (nastavení zůstane).</p>
        <div class="cta">
          <a class="btn primary" href="<?= $h(SPM_DOWNLOAD_URL) ?>">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m0 0l-5-5m5 5l5-5M4 21h16"/></svg>
            SPM <?= $h(SPM_LATEST_VERSION) ?> · zip</a>
          <a class="btn ghost" href="<?= $h($github) ?>" target="_blank" rel="noopener">GitHub</a>
        </div>
      </div>
      <div class="release">
        <div class="hd">## v<?= $h(SPM_LATEST_VERSION) ?> – co je nového</div>
<?php if ($notes): ?>
        <ul>
<?php foreach ($notes as $line): ?>
          <li><?= $h($line) ?></li>
<?php endforeach; ?>
        </ul>
<?php else: ?>
        <span class="t-dim">Opravy a vylepšení.</span>
<?php endif; ?>
      </div>
    </div>
  </div>
</section>

<footer>
  <div class="wrap">
    <span>© <?= date('Y') ?> Ondřej Vopravil · SPM – Server Project Manager</span>
    <span><a href="<?= $h($github) ?>" target="_blank" rel="noopener">github</a> · <a href="mailto:ondrej@vopravil.cz">ondrej@vopravil.cz</a></span>
  </div>
</footer>

<script>
  // postupné zobrazení sekcí a dopočítání čísel metrik
  (function () {
    var io = 'IntersectionObserver' in window ? new IntersectionObserver(function (es) {
      es.forEach(function (e) {
        if (!e.isIntersecting) return;
        e.target.classList.add('in');
        e.target.querySelectorAll('.count').forEach(function (el) {
          var to = +el.dataset.to, t0 = null;
          if (!to) return;
          (function step(t) {
            t0 = t0 || t;
            var p = Math.min(1, (t - t0) / 900);
            el.textContent = Math.round(to * (1 - Math.pow(1 - p, 3)));
            if (p < 1) requestAnimationFrame(step);
          })(performance.now());
        });
        io.unobserve(e.target);
      });
    }, { threshold: .15 }) : null;
    document.querySelectorAll('.reveal').forEach(function (el) { io ? io.observe(el) : el.classList.add('in'); });
  })();
</script>
</body>
</html>
