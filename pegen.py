#!/usr/bin/env python3.8

"""pegen -- PEG Generator.

Search the web for PEG Parsers for reference.
"""

from __future__ import annotations  # Requires Python 3.7 or later

import argparse
import contextlib
import os
import sys
import time
import token
import tokenize
import traceback
from typing import *

T = TypeVar('T')

exact_token_types = token.EXACT_TOKEN_TYPES  # type: ignore

Mark = int  # NewType('Mark', int)


def shorttok(tok: tokenizer.TokenInfo) -> str:
    return "%-25.25s" % f"{tok.start[0]}.{tok.start[1]}: {token.tok_name[tok.type]}:{tok.string!r}"


class Tokenizer:
    """Caching wrapper for the tokenize module.

    This is pretty tied to Python's syntax.
    """

    _tokens: List[tokenize.TokenInfo]

    def __init__(self, tokengen: Iterable[TokenInfo], *, verbose=False):
        self._tokengen = tokengen
        self._tokens = []
        self._index = 0
        self._verbose = verbose
        if verbose:
            self.report(False, False)

    def getnext(self) -> tokenize.TokenInfo:
        """Return the next token and updates the index."""
        cached = True
        while self._index == len(self._tokens):
            tok = next(self._tokengen)
            if tok.type in (token.NL, token.COMMENT):
                continue
            self._tokens.append(tok)
            cached = False
        tok = self._tokens[self._index]
        self._index += 1
        if self._verbose:
            self.report(cached, False)
        return tok

    def peek(self) -> tokenize.TokenInfo:
        """Return the next token *without* updating the index."""
        while self._index == len(self._tokens):
            tok = next(self._tokengen)
            if tok.type in (token.NL, token.COMMENT):
                continue
            self._tokens.append(tok)
        return self._tokens[self._index]

    def diagnose(self) -> tokenize.TokenInfo:
        if not self._tokens:
            self.getnext()
        return self._tokens[-1]

    def mark(self) -> Mark:
        return self._index

    def reset(self, index: Mark) -> None:
        if index == self._index:
            return
        assert 0 <= index <= len(self._tokens), (index, len(self._tokens))
        old_index = self._index
        self._index = index
        if self._verbose:
            self.report(True, index < old_index)

    def report(self, cached, back):
        if back:
            fill = '-'*self._index + '-'
        elif cached:
            fill = '-'*self._index + '>'
        else:
            fill = '-'*self._index + '*'
        if self._index == 0:
            print(f"{fill} (Bof)")
        else:
            tok = self._tokens[self._index - 1]
            print(f"{fill} {shorttok(tok)}")


def memoize(method: Callable[[Parser], T]):
    """Memoize a symbol method."""
    method_name = method.__name__

    def symbol_wrapper(self: Parser) -> T:
        mark = self.mark()
        key = mark, method_name
        # Fast path: cache hit, and not verbose.
        if key in self._symbol_cache and not self._verbose:
            tree, endmark = self._symbol_cache[key]
            if tree:
                self.reset(endmark)
            else:
                assert mark == endmark
            return tree
        # Slow path: no cache hit, or verbose.
        verbose = self._verbose
        fill = '  ' * self._level
        if key not in self._symbol_cache:
            if verbose:
                print(f"{fill}{method_name} ... (looking at {self.showpeek()})")
            self._level += 1
            tree = method(self)
            self._level -= 1
            if verbose:
                print(f"{fill}... {method_name} -> {tree!s:.100}")
            if tree:
                endmark = self.mark()
            else:
                endmark = mark
                self.reset(endmark)
            self._symbol_cache[key] = tree, endmark
        else:
            tree, endmark = self._symbol_cache[key]
            if verbose:
                print(f"{fill}{method_name} -> {tree!s:.100}")
            if tree:
                self.reset(endmark)
        return tree

    symbol_wrapper.__wrapped__ = method
    return symbol_wrapper


def memoize_left_rec(method: Callable[[Parser], T]):
    """Memoize a left-recursive symbol method."""
    method_name = method.__name__

    def symbol_wrapper(self: Parser) -> T:
        mark = self.mark()
        key = mark, method_name
        # Fast path: cache hit, and not verbose.
        if key in self._symbol_cache and not self._verbose:
            tree, endmark = self._symbol_cache[key]
            if tree:
                self.reset(endmark)
            else:
                assert mark == endmark
            return tree
        # Slow path: no cache hit, or verbose.
        verbose = self._verbose
        fill = '  ' * self._level
        if key not in self._symbol_cache:
            if verbose:
                print(f"{fill}{method_name} ... (looking at {self.showpeek()})")
            self._level += 1

            # For left-recursive rules we manipulate the cache and
            # loop until the rule shows no progress, then pick the
            # previous result.  For an explanation why this works, see
            # https://github.com/PhilippeSigaud/Pegged/wiki/Left-Recursion
            # (But we use the memoization cache instead of a static
            # variable.)
            #
            # TODO: Fix this for mutually-left-recursive rules!
            # E.g.
            #   start: foo '+' bar
            #   foo: start
            #   bar: NUMBER
            # (We don't clear the cache for foo.)

            # Prime the cache with a failure.
            self._symbol_cache[key] = None, mark
            lastresult, lastmark = None, mark
            depth = 0
            if verbose:
                print(f"{fill}Recursive {method_name} at {mark} depth {depth}")

            while True:
                self.reset(mark)
                result = method(self)
                endmark = self.mark()
                depth += 1
                if verbose:
                    print(f"{fill}Recursive {method_name} at {mark} depth {depth}: {result!s:.100} to {endmark}")
                if not result:
                    if verbose:
                        print(f"{fill}Fail with {lastresult!s:.100} to {lastmark}")
                    break
                if endmark <= lastmark:
                    if verbose:
                        print(f"{fill}Bailing with {lastresult!s:.100} to {lastmark}")
                    break
                self._symbol_cache[key] = lastresult, lastmark = result, endmark

            self.reset(lastmark)
            tree = lastresult

            self._level -= 1
            if verbose:
                print(f"{fill}{method_name} -> {tree!s:.100}")
            if tree:
                endmark = self.mark()
            else:
                endmark = mark
                self.reset(endmark)
            self._symbol_cache[key] = tree, endmark
        else:
            tree, endmark = self._symbol_cache[key]
            if verbose:
                print(f"{fill}{method_name} -> {tree!s:.100}")
            if tree:
                self.reset(endmark)
        return tree

    symbol_wrapper.__wrapped__ = method
    return symbol_wrapper


def memoize_expect(method: Callable[[Parser], Optional[tokenize.TokenInfo]]) -> bool:
    """Memoize the expect() method."""

    def expect_wrapper(self: Parser, type: str) -> Optional[tokenize.TokenInfo]:
        mark = self.mark()
        key = mark, type
        # Fast path: cache hit.
        if key in self._token_cache:
            res, endmark = self._token_cache[key]
            if res:
                self.reset(endmark)
                # Uncomment these when parsing Python, to save
                # up to 80% of memory (though little time!).
                # self._token_cache.clear()
                # self._symbol_cache.clear()
            return res
        # Slow path.
        if key not in self._token_cache:
            res = method(self, type)
            if res:
                endmark = self.mark()
            else:
                endmark = mark
            self._token_cache[key] = res, endmark
        else:
            res, endmark = self._token_cache[key]
        self.reset(endmark)
        return res

    expect_wrapper.__wrapped__ = method
    return expect_wrapper


class Parser(Generic[T]):
    """Parsing base class."""

    def __init__(self, tokenizer: Tokenizer, *, verbose=False):
        self._tokenizer = tokenizer
        self._verbose = verbose
        self._level = 0
        self._symbol_cache: Dict[Tuple[Mark,
                                       Callable[[Parser], Optional[T]]],
                                 Tuple[Optional[T], Mark]] = {}
        self._token_cache: Dict[Tuple[Mark, str], bool] = {}
        # Pass through common tokeniser methods.
        # TODO: Rename to _mark and _reset.
        self.mark = self._tokenizer.mark
        self.reset = self._tokenizer.reset

    def showpeek(self):
        tok = self._tokenizer.peek()
        return f"{tok.start[0]}.{tok.start[1]}: {token.tok_name[tok.type]}:{tok.string!r}"

    @memoize
    def name(self) -> Optional[tokenize.TokenInfo]:
        tok = self._tokenizer.peek()
        if tok.type == token.NAME:
            return self._tokenizer.getnext()
        return None

    @memoize
    def number(self) -> Optional[tokenize.TokenInfo]:
        tok = self._tokenizer.peek()
        if tok.type == token.NUMBER:
            return self._tokenizer.getnext()
        return None

    @memoize
    def string(self) -> Optional[tokenize.TokenInfo]:
        tok = self._tokenizer.peek()
        if tok.type == token.STRING:
            return self._tokenizer.getnext()
        return None

    @memoize
    def curly_stuff(self) -> Optional[tokenize.TokenInfo]:
        tok = self._tokenizer.peek()
        if tok.type == CURLY_STUFF:
            return self._tokenizer.getnext()
        return None

    @memoize_expect
    def expect(self, type: str) -> Optional[tokenize.TokenInfo]:
        tok = self._tokenizer.peek()
        if tok.string == type:
            return self._tokenizer.getnext()
        if type in exact_token_types:
            if tok.type == exact_token_types[type]:
                return self._tokenizer.getnext()
        if type in token.__dict__:
            if tok.type == token.__dict__[type]:
                return self._tokenizer.getnext()
        if tok.type == token.OP and tok.string == type:
            return self._tokenizer.getnext()
        return None

    def make_syntax_error(self, filename="<unknown>") -> NoReturn:
        tok = self._tokenizer.diagnose()
        return SyntaxError("pegen parse failure", (filename, tok.start[0], 1 + tok.start[1], tok.line))


class Rule:
    def __init__(self, name: str, alts: Alts):
        self.name = name
        self.alts = alts

    def __str__(self):
        return f"{self.name}: {self.alts}"

    def __repr__(self):
        return f"Rule({self.name!r}, {self.alts!r})"

    def __hash__(self):
        return hash(str(self))

    def __eq__(self, other):
        if not isinstance(other, Rule):
            return NotImplemented
        return repr(self) == repr(other)

    def gen_func(self, gen: ParserGenerator, rulename: str):
        is_loop = rulename.startswith('_loop_')
        # If it's a single parenthesized group, flatten it.
        alts = self.alts
        if (not is_loop
            and len(alts.alts) == 1
            and len(alts.alts[0].items) == 1
            and isinstance(alts.alts[0].items[0].item, Group)):
            alts = alts.alts[0].items[0].item.alts
        if alts.is_recursive(rulename):
            gen.print("@memoize_left_rec")
        else:
            gen.print("@memoize")
        gen.print(f"def {rulename}(self):")
        with gen.indent():
            gen.print(f"# {rulename}: {alts}")
            gen.print("mark = self.mark()")
            if is_loop:
                gen.print("children = []")
            alts.gen_body(gen, is_loop)
            if is_loop:
                gen.print("return children")
            else:
                gen.print("return None")


class Leaf:
    def __init__(self, name: str):
        self.value = name

    def __str__(self):
        return self.value


class NameLeaf(Leaf):
    def __repr__(self):
        return f"NameLeaf({self.value!r})"

    def make_call(self, gen: ParserGenerator) -> Tuple[str, str]:
        name = self.value
        if name in ('NAME', 'NUMBER', 'STRING', 'CURLY_STUFF'):
            name = name.lower()
            return name, f"self.{name}()"
        if name in ('NEWLINE', 'DEDENT', 'INDENT', 'ENDMARKER'):
            return name.lower(), f"self.expect({name!r})"
        return name, f"self.{name}()"

    def is_recursive(self, rulename: str) -> bool:
        return self.value == rulename


class StringLeaf(Leaf):
    def __repr__(self):
        return f"StringLeaf({self.value!r})"

    def make_call(self, gen: ParserGenerator) -> Tuple[str, str]:
        return 'string', f"self.expect({self.value})"

    def is_recursive(self, rulename: str) -> bool:
        return False


class Alts:
    def __init__(self, alts: List[Alt]):
        self.alts = alts

    def __str__(self):
        return " | ".join(str(alt) for alt in self.alts)

    def __repr__(self):
        return f"Alts({self.alts!r})"

    def gen_body(self, gen: ParserGenerator, is_loop: bool = False):
        if is_loop:
            assert len(self.alts) == 1
        for alt in self.alts:
            alt.gen_block(gen, is_loop)

    def make_call(self, gen: ParserGenerator) -> Tuple[str, str]:
        if len(self.alts) == 1 and len(self.alts[0].items) == 1:
            return self.alts[0].items[0].make_call(gen)
        name = gen.name_node(self)
        return name, f"self.{name}()"

    def is_recursive(self, rulename: str) -> bool:
        for alt in self.alts:
            if alt.is_recursive(rulename):
                return True
        return False


class Alt:
    def __init__(self, items: List[NamedItem], action: Optional[str] = None):
        self.items = items
        self.action = action

    def __str__(self):
        core = " ".join(str(item) for item in self.items)
        if self.action:
            return f"{core} {self.action}"
        else:
            return core

    def __repr__(self):
        if self.action:
            return f"Alt({self.items!r}, {self.action!r}"
        else:
            return f"Alt({self.items!r})"

    def gen_block(self, gen: ParserGenerator, is_loop: bool = False):
        names = []
        if is_loop:
            gen.print("while (")
        else:
            gen.print("if (")
        with gen.indent():
            first = True
            for item in self.items:
                if first:
                    first = False
                else:
                    gen.print("and")
                item.gen_item(gen, names)
        gen.print("):")
        with gen.indent():
            action = self.action
            if not action:
                action = f"[{', '.join(names)}]"
            else:
                assert action[0] == '{' and action[-1] == '}', repr(action)
                action = action[1:-1].strip()
            if is_loop:
                gen.print(f"children.append({action})")
                gen.print(f"mark = self.mark()")
            else:
                gen.print(f"return {action}")
        gen.print("self.reset(mark)")

    def is_recursive(self, rulename: str) -> bool:
        return bool(self.items) and self.items[0].is_recursive(rulename)


class NamedItem:
    def __init__(self, name: Optional[str], item: Item):
        self.name = name
        self.item = item

    def __str__(self):
        if self.name:
            return f"{self.name}={self.item}"
        else:
            return str(self.item)

    def __repr__(self):
        return f"NamedItem({self.name!r}, {self.item!r})"

    def gen_item(self, gen: ParserGenerator, names: List[str]):
        name, call = self.item.make_call(gen)
        name = dedupe(name, names)
        gen.print(f"({name} := {call})")

    def make_call(self, gen: ParserGenerator):
        name, call = self.item.make_call(gen)
        if self.name:
            name = self.name
        return name, call

    def is_recursive(self, rulename: str) -> bool:
        return self.item.is_recursive(rulename)


class Opt:
    def __init__(self, node: Plain):
        self.node = node

    def __str__(self):
        return f"{self.node}?"

    def __repr__(self):
        return f"Opt({self.node!r})"

    def make_call(self, gen: ParserGenerator) -> Tuple[str, str]:
        name, call = self.node.make_call(gen)
        return "opt", f"{call},"  # Note trailing comma!

    def is_recursive(self, rulename: str) -> bool:
        return False


class Repeat:
    """Shared base class for x* and x+."""

    def __init__(self, node: Plain):
        self.node = node

    def is_recursive(self, rulename: str) -> bool:
        return False


class Repeat0(Repeat):
    def __str__(self):
        return f"({self.node})*"

    def __repr__(self):
        return f"Repeat0({self.node!r})"

    def make_call(self, gen: ParserGenerator) -> Tuple[str, str]:
        name = gen.name_loop(self.node)
        return name, f"self.{name}(),"  # Also a trailing comma!


class Repeat1(Repeat):
    def __str__(self):
        return f"({self.node})+"

    def __repr__(self):
        return f"Repeat1({self.node!r})"

    def make_call(self, gen: ParserGenerator) -> Tuple[str, str]:
        name = gen.name_loop(self.node)
        return name, f"self.{name}()"  # But no trailing comma here!


class Group:
    def __init__(self, alts: Alts):
        self.alts = alts

    def __str__(self):
        return f"({self.alts})"

    def __repr__(self):
        return f"Group({self.alts!r})"

    def make_call(self, gen: ParserGenerator) -> Tuple[str, str]:
        return self.alts.make_call(gen)

    def is_recursive(self, rulename: str) -> bool:
        return self.alts.is_recursive(rulename)


Plain = Union[Leaf, Group]
Item = Union[Plain, Opt, Repeat]


class GrammarParser(Parser):
    """Hand-written parser for Grammar files."""

    @memoize
    def start(self) -> Optional[List[Rule]]:
        """
        start: rule+ ENDMARKER
        """
        mark = self.mark()
        rules = []
        while rule := self.rule():
            rules.append(rule)
            mark = self.mark()
        if self.expect('ENDMARKER'):
            return rules
        return None

    @memoize
    def rule(self) -> Optional[Rule]:
        """
        rule: NAME ':' alternatives NEWLINE
        """
        mark = self.mark()
        if ((name := self.name()) and
                self.expect(':') and
                (alts := self.alternatives()) and
                self.expect('NEWLINE')):
            return Rule(name.string, alts)
        self.reset(mark)
        return None

    @memoize
    def alternatives(self) -> Optional[Alts]:
        """
        alternatives: alternative ('|' alternative)*
        """
        mark = self.mark()
        alts = []
        if alt := self.alternative():
            alts.append(alt)
        else:
            return None
        mark = self.mark()
        while self.expect('|') and (alt := self.alternative()):
            alts.append(alt)
            mark = self.mark()
        self.reset(mark)
        if not alts:
            return None
        return Alts(alts)

    @memoize
    def alternative(self) -> Optional[Alt]:
        """
        alternative: named_item+ [CURLY_STUFF]
        """
        mark = self.mark()
        items = []
        while item := self.named_item():
            items.append(item)
            mark = self.mark()
        if not items:
            return None
        action = self.curly_stuff()
        return Alt(items, action.string if action else None)

    @memoize
    def named_item(self) -> Optional[NamedItem]:
        """
        named_item: NAME '=' item | item
        """
        mark = self.mark()
        if (name := self.name()) and self.expect('=') and (item := self.item()):
            return NamedItem(name.string, item)
        self.reset(mark)
        item = self.item()
        if not item:
            return None
        return NamedItem(None, item)

    @memoize
    def item(self) -> Optional[Item]:
        """
        item: '[' alternatives ']' | atom (' '* '?' | '*' | '+')?
        """
        mark = self.mark()
        if self.expect('[') and (alts := self.alternatives()) and self.expect(']'):
            return Opt(alts)
        self.reset(mark)
        if atom := self.atom():
            mark = self.mark()
            while self.expect(' '):
                pass
            if self.expect('?'):
                return Opt(atom)
            if self.expect('*'):
                return Repeat0(atom)
            if self.expect('+'):
                return Repeat1(atom)
            return atom
        return None

    @memoize
    def atom(self) -> Optional[Plain]:
        """
        atom: '(' alternatives ')' | NAME | STRING
        """
        mark = self.mark()
        if self.expect('(') and (alts := self.alternatives()) and self.expect(')'):
            return Group(alts)
        self.reset(mark)
        if name := self.name():
            return NameLeaf(name.string)
        if string := self.string():
            return StringLeaf(string.string)
        return None


PARSER_PREFIX = """#!/usr/bin/env python3.8
# @generated by pegen.py from {filename}
from __future__ import annotations

import ast
import sys
import tokenize

from pegen import memoize, memoize_left_rec, Parser

"""

PARSER_SUFFIX = """

if __name__ == '__main__':
    from pegen import simple_parser_main
    simple_parser_main(GeneratedParser)
"""


class ParserGenerator:

    def __init__(self, rules: List[Rule], file: IO[Text]):
        self.rules = rules
        self.file = file
        self.level = 0

    @contextlib.contextmanager
    def indent(self) -> None:
        self.level += 1
        try:
            yield
        finally:
            self.level -= 1

    def print(self, *args):
        if not args:
            print(file=self.file)
        else:
            print("    "*self.level, end="", file=self.file)
            print(*args, file=self.file)

    def printblock(self, lines):
        for line in lines.splitlines():
            self.print(line)

    def generate_parser(self, filename: str) -> None:
        self.print(PARSER_PREFIX.format(filename=filename))
        self.print("class GeneratedParser(Parser):")
        self.todo: Dict[str, Rule] = {}  # Rules to generate
        self.done: Dict[str, Rule] = {}  # Rules generated
        self.counter = 0
        for rule in self.rules:
            self.todo[rule.name] = rule
        while self.todo:
            for rulename, rule in list(self.todo.items()):
                self.done[rulename] = rule
                del self.todo[rulename]
                self.print()
                with self.indent():
                    rule.gen_func(self, rulename)
        self.print(PARSER_SUFFIX.rstrip('\n'))

    def name_node(self, alts: Alts) -> str:
        print(f"name_node({alts})")
        self.counter += 1
        name = f'_tmp_{self.counter}'  # TODO: Pick a nicer name.
        self.todo[name] = Rule(name, alts)
        return name

    def name_loop(self, node: Plain):
        self.counter += 1
        name = f'_loop_{self.counter}'  # TODO: It's ugly to signal via the name.
        self.todo[name] = Rule(name, Alts([Alt([NamedItem(None, node)])]))
        return name


def dedupe(name: str, names: List[str]) -> str:
    origname = name
    counter = 0
    while name in names:
        counter += 1
        name = f"{origname}_{counter}"
    names.append(name)
    return name


def print_memstats() -> bool:
    MiB: Final = 2**20
    try:
        import psutil
    except ImportError:
        return False
    print("Memory stats:")
    process = psutil.Process()
    meminfo = process.memory_info()
    res = {}
    res['rss'] = meminfo.rss / MiB
    res['vms'] = meminfo.vms / MiB
    if sys.platform == 'win32':
        res['maxrss'] = meminfo.peak_wset / MiB
    else:
        # See https://stackoverflow.com/questions/938733/total-memory-used-by-python-process
        import resource  # Since it doesn't exist on Windows.
        rusage = resource.getrusage(resource.RUSAGE_SELF)
        if sys.platform == 'darwin':
            factor = 1
        else:
            factor = 1024  # Linux
        res['maxrss'] = rusage.ru_maxrss * factor / MiB
    for key, value in res.items():
        print(f"  {key:12.12s}: {value:10.0f} MiB")
    return True


# Hack: extra token to represent '{ ... }'
CURLY_STUFF = token.N_TOKENS + 1
token.tok_name[CURLY_STUFF] = 'CURLY_STUFF'


def grammar_tokenizer(token_generator):
    for tok in token_generator:
        if tok.string == '{':
            start = tok.start
            nest = 1
            accumulated = ['{']
            for tok in token_generator:
                accumulated.append(tok.string)
                if tok.string == '{':
                    nest += 1
                elif tok.string == '}':
                    nest -= 1
                    if nest == 0:
                        end = tok.end
                        break
            yield tokenize.TokenInfo(CURLY_STUFF, " ".join(accumulated), start, end, "")
        else:
            yield tok


def simple_parser_main(parser_class):
    argparser = argparse.ArgumentParser()
    argparser.add_argument('-v', '--verbose', action='count', default=0,
                           help="Print timing stats; repeat for more debug output")
    argparser.add_argument('-q', '--quiet', action='store_true',
                           help="Don't print the parsed program")
    argparser.add_argument('-G', '--grammar-parser', action='store_true',
                           help="Recognize { ... } stuff; use for meta-grammar")
    argparser.add_argument('filename', help="Input file ('-' to use stdin)")

    args = argparser.parse_args()
    verbose = args.verbose
    verbose_tokenizer = verbose >= 2
    verbose_parser = verbose == 1 or verbose >= 3

    t0 = time.time()

    filename = args.filename
    if filename == '' or filename == '-':
        filename = "<stdin>"
        file = sys.stdin
    else:
        file = open(args.filename)
    try:
        tokengen = tokenize.generate_tokens(file.readline)
        if args.grammar_parser:
            tokengen = grammar_tokenizer(tokengen)
        tokenizer = Tokenizer(tokengen, verbose=verbose_tokenizer)
        parser = parser_class(tokenizer, verbose=verbose_parser)
        tree = parser.start()
        try:
            if file.isatty():
                endpos = 0
            else:
                endpos = file.tell()
        except IOError:
            endpos = 0
    finally:
        if file is not sys.stdin:
            file.close()

    t1 = time.time()

    if not tree:
        err = parser.make_syntax_error(filename)
        traceback.print_exception(err.__class__, err, None)
        sys.exit(1)

    if not args.quiet:
        print(tree)

    if verbose:
        dt = t1 - t0
        diag = tokenizer.diagnose()
        nlines = diag.end[0]
        if diag.type == token.ENDMARKER:
            nlines -= 1
        print(f"Total time: {dt:.3f} sec; {nlines} lines", end="")
        if endpos:
             print(f" ({endpos} bytes)", end="")
        if dt:
            print(f"; {nlines/dt:.0f} lines/sec")
        else:
            print()
        print("Caches sizes:")
        print(f"  token array : {len(tokenizer._tokens):10}")
        print(f"  symbol cache: {len(parser._symbol_cache):10}")
        print(f"  token cache : {len(parser._token_cache):10}")
        print_memstats()


argparser = argparse.ArgumentParser(prog='pegen', description="Experimental PEG-like parser generator")
argparser.add_argument('-q', '--quiet', action='store_true', help="Don't print the parsed grammar")
argparser.add_argument('-v', '--verbose', action='count', default=0,
                       help="Print timing stats; repeat for more debug output")
argparser.add_argument('-o', '--output', default='parse.py', metavar='OUT',
                       help="Where to write the generated parser (default parse.py)")
argparser.add_argument('filename', help="Grammar description")


def main() -> None:
    args = argparser.parse_args()
    verbose = args.verbose
    verbose_tokenizer = verbose >= 2
    verbose_parser = verbose == 1 or verbose >= 3
    t0 = time.time()

    with open(args.filename) as file:
        tokenizer = Tokenizer(grammar_tokenizer(tokenize.generate_tokens(file.readline)),
                              verbose=verbose_tokenizer)
        parser = GrammarParser(tokenizer, verbose=verbose_parser)
        rules = parser.start()
        if not rules:
            err = parser.make_syntax_error(args.filename)
            traceback.print_exception(err.__class__, err, None)
            sys.exit(1)
        endpos = file.tell()

    if not args.quiet:
        if args.verbose:
            for rule in rules:
                print(repr(rule))
        for rule in rules:
            print(rule)

    with open(args.output, 'w') as file:
        genr = ParserGenerator(rules, file)
        genr.generate_parser(args.filename)

    t1 = time.time()

    if not args.quiet:
        dt = t1 - t0
        diag = tokenizer.diagnose()
        nlines = diag.end[0]
        if diag.type == token.ENDMARKER:
            nlines -= 1
        print(f"Total time: {dt:.3f} sec; {nlines} lines", end="")
        if endpos:
             print(f" ({endpos} bytes)", end="")
        if dt:
            print(f"; {nlines/dt:.0f} lines/sec")
        else:
            print()
        print("Caches sizes:")
        print(f"  token array : {len(tokenizer._tokens):10}")
        print(f"  symbol cache: {len(parser._symbol_cache):10}")
        print(f"  token cache : {len(parser._token_cache):10}")
        if not print_memstats():
            print("(Can't find psutil; install it for memory stats.)")


if __name__ == '__main__':
    main()
