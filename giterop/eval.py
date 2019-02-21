"""
Public Api:

mapValue - returns a copy of the given value resolving any embedded queries or template strings
Ref.resolve given an expression, returns a ResultList
Ref.resolveOne given an expression, return value, none or a (regular) list
Ref.isRef return true if the given diction looks like a Ref

Internal:
evalRef() given expression (string or dictionary) return list of Results
Expr.resolve() given expression string, return list of Results
Results._mapValue same as mapValue but with lazily evaluation
"""
import six
import re
import operator
import collections
from collections import Mapping, MutableSequence
from ruamel.yaml.comments import CommentedMap
from .util import validateSchema, assertForm #, GitErOpError
from .result import ResultsList, Result, Results, ExternalValue, ResourceRef

def runTemplate(data, vars=None, dataLoader=None):
  from ansible.template import Templar
  from ansible.parsing.dataloader import DataLoader
  # implementation notes:
  #   see https://github.com/ansible/ansible/test/units/template/test_templar.py
  #   see ansible.template.vars.AnsibleJ2Vars,
  #   dataLoader is only used by _lookup and to set _basedir (else ./)
  return Templar(dataLoader or DataLoader(), variables=vars).template(data)

def applyTemplate(value, ctx):
  templar = ctx.currentResource.templar
  vars = dict(__giterop = ctx)
  vars.update(ctx.vars)
  templar.set_available_variables(vars)
  value = templar.template(value)
  if Ref.isRef(value):
    value = Ref(value).resolveOne(ctx)
  return value

def mapValue(value, resourceOrCxt):
  if not isinstance(resourceOrCxt, RefContext):
    resourceOrCxt = RefContext(resourceOrCxt)
  return _mapValue(value, resourceOrCxt)

def _mapValue(value, ctx):
  if Ref.isRef(value):
    value = Ref(value).resolveOne(ctx)

  if isinstance(value, Mapping):
    return dict((key, _mapValue(v, ctx)) for key, v in value.items())
  elif isinstance(value, (MutableSequence, tuple)):
    return [_mapValue(item, ctx) for item in value]
  elif isinstance(value, six.string_types):
    return applyTemplate(value, ctx)
  return value

class RefContext(object):
  def __init__(self, currentResource, vars=None, wantList=False, resolveExternal=False, trace=0):
    self.vars = vars or {}
    # the original context:
    self.currentResource = currentResource
    # the last resource encountered while evaluating:
    self._lastResource = currentResource
    # current segment is the final segment:
    self._rest = None
    self.wantList = wantList
    self.resolveExternal = resolveExternal
    self._trace = trace

  def copy(self, resource=None, vars=None, wantList=None):
    copy = RefContext(resource or self.currentResource, self.vars, self.wantList, self.resolveExternal, self._trace)
    if vars:
      copy.vars.update(vars)
    if wantList is not None:
      copy.wantList = wantList
    return copy

  def trace(self, *msg):
    if self._trace:
      print("%s (ctx: %s)" % (' '.join(str(a) for a in msg), self._lastResource))

class Expr(object):
  def __init__(self, exp, vars = None):
    self.vars = {
     'true': True, 'false': False, 'null': None
    }

    if vars:
      self.vars.update(vars)

    self.source = exp
    paths = list(parseExp(exp))
    if 'break' not in self.vars:
      # hack to check that we aren't a foreach expression
      if (not paths[0].key or paths[0].key[0] not in '.$') and (paths[0].key or paths[0].filters):
        # unspecified relative path: prepend a segment to select ancestors
        # and have the first segment only choose the first match
        # note: the first match modifier is set on the first segment and not .ancestors
        # because .ancestors is evaluated against the initial context resource and so
        # its first match is always going to be the result of the whole query (since the initial context is just one item)
        paths[:1] = [Segment('.ancestors', [], '', []), paths[0]._replace(modifier='?')]
    self.paths = paths

  def __repr__(self):
    # XXX vars
    return "Expr('%s')" % self.source

  def resolve(self, ctx):
    # returns a list of Results
    currentResource = ctx.currentResource
    # use Results._mapValues because we don't want to resolve ExternalValues
    vars = Results._mapValue(self.vars, currentResource)
    vars['start'] = currentResource
    context = ctx.copy(currentResource, vars)
    if not self.paths[0].key and not self.paths[0].filters: # starts with "::"
      currentResource = currentResource.all
      paths = self.paths[1:]
    elif self.paths[0].key and self.paths[0].key[0] == '$':
      #if starts with a var, use that as the start
      varName = self.paths[0].key[1:]
      currentResource = context.vars[varName]
      if len(self.paths) == 1:
        # bare reference to a var, just return it's value
        return [Result(currentResource)]
      paths = [self.paths[0]._replace(key='')] + self.paths[1:]
    else:
      paths = self.paths
    return evalExp([currentResource], paths, context)

class Ref(object):
  """
  A Ref objects describes a path to metadata associated with a resource.

  The syntax for a Ref path expression is:

  expr:  segment? ('::' segment)*

  segment: key? ('[' filter ']')* '?'?

  key: name | integer | var | '*'

  filter: '!'? expr? (('!=' | '=') test)?

  test: var | (^[$[]:?])*

  var: '$' name

  Semantics

  Each segment specifies a key in a resource or JSON/YAML object.
  "::" is used as the segment deliminated to allow for keys that contain "." and "/"

  Path expressions evaluations always start with a list of one or more Resources.
  and each segment selects the value associated with that key.
  If segment has one or more filters
  each filter is applied to that value -- each is treated as a predicate
  that decides whether value is included or not in the results.
  If the filter doesn't include a test the filter tests the existence or non-existence of the expression,
  depending on whether the expression is prefixed with a "!".
  If the filter includes a test the left side of the test needs to match the right side.
  If the right side is not a variable, that string will be coerced to left side's type before comparing it.
  If the left-side expression is omitted, the value of the segment's key is used and if that is missing, the current value is used.

  If the current value is a list and the key looks like an integer
  it will be treated like a zero-based index into the list.
  Otherwise the segment is evaluated again all values in the list and resulting value is a list.
  If the current value is a dictionary and the key is "*", all values will be selected.

  If a segment ends in "?", it will only include the first match.
  In other words, "a?::b::c" is a shorthand for "a[b::c]::0::b::c".
  This is useful to guarantee the result of evaluating expression is always a single result.

  The first segment:
  If the first segment is a variable reference the current value is set to that variable's value.
  If the key in the first segment is empty (e.g. the expression starts with '::') the current value will be set to the evaluation of '.all'.
  If the key in the first segment starts with '.' it is evaluated against the initial "current resource".
  Otherwise, the current value is set to the evaluation of ".ancestors?". In other words,
  the expression will be the result of evaluating it against the first ancestor of the current resource that it matches.

  If key or test needs to be a non-string type or contains a unallowed character use a var reference instead.

  When multiple steps resolve to lists the resultant lists are flattened.
  However if the final set of matches contain values that are lists those values are not flattened.

  For example, given:

  {x: [ {
          a: [{c:1}, {c:2}]
        },
        {
          a: [{c:3}, {c:4}]
        }
      ]
  }

  x:a:c resolves to:
    [1,2,3,4]
  not
    [[1,2], [3,4]])

  (Justification: It is inconvenient and fragile to tie data structures to the particular form of a query.
  If you want preserve structure (e.g. to know which values are part
  of which parent value or resource) use a less deep path and iterate over results.)

  Resources have a special set of keys:

  .            self
  ..           parent
  .parents     list of parents
  .ancestors   self and parents
  .root        root ancestor
  .children    child resources
  .descendents (including self)
  .all       dictionary of child resources with their names as keys
  .configurations
  """

  def __init__(self, exp, vars = None):
    self.vars = {
     'true': True, 'false': False, 'null': None
    }

    self.foreach = None
    if isinstance(exp, Mapping):
      if 'q' in exp:
        self.source = exp
        return

      self.vars.update(exp.get('vars', {}))
      self.foreach = exp.get('foreach')
      exp = exp.get('eval', exp.get('ref',''))

    if vars:
      self.vars.update(vars)
    self.source = exp

  def resolve(self, ctx, wantList=True):
    """
    Return a ResultList of matches
    Note that values in the list can be a list or None
    """
    ctx = ctx.copy(vars=self.vars, wantList=wantList)
    results = evalRef(self.source, ctx, True)
    if results and self.foreach:
      results = forEach(self.foreach, results, ctx)
    assert not isinstance(results, ResultsList), results
    results = ResultsList(results, ctx)
    ctx.trace('Ref.resolve(wantList=%s)' % wantList, self.source, results)
    if wantList and not wantList == 'result':
      return results
    else:
      if not results:
        return None
      elif len(results) == 1:
        if wantList == 'result':
          return results._attributes[0]
        else:
          return results[0]
      else:
        if wantList == 'result':
          return Result(results)
        else:
          return list(results)

  def resolveOne(self, ctx):
    """
    If no match return None
    If more than one match return a list of matches
    Otherwise return the match

    Note: If you want to distinguish between None values and no match
    or between single match that is a list and a list of matches
    use resolve() which always returns a (possible empty) of matches
    """
    return self.resolve(ctx, False)

  @staticmethod
  def isRef(value):
    if isinstance(value, Mapping):
      if 'ref' in value or 'eval' in value:
        return len([x for x in ['vars', 'foreach'] if x in value]) + 1 == len(value)
      if 'q' in value:
        return len(value) == 1
      return False
    return isinstance(value, Ref)

def evalAsBoolean(arg, ctx):
  result = evalRef(arg, ctx)
  return not not result[0].resolved if result else False

def ifFunc(arg, ctx):
  kw = ctx.kw
  result = evalAsBoolean(arg, ctx)
  if result:
    return evalForFunc(kw.get('then'), ctx)
  else:
    return evalForFunc(kw.get('else'), ctx)

def orFunc(arg, ctx):
  args = evalForFunc(arg, ctx)
  assert isinstance(args, MutableSequence), args
  for arg in args:
    val = evalForFunc(arg, ctx)
    if val:
      return val

def notFunc(arg, ctx):
  result = evalAsBoolean(arg, ctx)
  return not result

def andFunc(arg, ctx):
  args = evalForFunc(arg, ctx)
  assert isinstance(args, MutableSequence)
  for arg in args:
    val = evalForFunc(arg, ctx)
    if not val:
      return val
  return val

def quoteFunc(arg, ctx):
  return arg

def eqFunc(arg, ctx):
  args = evalForFunc(arg, ctx)
  assert isinstance(args, MutableSequence) and len(args) == 2
  return evalRef(args[0], ctx) == evalRef(args[1], ctx)

def validateSchemaFunc(arg, ctx):
  args = evalForFunc(arg, ctx)
  assert isinstance(args, MutableSequence) and len(args) == 2
  return validateSchema(evalForFunc(args[0], ctx), evalForFunc(args[1], ctx))

def _forEach(foreach, results, ctx):
  if isinstance(foreach, six.string_types):
    keyExp = None
    valExp = foreach
  else:
    keyExp = foreach.get('key')
    valExp = foreach.get('value')
    if not valExp and not keyExp:
      valExp = foreach

  ictx = ctx.copy(wantList=False)
  # ictx._trace = 1
  Break = object()
  Continue = object()
  def makeItems():
    for i, (k, v) in enumerate(results):
      ictx.currentResource = v
      ictx.vars['collection'] =  results
      ictx.vars['index'] = i
      ictx.vars['key'] = k
      ictx.vars['item'] = v
      ictx.vars['break'] = Break
      ictx.vars['continue'] = Continue
      if keyExp:
        key = evalForFunc(keyExp, ictx)
        if key is Break:
          break
        elif key is Continue:
          continue
      val = evalForFunc(valExp, ictx)
      if val is Break:
        break
      elif val is Continue:
        continue
      if keyExp:
        yield (key, val)
      else:
        yield val

  if keyExp:
    return [CommentedMap(makeItems())]
  else:
    return list(makeItems())

def forEach(foreach, results, ctx):
  # results will be list of Results
  return _forEach(foreach, enumerate(r.external or r.resolved for r in results), ctx)

def forEachFunc(foreach, ctx):
  results = ctx.currentResource
  if results:
    if isinstance(results, Mapping):
      return _forEach(foreach, results.items(), ctx)
    elif isinstance(results, MutableSequence):
      return _forEach(foreach, enumerate(results), ctx)
    else:
      return _forEach(foreach, [(0, results)], ctx)
  else:
    return results

_Funcs = {
  'if': ifFunc,
  'and': andFunc,
  'or': orFunc,
  'not': notFunc,
  'q': quoteFunc,
  'eq': eqFunc,
  'validate': validateSchemaFunc,
  'template': applyTemplate,
  'foreach': forEachFunc,
}

def getEvalFunc(name):
  return _Funcs.get(name)

def setEvalFunc(name, val):
  _Funcs[name] = val

# returns list of results
def evalRef(val, ctx, top=False):
  # functions and ResultsMap assume resolveOne semantics
  if top:
    ctx = ctx.copy(wantList = False)
  if isinstance(val, Mapping):
    for key in val:
      func = _Funcs.get(key)
      if func:
        args = val[key]
        ctx.kw = val
        ctx.currentFunc = key
        val = func(args, ctx)
        if key == 'q':
          return [Result(val)]
        break
  elif isinstance(val, six.string_types):
    expr = Expr(val, ctx.vars)
    results = expr.resolve(ctx)
    return results

  return [Result(Results._mapValue(val, ctx))]

def evalForFunc(val, ctx):
  results = evalRef(val, ctx)
  if not results:
    return None
  if len(results) == 1:
    return results[0].resolved
  else:
    return [r.resolved for r in results]

#return a segment
Segment = collections.namedtuple('Segment', ['key', 'test', 'modifier', 'filters'])
defaultSegment = Segment('', [], '', [])

def evalTest(value, test, context):
  comparor = test[0]
  key = test[1]
  try:
    if context and isinstance(key, six.string_types) and key.startswith('$'):
      compare = context.vars[key[1:]]
    else:
      # try to coerce string to value type
      compare = type(value)(key)
    context.trace('compare', value, compare, comparor(value, compare))
    if comparor(value, compare):
      return True
  except:
    context.trace('compare exception, ne:',  comparor is operator.ne)
    if comparor is operator.ne:
      return True
  return False

# given a Result and a key, return None or new Result
def lookup(result, key, context):
  try:
    # if key == '.':
    #   key = context.currentKey
    if context and isinstance(key, six.string_types) and key.startswith('$'):
      key = context.vars[key[1:]]

    if isinstance(result.resolved, ResourceRef):
      context._lastResource = result.resolved

    ctx = context.copy(context._lastResource)
    result = result.project(key, ctx)
    value = result.resolved
    context.trace('lookup %s, got %s' % (key, value))

    if not context._rest:
      assert not Ref.isRef(value)
      result.resolved = Results._mapValue(value, ctx)
      assert not isinstance(result.resolved, (ExternalValue, Result))

    return result
  except (KeyError, IndexError, TypeError, ValueError):
    if context._trace:
      context.trace('lookup return None due to exception:')
      import traceback
      traceback.print_exc()
    return None

# given a Result, yields the result
def evalItem(result, seg, context):
  """
    apply current item to current segment, return [] or [value]
  """
  if seg.key != '':
    result = lookup(result, seg.key, context)
    if not result:
      return

  value = result.resolved
  for filter in seg.filters:
    if _treatAsSingular(result, filter[0]):
      resultList = [value]
    else:
      resultList = value
    results = evalExp(resultList, filter, context)
    negate = filter[0].modifier == '!'
    if negate and results:
      return
    elif not negate and not results:
      return

  if seg.test and not evalTest(value, seg.test, context):
    return
  assert isinstance(result, Result), result
  yield result

def _treatAsSingular(result, seg):
  if seg.key == '*':
    return False
  # treat external values as single item even if they resolve to a list
  # treat lists as a single item if indexing into it
  return result.external or not isinstance(result.resolved, MutableSequence) or isinstance(seg.key, six.integer_types)

def recursiveEval(v, exp, context):
  """
  given a iterator of (previous) results,
  yields Results
  """
  context.trace('recursive evaluating', exp)
  matchFirst = exp[0].modifier == '?'
  useValue = exp[0].key == '*'
  for result in v:
    assert isinstance(result, Result), result
    item = result.resolved

    if _treatAsSingular(result, exp[0]):
      rest = exp[1:]
      context._rest = rest
      context.trace('evaluating item %s with key %s' % (item, exp[0].key))
      iv = evalItem(result, exp[0], context) # returns a generator that yields up to one result
    else:
      iv = result._values()
      if useValue:
        if not isinstance(item, Mapping):
          context.trace('* is skipping', item)
          continue
        rest = exp[1:] # advance past "*" segment
      else:
        # flattens
        rest = exp
        context.trace('flattening', item)

    # iv will be a generator or list
    if rest:
      results = recursiveEval(iv, rest, context)
      found = False
      for r in results:
        found = True
        context.trace('recursive result', r)
        assert isinstance(r, Result)
        yield r
      context.trace('found recursive %s matchFirst: %s' % (found, matchFirst))
      if found and matchFirst:
        return
    else:
      for r in iv:
        assert isinstance(r, Result)
        yield r
        if matchFirst:
          return

def evalExp(start, paths, context):
  "Returns a list of Results"
  context.trace('evalexp', start, paths)
  assert isinstance(start, MutableSequence), start
  return list(recursiveEval((Result(i) for i in start), paths, context))

def _makeKey(key):
  try:
    return int(key)
  except ValueError:
    return key

def parsePathKey(segment):
  #key, negation, test, matchFirst
  if not segment:
    return defaultSegment

  modifier = ''
  if segment[0] == '!':
    segment = segment[1:]
    modifier = '!'
  elif segment[-1] == '?':
    segment = segment[:-1]
    modifier = '?'

  parts = re.split(r'(=|!=)', segment, 1)
  if len(parts) == 3:
    key = parts[0]
    op = operator.eq if parts[1] == '=' else operator.ne
    return Segment(_makeKey(key), [op, parts[2]], modifier, [])
  else:
    return Segment(_makeKey(segment), [], modifier, [])

def parsePath(path, start):
  paths = path.split('::')
  segments = [parsePathKey(k.strip()) for k in paths]
  if start:
    if paths and paths[0]:
      # if the path didn't start with ':' merge with the last segment
      # e.g. foo[]? d=test[d]?
      segments[0] = start._replace(test=segments[0].test or start.test,
                    modifier=segments[0].modifier or start.modifier)
    else:
      return [start] + segments
  return segments

def parseExp(exp):
  #return list of steps
  rest = exp
  last = None

  while rest:
    steps, rest = parseStep(rest, last)
    last = None
    if steps:
      #we might need merge the next step into the last
      last = steps.pop()
      for step in steps:
        yield step

  if last:
    yield last

def parseStep(exp, start=None):
  split = re.split(r'(\[|\])', exp, 1)
  if len(split) == 1: #not found
    return parsePath(split[0], start), ''
  else:
    path, sep, rest = split

  paths = parsePath(path, start)

  filterExps = []
  while sep == '[':
    filterExp, rest = parseStep(rest)
    filterExps.append(filterExp)
    #rest will be anything after ]
    sep = rest and rest[0]

  #add filterExps to last Segment
  paths[-1] = paths[-1]._replace(filters = filterExps)
  return paths, rest

def runLookup(name, templar, *args, **kw):
  from ansible.plugins.loader import lookup_loader
  # "{{ lookup('url', 'https://toshio.fedorapeople.org/one.txt', validate_certs=True) }}"
  #       would end up calling the lookup plugin named url's run method like this::
  #           run(['https://toshio.fedorapeople.org/one.txt'], variables=available_variables, validate_certs=True)
  instance = lookup_loader.get(name, loader = templar._loader, templar = templar)
  # ansible_search_path = []
  result = instance.run(args, variables=templar._available_variables, **kw)
  # XXX check for wantList
  if not result:
    return None
  if len(result) == 1:
    return result[0]
  else:
    return result

def lookupFunc(arg, ctx):
  """
  lookup:
    - file: 'foo' or []
    - kw1: value
    - kw2: value
  """
  # XXX arg = mapValue(arg, ctx)
  if isinstance(arg, Mapping):
    assert len(arg) == 1
    name, args = list(arg.items())[0]
    kw = {}
  else:
    assertForm(arg, list)
    name, args = list(assertForm(arg[0]).items())[0]
    kw = dict(list(assertForm(kw).items())[0] for kw in arg[1:])

  if not isinstance(args, MutableSequence):
    args = [args]
  return runLookup(name, ctx.currentResource.templar, *args, **kw)

_Funcs['lookup'] = lookupFunc
