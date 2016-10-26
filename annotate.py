#!/usr/bin/env python

from mmap import mmap, ACCESS_READ
from contextlib import closing
from blessings import Terminal
import re, operator

FUN_RE = re.compile('L(.+?);->(.+)$')
FNP_RE = re.compile(r'^(.+?)\((.*)\)(.+)$')
INS_RE = re.compile(br'^\s+(\w\S+) (?:(.*?), )?(\S+)$', re.MULTILINE)
END_RE = re.compile(br'\.end method', re.MULTILINE)
PARAM_RE = re.compile(r'\[*(?:L.+?;|[^L])')

T = Terminal()

class StringValue(object):
    def __init__(self, value):
        self.value = value

    def __repr__(self):
        v = self.value
        lv = len(v)
        if lv > 30:
            v = v[:30] + '...[{0} chars]'.format(lv)
        return '"{0}"'.format(v)

class SimpleResult(object):
    def __init__(self, value):
        self.value = value

MATH_OPS = {
        'add': ('+', operator.__add__),
        'and': ('&', operator.__and__),
        'div': ('/', operator.__div__),
        'shl': ('<<', operator.__lshift__),
        'shr': ('>>', operator.__rshift__),
        }

class Tracer(object):
    level = 0
    variables = 0

    def trace(self, text):
        print self.level * '  ' + text

    def trace_fun(self, function, params=None, instance=None):
        cls, meth = FUN_RE.match(function).groups()
        name, param_list, retval_type = FNP_RE.match(meth).groups()
        param_types = PARAM_RE.findall(param_list)
        if params is None:
            params = []
            for p in param_types:
                params.append(chr(ord('a') + self.variables))
                self.variables += 1
        self.trace(T.yellow(function) + ' // (' + repr(instance) + ') ' + repr(params))
        if cls.startswith('java'):
            if function == 'Ljava/lang/String;->length()I':
                if isinstance(instance, StringValue):
                    return SimpleResult(len(instance.value))
                return SimpleResult('strlen({0})'.format(instance))
            elif function == 'Ljava/lang/String;->charAt(I)C':
                if isinstance(instance, StringValue) and isinstance(params[0], int):
                    return SimpleResult(ord(instance.value[params[0]]))
                return SimpleResult('{0}[{1}]'.format(instance, params[0]))
            return # TODO
        local_variables = {}
        if instance is not None:
            params.insert(0, instance)
        with open('smali/' + cls + '.smali') as f:
            with closing(mmap(f.fileno(), 0, access=ACCESS_READ)) as smali:
                m = re.search(r'^\.method .* ' + re.escape(meth) + '$', smali, re.MULTILINE)
                pos = m.end()
                m = END_RE.search(smali, pos)
                return self.trace_body(smali, pos, m.start(), params, local_variables)

    def trace_body(self, smali, start, end, params, local_variables):
        def decode_op(op):
            if op.startswith('p'):
                return params[int(op[1:])]
            elif op.startswith('v'):
                return local_variables[op]
            raise ValueError('Invalid operand ' + repr(op))

        last_lv = {}
        for m in INS_RE.finditer(smali, start, end):
            isn, p1, p2 = m.groups()
            self.trace(str(m.start()) + ' @ ' + repr(m.groups())[:80])
            if isn.startswith('invoke-'):
                cp = []
                #trace(p1, p2)
                if len(p1) > 2:
                    for iparam in p1[1:-1].split(', '):
                        cp.append(decode_op(iparam))
                #trace(isn, p1, cp)
                instance = None if isn.endswith('static') else cp.pop(0)
                self.level += 1
                result = self.trace_fun(p2, cp, instance)
                self.level -= 1
                if isinstance(result, SimpleResult):
                    last_result = result.value
                else:
                    last_result = 'result@{0}'.format(m.start())
            elif isn == 'return':
                value = local_variables[p2]
                self.trace(T.red('return {0}'.format(value)))
                return SimpleResult(value)
            elif isn == 'return-object':
                value = local_variables[p2]
                if value == 0:
                    value = 'null'
                self.trace(T.red('return {0}'.format(value)))
                return SimpleResult(value)
            elif isn.startswith('move-result'):
                local_variables[p2] = last_result
            elif isn.startswith('const/') or isn == 'const':
                local_variables[p1] = int(p2, 16)
            elif isn == 'const-string':
                local_variables[p1] = StringValue(p2[1:-1])
            elif isn.endswith('-int') and isn.split('-', 1)[0] in MATH_OPS:
                op_re, op_fn = MATH_OPS[isn.split('-', 1)[0]]
                target, source = p1.split(', ', 1)
                ds = decode_op(source)
                dd = decode_op(p2)
                if isinstance(ds, int) and isinstance(dd, int):
                    local_variables[target] = op_fn(ds, dd)
                else:
                    local_variables[target] = '({s} {o} {d})'.format(s=ds, o=op_re, d=decode_op(p2))
            elif '-int/2addr' in isn and isn.split('-', 1)[0] in MATH_OPS:
                op_re, op_fn = MATH_OPS[isn.split('-', 1)[0]]
                dt = decode_op(p1)
                ds = decode_op(p2)
                if isinstance(dt, int) and isinstance(ds, int):
                    local_variables[p1] = op_fn(dt, ds)
                else:
                    local_variables[p1] = '({s} {o} {d})'.format(s=ds, o=op_re, d=dt)
            elif '-int/lit' in isn and isn.split('-', 1)[0] in MATH_OPS:
                op_re, op_fn = MATH_OPS[isn.split('-', 1)[0]]
                target, source = p1.split(', ', 1)
                ds = decode_op(source)
                dd = int(p2, 16)
                if isinstance(ds, int):
                    local_variables[target] = op_fn(ds, dd)
                else:
                    local_variables[target] = '({s} {o} {d})'.format(s=ds, o=op_re, d=dd)
            elif isn == 'new-array':
                target, size = p1.split(', ', 1)
                local_variables[target] = 'new {t}[{s}]'.format(t=p2[-1], s=decode_op(size))
            elif isn == 'int-to-byte':
                dd = decode_op(p2)
                if isinstance(dd, int):
                    local_variables[p1] = dd & 0xFF
                else:
                    local_variables[p1] = '((byte){0})'.format(dd)
            elif isn == 'new-instance':
                local_variables[p1] = 'new ' + p2
            elif isn == 'sget-object':
                local_variables[p1] = 'get ' + p2
            elif isn.startswith('if-'):
                if isn.endswith('-nez'):
                    self.trace(T.blue('if ({0} != null) goto {1}'.format(decode_op(p1), p2)))
                elif isn.endswith('-ge'):
                    o1, o2 = p1.split(', ', 1)
                    self.trace(T.blue('if ({0} >= {1}) goto {2}'.format(decode_op(o1), decode_op(o2), p2)))
                elif isn.endswith('-lt'):
                    o1, o2 = p1.split(', ', 1)
                    self.trace(T.blue('if ({0} < {1}) goto {2}'.format(decode_op(o1), decode_op(o2), p2)))
                else:
                    raise NotImplementedError
                    #trace(level * '  ' + repr(m.groups()))
            elif isn == 'aput-byte':
                value, array = p1.split(', ', 1)
                self.trace(T.blue('{array}[{index}] = {value}'.format(array=array, value=value, index=p2)))
            else:
                raise NotImplementedError
                #trace(level * '  ' + repr(m.groups()))
            if local_variables and last_lv != local_variables:
                self.trace(T.green(repr(local_variables)))
                last_lv = local_variables.copy()
            #self.trace('')
        # TODO m.end()


if __name__ == '__main__':
    from sys import argv
    Tracer().trace_fun(argv[1])
