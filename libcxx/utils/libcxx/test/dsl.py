#===----------------------------------------------------------------------===##
#
# Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
#===----------------------------------------------------------------------===##

import distutils.util
import libcxx.test.newformat
import lit
import lit.util
import os
import pipes
import subprocess
import tempfile

def _memoize(f):
  cache = dict()
  def memoized(x):
    if x not in cache:
      cache[x] = f(x)
    return cache[x]
  return memoized

@_memoize
def _subprocess_call(command):
  devNull = open(os.devnull, 'w')
  return subprocess.call(command, shell=True, stdout=devNull, stderr=devNull)

@_memoize
def _subprocess_check_output(command):
  devNull = open(os.devnull, 'w')
  return lit.util.to_string(subprocess.check_output(command, shell=True, stderr=devNull))

def _makeConfigTest(config):
  sourceRoot = os.path.join(config.test_exec_root, '__config_src__')
  execRoot = os.path.join(config.test_exec_root, '__config_exec__')
  suite = lit.Test.TestSuite('__config__', sourceRoot, execRoot, config)
  if not os.path.exists(sourceRoot):
    os.makedirs(sourceRoot)
  tmp = tempfile.NamedTemporaryFile(dir=sourceRoot, delete=False)
  pathInSuite = [os.path.relpath(tmp.name, sourceRoot)]
  class TestWrapper(lit.Test.Test):
    def __enter__(self):       return self
    def __exit__(self, *args): os.remove(tmp.name)
  return TestWrapper(suite, pathInSuite, config)

def hasCompileFlag(config, flag):
  """
  Return whether the compiler in the configuration supports a given compiler flag.

  This is done by executing the %{cxx} substitution with the given flag and
  checking whether that succeeds.
  """
  with _makeConfigTest(config) as test:
    command = "%{{cxx}} -xc++ {} -Werror -fsyntax-only %{{flags}} %{{compile_flags}} {}".format(os.devnull, flag)
    command = libcxx.test.newformat.parseScript(test, preamble=[command], fileDependencies=[])[0]
    result = _subprocess_call(command)
    return result == 0

def hasLocale(config, locale):
  """
  Return whether the runtime execution environment supports a given locale.

  This is done by executing a program that tries to set the given locale using
  %{exec} -- this means that the command may be executed on a remote host
  depending on the %{exec} substitution.
  """
  with _makeConfigTest(config) as test:
    with open(test.getSourcePath(), 'w') as source:
      source.write("""
      #include <locale.h>
      int main(int, char** argv) {{
        if (::setlocale(LC_ALL, argv[1]) != NULL) return 0;
        else                                      return 1;
      }}
      """)
    commands = [
      "mkdir -p %T",
      "%{cxx} -xc++ %s %{flags} %{compile_flags} %{link_flags} -o %t.exe",
      "%{{exec}} %t.exe {}".format(pipes.quote(locale)),
    ]
    commands = libcxx.test.newformat.parseScript(test, preamble=commands, fileDependencies=['%t.exe'])
    result = _subprocess_call(' && '.join(commands))
    cleanup = libcxx.test.newformat.parseScript(test, preamble=['rm %t.exe'], fileDependencies=[])[0]
    _subprocess_call(cleanup)
    return result == 0

def compilerMacros(config, flags=''):
  """
  Return a dictionary of predefined compiler macros.

  The keys are strings representing macros, and the values are strings
  representing what each macro is defined to.

  If the optional `flags` argument (a string) is provided, these flags will
  be added to the compiler invocation when generating the macros.
  """
  with _makeConfigTest(config) as test:
    command = "%{{cxx}} -xc++ {} -dM -E %{{flags}} %{{compile_flags}} {}".format(os.devnull, flags)
    command = libcxx.test.newformat.parseScript(test, preamble=[command], fileDependencies=[])[0]
    unparsed = _subprocess_check_output(command)
    parsedMacros = dict()
    for line in filter(None, map(str.strip, unparsed.split('\n'))):
      assert line.startswith('#define ')
      line = line[len('#define '):]
      macro, _, value = line.partition(' ')
      parsedMacros[macro] = value
    return parsedMacros

def featureTestMacros(config, flags=''):
  """
  Return a dictionary of feature test macros.

  The keys are strings representing feature test macros, and the values are
  integers representing the value of the macro.
  """
  allMacros = compilerMacros(config, flags)
  return {m: int(v.rstrip('LlUu')) for (m, v) in allMacros.items() if m.startswith('__cpp_')}


class Feature(object):
  """
  Represents a Lit available feature that is enabled whenever it is supported.

  A feature like this informs the test suite about a capability of the compiler,
  platform, etc. Unlike Parameters, it does not make sense to explicitly
  control whether a Feature is enabled -- it should be enabled whenever it
  is supported.
  """
  def __init__(self, name, compileFlag=None, linkFlag=None, when=lambda _: True):
    """
    Create a Lit feature for consumption by a test suite.

    - name
        The name of the feature. This is what will end up in Lit's available
        features if the feature is enabled. This can be either a string or a
        callable, in which case it is passed the TestingConfig and should
        generate a string representing the name of the feature.

    - compileFlag
        An optional compile flag to add when this feature is added to a
        TestingConfig. If provided, this must be a string representing a
        compile flag that will be appended to the end of the %{compile_flags}
        substitution of the TestingConfig.

    - linkFlag
        An optional link flag to add when this feature is added to a
        TestingConfig. If provided, this must be a string representing a
        link flag that will be appended to the end of the %{link_flags}
        substitution of the TestingConfig.

    - when
        A callable that gets passed a TestingConfig and should return a
        boolean representing whether the feature is supported in that
        configuration. For example, this can use `hasCompileFlag` to
        check whether the compiler supports the flag that the feature
        represents. If omitted, the feature will always be considered
        supported.
    """
    self._name = name
    self._compileFlag = compileFlag
    self._linkFlag = linkFlag
    self._isSupported = when

  def isSupported(self, config):
    """
    Return whether the feature is supported by the given TestingConfig.
    """
    return self._isSupported(config)

  def enableIn(self, config):
    """
    Enable a feature in a TestingConfig.

    The name of the feature is added to the set of available features of
    `config`, and any compile or link flags provided upon construction of
    the Feature are added to the end of the corresponding substitution in
    the config.

    It is an error to call `f.enableIn(cfg)` if the feature `f` is not
    supported in that TestingConfig (i.e. if `not f.isSupported(cfg)`).
    """
    assert self.isSupported(config), \
      "Trying to enable feature {} that is not supported in the given configuration".format(self._name)

    addTo = lambda subs, sub, flag: [(s, x + ' ' + flag) if s == sub else (s, x) for (s, x) in subs]
    if self._compileFlag:
      config.substitutions = addTo(config.substitutions, '%{compile_flags}', self._compileFlag)
    if self._linkFlag:
      config.substitutions = addTo(config.substitutions, '%{link_flags}', self._linkFlag)

    name = self._name(config) if callable(self._name) else self._name
    config.available_features.add(name)


class Parameter(object):
  """
  Represents a parameter of a Lit test suite.

  Parameters are used to customize the behavior of test suites in a user
  controllable way, more specifically by passing `--param <KEY>=<VALUE>`
  when running Lit. Parameters have multiple possible values, and they can
  have a default value when left unspecified.

  Parameters can have a Feature associated to them, in which case the Feature
  is added to the TestingConfig if the parameter is enabled. It is an error if
  the Parameter is enabled but the Feature associated to it is not supported,
  for example trying to set the compilation standard to C++17 when `-std=c++17`
  is not supported by the compiler.

  One important point is that Parameters customize the behavior of the test
  suite in a bounded way, i.e. there should be a finite set of possible choices
  for `<VALUE>`. While this may appear to be an aggressive restriction, this
  is actually a very important constraint that ensures that the set of
  configurations supported by a test suite is finite. Otherwise, a test
  suite could have an unbounded number of supported configurations, and
  nobody wants to be stuck maintaining that. If it's not possible for an
  option to have a finite set of possible values (e.g. the path to the
  compiler), it can be handled in the `lit.cfg`, but it shouldn't be
  represented with a Parameter.
  """
  def __init__(self, name, choices, type, help, feature, default=None):
    """
    Create a Lit parameter to customize the behavior of a test suite.

    - name
        The name of the parameter that can be used to set it on the command-line.
        On the command-line, the parameter can be set using `--param <name>=<value>`
        when running Lit. This must be non-empty.

    - choices
        A non-empty set of possible values for this parameter. This must be
        anything that can be iterated. It is an error if the parameter is
        given a value that is not in that set, whether explicitly or through
        a default value.

    - type
        A callable that can be used to parse the value of the parameter given
        on the command-line. As a special case, using the type `bool` also
        allows parsing strings with boolean-like contents.

    - help
        A string explaining the parameter, for documentation purposes.
        TODO: We should be able to surface those from the Lit command-line.

    - feature
        A callable that gets passed the parsed value of the parameter (either
        the one passed on the command-line or the default one), and that returns
        either None or a Feature.

    - default
        An optional default value to use for the parameter when no value is
        provided on the command-line. If the default value is a callable, it
        is called with the TestingConfig and should return the default value
        for the parameter. Whether the default value is computed or specified
        directly, it must be in the 'choices' provided for that Parameter.
    """
    self._name = name
    if len(self._name) == 0:
      raise ValueError("Parameter name must not be the empty string")

    self._choices = list(choices) # should be finite
    if len(self._choices) == 0:
      raise ValueError("Parameter '{}' must be given at least one possible value".format(self._name))

    self._parse = lambda x: (distutils.util.strtobool(x) if type is bool and isinstance(x, str)
                                                         else type(x))
    self._help = help
    self._feature = feature
    self._default = default

  @property
  def name(self):
    """
    Return the name of the parameter.

    This is the name that can be used to set the parameter on the command-line
    when running Lit.
    """
    return self._name

  def getFeature(self, config, litParams):
    param = litParams.get(self.name, None)
    if param is None and self._default is None:
      raise ValueError("Parameter {} doesn't have a default value, but it was not specified in the Lit parameters".format(self.name))
    getDefault = lambda: self._default(config) if callable(self._default) else self._default
    value = self._parse(param) if param is not None else getDefault()
    if value not in self._choices:
      raise ValueError("Got value '{}' for parameter '{}', which is not in the provided set of possible choices: {}".format(value, self.name, self._choices))
    return self._feature(value)
