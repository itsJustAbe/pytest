"""
PluginManager, basic initialization and tracing.
"""
import sys
from inspect import isfunction, ismethod, isclass, formatargspec, getargspec
import py

py3 = sys.version_info > (3,0)

def hookspec_opts(firstresult=False, historic=False):
    """ returns a decorator which will define a function as a hook specfication.

    If firstresult is True the 1:N hook call (N being the number of registered
    hook implementation functions) will stop at I<=N when the I'th function
    returns a non-None result.

    If historic is True calls to a hook will be memorized and replayed
    on later registered plugins.
    """
    def setattr_hookspec_opts(func):
        if historic and firstresult:
            raise ValueError("cannot have a historic firstresult hook")
        if firstresult:
            func.firstresult = firstresult
        if historic:
            func.historic = historic
        return func
    return setattr_hookspec_opts


def hookimpl_opts(hookwrapper=False, optionalhook=False,
                  tryfirst=False, trylast=False):
    """ Return a decorator which marks a function as a hook implementation.

    If optionalhook is True a missing matching hook specification will not result
    in an error (by default it is an error if no matching spec is found).

    If tryfirst is True this hook implementation will run as early as possible
    in the chain of N hook implementations for a specfication.

    If trylast is True this hook implementation will run as late as possible
    in the chain of N hook implementations.

    If hookwrapper is True the hook implementations needs to execute exactly
    one "yield".  The code before the yield is run early before any non-hookwrapper
    function is run.  The code after the yield is run after all non-hookwrapper
    function have run.  The yield receives an ``CallOutcome`` object representing
    the exception or result outcome of the inner calls (including other hookwrapper
    calls).
    """
    def setattr_hookimpl_opts(func):
        if hookwrapper:
            func.hookwrapper = True
        if optionalhook:
            func.optionalhook = True
        if tryfirst:
            func.tryfirst = True
        if trylast:
            func.trylast = True
        return func
    return setattr_hookimpl_opts


class TagTracer:
    def __init__(self):
        self._tag2proc = {}
        self.writer = None
        self.indent = 0

    def get(self, name):
        return TagTracerSub(self, (name,))

    def format_message(self, tags, args):
        if isinstance(args[-1], dict):
            extra = args[-1]
            args = args[:-1]
        else:
            extra = {}

        content = " ".join(map(str, args))
        indent = "  " * self.indent

        lines = [
            "%s%s [%s]\n" %(indent, content, ":".join(tags))
        ]

        for name, value in extra.items():
            lines.append("%s    %s: %s\n" % (indent, name, value))
        return lines

    def processmessage(self, tags, args):
        if self.writer is not None and args:
            lines = self.format_message(tags, args)
            self.writer(''.join(lines))
        try:
            self._tag2proc[tags](tags, args)
        except KeyError:
            pass

    def setwriter(self, writer):
        self.writer = writer

    def setprocessor(self, tags, processor):
        if isinstance(tags, str):
            tags = tuple(tags.split(":"))
        else:
            assert isinstance(tags, tuple)
        self._tag2proc[tags] = processor


class TagTracerSub:
    def __init__(self, root, tags):
        self.root = root
        self.tags = tags
    def __call__(self, *args):
        self.root.processmessage(self.tags, args)
    def setmyprocessor(self, processor):
        self.root.setprocessor(self.tags, processor)
    def get(self, name):
        return self.__class__(self.root, self.tags + (name,))


def raise_wrapfail(wrap_controller, msg):
    co = wrap_controller.gi_code
    raise RuntimeError("wrap_controller at %r %s:%d %s" %
                   (co.co_name, co.co_filename, co.co_firstlineno, msg))

def wrapped_call(wrap_controller, func):
    """ Wrap calling to a function with a generator which needs to yield
    exactly once.  The yield point will trigger calling the wrapped function
    and return its CallOutcome to the yield point.  The generator then needs
    to finish (raise StopIteration) in order for the wrapped call to complete.
    """
    try:
        next(wrap_controller)   # first yield
    except StopIteration:
        raise_wrapfail(wrap_controller, "did not yield")
    call_outcome = CallOutcome(func)
    try:
        wrap_controller.send(call_outcome)
        raise_wrapfail(wrap_controller, "has second yield")
    except StopIteration:
        pass
    return call_outcome.get_result()


class CallOutcome:
    """ Outcome of a function call, either an exception or a proper result.
    Calling the ``get_result`` method will return the result or reraise
    the exception raised when the function was called. """
    excinfo = None
    def __init__(self, func):
        try:
            self.result = func()
        except BaseException:
            self.excinfo = sys.exc_info()

    def force_result(self, result):
        self.result = result
        self.excinfo = None

    def get_result(self):
        if self.excinfo is None:
            return self.result
        else:
            ex = self.excinfo
            if py3:
                raise ex[1].with_traceback(ex[2])
            py.builtin._reraise(*ex)


class TracedHookExecution:
    def __init__(self, pluginmanager, before, after):
        self.pluginmanager = pluginmanager
        self.before = before
        self.after = after
        self.oldcall = pluginmanager._inner_hookexec
        assert not isinstance(self.oldcall, TracedHookExecution)
        self.pluginmanager._inner_hookexec = self

    def __call__(self, hook, methods, kwargs):
        self.before(hook, methods, kwargs)
        outcome = CallOutcome(lambda: self.oldcall(hook, methods, kwargs))
        self.after(outcome, hook, methods, kwargs)
        return outcome.get_result()

    def undo(self):
        self.pluginmanager._inner_hookexec = self.oldcall


class PluginManager(object):
    """ Core Pluginmanager class which manages registration
    of plugin objects and 1:N hook calling.

    You can register new hooks by calling ``addhooks(module_or_class)``.
    You can register plugin objects (which contain hooks) by calling
    ``register(plugin)``.  The Pluginmanager is initialized with a
    prefix that is searched for in the names of the dict of registered
    plugin objects.  An optional excludefunc allows to blacklist names which
    are not considered as hooks despite a matching prefix.

    For debugging purposes you can call ``set_tracing(writer)``
    which will subsequently send debug information to the specified
    write function.
    """

    def __init__(self, prefix, excludefunc=None):
        self._prefix = prefix
        self._excludefunc = excludefunc
        self._name2plugin = {}
        self._plugins = []
        self._plugin2hookcallers = {}
        self.trace = TagTracer().get("pluginmanage")
        self.hook = HookRelay(self.trace.root.get("hook"))
        self._inner_hookexec = lambda hook, methods, kwargs: \
                               MultiCall(methods, kwargs, hook.firstresult).execute()

    def _hookexec(self, hook, methods, kwargs):
        return self._inner_hookexec(hook, methods, kwargs)

    def enable_tracing(self):
        """ enable tracing of hook calls and return an undo function. """
        hooktrace = self.hook._trace

        def before(hook, methods, kwargs):
            hooktrace.root.indent += 1
            hooktrace(hook.name, kwargs)

        def after(outcome, hook, methods, kwargs):
            if outcome.excinfo is None:
                hooktrace("finish", hook.name, "-->", outcome.result)
            hooktrace.root.indent -= 1

        return TracedHookExecution(self, before, after).undo

    def make_hook_caller(self, name, plugins):
        caller = getattr(self.hook, name)
        hc = HookCaller(caller.name, self._hookexec, caller._specmodule_or_class)
        for plugin in plugins:
            if hasattr(plugin, name):
                hc._add_plugin(plugin)
                # we also keep track of this hook caller so it
                # gets properly removed on plugin unregistration
                self._plugin2hookcallers.setdefault(plugin, []).append(hc)
        return hc

    def register(self, plugin, name=None):
        """ Register a plugin with the given name and ensure that all its
        hook implementations are integrated.  If the name is not specified
        we use the ``__name__`` attribute of the plugin object or, if that
        doesn't exist, the id of the plugin.  This method will raise a
        ValueError if the eventual name is already registered. """
        name = name or self._get_canonical_name(plugin)
        if self._name2plugin.get(name, None) == -1:
            return
        if self.hasplugin(name):
            raise ValueError("Plugin already registered: %s=%s\n%s" %(
                              name, plugin, self._name2plugin))
        self._name2plugin[name] = plugin
        self._scan_plugin(plugin)
        self._plugins.append(plugin)
        return True

    def unregister(self, plugin):
        """ unregister the plugin object and all its contained hook implementations
        from internal data structures. """
        self._plugins.remove(plugin)
        for name, value in list(self._name2plugin.items()):
            if value == plugin:
                del self._name2plugin[name]
        for hookcaller in self._plugin2hookcallers.pop(plugin):
            hookcaller._remove_plugin(plugin)

    def addhooks(self, module_or_class):
        """ add new hook definitions from the given module_or_class using
        the prefix/excludefunc with which the PluginManager was initialized. """
        names = []
        for name in dir(module_or_class):
            if name.startswith(self._prefix):
                hc = getattr(self.hook, name, None)
                if hc is None:
                    hc = HookCaller(name, self._hookexec, module_or_class)
                    setattr(self.hook, name, hc)
                else:
                    # plugins registered this hook without knowing the spec
                    hc.set_specification(module_or_class)
                    for plugin in hc._plugins:
                        self._verify_hook(hc, plugin)
                names.append(name)
        if not names:
            raise ValueError("did not find new %r hooks in %r"
                             %(self._prefix, module_or_class))

    def getplugins(self):
        """ return the complete list of registered plugins. NOTE that
        you will get the internal list and need to make a copy if you
        modify the list."""
        return self._plugins

    def isregistered(self, plugin):
        """ Return True if the plugin is already registered under its
        canonical name. """
        return self.hasplugin(self._get_canonical_name(plugin)) or \
               plugin in self._plugins

    def hasplugin(self, name):
        """ Return True if there is a registered with the given name. """
        return name in self._name2plugin

    def getplugin(self, name):
        """ Return a plugin or None for the given name. """
        return self._name2plugin.get(name)

    def _scan_plugin(self, plugin):
        self._plugin2hookcallers[plugin] = hookcallers = []
        for name in dir(plugin):
            if name[0] == "_" or not name.startswith(self._prefix):
                continue
            hook = getattr(self.hook, name, None)
            method = getattr(plugin, name)
            if hook is None:
                if self._excludefunc is not None and self._excludefunc(name):
                    continue
                hook = HookCaller(name, self._hookexec)
                setattr(self.hook, name, hook)
            elif hook.has_spec():
                self._verify_hook(hook, plugin)
                hook._apply_history(method)
            hookcallers.append(hook)
            hook._add_plugin(plugin)

    def _verify_hook(self, hook, plugin):
        method = getattr(plugin, hook.name)
        for arg in varnames(method):
            if arg not in hook.argnames:
                pluginname = self._get_canonical_name(plugin)
                raise PluginValidationError(
                    "Plugin %r\nhook %r\nargument %r not available\n"
                     "plugin definition: %s\n"
                     "available hookargs: %s" %(
                     pluginname, hook.name, arg, formatdef(method),
                       ", ".join(hook.argnames)))

    def check_pending(self):
        for name in self.hook.__dict__:
            if name.startswith(self._prefix):
                hook = getattr(self.hook, name)
                if not hook.has_spec():
                    for plugin in hook._plugins:
                        method = getattr(plugin, hook.name)
                        if not getattr(method, "optionalhook", False):
                            raise PluginValidationError(
                                "unknown hook %r in plugin %r" %(name, plugin))

    def _get_canonical_name(self, plugin):
        return getattr(plugin, "__name__", None) or str(id(plugin))



class MultiCall:
    """ execute a call into multiple python functions/methods. """

    def __init__(self, methods, kwargs, firstresult=False):
        self.methods = methods
        self.kwargs = kwargs
        self.kwargs["__multicall__"] = self
        self.firstresult = firstresult

    def execute(self):
        all_kwargs = self.kwargs
        self.results = results = []
        while self.methods:
            method = self.methods.pop()
            args = [all_kwargs[argname] for argname in varnames(method)]
            if hasattr(method, "hookwrapper"):
                return wrapped_call(method(*args), self.execute)
            res = method(*args)
            if res is not None:
                if self.firstresult:
                    return res
                results.append(res)
        if not self.firstresult:
            return results

    def __repr__(self):
        status = "%d meths" % (len(self.methods),)
        if hasattr(self, "results"):
            status = ("%d results, " % len(self.results)) + status
        return "<MultiCall %s, kwargs=%r>" %(status, self.kwargs)



def varnames(func, startindex=None):
    """ return argument name tuple for a function, method, class or callable.

    In case of a class, its "__init__" method is considered.
    For methods the "self" parameter is not included unless you are passing
    an unbound method with Python3 (which has no supports for unbound methods)
    """
    cache = getattr(func, "__dict__", {})
    try:
        return cache["_varnames"]
    except KeyError:
        pass
    if isclass(func):
        try:
            func = func.__init__
        except AttributeError:
            return ()
        startindex = 1
    else:
        if not isfunction(func) and not ismethod(func):
            func = getattr(func, '__call__', func)
        if startindex is None:
            startindex = int(ismethod(func))

    rawcode = py.code.getrawcode(func)
    try:
        x = rawcode.co_varnames[startindex:rawcode.co_argcount]
    except AttributeError:
        x = ()
    else:
        defaults = func.__defaults__
        if defaults:
            x = x[:-len(defaults)]
    try:
        cache["_varnames"] = x
    except TypeError:
        pass
    return x


class HookRelay:
    def __init__(self, trace):
        self._trace = trace


class HookCaller(object):
    def __init__(self, name, hook_execute, specmodule_or_class=None):
        self.name = name
        self._plugins = []
        self._wrappers = []
        self._nonwrappers = []
        self._hookexec = hook_execute
        if specmodule_or_class is not None:
            self.set_specification(specmodule_or_class)

    def has_spec(self):
        return hasattr(self, "_specmodule_or_class")

    def set_specification(self, specmodule_or_class):
        assert not self.has_spec()
        self._specmodule_or_class = specmodule_or_class
        specfunc = getattr(specmodule_or_class, self.name)
        argnames = varnames(specfunc, startindex=isclass(specmodule_or_class))
        assert "self" not in argnames  # sanity check
        self.argnames = ["__multicall__"] + list(argnames)
        self.firstresult = getattr(specfunc, 'firstresult', False)
        if hasattr(specfunc, "historic"):
            self._call_history = []

    def is_historic(self):
        return hasattr(self, "_call_history")

    def _remove_plugin(self, plugin):
        self._plugins.remove(plugin)
        meth = getattr(plugin, self.name)
        try:
            self._nonwrappers.remove(meth)
        except ValueError:
            self._wrappers.remove(meth)

    def _add_plugin(self, plugin):
        self._plugins.append(plugin)
        self._add_method(getattr(plugin, self.name))

    def _add_method(self, meth):
        if hasattr(meth, 'hookwrapper'):
            assert not self.is_historic()
            self._wrappers.append(meth)
        elif hasattr(meth, 'trylast'):
            self._nonwrappers.insert(0, meth)
        elif hasattr(meth, 'tryfirst'):
            self._nonwrappers.append(meth)
        else:
            nonwrappers = self._nonwrappers
            if not nonwrappers or not hasattr(nonwrappers[-1], "tryfirst"):
                nonwrappers.append(meth)
            else:
                for i in reversed(range(len(nonwrappers)-1)):
                    if hasattr(nonwrappers[i], "tryfirst"):
                        continue
                    nonwrappers.insert(i+1, meth)
                    break
                else:
                    nonwrappers.insert(0, meth)

    def __repr__(self):
        return "<HookCaller %r>" %(self.name,)

    def __call__(self, **kwargs):
        assert not self.is_historic()
        return self._hookexec(self, self._nonwrappers + self._wrappers, kwargs)

    def call_historic(self, proc=None, kwargs=None):
        self._call_history.append((kwargs or {}, proc))
        # historizing hooks don't return results
        self._hookexec(self, self._nonwrappers + self._wrappers, kwargs)

    def call_extra(self, methods, kwargs):
        """ Call the hook with some additional temporarily participating
        methods using the specified kwargs as call parameters. """
        old = list(self._nonwrappers), list(self._wrappers)
        for method in methods:
            self._add_method(method)
        try:
            return self(**kwargs)
        finally:
            self._nonwrappers, self._wrappers = old

    def _apply_history(self, method):
        if self.is_historic():
            for kwargs, proc in self._call_history:
                res = self._hookexec(self, [method], kwargs)
                if res and proc is not None:
                    proc(res[0])


class PluginValidationError(Exception):
    """ plugin failed validation. """


def formatdef(func):
    return "%s%s" % (
        func.__name__,
        formatargspec(*getargspec(func))
    )
