from __future__ import annotations

from dataclasses import dataclass, field

from tinyquery.errors import TinyQueryError
from tinyquery.expr import (
    Aggregate,
    BinaryOp,
    ColumnRef,
    Expr,
    Literal,
    NotOp,
    SelectItem,
    Star,
)


@dataclass
class TableRef:
    name: str
    alias: str


@dataclass
class JoinClause:
    table: TableRef
    left_key: Expr
    right_key: Expr


@dataclass
class OrderTerm:
    expr: Expr
    descending: bool = False


@dataclass
class Query:
    select: list[SelectItem]
    from_table: TableRef
    joins: list[JoinClause] = field(default_factory=list)
    where: Expr | None = None
    group_by: list[Expr] = field(default_factory=list)
    order_by: list[OrderTerm] = field(default_factory=list)
    limit: int | None = None


KEYWORDS = {
    "SELECT",
    "FROM",
    "JOIN",
    "INNER",
    "ON",
    "WHERE",
    "GROUP",
    "BY",
    "AS",
    "AND",
    "OR",
    "NOT",
    "COUNT",
    "SUM",
    "MIN",
    "MAX",
    "AVG",
    "ORDER",
    "LIMIT",
    "ASC",
    "DESC",
}

_STOP_ALIAS = {
    "FROM",
    "WHERE",
    "GROUP",
    "JOIN",
    "INNER",
    "ORDER",
    "LIMIT",
    "ON",
}

COMPARISONS = {"=", "!=", "<", ">", "<=", ">="}


@dataclass
class _Token:
    kind: str
    value: object
    pos: int


class Parser:
    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.tokens = _tokenize(sql)
        self.i = 0

    def parse(self) -> Query:
        query = self._query()
        if not self._check("EOF"):
            raise TinyQueryError(f"unexpected input at position {self._peek().pos}")
        return query

    def _query(self) -> Query:
        self._expect("SELECT")
        select = self._select_list()
        self._expect("FROM")
        from_table = self._table_ref()
        joins: list[JoinClause] = []
        while self._match_join():
            table = self._table_ref()
            self._expect("ON")
            left = self._comparison()
            if not isinstance(left, BinaryOp) or left.op != "=":
                raise TinyQueryError("JOIN ... ON only supports equality, like t1.id = t2.id")
            joins.append(JoinClause(table, left.left, left.right))
        where = None
        if self._match("WHERE"):
            where = self._or_expr()
        group_by: list[Expr] = []
        if self._match("GROUP"):
            self._expect("BY")
            group_by.append(self._value_expr())
            while self._match(","):
                group_by.append(self._value_expr())
        order_by: list[OrderTerm] = []
        if self._match("ORDER"):
            self._expect("BY")
            order_by.append(self._order_term())
            while self._match(","):
                order_by.append(self._order_term())
        limit = None
        if self._match("LIMIT"):
            token = self._expect("NUMBER")
            if not isinstance(token.value, int):
                raise TinyQueryError("LIMIT must be an integer")
            if token.value < 0:
                raise TinyQueryError("LIMIT must be >= 0")
            limit = token.value
        return Query(select, from_table, joins, where, group_by, order_by, limit)

    def _match_join(self) -> bool:
        if self._check("INNER"):
            self._advance()
            self._expect("JOIN")
            return True
        return self._match("JOIN")

    def _select_list(self) -> list[SelectItem]:
        items = [self._select_item()]
        while self._match(","):
            items.append(self._select_item())
        return items

    def _select_item(self) -> SelectItem:
        if self._check("*"):
            self._advance()
            return SelectItem(Star())
        expr: Expr | Aggregate
        if self._peek().kind in {"COUNT", "SUM", "MIN", "MAX", "AVG"}:
            expr = self._aggregate()
        else:
            expr = self._value_expr()
        alias = None
        if self._match("AS"):
            alias = self._expect("IDENT").value
            assert isinstance(alias, str)
        elif self._check("IDENT") and str(self._peek().value).upper() not in KEYWORDS:
            # optional alias without AS, but do not steal FROM/WHERE/GROUP
            if self._peek().value not in {"FROM"}:
                # only take bare alias if next is not a clause keyword
                nxt = str(self._peek().value).upper()
                if nxt not in _STOP_ALIAS:
                    alias = str(self._advance().value)
        return SelectItem(expr, alias)

    def _aggregate(self) -> Aggregate:
        func = str(self._advance().value).upper()
        self._expect("(")
        arg: Expr | None
        if func == "COUNT" and self._match("*"):
            arg = None
        else:
            arg = self._value_expr()
        self._expect(")")
        return Aggregate(func, arg)

    def _table_ref(self) -> TableRef:
        name = str(self._expect("IDENT").value)
        alias = name
        if self._match("AS"):
            alias = str(self._expect("IDENT").value)
        elif self._check("IDENT"):
            nxt = str(self._peek().value).upper()
            if nxt not in _STOP_ALIAS and nxt != "SELECT":
                alias = str(self._advance().value)
        return TableRef(name, alias)

    def _order_term(self) -> OrderTerm:
        expr = self._value_expr()
        descending = False
        if self._match("DESC"):
            descending = True
        else:
            self._match("ASC")
        return OrderTerm(expr, descending)

    def _or_expr(self) -> Expr:
        expr = self._and_expr()
        while self._match("OR"):
            expr = BinaryOp("OR", expr, self._and_expr())
        return expr

    def _and_expr(self) -> Expr:
        expr = self._not_expr()
        while self._match("AND"):
            expr = BinaryOp("AND", expr, self._not_expr())
        return expr

    def _not_expr(self) -> Expr:
        if self._match("NOT"):
            return NotOp(self._not_expr())
        return self._comparison()

    def _comparison(self) -> Expr:
        left = self._value_expr()
        if self._peek().kind in COMPARISONS:
            op = str(self._advance().value)
            return BinaryOp(op, left, self._value_expr())
        return left

    def _value_expr(self) -> Expr:
        token = self._peek()
        if token.kind == "IDENT":
            return self._column_ref()
        if token.kind == "NUMBER":
            self._advance()
            assert isinstance(token.value, (int, float))
            return Literal(token.value)
        if token.kind == "STRING":
            self._advance()
            return Literal(token.value)
        if self._match("("):
            expr = self._or_expr()
            self._expect(")")
            return expr
        raise TinyQueryError(f"expected expression at position {token.pos}")

    def _column_ref(self) -> ColumnRef:
        name = str(self._expect("IDENT").value)
        if self._match("."):
            col = str(self._expect("IDENT").value)
            return ColumnRef(col, table=name)
        return ColumnRef(name)

    def _match(self, kind: str) -> bool:
        if self._check(kind):
            self._advance()
            return True
        return False

    def _check(self, kind: str) -> bool:
        return self._peek().kind == kind

    def _peek(self) -> _Token:
        return self.tokens[self.i]

    def _advance(self) -> _Token:
        token = self.tokens[self.i]
        self.i += 1
        return token

    def _expect(self, kind: str) -> _Token:
        token = self._peek()
        if token.kind != kind:
            raise TinyQueryError(f"expected {kind} at position {token.pos}, got {token.kind}")
        return self._advance()


def parse(sql: str) -> Query:
    return Parser(sql).parse()


def _tokenize(sql: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        if ch in "(),.*":
            tokens.append(_Token(ch, ch, i))
            i += 1
            continue
        if ch in "<>=!":
            start = i
            if ch in "<>=" and i + 1 < n and sql[i + 1] == "=":
                tokens.append(_Token(sql[i : i + 2], sql[i : i + 2], start))
                i += 2
                continue
            if ch == "!" and i + 1 < n and sql[i + 1] == "=":
                tokens.append(_Token("!=", "!=", start))
                i += 2
                continue
            tokens.append(_Token(ch, ch, start))
            i += 1
            continue
        if ch == "'":
            start = i
            i += 1
            chars: list[str] = []
            while i < n:
                if sql[i] == "'" and i + 1 < n and sql[i + 1] == "'":
                    chars.append("'")
                    i += 2
                    continue
                if sql[i] == "'":
                    i += 1
                    break
                chars.append(sql[i])
                i += 1
            else:
                raise TinyQueryError(f"unterminated string at position {start}")
            tokens.append(_Token("STRING", "".join(chars), start))
            continue
        if ch.isdigit() or (ch == "-" and i + 1 < n and sql[i + 1].isdigit()):
            start = i
            if ch == "-":
                i += 1
            while i < n and sql[i].isdigit():
                i += 1
            if i < n and sql[i] == ".":
                i += 1
                while i < n and sql[i].isdigit():
                    i += 1
                tokens.append(_Token("NUMBER", float(sql[start:i]), start))
            else:
                tokens.append(_Token("NUMBER", int(sql[start:i]), start))
            continue
        if ch.isalpha() or ch == "_":
            start = i
            i += 1
            while i < n and (sql[i].isalnum() or sql[i] == "_"):
                i += 1
            word = sql[start:i]
            upper = word.upper()
            if upper in KEYWORDS:
                tokens.append(_Token(upper, upper, start))
            else:
                tokens.append(_Token("IDENT", word, start))
            continue
        raise TinyQueryError(f"unexpected character {ch!r} at position {i}")
    tokens.append(_Token("EOF", None, n))
    return tokens
