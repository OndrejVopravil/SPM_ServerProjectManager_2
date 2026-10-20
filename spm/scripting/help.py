"""Nápověda k jazyku skriptů (zobrazí se v okně Skripty).

Každý příkaz a funkce má vlastní oddíl s kotvou <a name="…"> (malými písmeny) – obsah na
začátku na ně odkazuje. Test hlídá, že žádný příkaz ani funkce jazyka v nápovědě nechybí.
"""

# (kotva, nadpis) – příkazy a funkce v pořadí, v jakém jsou v nápovědě
COMMANDS = [
    ("set", "SET"), ("files", "FILES"), ("parse", "PARSE"), ("if", "IF / ELSE / END"),
    ("for", "FOR"), ("log", "LOG"), ("stop", "STOP"), ("exit", "EXIT"), ("copy", "COPY"),
    ("zip", "ZIP"), ("unzip", "UNZIP"), ("mkdir", "MKDIR"), ("delete", "DELETE"),
    ("rename", "RENAME"), ("replace", "REPLACE NAMES"), ("write", "WRITE / APPEND"),
    ("mirror", "MIRROR"), ("form", "FORM"), ("call", "CALL"), ("before", "BEFORE"),
    ("run", "RUN"), ("post", "POST"),
]
FUNCTIONS = [
    ("count", "COUNT"), ("first", "FIRST"), ("last", "LAST"), ("one", "ONE"), ("upper", "UPPER"),
    ("lower", "LOWER"), ("replace_f", "REPLACE"), ("match", "MATCH"), ("tab", "TAB"),
    ("activate", "ACTIVATE"), ("format", "FORMAT"), ("ask", "ASK"), ("input", "INPUT"), ("choose", "CHOOSE"),
    ("secret", "SECRET"),
]


def _index() -> str:
    def links(items):
        return " · ".join(f'<a href="#{a}">{t}</a>' for a, t in items)
    return (f'<p><b>Základy:</b> <a href="#zaklady">Jak skript běží</a> · <a href="#zapis">Zápis</a> · '
            f'<a href="#mnoziny">Množiny souborů</a> · <a href="#promenne">Vestavěné proměnné</a> · '
            f'<a href="#vlastnosti">Vlastnosti souboru</a> · <a href="#pocitani">Počítání</a> · '
            f'<a href="#podminky">Podmínky</a> · '
            f'<a href="#cesty">BASE / KEEP / FLAT</a> · <a href="#editor">Editor</a> · '
            f'<a href="#ukazky">Ukázky</a></p>'
            f'<p><b>Příkazy:</b> {links(COMMANDS)}</p><p><b>Funkce:</b> {links(FUNCTIONS)}</p>')


HELP_HTML = """
<style>
pre, code { font-family: Consolas, "Courier New", monospace; }
h3 { margin-top: 18px; }
</style>
<h2>Skripty odevzdání – nápověda</h2>
<p>Hledat můžete v poli nad nápovědou (Enter = další výskyt, Shift+Enter = předchozí,
Esc = zrušit). Na příkaz nebo funkci skočíte odkazem:</p>
""" + _index() + """

<a name="zaklady"></a><h3>Jak skript běží</h3>
<p>Skripty vybrané u záložky (okno Skripty, zaškrtnuté) se spustí při každém <b>odevzdání se
změnami</b> – i při deaktivaci a publikování. Když jsou všechny odevzdávané soubory beze
změny, skript se nespustí.</p>
<ol>
<li>Potvrdíte dialog odevzdání.</li>
<li>Proběhnou bloky <a href="#before">BEFORE</a> (smí vytvořit soubory v místní kopii – ty se
odevzdají také).</li>
<li>Soubory se nahrají na server, zámky se uvolní.</li>
<li>Proběhne zbytek skriptu (zrcadlení, kopie, archivy…).</li>
<li>Při deaktivaci se smaže místní kopie.</li>
</ol>
<p>Skript pracuje <b>jen se soubory tohoto odevzdání</b> (množina <code>commit</code>) a smí je
jen číst. Chyba v zápisu skriptu nebo chyba v BEFORE = <b>nic se neodevzdá</b>. Chyba po
nahrání = data jsou odevzdaná, místní kopie zůstane a skript spustíte znovu pravým tlačítkem
na projektu (<i>Spustit skripty znovu</i>). Záznam každého běhu je v Historii projektu.</p>

<a name="zapis"></a><h3>Zápis skriptu</h3>
<ul>
<li>Jeden příkaz = jeden řádek. Klíčová slova jsou anglicky, na velikosti písmen nezáleží
(<code>COPY</code> = <code>copy</code>). Na velikosti písmen nezáleží ani u proměnných.</li>
<li>Komentář začíná znakem <code>#</code> a platí do konce řádku.</li>
<li>Text je v uvozovkách. Proměnné a výrazy se do něj vkládají ve složených závorkách:
<code>"{PROJECT}_{DATE}.zip"</code>, <code>"{f.stem}"</code>,
<code>"{COUNT(commit "*.nc")}"</code> (uvozovky uvnitř závorek text neukončí).</li>
<li>Uvozovka v textu se píše zdvojeně: <code>"řekl ""ahoj"" a šel"</code> → řekl "ahoj" a šel.
Složená závorka zdvojeně: <code>"{{"</code> → {.</li>
<li>Cesty můžete psát s <code>\\</code> i <code>/</code>. Relativní cesta (bez disku)
patří do dočasné pracovní složky <code>{WORK}</code>, která se po skriptu smaže.</li>
<li>Skript nesmí zapisovat do složek záložek SPM (síťových ani místních) – do místní
kopie projektu jen v <a href="#before">BEFORE</a>, do jiných záložek jen přes
<a href="#mirror">MIRROR</a>.</li>
</ul>

<a name="mnoziny"></a><h3>Množiny souborů a masky</h3>
<p><code>commit</code> = všechny soubory tohoto odevzdání. Vlastní množiny vytváří
<a href="#files">FILES</a>, výběr s maskou lze napsat i přímo do příkazu nebo funkce:
<code>COUNT(commit "*.nc")</code>, <code>ZIP commit "*.h" TO …</code>.</p>
<table border="1" cellspacing="0" cellpadding="3">
<tr><th>Maska</th><th>Vybere</th></tr>
<tr><td><code>"*.nc"</code></td><td>.nc kdekoliv v projektu (maska bez lomítka = jen název souboru)</td></tr>
<tr><td><code>"Dily/*.h"</code></td><td>.h přímo ve složce Dily</td></tr>
<tr><td><code>"Dily/**"</code></td><td>vše ve složce Dily a jejích podsložkách</td></tr>
<tr><td><code>"**/CAM/*.nc"</code></td><td>.nc ve složkách CAM (v jakékoliv hloubce)</td></tr>
<tr><td><code>"**/PGMs_*/*.h"</code></td><td>.h ve složkách, jejichž název začíná PGMs_</td></tr>
<tr><td><code>"*.h;*.html"</code></td><td>víc masek oddělených středníkem = „nebo“</td></tr>
<tr><td><code>"*.nc;!stare/**"</code></td><td><code>!</code> = vynechat (zde vše ze složky stare)</td></tr>
<tr><td><code>"E0?.nc"</code></td><td><code>?</code> = právě jeden znak</td></tr>
</table>
<p>Na velikosti písmen v maskách nezáleží. <b>Stav souboru</b> (<code>STATUS</code> u výběru,
vlastnost <code>.status</code>):</p>
<table border="1" cellspacing="0" cellpadding="3">
<tr><td><code>added</code></td><td>nový soubor</td></tr>
<tr><td><code>modified</code></td><td>změněný obsah</td></tr>
<tr><td><code>renamed</code></td><td>přejmenovaný / přesunutý (původní cesta je v <code>.old_path</code>)</td></tr>
<tr><td><code>deleted</code></td><td>smazaný (soubor už neexistuje – COPY a ZIP ho přeskočí, MIRROR ho smaže v cíli)</td></tr>
<tr><td><code>unchanged</code></td><td>beze změny (odevzdáváte ho jen uvolněním zámku)</td></tr>
<tr><td><code>changed</code></td><td>vše kromě unchanged (added + modified + renamed + deleted)</td></tr>
</table>
<p>Přejmenovaný soubor, jehož nový název masce odpovídá a starý ne, je ve výběru jako
<code>added</code>; naopak jako <code>deleted</code> – zrcadlo ho pak v cíli smaže.</p>

<a name="promenne"></a><h3>Vestavěné proměnné</h3>
<table border="1" cellspacing="0" cellpadding="3">
<tr><td><code>{USER}</code>, <code>{PC}</code>, <code>{NICK}</code></td><td>přihlášený uživatel, počítač, přezdívka (z Nastavení)</td></tr>
<tr><td><code>{PROJECT}</code>, <code>{PROJECT_ID}</code></td><td>název projektu, jeho trvalá identita</td></tr>
<tr><td><code>{COMMENT}</code></td><td>komentář odevzdání</td></tr>
<tr><td><code>{COMMIT_ID}</code></td><td>číslo odevzdání v historii projektu (v BEFORE prázdné)</td></tr>
<tr><td><code>{ACTION}</code></td><td>submit (odevzdání), deactivate (deaktivace), publish (publikování), test (zkušební běh)</td></tr>
<tr><td><code>{DATE}</code>, <code>{TIME}</code></td><td>datum RRMMDD (261017), čas HHMM (0905)</td></tr>
<tr><td><code>{YY} {YYYY} {MM} {DD} {HH} {MIN} {SS}</code></td><td>jednotlivé části data a času</td></tr>
<tr><td><code>{LOCAL_DIR}</code>, <code>{SERVER_DIR}</code></td><td>místní kopie projektu, projekt na síti</td></tr>
<tr><td><code>{SERVER_ROOT}</code></td><td>síťová složka záložky</td></tr>
<tr><td><code>{WORK}</code></td><td>dočasná pracovní složka (po skriptu se smaže)</td></tr>
<tr><td><code>{TAB}</code>, <code>{SERVER_NAME}</code></td><td>název záložky, název síťové složky ze spm_config.json</td></tr>
<tr><td><code>{TAB:Výroba}</code></td><td>síťová složka záložky „Výroba“ (viz funkce <a href="#tab">TAB</a>)</td></tr>
<tr><td><code>{SECRET:cam}</code></td><td>tajná hodnota z Nastavení (viz funkce <a href="#secret">SECRET</a>)</td></tr>
</table>

<a name="vlastnosti"></a><h3>Vlastnosti souboru</h3>
<p>Proměnná se souborem (z <a href="#first">FIRST</a>, <a href="#one">ONE</a>, <a href="#for">FOR</a>…)
má vlastnosti. Příklad pro <code>Zakazka/Dily/A/1234_hridel.nc</code>:</p>
<table border="1" cellspacing="0" cellpadding="3">
<tr><td><code>.name</code></td><td>1234_hridel.nc</td></tr>
<tr><td><code>.stem</code></td><td>1234_hridel (bez přípony)</td></tr>
<tr><td><code>.ext</code></td><td>.nc</td></tr>
<tr><td><code>.folder</code></td><td>A (složka, ve které soubor leží)</td></tr>
<tr><td><code>.parent(2)</code></td><td>Dily (o 2 úrovně výš; parent(1) = folder)</td></tr>
<tr><td><code>.dir</code></td><td>Zakazka/Dily/A</td></tr>
<tr><td><code>.path</code></td><td>Zakazka/Dily/A/1234_hridel.nc (cesta v projektu)</td></tr>
<tr><td><code>.full</code></td><td>úplná cesta na disku (místní kopie, jinak server)</td></tr>
<tr><td><code>.status</code></td><td>stav v odevzdání (added, modified…)</td></tr>
<tr><td><code>.old_path</code>, <code>.old_name</code></td><td>u přejmenovaného původní cesta a název</td></tr>
<tr><td><code>.size</code></td><td>velikost v bajtech</td></tr>
</table>
<p>Vlastnosti name, stem, ext, folder, parent(n) a dir má i obyčejný text s cestou:
<code>SET p = "C:\\data\\x.zip"</code> → <code>{p.stem}</code> = x.</p>

<a name="pocitani"></a><h3>Počítání</h3>
<p>Sčítání <code>+</code>, odčítání <code>-</code>, násobení <code>*</code>, dělení <code>/</code>
a závorky. Násobení a dělení mají přednost: <code>2 + 3 * 4</code> = 14,
<code>(2 + 3) * 4</code> = 20. Počítat lze všude, kde se zadává hodnota – v SET, v podmínkách,
v argumentech funkcí i ve složených závorkách v textu.</p>
<ul>
<li>Desetinné číslo se ve skriptu píše s <b>tečkou</b>: <code>1.21</code> (čárka odděluje
argumenty funkcí).</li>
<li>Text, který je číslem, se při počítání převede sám – i s desetinnou čárkou: hodnota
z <a href="#input">INPUT</a> „2,5“ nebo z <a href="#parse">PARSE</a> „015“.</li>
<li>Celá čísla zůstávají celá (<code>7 / 2</code> = 3,5, ale <code>8 / 2</code> = 4).
Desetinné číslo se do textu vypíše s desetinnou čárkou a nejvýš 6 místy bez koncových nul
(10 / 3 → 3,333333); přesný tvar určí <a href="#format">FORMAT</a>.</li>
<li>Dělení nulou a počítání s textem, který není číslem, je chyba. Text se spojuje
v uvozovkách: <code>"{a}{b}"</code>.</li>
<li>Formát čísla rovnou při nastavení proměnné: <code>SET x = výraz FORMAT "000.00"</code> –
proměnná si ho pamatuje a dostane ho každé další číslo, které se do ní uloží.</li>
</ul>
<pre>SET kusy = COUNT(commit "*.nc")
SET dalsi = kusy + 1
SET cena = (kusy * 150 + 200) * 1.21 FORMAT "0.00"     # 4 kusy → 968.00
PARSE f.name "{zakazka}_{pozice}.h"
SET dalsi_pozice = pozice + 10 FORMAT "000"             # "015" → 025
IF kusy * 2 > 10
    LOG "Hodně programů: {kusy}, další číslo {dalsi}, cena {cena}"
END
LOG "Průměr: {FORMAT(cena / kusy, "0,00")} Kč"</pre>

<a name="podminky"></a><h3>Podmínky</h3>
<table border="1" cellspacing="0" cellpadding="3">
<tr><td><code>a == b</code>, <code>a != b</code></td><td>rovná se / nerovná se (texty bez ohledu na velikost písmen)</td></tr>
<tr><td><code>&lt; &gt; &lt;= &gt;=</code></td><td>porovnání – čísla číselně (i texty, které jsou číslem: „015“ = 15), jiné texty abecedně</td></tr>
<tr><td><code>a CONTAINS b</code></td><td>text a obsahuje b</td></tr>
<tr><td><code>a STARTS b</code>, <code>a ENDS b</code></td><td>začíná / končí na</td></tr>
<tr><td><code>a LIKE "*.nc"</code></td><td>odpovídá vzoru (* a ?)</td></tr>
<tr><td><code>EXISTS "cesta"</code></td><td>soubor nebo složka existuje</td></tr>
<tr><td><code>NOT</code>, <code>AND</code>, <code>OR</code></td><td>zápor, a zároveň, nebo</td></tr>
<tr><td><code>IF hodnota</code></td><td>bez porovnání: prázdný text, 0 a prázdná množina = nepravda</td></tr>
</table>
<pre>IF COUNT(nc) > 0 AND ACTION == "deactivate"
    LOG "Deaktivace s NC programy"
END
IF NOT EXISTS "P:\\Predani\\{PROJECT}"
    MKDIR "P:\\Predani\\{PROJECT}"
END
FOR f IN commit
    IF f.name CONTAINS "hridel" OR f.ext == ".h"
        LOG "{f.path}"
    END
END</pre>

<a name="cesty"></a><h3>Cesty v cíli: BASE / KEEP / FLAT</h3>
<p>Volba za příkazy <a href="#copy">COPY</a>, <a href="#zip">ZIP</a> a <a href="#mirror">MIRROR</a>
říká, jakou část cesty souboru v cíli ponechat. Pro <code>Zakazka/Dily/A/x.h</code>:</p>
<table border="1" cellspacing="0" cellpadding="3">
<tr><td><code>BASE PROJECT</code> (výchozí)</td><td>Zakazka/Dily/A/x.h – celá cesta od kořene projektu</td></tr>
<tr><td><code>BASE "Zakazka/Dily"</code></td><td>A/x.h – cesta od dané složky (soubory mimo ni = chyba)</td></tr>
<tr><td><code>KEEP 2</code></td><td>Dily/A/x.h – ponechá 2 složky nad souborem</td></tr>
<tr><td><code>KEEP 1</code></td><td>A/x.h</td></tr>
<tr><td><code>KEEP 0</code> nebo <code>FLAT</code></td><td>x.h – jen název</td></tr>
</table>

<h2>Příkazy</h2>

<a name="set"></a><h3>SET – proměnná</h3>
<p><code>SET název = hodnota [FORMAT "maska"]</code> – uloží text, číslo, výsledek
<a href="#pocitani">výpočtu</a>, soubor nebo množinu. Vestavěné proměnné (PROJECT,
DATE…) přepsat nelze. Názvy: písmena, číslice a _, na velikosti písmen nezáleží.</p>
<p><b>FORMAT</b> (viz funkce <a href="#format">FORMAT</a>) určí tvar čísla a proměnná si ho
<b>pamatuje</b>: každé další číslo, které se do ní uloží (dalším SET, z INPUT, FORM…),
dostane stejný tvar. Jiný FORMAT u stejné proměnné tvar změní; text, který není číslem,
se uloží beze změny. Formát platí jen pro tuto proměnnou – <code>SET b = a</code> zkopíruje
hodnotu, ne formát.</p>
<pre>SET cil = "P:\\Predani\\{PROJECT}"
SET prog = FIRST(commit "*.nc")
SET pocet = COUNT(commit "*.h")
SET dalsi = pocet + 1
SET cislo = pocet * 10 FORMAT "0000"          # 3 → 0030
SET a = 1 FORMAT "000"                        # 001
SET a = a + 1                                 # 002 – formát zůstává</pre>

<a name="files"></a><h3>FILES – množina souborů</h3>
<p><code>FILES název = zdroj "maska" [STATUS stav, stav…]</code> – zdroj je <code>commit</code>
nebo jiná množina. Maska i STATUS jsou nepovinné.</p>
<pre>FILES nc   = commit "*.nc"
FILES nove = commit "**/PGMs_*/*.h;**/PGMs_*/*.html" STATUS changed
FILES jenH = nove "*.h"                       # výběr z jiné množiny
FILES smaz = commit STATUS deleted</pre>

<a name="parse"></a><h3>PARSE – rozebrání textu podle vzoru</h3>
<p><code>PARSE text "vzor"</code> – části vzoru ve složených závorkách se uloží do proměnných;
<code>*</code> = cokoliv, <code>?</code> = jeden znak. Text musí vzoru odpovídat celý (bez
ohledu na velikost písmen), jinak skript skončí chybou.</p>
<pre>PARSE prog.name "{zakazka}_{nazev}.nc"       # 1234_hridel.nc → zakazka=1234, nazev=hridel
PARSE f.name "{zakazka}_*_{pozice}_*.h"      # 8800_A_15_v2.h → zakazka=8800, pozice=15
PARSE f.folder "PGMs_{stroj}"                 # PGMs_DMU80 → stroj=DMU80</pre>

<a name="if"></a><h3>IF / ELSE / END – podmínka</h3>
<p>Příkazy mezi IF a END (nebo ELSE) se provedou jen při splněné <a href="#podminky">podmínce</a>.</p>
<pre>IF COUNT(nc) > 0
    MIRROR nc TO "{TAB:Výroba}"
ELSE
    LOG "Žádné NC programy"
END</pre>

<a name="for"></a><h3>FOR – pro každý soubor</h3>
<p><code>FOR proměnná IN množina … END</code> – příkazy se provedou pro každý soubor množiny.</p>
<pre>FOR f IN commit "*.nc" STATUS changed
    LOG "{f.path} ({f.status})"
    APPEND "{WORK}/seznam.txt"
        {f.name}
    END
END</pre>

<a name="log"></a><h3>LOG – zápis do záznamu běhu</h3>
<p>Zapíše text do záznamu běhu (výsledek odevzdání → Podrobnosti, Historie projektu, zkušební běh).
Nic nezastaví ani nezobrazí.</p>
<pre>LOG "Zrcadleno souborů: {COUNT(nc)}"
LOG "Program {prog.name} ze složky {prog.folder}"</pre>

<a name="stop"></a><h3>STOP – ukončení s chybou</h3>
<p>Ukončí skript s chybou a textem. V BEFORE se pak nic neodevzdá; po nahrání zůstane projekt
s „nedokončeným skriptem“.</p>
<pre>IF COUNT(commit "*.nc") == 0
    STOP "Chybí NC program – odevzdejte i složku PGMs"
END</pre>

<a name="exit"></a><h3>EXIT – ukončení bez chyby</h3>
<p>Ukončí skript (ve volaném skriptu jen ten volaný). Další skripty záložky běží dál.</p>
<pre>IF ACTION == "submit"
    EXIT                                    # při běžném odevzdání nic nedělat
END</pre>

<a name="copy"></a><h3>COPY – kopírování</h3>
<p><code>COPY co TO "složka" [BASE / KEEP / FLAT] [NAME "vzor názvu"]</code> – co: množina,
soubor nebo cesta (soubor nebo celá složka). Cíl je vždy složka; existující soubory se přepíšou.
<code>NAME</code> přejmenuje kopie – v něm je soubor jako <code>{file}</code>.</p>
<pre>COPY commit "*.pdf" TO "P:\\Predani\\{PROJECT}" FLAT
COPY nc TO "{WORK}/nc" KEEP 1
COPY nc TO "D:\\NC" FLAT NAME "{file.stem}_{DATE}{file.ext}"
COPY "{WORK}/x.zip" TO "P:\\Predani"          # jeden soubor
COPY "{WORK}/nc" TO "P:\\NC"                   # obsah složky</pre>

<a name="zip"></a><h3>ZIP – zabalení do archivu</h3>
<p><code>ZIP co TO "archiv.zip" [BASE / KEEP / FLAT]</code> – co: množina, soubor nebo složka.
Smazané soubory se přeskočí; když není co balit, archiv nevznikne. Dva soubory se stejnou
cestou v archivu = chyba (zvolte jiné KEEP / BASE).</p>
<pre>ZIP commit "**/PGMs_*/*.h" STATUS changed TO "D:\\TEMP\\{PROJECT}_nc.zip" KEEP 1
ZIP "{WORK}/nc" TO "{WORK}/nc.zip"</pre>

<a name="unzip"></a><h3>UNZIP – rozbalení archivu</h3>
<p><code>UNZIP "archiv.zip" TO "složka"</code> – existující soubory se přepíšou.</p>
<pre>UNZIP "W:\\Sablony\\cam.zip" TO "{WORK}/cam"</pre>

<a name="mkdir"></a><h3>MKDIR – vytvoření složky</h3>
<pre>MKDIR "P:\\Predani\\{PROJECT}\\{DATE}"</pre>

<a name="delete"></a><h3>DELETE – smazání</h3>
<p>Smaže soubor nebo složku. V <code>{WORK}</code> rovnou, jinde do Koše. Neexistující
cesta se jen zapíše do záznamu.</p>
<pre>DELETE "{WORK}/nc"
DELETE "P:\\Predani\\{PROJECT}\\stare.zip"</pre>

<a name="rename"></a><h3>RENAME – přejmenování</h3>
<p><code>RENAME "cesta" TO "nový název"</code> – ve stejné složce; cíl nesmí existovat.</p>
<pre>RENAME "{WORK}/x.zip" TO "{PROJECT}_{DATE}.zip"</pre>

<a name="replace"></a><h3>REPLACE NAMES – nahrazení v názvech</h3>
<p><code>REPLACE NAMES IN "složka" "co" "čím"</code> – v názvech souborů i složek (i v podsložkách),
bez ohledu na velikost písmen.</p>
<pre>REPLACE NAMES IN "{WORK}/nc" "_v1" "_v2"</pre>

<a name="write"></a><h3>WRITE / APPEND – textový soubor</h3>
<p><code>WRITE "soubor" [ENCODING …] [CRLF / LF]</code>, pak řádky textu a řádek <code>END</code>.
WRITE soubor přepíše, APPEND přidá na konec. Proměnné se dosadí, společné odsazení řádků se
odebere. Kódování: <code>UTF-8</code> (výchozí), <code>UTF-8-BOM</code>,
<code>CP1250</code> / <code>ANSI</code> (pro .bat), <code>CP852</code> / <code>OEM</code>,
<code>ASCII</code>. Konce řádků: <code>CRLF</code> (výchozí, Windows) nebo <code>LF</code>.</p>
<pre>WRITE "{WORK}/spust.bat" ENCODING ANSI
    @echo off
    echo Projekt {PROJECT} odevzdal {USER}
END
APPEND "D:\\log\\odevzdani.txt"
    {DATE} {TIME} {PROJECT} {COMMENT}
END</pre>

<a name="mirror"></a><h3>MIRROR – zrcadlení do jiné záložky</h3>
<p><code>MIRROR co TO "cíl" [BASE / KEEP / FLAT]</code> – cíl je síťová složka jiné záložky
(stejnojmenný projekt), nebo projekt a složka v ní. Zrcadlené soubory se v cíli nedají
aktivovat ani převzít – mění je jen zrcadlo. Podle stavu: nové a změněné se zkopírují
(stará verze do zálohy cíle), přejmenované se přejmenují, smazané se smažou (do koše projektu
v cíli), beze změny se zkontrolují a případně doplní. Ve zdrojovém projektu se soubory
označí „→ cíl“ (sloupec Zrcadlo ve stromu). Nelze v BEFORE.</p>
<p>Když se změněný (nebo smazaný) soubor tentokrát do cíle nezrcadlí – skript ho vynechal
maskou, MIRROR přeskočil, nebo skript skončil chybou – ztratí ve zdroji označení „→ cíl“
a jeho kopie v cíli se označí „← zdroj · neaktuální“ (oranžově). Příznak zmizí při dalším
zrcadlení souboru. V cíli lze neaktuální soubor pravým tlačítkem <i>Uvolnit ze zrcadla</i>
– pak je to běžný soubor cílového projektu.</p>
<pre>MIRROR nc TO "{TAB:Výroba}"                          # stejný projekt, stejné cesty
MIRROR nc TO "{TAB:Výroba}/{PROJECT}/NCFiles" KEEP 1  # do složky NCFiles
MIRROR nc TO "{TAB:Výroba}/CAM_{PROJECT}" FLAT        # do jiného projektu</pre>

<a name="form"></a><h3>FORM – formulář s více údaji</h3>
<p>Jedno okno s více položkami; každá uloží hodnotu do proměnné. Položky:
<code>INPUT proměnná "popisek" [DEFAULT hodnota]</code> (text),
<code>CHOOSE proměnná "popisek" FROM "a", "b" [DEFAULT "b"]</code> nebo <code>FROM množina</code>
(výběr), <code>ASK proměnná "popisek" [DEFAULT 1]</code> (zaškrtnutí – 1 / 0).
Storno ukončí skript s chybou.</p>
<pre>FORM "Předání do výroby – {PROJECT}"
    INPUT  zak   "Číslo zakázky" DEFAULT "{PROJECT}"
    CHOOSE stroj "Stroj" FROM "DMU50", "DMU80" DEFAULT "DMU80"
    CHOOSE prog  "Program" FROM commit "*.nc"
    ASK    mail  "Poslat e-mail" DEFAULT 1
END
LOG "{zak} → {stroj}, program {prog.name}"</pre>

<a name="call"></a><h3>CALL – spuštění jiného skriptu</h3>
<p>Volaný skript pracuje se stejnými soubory a se <b>stejnými proměnnými</b> (co nastaví,
vidí volající a naopak). EXIT ukončí jen volaný skript.</p>
<pre>CALL SERVER "spolecne"    # &lt;síťová složka záložky&gt;\\.spm\\scripts\\spolecne.spm
CALL PROJECT "projekt"    # &lt;projekt na síti&gt;\\.spm\\scripts\\projekt.spm
CALL "pomocny"            # místní skript (složka scripts vedle aplikace)</pre>
<p>Síťové skripty záložky upravíte v okně Skripty (seznam <i>Síťové skripty záložky</i>);
kdo je smí měnit, určují práva ke složce na síti.</p>

<a name="before"></a><h3>BEFORE – před odevzdáním</h3>
<p>Příkazy v bloku <code>BEFORE … END</code> běží <b>před nahráním</b> nad soubory vybranými
k odevzdání. Jen tady smí skript vytvářet a přepisovat soubory v místní kopii projektu
(<code>{LOCAL_DIR}</code>) – vytvořené a změněné soubory se <b>automaticky odevzdají</b> spolu
s ostatními (jedno odevzdání, jeden komentář).</p>
<p><b>Soubory, které nemáte aktivované:</b> skript nesmí potichu přepsat existující soubor
projektu, který nemáte zamčený. Když do něj chce zapsat (WRITE, APPEND, COPY, ZIP, UNZIP),
zeptá se, zda ho aktivovat a přepsat – <i>Ano</i> soubor zamkne, stáhne jeho aktuální verzi
ze serveru a teprve pak do něj zapíše; <i>Ne</i> = chyba a nic se neodevzdá. Drží-li soubor
někdo jiný (nebo ho spravuje zrcadlo), je to chyba vždy. Bez ptaní soubor aktivujete předem
příkazem <a href="#activate">ACTIVATE</a>. Soubor změněný programem (RUN), který nemáte
aktivovaný, se neodevzdá – odevzdání skončí chybou.</p>
<ul>
<li>Mazat a přejmenovávat soubory projektu ani zrcadlit (MIRROR) v BEFORE nelze.</li>
<li>Když BEFORE selže (STOP, Storno, chyba), <b>nic se neodevzdá</b>.</li>
<li>Proměnné z BEFORE platí i ve zbytku skriptu; vytvořené soubory jsou pak v <code>commit</code>.</li>
<li>BEFORE musí být na nejvyšší úrovni skriptu (ne v IF / FOR); při více skriptech proběhnou
nejdřív bloky BEFORE všech skriptů.</li>
<li>EXIT v BEFORE ukončí celý skript (i část po odevzdání).</li>
</ul>
<pre>BEFORE
    FILES h = commit "**/PGMs_*/*.h" STATUS changed
    IF COUNT(h) > 0
        PARSE FIRST(h "*.h").name "{zakazka}_*_{pozice}_*.h"
        ZIP h TO "{LOCAL_DIR}/Export/{zakazka}_{pozice}.zip" KEEP 1
    END
END
MIRROR commit "**/PGMs_*/*.h;**/Export/*.zip" STATUS changed TO "{TAB:VYROBA}"</pre>

<a name="run"></a><h3>RUN – spuštění programu</h3>
<p><code>RUN "program" argumenty… [IN "složka"] [WAIT sekundy | NOWAIT] [-&gt; proměnná]</code></p>
<ul>
<li>Argumenty se oddělují mezerou; každý je text (nebo proměnná). Soubor nebo množina
z <a href="#files">FILES</a> se předá jako úplné cesty souborů.</li>
<li>Program běží v pracovní složce <code>{WORK}</code> (jinou určí <code>IN</code>) a skript
čeká na jeho dokončení – nejvýš 300 s; jiný limit <code>WAIT 600</code>, bez limitu
<code>WAIT 0</code>. <code>NOWAIT</code> program jen spustí a nečeká (např. otevření okna).</li>
<li>S <code>-&gt; proměnná</code> se uloží výsledek: <code>.code</code> (návratový kód, 0 = OK)
a <code>.output</code> (výpis programu) – chybu pak řešíte sami. Bez proměnné skončí skript
s chybou, když program vrátí jiný kód než 0.</li>
<li>Výpis programu (posledních 30 řádků) je v záznamu běhu. Při zkušebním běhu se program
nespustí (výsledek: kód 0, prázdný výpis).</li>
<li>V <a href="#before">BEFORE</a> může program vytvořit soubory v místní kopii projektu – odevzdají se také.</li>
<li>Soubor .py spustíte přes Python: <code>RUN "py" "skript.py" …</code> (nebo úplnou cestou k python.exe).</li>
</ul>
<pre>RUN "C:\\Tools\\postproc.exe" "/stroj" "{stroj}" "{SERVER_DIR}"
FILES nc = commit "*.nc" STATUS changed
RUN "py" "D:\\skripty\\kontrola.py" nc WAIT 120 -&gt; kontrola
IF kontrola.code != 0
    STOP "Kontrola NC selhala: {kontrola.output}"
END
RUN "notepad.exe" "{WORK}/seznam.txt" NOWAIT</pre>

<a name="post"></a><h3>POST – volání API</h3>
<p><code>POST "url" [HEADER "název" "hodnota"]… [TIMEOUT sekundy] [-&gt; proměnná]</code>, na
dalších řádcích tělo požadavku <code>klíč = hodnota</code> a řádek <code>END</code> (i když je
tělo prázdné). Tělo se odešle jako JSON objekt: text jako text, čísla (COUNT…) jako čísla,
soubor jako cesta v projektu, množina jako seznam cest. Klíč s mezerou nebo diakritikou
napište v uvozovkách.</p>
<ul>
<li>S <code>-&gt; proměnná</code> se uloží odpověď: <code>.status</code> (200 = OK),
<code>.body</code> (text odpovědi) a <code>.json("klíč")</code> – hodnota z JSON odpovědi
(<code>"a.b"</code> = vnořená, <code>"polozky.0.id"</code> = první položka seznamu).
Bez proměnné skončí skript s chybou, když server vrátí chybu (stav 400 a víc).</li>
<li>Čeká se nejvýš 30 s (jinak <code>TIMEOUT</code>). Nedostupný server = chyba vždy.</li>
<li>Klíče API patří do tajných hodnot (<a href="#secret">SECRET</a>), ne do skriptu.</li>
<li>Při zkušebním běhu se nic neodešle (odpověď: stav 200, prázdné tělo).</li>
</ul>
<pre>FILES nc = commit "*.nc" STATUS changed
POST "https://cam.firma.cz/api/predani" HEADER "Authorization" "Bearer {SECRET:cam}" -&gt; odpoved
    projekt = PROJECT
    uzivatel = USER
    "počet programů" = COUNT(nc)
    soubory = nc
END
IF odpoved.status != 200
    STOP "API odmítlo předání: {odpoved.body}"
END
LOG "Číslo předání: {odpoved.json("id")}"</pre>

<h2>Funkce</h2>
<p>Funkce vracejí hodnotu – používají se v SET, podmínkách a ve složených závorkách v textu.</p>

<a name="count"></a><h3>COUNT(množina) – počet souborů</h3>
<pre>IF COUNT(commit "*.nc") > 0
    LOG "Odevzdáno {COUNT(commit STATUS changed)} změněných souborů"
END</pre>

<a name="first"></a><h3>FIRST(množina) – první soubor</h3>
<p>První soubor podle abecedy (celé cesty). Prázdná množina = chyba.</p>
<pre>SET prog = FIRST(commit "*.nc")
LOG "{FIRST(nc).name}"</pre>

<a name="last"></a><h3>LAST(množina) – poslední soubor</h3>
<p>Poslední soubor podle abecedy (celé cesty). Prázdná množina = chyba.</p>
<pre>SET posledni = LAST(commit "**/Verze_*/*.pdf")</pre>

<a name="one"></a><h3>ONE(množina) – právě jeden soubor</h3>
<p>Musí odpovídat právě jeden soubor. Žádný = chyba; víc = nabídne výběr (bez okna chyba).</p>
<pre>SET prog = ONE(commit "*hridel*.nc")</pre>

<a name="format"></a><h3>FORMAT(číslo, "maska") – tvar čísla</h3>
<p>Maska je z nul a nepovinného oddělovače (tečka nebo čárka):</p>
<ul>
<li>nuly <b>před</b> oddělovačem = nejmenší počet cifer celé části (chybějící se doplní nulami
zleva; delší číslo se nezkrátí),</li>
<li>nuly <b>za</b> oddělovačem = přesný počet desetinných míst (zaokrouhlí se, 5 nahoru),</li>
<li>oddělovač masky se použije i ve výsledku; bez oddělovače se zaokrouhlí na celé číslo.</li>
</ul>
<table border="1" cellspacing="0" cellpadding="3">
<tr><th>Hodnota</th><th>Maska</th><th>Výsledek</th></tr>
<tr><td>7</td><td><code>"000"</code></td><td>007</td></tr>
<tr><td>3.14159</td><td><code>"0.00"</code></td><td>3.14</td></tr>
<tr><td>3.05</td><td><code>"00,0"</code></td><td>03,1</td></tr>
<tr><td>2.5</td><td><code>"0"</code></td><td>3</td></tr>
<tr><td>1234.5</td><td><code>"00.0"</code></td><td>1234.5</td></tr>
</table>
<pre>LOG "Pozice {FORMAT(pozice, "000")}"
SET soubor = "{PROJECT}_{FORMAT(COUNT(nc), "00")}.zip"
SET cena = 1452.5 FORMAT "0,00"             # stejné jako SET cena = FORMAT(1452.5, "0,00")</pre>

<a name="activate"></a><h3>ACTIVATE – aktivace souboru projektu (příkaz i funkce)</h3>
<p><code>ACTIVATE "soubor"</code> nebo <code>IF ACTIVATE("soubor")</code> – jen v
<a href="#before">BEFORE</a>. Soubor se zadá úplnou cestou v místní kopii
(<code>"{LOCAL_DIR}/test.txt"</code>), na serveru, cestou v projektu (<code>"test.txt"</code>)
nebo jako soubor z množiny.</p>
<table border="1" cellspacing="0" cellpadding="3">
<tr><th>Soubor</th><th>Co se stane</th><th>Funkce vrátí</th></tr>
<tr><td>v projektu není</td><td>nic – skript pokračuje (soubor může vytvořit, odevzdá se jako nový)</td><td>0</td></tr>
<tr><td>existuje, nikdo ho nedrží</td><td>zamkne se, stáhne se jeho aktuální verze ze serveru a přidá
se do množiny <code>commit</code> (odevzdá se a uvolní)</td><td>1</td></tr>
<tr><td>už ho máte aktivovaný</td><td>přidá se do <code>commit</code></td><td>1</td></tr>
<tr><td>drží ho jiný / spravuje ho zrcadlo</td><td>nic – důvod se zapíše do záznamu</td><td>0</td></tr>
</table>
<pre>BEFORE
    ACTIVATE "{LOCAL_DIR}/test.txt"
    FILES txt = commit "test.txt"
    IF COUNT(txt) > 0                       # existoval – připsat
        APPEND "{LOCAL_DIR}/test.txt"
            {DATE} {USER}: {COMMENT}
        END
    ELSE                                     # nový soubor
        WRITE "{LOCAL_DIR}/test.txt"
            {DATE} {USER}: {COMMENT}
        END
    END
    IF NOT ACTIVATE("Export/seznam.csv")
        LOG "seznam.csv se vytvoří nově"
    END
END</pre>

<a name="upper"></a><h3>UPPER(text) – velká písmena</h3>
<pre>SET kod = UPPER(prog.stem)          # hridel_a → HRIDEL_A</pre>

<a name="lower"></a><h3>LOWER(text) – malá písmena</h3>
<pre>SET nazev = LOWER("{PROJECT}.zip")</pre>

<a name="replace_f"></a><h3>REPLACE(text, "co", "čím") – nahrazení v textu</h3>
<p>Nahradí všechny výskyty (bez ohledu na velikost písmen).</p>
<pre>SET nazev = REPLACE(f.name, "_v1", "_v2")     # deska_v1.nc → deska_v2.nc</pre>

<a name="match"></a><h3>MATCH(text, "regulární výraz" [, skupina]) – výřez textu</h3>
<p>Vrátí část textu podle regulárního výrazu – první skupinu v závorkách (nebo zadanou
skupinu), bez skupin celý nalezený text. Nic nenalezeno = prázdný text.</p>
<pre>SET cislo = MATCH(f.name, "^(\\d+)")           # 1234_hridel.nc → 1234
SET verze = MATCH(f.name, "_v(\\d+)\\.", 1)       # deska_v12.nc → 12</pre>

<a name="tab"></a><h3>TAB("název") – síťová složka záložky</h3>
<p>Síťová složka záložky podle jejího názvu, nebo podle názvu síťové složky ze spm_config.json
(ten je stejný u všech uživatelů). V textu zkráceně <code>{TAB:název}</code>.</p>
<pre>MIRROR nc TO "{TAB:Výroba}"
SET vyroba = TAB("Výroba")</pre>

<a name="ask"></a><h3>ASK("otázka") – Ano / Ne</h3>
<p>Zobrazí otázku; Ano = 1, Ne = 0.</p>
<pre>IF ASK("Poslat programy {PROJECT} do výroby?")
    MIRROR nc TO "{TAB:Výroba}"
END</pre>

<a name="input"></a><h3>INPUT("popisek" [, výchozí]) – zadání textu</h3>
<p>Zeptá se na text; druhý údaj je předvyplněná hodnota. Storno ukončí skript s chybou.</p>
<pre>SET zak = INPUT("Číslo zakázky", "{PROJECT}")</pre>

<a name="choose"></a><h3>CHOOSE("popisek", možnosti…) – výběr ze seznamu</h3>
<p>Výběr jedné z hodnot, nebo jednoho souboru z množiny. Storno ukončí skript s chybou.</p>
<pre>SET stroj = CHOOSE("Stroj", "DMU50", "DMU80", "Hermle")
SET prog  = CHOOSE("Který program?", commit "*.nc")</pre>

<a name="secret"></a><h3>SECRET("název") – tajná hodnota</h3>
<p>Hodnota z <i>Nastavení → Tajné hodnoty pro skripty</i> (např. API klíč). Je uložená jen
v nastavení vašeho počítače – každý uživatel má vlastní – a v záznamu běhu ani v chybách se
nikdy nevypíše (nahradí ji •••). V textu zkráceně <code>{SECRET:název}</code>. Když
hodnota v nastavení chybí, skript skončí chybou.</p>
<pre>POST "https://api.firma.cz/nc" HEADER "X-Api-Key" "{SECRET:nc_api}"
END
SET token = SECRET("cam")</pre>

<a name="editor"></a><h3>Editor skriptu</h3>
<table border="1" cellspacing="0" cellpadding="3">
<tr><td>Enter</td><td>nový řádek se stejným odsazením jako předchozí</td></tr>
<tr><td>Tab / Shift+Tab</td><td>u více označených řádků odsadí / vrátí odsazení všech najednou;
Shift+Tab bez výběru vrátí odsazení aktuálního řádku</td></tr>
<tr><td>Ctrl+S</td><td>uložit</td></tr>
<tr><td>Zkontrolovat</td><td>kontrola zápisu – kurzor skočí na chybný řádek</td></tr>
<tr><td>Zkušební běh</td><td>provede skript nad vybraným projektem bez zápisu (dialogy se ukážou)</td></tr>
</table>

<a name="ukazky"></a><h3>Ukázky</h3>
<p><b>Zrcadlení NC programů a archiv do předávací složky</b></p>
<pre>FILES nc = commit "**/PGMs_*/*.h;**/PGMs_*/*.html;**/PGMs_*/*.pdf" STATUS changed
IF COUNT(nc) > 0
    MIRROR nc TO "{TAB:VYROBA}/{PROJECT}/NCFiles"
    PARSE FIRST(nc "*.h").name "{zakazka}_*_{pozice}_*.h"
    ZIP nc TO "D:\\01_GPP2\\03_TEMP\\{zakazka}_{pozice}_ncFiles.zip" KEEP 1
    LOG "Zrcadleno souborů: {COUNT(nc)}"
END</pre>
<p><b>Dotaz při deaktivaci a dávka pro další zpracování</b></p>
<pre>IF ACTION == "deactivate" AND ASK("Vytvořit dávku pro postprocesor?")
    SET stroj = CHOOSE("Stroj", "DMU50", "DMU80")
    WRITE "D:\\Davky\\{PROJECT}_{stroj}.bat" ENCODING ANSI
        @echo off
        postproc.exe /stroj {stroj} /projekt "{SERVER_DIR}"
    END
END</pre>
<p><b>Společná nastavení ze sítě, osobní akce lokálně</b></p>
<pre># síťový skript spolecne.spm (CALL SERVER)
SET predani = "P:\\Predani\\{PROJECT}"

# místní skript
CALL SERVER "spolecne"
COPY commit "*.pdf" STATUS changed TO "{predani}" FLAT</pre>
"""
