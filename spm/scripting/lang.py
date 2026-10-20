"""Jazyk skriptů SPM – rozbor textu skriptu na příkazy (bez provádění).

Jeden příkaz = jeden řádek. Klíčová slova jsou anglicky a nezáleží u nich na velikosti
písmen. Texty jsou v uvozovkách ("" uvnitř = jedna uvozovka) a mohou obsahovat proměnné
ve složených závorkách: "{PROJECT}_{DATE}", "{f.stem}", "{TAB:Vyroba}". Komentář začíná #.

Bloky: IF … [ELSE …] END, FOR … END, WRITE/APPEND … END (text souboru).
Podrobný popis příkazů je v nápovědě (scripting.help).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


class ScriptSyntaxError(Exception):
    def __init__(self, line: int, message: str, script: str = ""):
        self.line, self.message, self.script = line, message, script
        where = f"{script}, řádek {line}" if script else f"Řádek {line}"
        super().__init__(f"{where}: {message}")


# --- tokeny -------------------------------------------------------------------

_TOKEN_RE = re.compile(r"""
    (?P<space>\s+)
  | (?P<comment>\#.*)
  | (?P<num>\d+(?:\.\d+)?)(?![^\W\d])
  | (?P<name>[^\W\d]\w*)
  | (?P<op>->|==|!=|<=|>=|[=<>(),.+\-*/])
""", re.X)


@dataclass
class Token:
    kind: str   # str, num, name, op
    value: str  # u str už bez uvozovek
    col: int

    def is_word(self, *words: str) -> bool:
        return self.kind == "name" and self.value.upper() in words


def _scan_string(text: str, pos: int, line: int) -> tuple[str, int]:
    """Text v uvozovkách od pozice pos (uvozovka). "" = uvozovka; uvnitř {…} uvozovky text
    neukončují (výraz může obsahovat masku: "{COUNT(commit "*.nc")}"). Vrací (obsah, konec)."""
    buf: list[str] = []
    depth = 0
    i = pos + 1
    while i < len(text):
        ch = text[i]
        if depth:
            if ch == "}":
                depth -= 1
        elif ch == '"':
            if text.startswith('""', i):
                buf.append('"')
                i += 2
                continue
            return "".join(buf), i + 1
        elif ch == "{":
            if text.startswith("{{", i):
                buf.append("{{")
                i += 2
                continue
            depth = 1
        buf.append(ch)
        i += 1
    raise ScriptSyntaxError(line, "Neukončený text – chybí koncová uvozovka.")


def tokenize(text: str, line: int) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    while pos < len(text):
        if text[pos] == '"':
            value, end = _scan_string(text, pos, line)
            check_template(value, line)
            tokens.append(Token("str", value, pos))
            pos = end
            continue
        m = _TOKEN_RE.match(text, pos)
        if m is None:
            raise ScriptSyntaxError(line, f"Nečekaný znak „{text[pos]}“.")
        kind = m.lastgroup
        if kind == "comment":
            break
        if kind != "space":
            tokens.append(Token(kind, m.group(), pos))
        pos = m.end()
    return tokens


# --- výrazy -------------------------------------------------------------------

@dataclass
class Str:
    raw: str  # text s {proměnnými}


@dataclass
class Num:
    value: int | float


@dataclass
class BinOp:
    """Počítání: + - * /."""
    op: str
    left: object
    right: object


@dataclass
class Neg:
    expr: object


@dataclass
class Var:
    name: str


@dataclass
class Prop:
    base: object
    name: str
    args: list = field(default_factory=list)


@dataclass
class Call:
    func: str
    args: list


@dataclass
class SetExpr:
    """Výběr souborů: <množina> "maska" [STATUS a,b]."""
    source: str             # název množiny (commit nebo vlastní)
    mask: Str | None = None
    statuses: list[str] | None = None


@dataclass
class Compare:
    left: object
    op: str
    right: object


@dataclass
class Exists:
    expr: object


@dataclass
class Not:
    cond: object


@dataclass
class BoolOp:
    op: str  # AND / OR
    items: list


@dataclass
class Truthy:
    expr: object


FUNCTIONS = {"FIRST", "LAST", "ONE", "COUNT", "UPPER", "LOWER", "REPLACE", "MATCH", "TAB",
             "ASK", "INPUT", "CHOOSE", "SECRET", "FORMAT", "ACTIVATE"}
COMPARE_WORDS = {"CONTAINS", "STARTS", "ENDS", "LIKE"}
STATUSES = ("added", "modified", "renamed", "deleted", "unchanged")
# changed = vše kromě unchanged (i smazané – MIRROR je musí v cíli smazat; COPY / ZIP je přeskočí)
STATUS_ALIASES = {"changed": ("added", "modified", "renamed", "deleted")}

# --- příkazy ------------------------------------------------------------------


@dataclass
class Mapping:
    """Jak se z cesty souboru v projektu stane cesta v cíli (COPY, ZIP, MIRROR)."""
    kind: str = "project"   # project | base | keep
    base: Str | None = None
    keep: int = 0


@dataclass
class Command:
    keyword: str
    line: int


@dataclass
class SetCmd(Command):
    name: str
    expr: object
    format: Str | None = None   # SET x = … FORMAT "000.00"


@dataclass
class FilesCmd(Command):
    name: str
    expr: SetExpr


@dataclass
class ParseCmd(Command):
    expr: object
    pattern: Str


@dataclass
class IfCmd(Command):
    cond: object
    then: list
    otherwise: list


@dataclass
class ForCmd(Command):
    name: str
    expr: object
    body: list


@dataclass
class CopyCmd(Command):
    src: object
    dest: object
    mapping: Mapping
    rename: Str | None = None


@dataclass
class ZipCmd(Command):
    src: object
    dest: object
    mapping: Mapping


@dataclass
class UnzipCmd(Command):
    src: object
    dest: object


@dataclass
class PathCmd(Command):
    """MKDIR, DELETE."""
    path: object


@dataclass
class RenameCmd(Command):
    path: object
    new_name: object


@dataclass
class ReplaceNamesCmd(Command):
    folder: object
    old: object
    new: object


@dataclass
class WriteCmd(Command):
    path: object
    lines: list[str]
    append: bool
    encoding: str
    newline: str


@dataclass
class MirrorCmd(Command):
    src: object
    dest: object
    mapping: Mapping


@dataclass
class MessageCmd(Command):
    """LOG, STOP."""
    expr: object


@dataclass
class ExitCmd(Command):
    pass


@dataclass
class CallCmd(Command):
    """CALL SERVER "x.spm" / CALL PROJECT "x.spm" / CALL "cesta.spm"."""
    where: str   # server / project / local
    path: object


@dataclass
class ActivateCmd(Command):
    """ACTIVATE "soubor" – v BEFORE aktivuje (zamkne) existující soubor projektu."""
    path: object


@dataclass
class BeforeCmd(Command):
    """BEFORE … END: příkazy, které běží před nahráním odevzdání na server."""
    body: list


@dataclass
class FormField:
    kind: str            # INPUT / CHOOSE / ASK
    name: str            # proměnná
    label: object        # popisek (výraz)
    default: object = None
    options: list = field(default_factory=list)  # CHOOSE: hodnoty nebo jedna množina souborů
    line: int = 0


@dataclass
class FormCmd(Command):
    title: object
    fields: list[FormField]


@dataclass
class RunCmd(Command):
    """RUN "program" argumenty… [IN "složka"] [WAIT s / NOWAIT] [-> proměnná]."""
    args: list
    cwd: object = None
    wait: int | None = None     # časový limit v s (0 = bez limitu); None = výchozí
    nowait: bool = False
    result: str | None = None


@dataclass
class PostCmd(Command):
    """POST "url" [HEADER "název" "hodnota"]… [TIMEOUT s] [-> proměnná], tělo klíč = hodnota … END."""
    url: object
    headers: list               # [(název, hodnota)]
    timeout: int | None
    result: str | None
    body: list                  # [(klíč, výraz)] -> JSON objekt


PLANNED: set[str] = set()  # příkazy z dalších etap (rozpoznají se, ale zatím je nelze použít)
RESERVED = {"SET", "FILES", "PARSE", "IF", "ELSE", "END", "FOR", "IN", "COPY", "ZIP", "UNZIP",
            "MKDIR", "DELETE", "RENAME", "REPLACE", "NAMES", "WRITE", "APPEND", "MIRROR", "LOG",
            "STOP", "EXIT", "TO", "BASE", "KEEP", "FLAT", "NAME", "STATUS", "ENCODING", "CRLF",
            "LF", "AND", "OR", "NOT", "EXISTS", "CALL", "FORM", "ASK", "INPUT", "CHOOSE", "FROM",
            "DEFAULT", "BEFORE", "RUN", "POST", "WAIT", "NOWAIT", "HEADER", "TIMEOUT",
            "SECRET", "ACTIVATE"} | COMPARE_WORDS | PLANNED
ENCODINGS = {"UTF-8": "utf-8", "UTF8": "utf-8", "UTF-8-BOM": "utf-8-sig", "UTF8BOM": "utf-8-sig",
             "CP1250": "cp1250", "ANSI": "cp1250", "WINDOWS-1250": "cp1250", "CP852": "cp852",
             "OEM": "cp852", "ASCII": "ascii"}


class _Line:
    """Kurzor nad tokeny jednoho řádku."""

    def __init__(self, tokens: list[Token], line: int):
        self.tokens, self.pos, self.line = tokens, 0, line

    def error(self, message: str) -> ScriptSyntaxError:
        return ScriptSyntaxError(self.line, message)

    def peek(self) -> Token | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def next(self) -> Token:
        tok = self.peek()
        if tok is None:
            raise self.error("Příkaz je neúplný.")
        self.pos += 1
        return tok

    def at_end(self) -> bool:
        return self.pos >= len(self.tokens)

    def end(self) -> None:
        tok = self.peek()
        if tok is not None:
            raise self.error(f"Nečekané „{tok.value}“ na konci příkazu.")

    def word(self, *words: str) -> bool:
        tok = self.peek()
        if tok is not None and tok.is_word(*words):
            self.pos += 1
            return True
        return False

    def expect_word(self, word: str, hint: str = "") -> None:
        if not self.word(word):
            raise self.error(f"Očekává se {word}" + (f" ({hint})." if hint else "."))

    def op(self, value: str) -> bool:
        tok = self.peek()
        if tok is not None and tok.kind == "op" and tok.value == value:
            self.pos += 1
            return True
        return False

    def expect_op(self, value: str) -> None:
        if not self.op(value):
            raise self.error(f"Očekává se „{value}“.")

    def name(self, what: str = "název proměnné") -> str:
        tok = self.next()
        if tok.kind != "name":
            raise self.error(f"Očekává se {what}.")
        if tok.value.upper() in RESERVED:
            raise self.error(f"„{tok.value}“ je klíčové slovo – nelze ho použít jako {what}.")
        return tok.value

    def string(self, what: str = "text v uvozovkách") -> Str:
        tok = self.next()
        if tok.kind != "str":
            raise self.error(f"Očekává se {what}.")
        return Str(tok.value)

    def number(self, what: str = "číslo") -> int:
        tok = self.next()
        if tok.kind != "num":
            raise self.error(f"Očekává se {what}.")
        return int(tok.value)

    # --- výrazy ---

    def expr(self):
        """Výraz s počítáním: + a - , * a / (mají přednost), závorky, záporné číslo."""
        node = self.term()
        while True:
            tok = self.peek()
            if tok is None or tok.kind != "op" or tok.value not in "+-" or tok.value == "->":
                return node
            self.pos += 1
            node = BinOp(tok.value, node, self.term())

    def term(self):
        node = self.factor()
        while True:
            tok = self.peek()
            if tok is None or tok.kind != "op" or tok.value not in ("*", "/"):
                return node
            self.pos += 1
            node = BinOp(tok.value, node, self.factor())

    def factor(self):
        if self.op("-"):
            return Neg(self.factor())
        if self.op("+"):
            return self.factor()
        if self.op("("):
            node = self.expr()
            self.expect_op(")")
            return node
        return self.primary()

    def primary(self):
        tok = self.next()
        if tok.kind == "str":
            return Str(tok.value)
        if tok.kind == "num":
            return Num(float(tok.value) if "." in tok.value else int(tok.value))
        if tok.kind != "name":
            raise self.error(f"Očekává se hodnota, ne „{tok.value}“.")
        upper = tok.value.upper()
        if upper in FUNCTIONS and self.op("("):
            node = Call(upper, self.args())
        elif upper in RESERVED:
            raise self.error(f"Očekává se hodnota, ne klíčové slovo „{tok.value}“.")
        else:
            node = Var(tok.value)
        while self.op("."):
            prop = self.next()
            if prop.kind != "name":
                raise self.error("Za tečkou se očekává název vlastnosti (např. f.name).")
            args = self.args() if self.op("(") else []
            node = Prop(node, prop.value.lower(), args)
        return node

    def args(self) -> list:
        """Argumenty funkce; mohou to být i množiny s maskou: COUNT(commit "*.nc")."""
        args = []
        if self.op(")"):
            return args
        while True:
            args.append(self.set_expr())
            if self.op(")"):
                return args
            self.expect_op(",")

    def set_tail(self, source: str) -> SetExpr:
        mask = None
        tok = self.peek()
        if tok is not None and tok.kind == "str":
            mask = Str(self.next().value)
        statuses = None
        if self.word("STATUS"):
            statuses = []
            while True:
                word = self.name_any("stav souboru")
                values = STATUS_ALIASES.get(word.lower(), (word.lower(),))
                for v in values:
                    if v not in STATUSES:
                        raise self.error(f"Neznámý stav „{word}“ (možné: {', '.join(STATUSES)}, "
                                         "changed).")
                    statuses.append(v)
                if not self.op(","):
                    break
        return SetExpr(source, mask, statuses)

    def name_any(self, what: str) -> str:
        tok = self.next()
        if tok.kind != "name":
            raise self.error(f"Očekává se {what}.")
        return tok.value

    def set_expr(self) -> object:
        """Zdroj souborů: množina s maskou (commit "*.nc" STATUS added), proměnná se souborem /
        množinou, nebo text cesty."""
        tok = self.peek()
        if tok is not None and tok.kind == "name" and tok.value.upper() not in RESERVED \
                and self.pos + 1 < len(self.tokens):
            nxt = self.tokens[self.pos + 1]
            if nxt.kind == "str" or nxt.is_word("STATUS"):
                self.pos += 1
                return self.set_tail(tok.value)
        return self.expr()

    def condition(self):
        items = [self.and_cond()]
        while self.word("OR"):
            items.append(self.and_cond())
        return items[0] if len(items) == 1 else BoolOp("OR", items)

    def and_cond(self):
        items = [self.not_cond()]
        while self.word("AND"):
            items.append(self.not_cond())
        return items[0] if len(items) == 1 else BoolOp("AND", items)

    def not_cond(self):
        if self.word("NOT"):
            return Not(self.not_cond())
        if self.word("EXISTS"):
            return Exists(self.expr())
        left = self.expr()
        tok = self.peek()
        if tok is None or tok.is_word("AND", "OR"):
            return Truthy(left)
        if tok.kind == "op" and tok.value in ("==", "!=", "<", ">", "<=", ">="):
            self.pos += 1
            return Compare(left, tok.value, self.expr())
        if tok.kind == "op" and tok.value == "=":
            raise self.error("Pro porovnání použijte == (jedno = je přiřazení v SET).")
        if tok.kind == "name" and tok.value.upper() in COMPARE_WORDS:
            self.pos += 1
            return Compare(left, tok.value.upper(), self.expr())
        raise self.error(f"Nečekané „{tok.value}“ v podmínce.")

    def mapping(self, allow_name: bool = False) -> tuple[Mapping, Str | None]:
        mapping, rename = Mapping(), None
        seen = False
        while not self.at_end():
            if self.word("BASE"):
                if seen:
                    raise self.error("Lze použít jen jedno z BASE / KEEP / FLAT.")
                seen = True
                if self.word("PROJECT"):
                    mapping = Mapping("project")
                else:
                    mapping = Mapping("base", base=self.string("složka v uvozovkách nebo PROJECT"))
            elif self.word("KEEP"):
                if seen:
                    raise self.error("Lze použít jen jedno z BASE / KEEP / FLAT.")
                seen = True
                mapping = Mapping("keep", keep=self.number("počet složek (např. KEEP 2)"))
            elif self.word("FLAT"):
                if seen:
                    raise self.error("Lze použít jen jedno z BASE / KEEP / FLAT.")
                seen = True
                mapping = Mapping("keep", keep=0)
            elif allow_name and self.word("NAME"):
                rename = self.string("vzor názvu, např. \"{file.stem}_{DATE}{file.ext}\"")
            else:
                raise self.error(f"Nečekané „{self.peek().value}“ (možné: BASE, KEEP, FLAT"
                                 + (", NAME" if allow_name else "") + ").")
        return mapping, rename


# --- rozbor celého skriptu ------------------------------------------------------

@dataclass
class Script:
    name: str
    body: list

    @property
    def before(self) -> list:
        """Příkazy bloků BEFORE (běží před nahráním)."""
        return [c for b in self.body if isinstance(b, BeforeCmd) for c in b.body]

    @property
    def after(self) -> list:
        """Ostatní příkazy (běží po nahrání)."""
        return [c for c in self.body if not isinstance(c, BeforeCmd)]


def parse_script(text: str, name: str = "") -> Script:
    try:
        lines = text.splitlines()
        body, i, end = _parse_block(lines, 0, top=True)
        return Script(name, body)
    except ScriptSyntaxError as e:
        raise ScriptSyntaxError(e.line, e.message, name) from None


def _parse_block(lines: list[str], i: int, top: bool = False,
                 closers: tuple[str, ...] = ("END",), in_before: bool = False) -> tuple[list, int, str]:
    """Vrátí (příkazy, index řádku za blokem, slovo, které blok ukončilo)."""
    body: list = []
    while i < len(lines):
        number = i + 1
        tokens = tokenize(lines[i], number)
        i += 1
        if not tokens:
            continue
        first = tokens[0]
        if first.kind != "name":
            raise ScriptSyntaxError(number, "Řádek musí začínat příkazem (např. SET, FILES, COPY).")
        keyword = first.value.upper()
        cur = _Line(tokens, number)
        cur.pos = 1
        if keyword in ("END", "ELSE"):
            if top or keyword not in closers:
                raise ScriptSyntaxError(number, f"{keyword} bez odpovídajícího "
                                        + ("IF." if keyword == "ELSE" else "IF / FOR / WRITE / FORM / BEFORE."))
            cur.end()
            return body, i, keyword
        if keyword == "IF":
            cond = cur.condition()
            cur.end()
            then, i, closer = _parse_block(lines, i, closers=("END", "ELSE"), in_before=in_before)
            if closer == "":
                raise ScriptSyntaxError(number, "IF bez END.")
            otherwise: list = []
            if closer == "ELSE":
                otherwise, i, closer = _parse_block(lines, i, closers=("END",), in_before=in_before)
                if closer == "":
                    raise ScriptSyntaxError(number, "IF bez END.")
            body.append(IfCmd(keyword, number, cond, then, otherwise))
            continue
        if keyword == "FOR":
            var = cur.name("název proměnné cyklu")
            cur.expect_word("IN", "FOR f IN množina")
            expr = cur.set_expr()
            cur.end()
            inner, i, closer = _parse_block(lines, i, closers=("END",), in_before=in_before)
            if closer == "":
                raise ScriptSyntaxError(number, "FOR bez END.")
            body.append(ForCmd(keyword, number, var, expr, inner))
            continue
        if keyword in ("WRITE", "APPEND"):
            path = cur.expr()
            encoding, newline = "utf-8", "\r\n"
            while not cur.at_end():
                if cur.word("ENCODING"):
                    tok = cur.next()
                    enc = ENCODINGS.get(tok.value.upper())
                    if enc is None:
                        raise cur.error(f"Neznámé kódování „{tok.value}“ (možné: UTF-8, UTF-8-BOM, "
                                        "CP1250 / ANSI, CP852 / OEM, ASCII).")
                    encoding = enc
                elif cur.word("CRLF"):
                    newline = "\r\n"
                elif cur.word("LF"):
                    newline = "\n"
                else:
                    raise cur.error(f"Nečekané „{cur.peek().value}“ (možné: ENCODING, CRLF, LF).")
            text_lines = []
            while i < len(lines) and lines[i].strip().upper() != "END":
                check_template(lines[i], i + 1)
                text_lines.append(lines[i])
                i += 1
            if i >= len(lines):
                raise ScriptSyntaxError(number, f"{keyword} bez END (text souboru končí řádkem END).")
            i += 1
            body.append(WriteCmd(keyword, number, path, _dedent(text_lines), keyword == "APPEND",
                                 encoding, newline))
            continue
        if keyword == "BEFORE":
            if not top:
                raise ScriptSyntaxError(number, "BEFORE může být jen na nejvyšší úrovni skriptu "
                                        "(ne uvnitř IF, FOR ani jiného BEFORE).")
            cur.end()
            inner, i, closer = _parse_block(lines, i, closers=("END",), in_before=True)
            if closer == "":
                raise ScriptSyntaxError(number, "BEFORE bez END.")
            body.append(BeforeCmd(keyword, number, inner))
            continue
        if keyword == "MIRROR" and in_before:
            raise ScriptSyntaxError(number, "MIRROR nelze použít v BEFORE – data ještě nejsou na "
                                    "serveru. Napište ho za blok BEFORE.")
        if keyword == "POST":
            body.append(_parse_post(cur, lines, i, number))
            i = body[-1].end_line
            continue
        if keyword == "FORM":
            title = cur.expr()
            cur.end()
            fields, i = _parse_form(lines, i, number)
            body.append(FormCmd(keyword, number, title, fields))
            continue
        body.append(_parse_simple(keyword, cur, number))
    return body, i, ""


def _result_name(cur: _Line) -> str | None:
    """Volitelné „-> proměnná“ na konci příkazu."""
    if cur.op("->"):
        return cur.name("název proměnné pro výsledek")
    return None


def _parse_post(cur: _Line, lines: list[str], i: int, number: int) -> PostCmd:
    url = cur.expr()
    headers, timeout, result = [], None, None
    while not cur.at_end():
        if cur.word("HEADER"):
            headers.append((cur.expr(), cur.expr()))
        elif cur.word("TIMEOUT"):
            timeout = cur.number("počet sekund")
        elif cur.op("->"):
            result = cur.name("název proměnné pro výsledek")
        else:
            raise cur.error(f"Nečekané „{cur.peek().value}“ (možné: HEADER, TIMEOUT, -> proměnná).")
    body = []
    while i < len(lines):
        line_no = i + 1
        tokens = tokenize(lines[i], line_no)
        i += 1
        if not tokens:
            continue
        item = _Line(tokens, line_no)
        if tokens[0].is_word("END") and len(tokens) == 1:
            cmd = PostCmd("POST", number, url, headers, timeout, result, body)
            cmd.end_line = i
            return cmd
        key_tok = item.next()
        if key_tok.kind not in ("name", "str"):
            raise ScriptSyntaxError(line_no, "Tělo POST: řádky ve tvaru klíč = hodnota (např. "
                                    "projekt = \"{PROJECT}\"), na konci END.")
        item.expect_op("=")
        body.append((key_tok.value, item.set_expr()))
        item.end()
    raise ScriptSyntaxError(number, "POST bez END (tělo požadavku končí řádkem END; i prázdné).")


def _parse_form(lines: list[str], i: int, number: int) -> tuple[list[FormField], int]:
    """Řádky formuláře až po END: INPUT / CHOOSE / ASK proměnná "popisek" …"""
    fields: list[FormField] = []
    while i < len(lines):
        line_no = i + 1
        tokens = tokenize(lines[i], line_no)
        i += 1
        if not tokens:
            continue
        cur = _Line(tokens, line_no)
        kind = cur.next().value.upper()
        if kind == "END":
            cur.end()
            if not fields:
                raise ScriptSyntaxError(number, "FORM bez položek (INPUT / CHOOSE / ASK).")
            return fields, i
        if kind not in ("INPUT", "CHOOSE", "ASK"):
            raise ScriptSyntaxError(line_no, f"Ve FORM může být jen INPUT, CHOOSE nebo ASK (ne „{kind}“).")
        f = FormField(kind, cur.name(), cur.expr(), line=line_no)
        if kind == "CHOOSE":
            cur.expect_word("FROM", "CHOOSE proměnná \"popisek\" FROM \"a\", \"b\" nebo FROM množina")
            f.options.append(cur.set_expr())
            while cur.op(","):
                f.options.append(cur.expr())
        if cur.word("DEFAULT"):
            f.default = cur.expr()
        cur.end()
        if any(x.name.lower() == f.name.lower() for x in fields):
            raise ScriptSyntaxError(line_no, f"Proměnná {f.name} je ve FORM dvakrát.")
        fields.append(f)
    raise ScriptSyntaxError(number, "FORM bez END.")


def _dedent(lines: list[str]) -> list[str]:
    indents = [len(l) - len(l.lstrip()) for l in lines if l.strip()]
    cut = min(indents, default=0)
    return [l[cut:] if l.strip() else "" for l in lines]


def _parse_simple(keyword: str, cur: _Line, number: int):
    if keyword == "SET":
        name = cur.name()
        cur.expect_op("=")
        cmd = SetCmd(keyword, number, name, cur.expr())
        if cur.word("FORMAT"):
            cmd.format = cur.string("maska formátu, např. \"000.00\"")
    elif keyword == "FILES":
        name = cur.name("název množiny")
        cur.expect_op("=")
        tok = cur.next()
        if tok.kind != "name":
            raise cur.error("Očekává se zdroj souborů – commit nebo název jiné množiny.")
        cmd = FilesCmd(keyword, number, name, cur.set_tail(tok.value))
    elif keyword == "PARSE":
        expr = cur.expr()
        cmd = ParseCmd(keyword, number, expr, cur.string("vzor, např. \"{zakazka}_{dil}.nc\""))
    elif keyword in ("COPY", "ZIP", "MIRROR"):
        src = cur.set_expr()
        cur.expect_word("TO", f"{keyword} co TO kam")
        dest = cur.expr()
        mapping, rename = cur.mapping(allow_name=keyword == "COPY")
        if keyword == "COPY":
            cmd = CopyCmd(keyword, number, src, dest, mapping, rename)
        elif keyword == "ZIP":
            cmd = ZipCmd(keyword, number, src, dest, mapping)
        else:
            cmd = MirrorCmd(keyword, number, src, dest, mapping)
    elif keyword == "UNZIP":
        src = cur.expr()
        cur.expect_word("TO", "UNZIP archiv TO složka")
        cmd = UnzipCmd(keyword, number, src, cur.expr())
    elif keyword in ("MKDIR", "DELETE"):
        cmd = PathCmd(keyword, number, cur.expr())
    elif keyword == "RENAME":
        path = cur.expr()
        cur.expect_word("TO", "RENAME soubor TO nový_název")
        cmd = RenameCmd(keyword, number, path, cur.expr())
    elif keyword == "REPLACE":
        cur.expect_word("NAMES", "REPLACE NAMES IN složka \"co\" \"čím\"")
        cur.expect_word("IN", "REPLACE NAMES IN složka \"co\" \"čím\"")
        folder = cur.expr()
        old = cur.expr()
        cmd = ReplaceNamesCmd(keyword, number, folder, old, cur.expr())
    elif keyword in ("LOG", "STOP"):
        cmd = MessageCmd(keyword, number, cur.expr())
    elif keyword == "EXIT":
        cmd = ExitCmd(keyword, number)
    elif keyword == "ACTIVATE":
        cmd = ActivateCmd(keyword, number, cur.expr())
    elif keyword == "CALL":
        where = "server" if cur.word("SERVER") else "project" if cur.word("PROJECT") else "local"
        cmd = CallCmd(keyword, number, where, cur.expr())
    elif keyword in ("ASK", "INPUT", "CHOOSE"):
        raise cur.error(f"{keyword} je funkce – např. IF ASK(\"Pokračovat?\") nebo "
                        f"SET x = {keyword}(\"popisek\", …); více údajů najednou zadá FORM.")
    elif keyword == "RUN":
        # argumenty oddělené mezerou – množinu jen jako proměnnou (FILES), maska by byla nejednoznačná
        args = [cur.expr()]
        while not cur.at_end() and not cur.peek().is_word("IN", "WAIT", "NOWAIT") \
                and not (cur.peek().kind == "op" and cur.peek().value == "->"):
            args.append(cur.expr())
        cmd = RunCmd(keyword, number, args)
        while not cur.at_end():
            if cur.word("IN"):
                cmd.cwd = cur.expr()
            elif cur.word("WAIT"):
                cmd.wait = cur.number("počet sekund (0 = bez limitu)")
            elif cur.word("NOWAIT"):
                cmd.nowait = True
            elif cur.op("->"):
                cmd.result = cur.name("název proměnné pro výsledek")
            else:
                raise cur.error(f"Nečekané „{cur.peek().value}“ (možné: IN, WAIT, NOWAIT, -> proměnná).")
        if cmd.nowait and (cmd.wait is not None or cmd.result):
            raise cur.error("NOWAIT nelze s WAIT ani s -> proměnná (na výsledek se nečeká).")
    elif keyword in PLANNED:
        raise cur.error(f"Příkaz {keyword} bude dostupný v další verzi aplikace.")
    else:
        raise cur.error(f"Neznámý příkaz „{cur.tokens[0].value}“.")
    cur.end()
    return cmd


# --- interpolace textu ------------------------------------------------------------

def split_template(raw: str, line: int = 0) -> list[tuple[str, str]]:
    """Rozdělí text na části („text“, …) a („expr“, zdroj výrazu). {{ a }} = složená závorka."""
    parts: list[tuple[str, str]] = []
    buf = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == "{" and raw.startswith("{{", i):
            buf.append("{")
            i += 2
        elif ch == "}" and raw.startswith("}}", i):
            buf.append("}")
            i += 2
        elif ch == "{":
            end = raw.find("}", i)
            if end < 0:
                raise ScriptSyntaxError(line, f"V textu „{raw}“ chybí uzavírací }}.")
            if buf:
                parts.append(("text", "".join(buf)))
                buf = []
            parts.append(("expr", raw[i + 1:end].strip()))
            i = end + 1
        elif ch == "}":
            raise ScriptSyntaxError(line, f"V textu „{raw}“ je }} bez {{ (pro znak }} napište }}}}).")
        else:
            buf.append(ch)
            i += 1
    if buf:
        parts.append(("text", "".join(buf)))
    return parts


def parse_inline(source: str, line: int):
    """Výraz uvnitř {…}; zvláštní tvary TAB:název a SECRET:název."""
    m = re.fullmatch(r"(?i)(TAB|SECRET)\s*:\s*(.+)", source)
    if m:
        return Call(m.group(1).upper(), [Str(m.group(2).strip())])
    if not source:
        raise ScriptSyntaxError(line, "Prázdné {} v textu.")
    cur = _Line(tokenize(source, line), line)
    expr = cur.expr()
    cur.end()
    return expr


def check_template(raw: str, line: int) -> None:
    """Kontrola {výrazů} v textu už při rozboru skriptu (chyba se ukáže při kontrole)."""
    for kind, src in split_template(raw, line):
        if kind == "expr":
            parse_inline(src, line)
