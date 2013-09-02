#!/usr/bin/env python

import re
import datetime
import operator
import sys
import os
import glob
import csv
import readline
import new
import time
import atexit
from itertools import chain, tee, islice, groupby
from dpark.moosefs import walk
from dpark import _ctx as dpark
from dpark.dependency import Aggregator
from math import log

########## Combinators ########## 
def result(v):
    return lambda tokens: iter([(v, tokens)])

def zero():
    return lambda tokens: iter([])

def item():
    def _(tokens):
        tokens = iter(tokens)
        yield tokens.next(), tokens
    return _

def bind(p, f):
    return lambda tokens: chain.from_iterable(f(v)(t) for v, t in p(tokens))

def seq(p, q):
    return bind(p, lambda x:
                bind(q, lambda y:
                     result((x,y))
                    )
               )
def seqs(*args):
    if not args:
        return zero()
    elif len(args) == 1:
        return bind(args[0], lambda x: result((x,)))
    return bind(args[0], lambda x:
                bind(seqs(*args[1:]), lambda xs:
                     result((x,) + xs)
                    )
               )

def sat(p):
    return bind(item(), lambda x:
                result(x) if p(x) else zero()
               )

def plus(p, q):
    return lambda tokens: chain.from_iterable(map(lambda (f,k):f(k), zip((p, q), tee(tokens))))

def pluses(*args):
    if not args:
        return zero()
    elif len(args) == 1:
        return args[0]
    return plus(args[0], pluses(*args[1:]))

def many(p):
    return plus(bind(p, lambda x:
                bind(many(p), lambda xs:
                     result ((x,) + xs)
                    )
               ), result(()))
def many1(p):
    return bind(p, lambda x:
                bind(many(p), lambda xs:
                     result((x,) + xs)
                    )
               )
def sepby1(p, sep):
    return bind(p, lambda x:
                bind(many(
                    bind(sep, lambda _:
                         bind(p, lambda y:
                              result(y)
                             )
                        )), lambda xs:
                    result((x,) + xs)
                )
               )

def sepby(p, sep):
    return plus(sepby1(p, sep), result(()))

def bracket(p, sep, open_, close_):
    return bind(open_, lambda _:
                bind(sepby(p, sep), lambda x:
                     bind(close_, lambda _:
                          result(x)
                         )
                    )
               )

def chainl1(p, op):
    def rest(x):
        return plus(bind(op, lambda f:
                         bind(p, lambda y:
                              rest(f(x,y))
                             )
                        ),
                    result(x))
    return bind(p, rest)

########## Lexer ########## 
def quote(s, c):
    return re.sub(r'([\\%s])' % c, r'\\\1', s)

def unquote(s, c):
    return re.sub(r'\\([\\%s])' % c, r'\1', s)

class Token(object):
    value = None

    @classmethod
    def sat(cls, v=None):
        return sat(lambda x: isinstance(x, cls) and (v is None or x.value == v))

class Keyword(Token):
    def __init__(self, token):
        self.value = token.upper()

    def __repr__(self):
        return 'Keyword %s' % self.value

class Identity(Token):
    def __init__(self, token):
        self.value = unquote(token, '`')

    def __repr__(self):
        return 'Identity %s' % self.value

class Type(Token):
    STR, INT, FLOAT = range(3)
    mappping = {
        'varchar':STR,
        'char':STR,
        'string':STR,
        'int':INT,
        'float':FLOAT,
    }
    reverse_mapping = dict(reversed(i) for i in mappping.items())
    @classmethod
    def to_str(cls, value):
        return cls.reverse_mapping[value]

    def __init__(self, token='string'):
        self.value = self.mappping.get(token)

    def __repr__(self):
        return 'Type %s' % self.value

class Number(Token):
    def __init__(self, token):
        self.value = float(token)

    def __repr__(self):
        return 'Number %s' % self.value

class Expression(Token):
    def __init__(self, token):
        self.value = unquote(token[2:-1], '"')

    def __repr__(self):
        return 'Expression \'%s\'' % self.value

class String(Token):
    def __init__(self, token):
        self.value = unquote(token[1:-1], '"')

    def __repr__(self):
        return 'String \'%s\'' % self.value

class SpecialChar(Token):
    def __init__(self, token):
        self.value = token

    def __repr__(self):
        return 'SpecialChar \'%s\'' % self.value

KEYWORDS = r'select|from|where|like|having|order|not|and|or|group|by|desc|asc|'\
        r'as|limit|in|sum|count|avg|max|min|adcount|outfile|into|drop|show|create|'\
        r'table|if|exists|all|distinct|tables|inner|left|right|outer|join|using'

lexer = re.Scanner([
    (r'\b(' + KEYWORDS + r')\b', lambda _,t: Keyword(t)),
    (r'\b(int|float|string|char|varchar)\b', lambda _,t: Type(t)),
    (r'`(\\`|\\\\|[^\\`])+`', lambda _,t:Identity(t[1:-1])),
    (r'\b([_a-z][.\w]*)\b', lambda _,t:Identity(t)),
    (r'(\d+(\.\d*)?|\.\d+)(e[+-]?\d+)?', lambda _,t:Number(t)),
    (r'\$"(\\"|\\\\|[^\\"])*"', lambda _,t:Expression(t)),
    (r'"(\\"|\\\\|[^\\"])*"', lambda _,t:String(t)),
    (r'[-()*+,;=/]|<>|>=?|<=?',lambda _,t:SpecialChar(t)),
    (r'\s+', None),
], re.I)

########## Expressions ########## 
class Table(object):
    def get_columns(self):
        pass

    def gen_rdd(self, **kwargs):
        pass

class PartialTable(Table):
    def __init__(self, table_name, alias, selectors):
        table = tables[table_name]
        self.table_name = table_name
        self.expr = table.expr
        self.selectors = selectors
        self.columns = table.columns
        self.alias = alias
        if alias:
            self.columns = [('%s.%s' % (alias, c), t) for c,t in self.columns]

    def __repr__(self):
        return '%s%s%s' % (
            self.table_name,
            '<%s>' % ','.join(self.selectors)
            if self.selectors else '',
            (' AS %s' % self.alias) if self.alias else ''
        )

    def get_columns(self):
        return self.columns

    def gen_rdd(self, **kwargs):
        use_limit = kwargs.get('use_limit', False)
        converters = []
        for c, t in self.columns:
            if t == Type.INT:
                converters.append(lambda x:int(x) if x else 0)
            elif t == Type.FLOAT:
                converters.append(lambda x:float(x) if x else 0)
            else:
                converters.append(lambda x:x)

        expr = self.expr
        if self.selectors:
            r = expr.file_path
            path = set()
            for s in self.selectors:
                for p in glob.glob('%s/%s' % (r, s)):
                    if not os.path.isdir(p):
                        path.add(p)
                    else:
                        for root, _, names in walk(p, followlinks=True):
                            path.update(os.path.join(root, name) for name in names
                                        if not name.startswith('.'))

            path = list(path)
        else:
            path = expr.file_path

        if use_limit and isinstance(path, basestring):
            for root, _, names in walk(path, followlinks=True):
                if names:
                    path = [os.path.join(root, name) for name in names]
                    break
        
        rdd = dpark.textFile(path)
        if expr.expr:
            rdd = eval('rdd.' + expr.expr, global_env, {'rdd':rdd})

        row = rdd.first()
        if isinstance(row, basestring):
            if '\t' in row:
                rdd = rdd.fromCsv('excel-tab')
            elif ',' in row:
                rdd = rdd.fromCsv('excel')
            else:
                rdd = rdd.map(lambda l:l.split(' '))

        return rdd.map(lambda x:[c(x[i]) for i, c in enumerate(converters)])

class CartesianTable(Table):
    def __init__(self, tables):
        self.tables = tables

    def __repr__(self):
        return ','.join(str(t) for t in self.tables)

    def get_columns(self):
        return [c for t in self.tables for c in t.get_columns()]

    def gen_rdd(self, **kwargs):
        tables = self.tables
        if len(self.tables) == 1:
            return tables[0].gen_rdd(**kwargs)
        
        rdd = tables[0].gen_rdd()
        for t in tables[1:]:
            rdd = rdd.cartesian(t.gen_rdd()).map(lambda x:list(chain.from_iterable(x)))

        return rdd

class SubqueryTable(Table):
    def __init__(self, st, alias):
        self.st = st
        self.alias = alias

    def __repr__(self):
        return '(%s) AS %s' % (str(self.st), self.alias)

    def get_columns(self):
        if self.st.select_list == '*':
            return [('%s.%s' % (self.alias, c), t) for c,t in self.st.table.get_columns()]
        else:
            return [('%s.%s' % (self.alias, a or c), Type.STR)
                    for c, a in self.st.select_list]

    def gen_rdd(self, **kwargs):
        use_limit = kwargs.get('use_limit', False)
        rdd = self.st.gen_rdd(use_limit=use_limit)
        if self.st.limit:
            rdd = dpark.makeRDD(take(rdd, self.st.limit))

        return rdd

class InnerJoinTable(Table):
    def __init__(self, x, y):
        self.x = x
        self.y = y

    def __repr__(self):
        return '%s INNER JOIN %s USING (%s, %s)' % (
           str(self.x[0]), str(self.y[0]), self.x[1], self.y[1]
        )

    def get_columns(self):
        return self.x[0].get_columns() + self.y[0].get_columns()

    def gen_rdd(self, **kwargs):
        x_columns = [c for c, _ in self.x[0].get_columns()]
        x_c_len = len(x_columns)
        y_columns = [c for c, _ in self.y[0].get_columns()]
        y_c_len = len(y_columns)
        x_index = x_columns.index(self.x[1])
        y_index = y_columns.index(self.y[1])
        x_rdd = self.x[0].gen_rdd().map(lambda x:(x[x_index], x))
        y_rdd = self.y[0].gen_rdd().map(lambda y:(y[y_index], y))
        return x_rdd.join(y_rdd).map(lambda (_,(x,y)): x+y)

class LeftJoinTable(Table):
    def __init__(self, x, y):
        self.x = x
        self.y = y

    def __repr__(self):
        return '%s LEFT JOIN %s USING (%s, %s)' % (
           str(self.x[0]), str(self.y[0]), self.x[1], self.y[1]
        )

    def get_columns(self):
        return self.x[0].get_columns() + self.y[0].get_columns()

    def gen_rdd(self, **kwargs):
        x_columns = [c for c, _ in self.x[0].get_columns()]
        x_c_len = len(x_columns)
        y_columns = [c for c, _ in self.y[0].get_columns()]
        y_c_len = len(y_columns)
        x_index = x_columns.index(self.x[1])
        y_index = y_columns.index(self.y[1])
        x_rdd = self.x[0].gen_rdd().map(lambda x:(x[x_index], x))
        y_rdd = self.y[0].gen_rdd().map(lambda y:(y[y_index], y))
        return x_rdd.leftOuterJoin(y_rdd)\
                .map(lambda (_,(x,y)): x + (y if y is not None else ([None] * y_c_len)))

class RightJoinTable(Table):
    def __init__(self, x, y):
        self.x = x
        self.y = y

    def __repr__(self):
        return '%s RIGHT JOIN %s USING (%s, %s)' % (
           str(self.x[0]), str(self.y[0]), self.x[1], self.y[1]
        )

    def get_columns(self):
        return self.x[0].get_columns() + self.y[0].get_columns()

    def gen_rdd(self, **kwargs):
        x_columns = [c for c, _ in self.x[0].get_columns()]
        x_c_len = len(x_columns)
        y_columns = [c for c, _ in self.y[0].get_columns()]
        y_c_len = len(y_columns)
        x_index = x_columns.index(self.x[1])
        y_index = y_columns.index(self.y[1])
        x_rdd = self.x[0].gen_rdd().map(lambda x:(x[x_index], x))
        y_rdd = self.y[0].gen_rdd().map(lambda y:(y[y_index], y))
        return x_rdd.rightOuterJoin(y_rdd)\
                .map(lambda (_,(x,y)): (x if x is not None else ([None] * x_c_len)) + y)


class OuterJoinTable(Table):
    def __init__(self, x, y):
        self.x = x
        self.y = y

    def __repr__(self):
        return '%s OUTER JOIN %s USING (%s, %s)' % (
           str(self.x[0]), str(self.y[0]), self.x[1], self.y[1]
        )

    def get_columns(self):
        return self.x[0].get_columns() + self.y[0].get_columns()

    def gen_rdd(self, **kwargs):
        x_columns = [c for c, _ in self.x[0].get_columns()]
        x_c_len = len(x_columns)
        y_columns = [c for c, _ in self.y[0].get_columns()]
        y_c_len = len(y_columns)
        x_index = x_columns.index(self.x[1])
        y_index = y_columns.index(self.y[1])
        x_rdd = self.x[0].gen_rdd().map(lambda x:(x[x_index], x))
        y_rdd = self.y[0].gen_rdd().map(lambda y:(y[y_index], y))
        return x_rdd.outerJoin(y_rdd)\
                .map(lambda (_,(x,y)): (x if x is not None else ([None] * x_c_len)) +
                     (y if y is not None else ([None] * y_c_len)))


class Schema(object):
    def __init__(self, table, select_list):
        mappers = {}
        self.mappers = mappers
        for i, (c,_) in enumerate(table.get_columns()):
            mappers[c] = lambda _, i=i:lambda row:row[i]

        if select_list != '*':
            for (e, n) in select_list:
                if n:
                    mappers[n] = e

    def get_mapper(self, v):
        return self.mappers[v]

def combine(rep, fun, *args):
    if all(isinstance(arg, LiteralExpr) for arg in args):
        return LiteralExpr(fun(*[arg.value for arg in args]))
    return CombineExpr(rep, fun, *args)

class Expr(object):
    def get_subexpr(self):
        return []

    def __mul__(self, other):
        return combine('*', operator.mul, self, other)

    def __div__(self, other):
        return combine('/', operator.div, self, other)

    def __add__(self, other):
        return combine('+', operator.add, self, other)

    def __sub__(self, other):
        return combine('-', operator.sub, self, other)

    def __eq__(self, other):
        return combine('=', operator.eq, self, other)

    def __ne__(self, other):
        return combine('<>', operator.ne, self, other)
                                
    def __gt__(self, other):
        return combine('>', operator.gt, self, other)
                                
    def __lt__(self, other):
        return combine('<', operator.lt, self, other)
                                
    def __ge__(self, other):
        return combine('>=', operator.ge, self, other)
                                
    def __le__(self, other):
        return combine('<=', operator.le, self, other)
                                
    def and_(self, other):
        return combine('AND', operator.and_, self, other)
                                
    def or_(self, other):
        return combine('OR', operator.or_, self, other)
                                
    def not_(self):
        return combine('NOT', operator.not_, self)

class CombineExpr(Expr):
    def __init__(self, rep, fun, *args):
        self.fun = fun
        self.args = args
        self.rep = rep

    def __call__(self, schema):
        fun = self.fun
        lf = [arg(schema) for arg in self.args]
        return lambda row: fun(*[l(row) for l in lf])

    def __repr__(self):
        if len(self.args) == 2:
            return '(%s %s %s)' % (self.args[0], self.rep, self.args[1])
        return '(%s)' % ' '.join([self.rep] + map(str, self.args))

    def get_subexpr(self):
        return self.args

class LiteralExpr(Expr):
    def __init__(self, x):
        self.value = x

    def __call__(self, schema):
        return lambda _, v = self.value: v

    def __repr__(self):
        return str(self.value)

class ColumnRefExpr(Expr):
    def __init__(self, x):
        self.value = x

    def __call__(self, schema):
        mapper = schema.get_mapper(self.value)
        return mapper(schema)

    def __repr__(self):
        return self.value

class NativeExpr(Expr):
    def __init__(self, x):
        self.value = unquote(x, '"').replace('\n', ' ')

    def __call__(self, schema):
        funs = [(k,v(schema)) for k,v in schema.mappers.items()
                if not isinstance(v, (NativeExpr, SetExpr))]
        return lambda row, v=self.value:eval(v, global_env, dict((k, v(row)) for k,v in funs))

    def __repr__(self):
        return '$"%s"' % self.value

class SetExpr(Expr):
    def __call__(self, schema):
        mapper = schema.get_mapper(self)
        return mapper(schema)

    def compute(self, schema):
        pass

class CountAllExpr(SetExpr):
    def __init__(self):
        SetExpr.__init__(self)

    def compute(self, schema):
        return (
            lambda x: 1,
            lambda r, x: r + 1,
            lambda r1, r2: r1 + r2,
            lambda r: r,
        )

    def __repr__(self):
        return 'COUNT (*)'

class CountExpr(SetExpr):
    def __init__(self, expr, is_distinct):
        SetExpr.__init__(self)
        self.expr = expr
        self.is_distinct = is_distinct

    def compute(self, schema):
        if not self.is_distinct:
            return   (
                lambda x: 1,
                lambda r, x: r + 1,
                lambda r1, r2: r1 + r2,
                lambda r: r,
            )
        else:
            expr = self.expr(schema)
            return (
                lambda x: set([expr(x)]),
                lambda r, x: r.add(expr(x)) or r,
                lambda r1, r2: r1.union(r2),
                lambda r: len(r),
            )

    def __repr__(self):
        return 'COUNT (%s%s)' % ('DISTINCT ' if self.is_distinct else '', self.expr)

    def get_subexpr(self):
        return [self.expr]

class SumExpr(SetExpr):
    def __init__(self, expr, is_distinct):
        SetExpr.__init__(self)
        self.expr = expr
        self.is_distinct = is_distinct

    def compute(self, schema):
        expr = self.expr(schema)
        if not self.is_distinct:
            return (
                lambda x: expr(x),
                lambda r, x: r + expr(x),
                lambda r1, r2: r1 + r2,
                lambda r : r,
            )
        else:
            return (
                lambda x: set([expr(x)]),
                lambda r, x: r.add(expr(x)) or r,
                lambda r1, r2: r1.union(r2),
                lambda r: sum(r),
            )

    def __repr__(self):
        return 'SUM (%s%s)' % ('DISTINCT ' if self.is_distinct else '', self.expr)

    def get_subexpr(self):
        return [self.expr]

class AvgExpr(SetExpr):
    def __init__(self, expr, is_distinct):
        SetExpr.__init__(self)
        self.expr = expr
        self.is_distinct = is_distinct

    def compute(self, schema):
        expr = self.expr(schema)
        if not self.is_distinct:
            return (
                lambda x: (expr(x), 1),
                lambda r, x: (r[0] + expr(x), r[1] + 1),
                lambda r1, r2: (r1[0] + r2[0], r1[1] + r2[1]),
                lambda r: float(r[0]) / r[1], 
            )
                
        else:
            return (
                lambda x: set([expr(x)]),
                lambda r, x: r.add(expr(x)) or r,
                lambda r1, r2: r1.union(r2),
                lambda r: float(sum(r)) / len(r), 
            )

    def __repr__(self):
        return 'AVG (%s%s)' % ('DISTINCT ' if self.is_distinct else '', self.expr)

    def get_subexpr(self):
        return [self.expr]

class MaxExpr(SetExpr):
    def __init__(self, expr, _):
        SetExpr.__init__(self)
        self.expr = expr

    def compute(self, schema):
        expr = self.expr(schema)
        return (
            lambda x: expr(x),
            lambda r, x: max(r, expr(x)),
            lambda r1, r2: max(r1, r2),
            lambda r: r,
        )

    def __repr__(self):
        return 'MAX (%s)' % self.expr

    def get_subexpr(self):
        return [self.expr]

class MinExpr(SetExpr):
    def __init__(self, expr, _):
        SetExpr.__init__(self)
        self.expr = expr

    def compute(self, schema):
        expr = self.expr(schema)
        return (
            lambda x: expr(x),
            lambda r, x: min(r, expr(x)),
            lambda r1, r2: min(r1, r2),
            lambda r: r,
        )

    def __repr__(self):
        return 'MIN (%s)' % self.expr

    def get_subexpr(self):
        return [self.expr]

class AdcountExpr(SetExpr):
    def __init__(self, expr=None):
        SetExpr.__init__(self)
        self.expr = expr if expr is not None else tuple

    def compute(self, schema):
        try:
            from pyhll import HyperLogLog
        except ImportError:
            from dpark.hyperloglog import HyperLogLog

        expr = self.expr(schema)
        return (
            lambda x: HyperLogLog([expr(x)], 16),
            lambda r, x: r.add(expr(x)) or r,
            lambda r1, r2: r1.update(r2) or r1,
            lambda r: len(r)
        )

    def __repr__(self):
        return 'ADCOUNT (%s)' % (self.expr or '*')

    def get_subexpr(self):
        return [self.expr] if self.expr is not None else []

########## Parsers ########## 
def number():
    return bind(
        pluses(
            SpecialChar.sat('+'),
            SpecialChar.sat('-'),
            result(None),
        ), lambda sign:
        bind(Number.sat(), lambda x:
             result(LiteralExpr(x.value)) if not sign or sign.value != '-'
             else result(LiteralExpr(-x.value))
            )
    )

def set_fun(name, cls):
    return bind(seq(Keyword.sat(name), SpecialChar.sat('(')), lambda _:
             bind(pluses(Keyword.sat('DISTINCT'), Keyword.sat('ALL'), result(None)), lambda q:
                  bind(value_expression(), lambda expr:
                       bind(SpecialChar.sat(')'), lambda _:
                            result(cls(expr, q and q.value =='DISTINCT')))
                      )
                 )
            )
def set_functions():
    return pluses(
        bind(seqs(Keyword.sat('COUNT'), SpecialChar.sat('('), SpecialChar.sat('*'),
                  SpecialChar.sat(')')), lambda _: result(CountAllExpr())),
        set_fun('COUNT', CountExpr),
        set_fun('SUM', SumExpr),
        set_fun('AVG', AvgExpr),
        set_fun('MAX', MaxExpr),
        set_fun('MIN', MinExpr),
        bind(seqs(Keyword.sat('ADCOUNT'), SpecialChar.sat('('), SpecialChar.sat('*'),
                  SpecialChar.sat(')')), lambda _: result(AdcountExpr())),
        bind(seq(Keyword.sat('ADCOUNT'), SpecialChar.sat('(')), lambda _:
             bind(value_expression(), lambda x:
                  bind(SpecialChar.sat(')'), lambda _:
                       result(AdcountExpr(x))
                      )
                 )
            ),
    )

def factor():
    return pluses(
        number(),
        bind(Identity.sat(), lambda x:result(ColumnRefExpr(x.value))),
        bind(SpecialChar.sat('('), lambda _:
             bind(numberic_value_expression(), lambda x:
                  bind(SpecialChar.sat(')'), lambda _:
                       result(x)
                      )
                 )
            ),
        set_functions(),
        bind(Expression.sat(), lambda x:result(NativeExpr(x.value))),
    )

def mul_or_div():
    return plus(
        bind(SpecialChar.sat('*'), lambda _:result(lambda x,y:x*y)),
        bind(SpecialChar.sat('/'), lambda _:result(lambda x,y:x/y))
    )

def term():
    return chainl1(factor(), mul_or_div())

def add_or_sub():
    return plus(
        bind(SpecialChar.sat('+'), lambda _:result(lambda x,y:x+y)),
        bind(SpecialChar.sat('-'), lambda _:result(lambda x,y:x-y))
    )

def numberic_value_expression():
    return chainl1(term(), add_or_sub())

def string_factor():
    return pluses(
        bind(String.sat(), lambda x:result(LiteralExpr(x.value))),
        bind(Identity.sat(), lambda x:result(ColumnRefExpr(x.value))),
        bind(SpecialChar.sat('('), lambda _:
             bind(string_value_expression(), lambda x:
                  bind(SpecialChar.sat(')'), lambda _:
                       result(x)
                      )
                 )
            ),
        bind(Expression.sat(), lambda x:result(NativeExpr(x.value))),
    )

def string_value_expression():
    return chainl1(string_factor(),
                   bind(SpecialChar.sat('+'), lambda _:result(lambda x,y:x+y)))

def value_expression():
    return pluses(numberic_value_expression(), string_value_expression())

def select_sublist():
    return seq(value_expression(), plus(
        bind(seq(Keyword.sat('AS'), Identity.sat()), lambda (_, x):result(x.value)),
        result(None)
    ))

def select_list():
    return plus(
        bind(SpecialChar.sat('*'), lambda _:result('*')),
        sepby(select_sublist(), SpecialChar.sat(','))
    )

def partial_table():
    return bind(
        seqs(
            bind(Identity.sat(), lambda x: result(x.value)),
            plus(
                bracket(
                    String.sat(), SpecialChar.sat(','),
                    SpecialChar.sat('<'), SpecialChar.sat('>')
                ),
                result(())
            ),
            plus(
                bind(seq(Keyword.sat('AS'), Identity.sat()),
                     lambda (_, alias): result(alias.value)),
                result(None)
            )
        ),
        lambda (x, s, alias): result(
            PartialTable(x, alias, [ss.value for ss in s if ss.value])
        )
    )

def table_subquery():
    return bind(SpecialChar.sat('('), lambda _:
                bind(select_statement(), lambda st:
                     bind(seq(SpecialChar.sat(')'), Keyword.sat('AS')), lambda _:
                          bind(Identity.sat(), lambda alias:
                               result(SubqueryTable(st, alias.value))
                              )
                         )
                    )
               )

def table_reference():
    return plus(
        partial_table(),
        table_subquery()
    );

def table_references():
    return bind(sepby1(
        table_reference(),
        SpecialChar.sat(',')
    ), lambda x: result(CartesianTable(x)))

def join_table(word, cls):
    return bind(table_reference(), lambda x:
                bind(seq(Keyword.sat(word), Keyword.sat('JOIN')), lambda _:
                     bind(table_reference(), lambda y:
                          bind(seqs(
                              Keyword.sat('USING'),
                              SpecialChar.sat('('),
                              Identity.sat(),
                              SpecialChar.sat(','),
                              Identity.sat(),
                              SpecialChar.sat(')'),
                          ), lambda (_, _1, x_c, _2, y_c, _3):
                              result(cls((x, x_c.value), (y, y_c.value)))
                          ))
                    )
               )

def from_clause():
    return bind(
        Keyword.sat('FROM'), lambda _:
        pluses(
            table_references(),
            join_table('INNER', InnerJoinTable),
            join_table('LEFT', LeftJoinTable),
            join_table('RIGHT', RightJoinTable),
            join_table('OUTER', OuterJoinTable),
        )
    )

def compare_op():
    return pluses(
        bind(SpecialChar.sat('='), lambda _:result(lambda x,y: x==y)),
        bind(SpecialChar.sat('<>'), lambda _:result(lambda x,y: x!=y)),
        bind(SpecialChar.sat('<'), lambda _:result(lambda x,y: x<y)),
        bind(SpecialChar.sat('>'), lambda _:result(lambda x,y: x>y)),
        bind(SpecialChar.sat('<='), lambda _:result(lambda x,y: x<=y)),
        bind(SpecialChar.sat('>='), lambda _:result(lambda x,y: x>=y)),
    )

def comparison_predicate():
    return bind(value_expression(), lambda x:
                bind(compare_op(), lambda op:
                     bind(value_expression(), lambda y:
                          result(op(x,y))
                         )
                    )
               )

def predict():
    return pluses(
        comparison_predicate(),
        bind(Expression.sat(), lambda x:result(NativeExpr(x.value))),
    )

def boolean_primary():
    return plus(predict(),
                bind(SpecialChar.sat('('), lambda _:
                     bind(search_condition(), lambda x:
                          bind(SpecialChar.sat(')'), lambda _:
                               result(x)
                              )
                         )
                    )
               )

def boolean_factor():
    return bind(seq(plus(Keyword.sat('NOT'), result(None)),
                    boolean_primary()),
                lambda (n, x): result(x if n is None else x.not_()))

def boolean_term():
    return chainl1(boolean_factor(),
                   bind(Keyword.sat('AND'), lambda _:result(lambda x,y:x.and_(y))))

def search_condition():
    return chainl1(boolean_term(),
                   bind(Keyword.sat('OR'), lambda _:result(lambda x,y:x.or_(y))))

def where_clause():
    return bind(seq(Keyword.sat('WHERE'), search_condition()), lambda (_,x): result(x))

def group_by_clause():
    return bind(seq(Keyword.sat('GROUP'), Keyword.sat('BY')), lambda _:
                bind(sepby1(value_expression(), SpecialChar.sat(',')), lambda x:
                     result(x)
                    )
               )

def having_clause():
    return bind(seq(Keyword.sat('HAVING'), search_condition()), lambda (_,x): result(x))

def table_expression():
    return seqs(from_clause(),
                plus(where_clause(), result(None)), 
                plus(group_by_clause(), result(None)), 
                plus(having_clause(), result(None)))

def select_statement():
    return bind(seqs(
        Keyword.sat('SELECT'),
        select_list(),
        table_expression(),
        plus(
            bind(seqs(Keyword.sat('ORDER'), Keyword.sat('BY'),
                      value_expression(),
                      pluses(
                          Keyword.sat('ASC'),
                          Keyword.sat('DESC'),
                          result(None)
                      )
                     ),
                lambda (_,__,r, s):result((r, s.value if s else 'ASC'))),
            result(None),
        ),
        plus(
            bind(seq(Keyword.sat('LIMIT'), Number.sat()), lambda (_,l):result(l.value)),
            result(None),
        ),
        plus(
            bind(seqs(Keyword.sat('INTO'), Keyword.sat('OUTFILE'), String.sat()),
                 lambda (_,__,f):result(f.value)),
            result(None),
        ),
    ), lambda (_, select_list, table_expression, order, limit, outfile):
        result(SelectSt(select_list, table_expression, order, limit, outfile))
    )


def textfile_expr():
    return bind(seqs(Keyword.sat('FROM'), String.sat(),
                     plus(Expression.sat(), result(None))),
                lambda (_, textFile, expr): result(TextFileExpr(textFile,expr)))

def create_table_statement():
    return bind(seqs(
        Keyword.sat('CREATE'),
        Keyword.sat('TABLE'), 
        plus(seqs(Keyword.sat('IF'), Keyword.sat('NOT'), Keyword.sat('EXISTS')), result(None)),
        Identity.sat(),
    ), lambda (_, __, exists, table_name):
        bind(bracket(
            seq(Identity.sat(), plus(Type.sat(),result(Type()))),
            SpecialChar.sat(','),
            SpecialChar.sat('('), 
            SpecialChar.sat(')')), lambda columns:
            bind(textfile_expr(), lambda expr:
                 result(CreateTableSt(table_name, columns,
                                      expr, exists is not None))
                )
        )
    )

def drop_table_statement():
    return bind(seq(
        Keyword.sat('DROP'),
        Keyword.sat('TABLE')
    ), lambda _:
        bind(sepby1(Identity.sat(), SpecialChar.sat(',')),
             lambda table_names:
             result(DropTableSt([x.value for x in table_names]))
            )
    )

def show_tables_statement():
    return bind(seq(
        Keyword.sat('SHOW'),
        Keyword.sat('TABLES'),
    ), lambda _:
        bind(plus(bind(seq(Keyword.sat('LIKE'), String.sat()),
                       lambda x: result(x[1].value)),
                  result(None)), lambda patten:
             result(ShowTablesSt(patten))
            )
    )

def show_create_table_statement():
    return bind(seqs(
        Keyword.sat('SHOW'),
        Keyword.sat('CREATE'),
        Keyword.sat('TABLE'),
    ), lambda _:
        bind(Identity.sat(), lambda table_name:
             result(ShowCreateTablesSt(table_name.value))
            )
    )

def python_statement():
    return bind(Expression.sat(), lambda x:
                result(PythonSt(x.value))
               )

def statement_parser():
    return bind(pluses(
        create_table_statement(),
        select_statement(),
        drop_table_statement(),
        show_tables_statement(),
        show_create_table_statement(),
        python_statement(),
    ), lambda x:
        bind(SpecialChar.sat(';'), lambda _:
             result(x)
            )
    )

def script_parser():
    return many(statement_parser())

########## Runtimes ########## 
tables = {}
global_env = {}

def take(rdd, n):
    def take_(n, splits):
        return list(chain.from_iterable(
            dpark.runJob(rdd, lambda x:list(islice(x, n)), splits)))

    n = int(n)
    if n == 0:
        return []

    r = take_(n, [0])
    if len(r) < n:
        for _, splits in groupby(xrange(1, len(rdd)), lambda x:int(log(x + 15)/log(4))):
            splits = list(splits)
            r.extend(take_(n - len(r), splits))
            if len(r) >= n:
                r = r[:n]
                break

    return r


class TextFileExpr(object):
    def __init__(self, file_path, expr):
        self.file_path = os.path.realpath(file_path.value)
        self.expr = unquote(expr.value, '"').replace('\n', ' ') if expr is not None else None

    def __repr__(self):
        return 'FROM "%s"%s' % (
            self.file_path,
            ' $"%s"' % self.expr if self.expr is not None else ''
        )

class SQLStatement(object):
    def do_execute(self):
        pass

    def execute(self):
        start_time = time.time()
        self.do_execute()
        print 'OK. (%.2f sec)' % (time.time() - start_time)

class CreateTableSt(SQLStatement):
    def __init__(self, table_name, columns, expr, check_exists):
        self.table_name = table_name.value
        self.columns = [(c.value, t.value) for c,t in columns]
        self.expr = expr
        self.check_exists = check_exists

    def do_execute(self):
        if not self.check_exists or self.table_name not in tables:
            tables[self.table_name] = self
        else:
            raise ValueError('Table %s already exists' % self.table_name)

    def __repr__(self):
        return 'CREATE TABLE `%s` (%s) %s' % (
            quote(self.table_name, '`'),
            ','.join('%s %s' % (c,Type.to_str(t)) for c,t in self.columns),
            self.expr)

class DropTableSt(SQLStatement):
    def __init__(self, table_names):
        self.table_names = table_names

    def __repr__(self):
        return 'DROP TABLE %s' % (','.join(self.table_names))

    def do_execute(self):
        for name in self.table_names:
            if name in tables:
                del tables[name]

class SelectSt(SQLStatement):
    def __init__(self, select_list, table_expression, order, limit, outfile):
        self.select_list = select_list
        self.table, self.where, self.group_by, self.having = table_expression
        self.order = order
        self.limit = max(0, limit) if limit is not None else None
        self.outfile = outfile

    def __repr__(self):
        return 'SELECT %s FROM %s%s%s%s%s%s%s' % (
            self.select_list, self.table, 
            ' WHERE %s'  % self.where if self.where else '',
            ' GROUP BY %s' % self.group_by if self.group_by else '',
            ' HAVING %s' % self.having if self.having else '',
            ' ORDER BY %s %s' % self.order if self.order else '',
            ' LIMIT %s' % self.limit if self.limit else '',
            ' INTO OUTFILE "%s"' % self.outfile if self.outfile else '',
        )
    
    def gen_rdd(self, **kwargs):
        use_limit = kwargs.get('use_limist', False)
        table = self.table
        q = [x[0] for x in self.select_list
             if isinstance(x, tuple) and isinstance(x[0], Expr)]
        scope = set()
        while q:
            expr = q.pop(0)
            if isinstance(expr, SetExpr):
                scope.add(expr)

            q += expr.get_subexpr()

        schema = Schema(table, self.select_list)
        use_limit = use_limit or (
            self.limit is not None and not scope and self.group_by is None \
            and self.where is None and self.having is None
        )
        rdd = table.gen_rdd(use_limit=use_limit)
        if self.where:
            rdd = rdd.filter(self.where(schema))
        
        if scope or self.group_by is not None:
            column_len = len(table.get_columns())
            creators = [lambda x:x]
            mergers = [lambda r, x: r]
            combiners = [lambda r1, r2: r1]
            mappers = [lambda r: r]
            for i, s in enumerate(scope):
                schema.mappers[s] = lambda _, i=column_len+i:lambda row: row[i]
                creator, merger, combiner, mapper = s.compute(schema)
                creators.append(creator)
                mergers.append(merger)
                combiners.append(combiner)
                mappers.append(mapper)

            if self.group_by is not None:
                keys = [c(schema) for c in self.group_by]
                key = lambda row:tuple(k(row) for k in keys)
                agg = Aggregator(
                    lambda x:[c(x) for c in creators],
                    lambda r, x:[m(r[i], x) for i, m in enumerate(mergers)],
                    lambda r1, r2:[c(r1[i], r2[i]) for i, c in enumerate(combiners)],
                )
                rdd = rdd.map(lambda x:(key(x), x)).combineByKey(agg)\
                        .mapValue(lambda r:[m(r[i]) for i, m in enumerate(mappers)])\
                        .map(lambda (k,v): v[0] + v[1:])
            else:
                def fun(split):
                    r = None
                    for x in iter(split):
                        if r is None:
                            r = [c(x) for c in creators]
                        else:
                            r = [m(r[i], x) for i, m in enumerate(mergers)]

                    return [r]

                rdd = rdd.mapPartitions(fun).filter(lambda x: x is not None)
                result = rdd.reduce(
                    lambda r1, r2:[c(r1[i], r2[i]) for i, c in enumerate(combiners)])
                result = [m(result[i]) for i, m in enumerate(mappers)]
                rdd = dpark.makeRDD([result[0] + result[1:]])

        if self.select_list == '*':
            output_mapper = lambda row: row
        else:
            fun_list = [c(schema) for c,_ in self.select_list]
            output_mapper = lambda row: [f(row) for f in fun_list]


        if self.having:
            rdd = rdd.filter(self.having(schema))

        if self.order:
            reverse = (self.order[1] == 'DESC')
            rdd = rdd.sort(key = self.order[0](schema), reverse=reverse)
            
        rdd = rdd.map(output_mapper)
        return rdd

    def do_execute(self):
        rdd = self.gen_rdd()

        result = None
        if self.limit:
            result = take(rdd, self.limit)

        if self.outfile is None:
            if result is None:
                result = rdd.collect()

            if self.select_list == '*':
                output_field_names = [c for c,t in self.table.get_columns()]
            else:
                output_field_names = [n if n else str(c) for c, n in self.select_list]

            pprint(output_field_names, result)
        else:
            if result is None:
                rdd.saveAsCSVFile(self.outfile)
            else:
                with open(self.outfile, 'wb+') as f:
                    writer = csv.writer(f)
                    for row in result:
                        writer.writerow(row)

class PythonSt(SQLStatement):
    def __init__(self, statements):
        self.statements = statements

    def __repr__(self):
        return '$"%s"' % self.statements

    def do_execute(self):
        exec(self.statements, global_env)

class ShowTablesSt(SQLStatement):
    def __init__(self, pattern):
        self.pattern = pattern

    def __repr__(self):
        return 'SHOW TABLES%s' % ('' if self.pattern is None else ' LIKE %s' % self.pattern)

    def do_execute(self):
        t = tables.keys()
        if self.pattern is not None:
            t = filter(lambda x: re.match('^%s$' % self.pattern, x), t)

        t = [[x] for x in t]
        pprint(['Tables'], t)

class ShowCreateTablesSt(SQLStatement):
    def __init__(self, table_name):
        self.table_name = table_name

    def __repr__(self):
        return 'SHOW CREATE TABLE %s' % self.table_name

    def do_execute(self):
        pprint(['Table', 'Create Table'], [[self.table_name, tables[self.table_name]]])

def first(it):
    def _():
        yield it.next()
    return list(_())

def pprint(fields, table):
    width = [max(5, len(f)) for f in fields]
    for row in table:
        for i, c in enumerate(row):
            width[i] = max(width[i], len(str(c)))

    splitter = '+%s+' % '+'.join(''.join(['-']*w) for w in width)
    def print_row(row):
        pr = []
        for i, c in enumerate(row):
            s = str(c)
            l = width[i] - len(s)
            pr.append(''.join([' ']*l+[s]))

        print '|%s|' % '|'.join(pr)
    print splitter
    print_row(fields)
    print splitter
    for row in table:
        print_row(row)

    print splitter

class Console(object):
    CMDS = ['help', 'quit',]

    def __init__(self):
        main = sys.modules.pop('__main__')
        try:
            self.dummy_main = new.module('__main__')
            sys.modules['__main__'] = self.dummy_main
            from rlcompleter import Completer as RlCompleter
            self.python_completer = RlCompleter()
        finally:
            sys.modules['__main__'] = main

        readline.set_completer_delims(' \t\n;`\"()')
        readline.parse_and_bind('tab: complete')
        readline.set_completer(self.complete)
        base_dir = os.path.expanduser('~/.dq')
        if not os.path.exists(base_dir):
            try:
                os.mkdir(base_dir)
            except IOError:
                pass

        hist_file = os.path.join(base_dir, 'hist')
        try:
            readline.read_history_file(hist_file)
        except IOError:
            pass
        
        atexit.register(readline.write_history_file, hist_file)

        init_file = os.path.join(base_dir, 'init')
        if os.path.exists(init_file):
            with open(init_file, 'r') as f:
                self.run_script(f.read())

        self.sql = ''

    def complete(self, text, state):
        try:
            if state == 0:
                buffer = self.sql + readline.get_line_buffer()

                quote = len(buffer)
                while quote >= 0:
                    quote = buffer[:quote].rfind('"')
                    if quote > 0 and buffer[quote-1] == '\\':
                        continue
                    break

                if quote > 0 and buffer[quote-1] == '$':
                    self.dummy_main.__dict__.update(global_env)
                    self.do_complete = self.python_completer.complete
                else:
                    self.do_complete = self._complete

            return self.do_complete(text, state)
        except Exception, e:
            import traceback; traceback.print_exc()

    def _complete(self, text, state):
        if state == 0:
            if '/' in text:
                options = glob.glob(text+'*')
                self.matches = options
            else:
                options = [quote(c, '`') for c in tables.keys()
                           if c.lower().startswith(text.lower())]
                options += [c.upper() + ' ' for c in KEYWORDS.split('|')
                           if c.lower().startswith(text.lower())]
                self.matches = options

        if state < len(self.matches):
            return self.matches[state]
        return None

    do_complete = _complete

    def help(self, args):
        usage = '''Supported SQL:
\tCREATE TABLE [IF NOT EXISTS] tbl_name (create_definition[, ...]) FROM "file_path" [$"python_expression"]
\tSELECT expr FROM tbl_name[<path_sel, ...>] [AS alias] [[INNER|OUTER|LEFT|RIGHT] JOIN tbl_ref USING (t1_col, t2_col)] [WHERE condition] [GROUP BY cols]  [HAVING condition] [ORDER BY cols [ASC | DESC]] [LIMIT n] [INTO OUTFILE file]
\tDROP TABLE tbl_name, ...
\tSHOW TABLES [LIKE "pattern"]
\tSHOW CREATE TABLE tbl_name'
\t$"python statement blocks";
Use Python expression:
\tPython expressions can be used as a sql expression with $"..." wrapped around'''
        print usage

    def quit(self, args):
        sys.exit(0)

    def run_sql(self):
        l, remain = lexer.scan(self.sql)
        if remain:
            raise Exception('Fail to lex statement: %s' % remain)

        result = first(statement_parser()(l))
        if not result:
            raise Exception('Fail to parse statement')

        st, remain = result[0]
        remain = list(remain)
        if remain:
            raise Exception('Fail to parse statement: %s' 
                            % ' '.join(str(t) for t in remain))
        
        st.execute()

    def run_script(self, script):
        l, remain = lexer.scan(script)
        if remain:
            raise Exception('Fail to lex statement: %s' % remain)

        result = first(script_parser()(l))
        if not result:
            raise Exception('Fail to parse script')

        st, remain = result[0]
        remain = list(remain)
        if remain:
            raise Exception('Fail to parse script: %s'
                            % ' '.join(str(t) for t in remain))
        
        for s in st:
            s.execute()

    def run(self):
        print "Welcome to DQuery 1.0, enjoy SQL and DPark! type 'help' for help."
        while True:
            try:
                if self.sql:
                    self.sql += '\n' + raw_input('... ')
                else:
                    self.sql  = raw_input('>>> ')
            except EOFError:
                print 'quit'
                break
            
            try:
                for c in self.CMDS:
                    s = self.sql.strip()
                    if s.lower().startswith(c):
                        arg = s[len(c):].strip()
                        getattr(self,c)(arg)
                        self.sql = ''
                        continue

                if not self.sql.rstrip().endswith(';'):
                    continue

                self.run_sql()
            except Exception, e:
                import traceback; traceback.print_exc()
            self.sql = ''

if __name__ == '__main__':
    from dpark import optParser
    optParser.set_default('master', 'flet6')
    optParser.add_option('-e', '--query', type='string', default='',
            help='execute the SQL qeury then exit')
    optParser.add_option('-s', '--script', type='string', default='',
            help='execute the SQL script file then exit')
    options, args = optParser.parse_args()
    console = Console()
    if options.query:
        console.run_script(options.query)
    elif options.script:
        with open(options.script) as f:
            console.run_script(f.read())
    else:
        console.run()
