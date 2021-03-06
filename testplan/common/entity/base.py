"""
Module containing base classes that represent object entities that can accept
configuration, start/stop/run/abort, create results and have some state.
"""

import os
import signal
import time
import uuid
import threading
import inspect

from collections import deque, OrderedDict

from schema import Or, And, Use

from testplan.common.globals import get_logger
from testplan.common.config import Config
from testplan.common.config import ConfigOption
from testplan.common.utils.exceptions import format_trace
from testplan.common.utils.thread import execute_as_thread
from testplan.common.utils.timing import wait
from testplan.common.utils.path import makeemptydirs, makedirs, default_runpath


class Environment(object):
    """
    A collection of resources that can be started/stopped.

    :param parent: Reference to parent object.
    :type parent: :py:class:`Entity <testplan.common.entity.base.Entity>`
    """

    def __init__(self, parent=None):
        self._resources = OrderedDict()
        self.parent = parent
        self.start_exceptions = OrderedDict()
        self.stop_exceptions = OrderedDict()
        self._logger = None

    @property
    def cfg(self):
        """Configuration obejct of parent object."""
        return self.parent.cfg if self.parent else None

    @property
    def runpath(self):
        """Runpath of parent object."""
        return self.parent.runpath if self.parent else None

    @property
    def logger(self):
        if self._logger is None:
            if self.parent is not None:
                self._logger = self.parent.logger
            else:
                self._logger = Entity.logger
        return self._logger

    def add(self, item, uid=None):
        """
        Adds a :py:class:`Resource <testplan.common.entity.base.Resource>` to
        the Environment.

        :param item: Resource to be added.
        :type item: :py:class:`Resource <testplan.common.entity.base.Resource>`
        :param uid: Unique identifier.
        :type uid: ``str`` or ``NoneType``

        :return: Unique identifier assigned to item added.
        :rtype: ``str``
        """
        if uid is None:
            uid = item.uid()
        item.context = self
        if uid in self._resources:
            raise RuntimeError('Uid {} already in context.'.format(uid))
        self._resources[uid] = item
        return uid

    def remove(self, uid):
        """
        Remove resource with the given uid from the environment.
        """
        del self._resources[uid]

    def first(self):
        return next(uid for uid in self._resources.keys())

    def __getattr__(self, item):
        context = self.__getattribute__('_resources')

        if item in context:
            return context[item]

        if self.parent and self.parent.cfg.initial_context:
            if item in self.parent.cfg.initial_context:
                return self.parent.cfg.initial_context[item]

        return self.__getattribute__(item)

    def __getitem__(self, item):
        return getattr(self, item)

    def __contains__(self, item):
        return item in self._resources

    def __iter__(self):
        return iter(self._resources.values())

    def __repr__(self):
        if self.parent and self.parent.cfg.initial_context:
            ctx = self.parent.cfg.initial_context
            initial = {key: val for key, val in ctx.items()}
            res = {key: val for key, val in self._resources.items()}
            initial.update(res)
            return '{}[{}]'.format(self.__class__.__name__, initial)
        else:
            return '{}[{}]'.format(self.__class__.__name__,
                                   list(self._resources.items()))

    def all_status(self, target):
        """
        Check all resources has target status.
        """
        return all(resource.status.tag == target
                   for resource in self._resources)

    def start(self):
        """
        Start all resources sequentially and log errors.
        """
        # Trigger start all resources
        for resource in self._resources.values():
            try:
                self.logger.debug('Starting {}'.format(resource))
                resource.start()
                if resource.cfg.async_start is False:
                    resource.wait(resource.STATUS.STARTED)
                self.logger.debug('Started {}'.format(resource))
            except Exception as exc:
                msg = 'While starting resource [{}]{}{}'.format(
                    resource.cfg.name, os.linesep,
                    format_trace(inspect.trace(), exc))
                self.start_exceptions[resource] = msg
                # Environment start failure. Won't start the rest.
                break

        # Wait resources status to be STARTED.
        for resource in self._resources.values():
            if resource.cfg.async_start is False:
                continue
            if resource in self.start_exceptions:
                break
            else:
                resource.wait(resource.STATUS.STARTED)

    def stop(self, reversed=False):
        """
        Stop all resources in reverse order and log exceptions.
        """
        resources = list(self._resources.values())
        if reversed is True:
            resources = resources[::-1]

        # Stop all resources
        for resource in resources:
            if resource.status.tag is None:
                # Skip resources not even triggered to start.
                continue
            try:
                self.logger.debug('Stopping {}'.format(resource))
                resource.stop()
                self.logger.debug('Stopped {}'.format(resource))
            except Exception as exc:
                msg = 'While stopping resource [{}]{}{}'.format(
                    resource.cfg.name, os.linesep,
                    format_trace(inspect.trace(), exc))
                self.stop_exceptions[resource] = msg

        # Wait resources status to be STOPPED.
        for resource in resources:
            if resource in self.stop_exceptions:
                continue
            elif resource.status.tag is None:
                # Skip resources not even triggered to start.
                continue
            else:
                resource.wait(resource.STATUS.STOPPED)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()


class StatusTransitionException(Exception):
    """To be raised on illegal state transition attempt."""
    pass


class EntityStatus(object):
    """
    Represents current status of an
    :py:class:`Entity <testplan.common.entity.base.Entity>` object.

    TODO: Utilise metadata sto store information.
    """

    NONE = None
    PAUSING = 'PAUSING'
    PAUSED = 'PAUSED'
    RESUMING = 'RESUMING'

    def __init__(self):
        """TODO."""
        self._current = self.NONE
        self._metadata = OrderedDict()
        self._transitions = self.transitions()

    @property
    def tag(self):
        """Current status value."""
        return self._current

    @property
    def metadata(self):
        """TODO."""
        return self._metadata

    def change(self, new):
        """Transition to new state."""
        current = self._current
        try:
            if current == new or new in self._transitions[current]:
                self._current = new
            else:
                msg = 'On status change from {} to {}'.format(current, new)
                raise StatusTransitionException(msg)
        except KeyError as exc:
            msg = 'On status change from {} to {} - {}'.format(
                current, new, exc)
            raise StatusTransitionException(msg)

    def update_metadata(self, **metadata):
        """TODO."""
        self._metadata.update(metadata)

    def clear_metadata(self):
        """TODO."""
        self._metadata = OrderedDict()

    def transitions(self):
        """"
        Returns all legal transitions of the status of the
        :py:class:`Entity <testplan.common.entity.base.Entity>`.
        """
        return {self.PAUSING: set([self.PAUSED]),
                self.PAUSED: set([self.RESUMING])}


class EntityConfig(Config):
    """
    Configuration object for
    :py:class:`Entity <testplan.common.entity.base.Entity>` object.

    All classes that inherit
    :py:class:`Entity <testplan.common.entity.base.Entity>` can define a
    configuration that inherits this ones schema.
    """

    def configuration_schema(self):
        """
        Schema for options validation and assignment of default values.
        """
        return {ConfigOption('runpath', default=None):
                    Or(None, str, lambda x: callable(x)),
                ConfigOption('initial_context', default={}): dict,
                ConfigOption('path_cleanup', default=None): Or(None, bool),
                ConfigOption('status_wait_timeout', default=3600): int,
                ConfigOption('abort_wait_timeout', default=30): int,
                ConfigOption('active_loop_sleep', default=0.001): float}


class Entity(object):
    """
    Base class for :py:class:`Entity <testplan.common.entity.base.Entity>`
    and :py:class:`Resource <testplan.common.entity.base.Resource>` objects
    providing common functionality like runpath creation, abort policy
    and common attributes.

    :param runpath: Path to be used for temp/output files by entity.
    :type runpath: ``str`` or ``NoneType`` callable that returns ``str``
    :param initial_context: Initial key: value pair context information.
    :type initial_context: ``dict``
    :param path_cleanup: Remove previous runpath created dirs/files.
    :type path_cleanup: ``bool`` or ``None``
    :param status_wait_timeout: Timeout for wait status events.
    :type status_wait_timeout: ``int``
    :param abort_wait_timeout: Timeout for entity abort.
    :type abort_wait_timeout: ``int``
    :param active_loop_sleep: Sleep time on busy waiting loops.
    :type active_loop_sleep: ``float``
    """
    CONFIG = EntityConfig
    STATUS = EntityStatus

    def __init__(self, **options):
        self._cfg = self.__class__.CONFIG(**options)
        self._status = self.__class__.STATUS()
        self._wait_handlers = {}
        self._runpath = None
        self._scratch = None
        self._parent = None
        self._uid = None
        self._should_abort = False
        self._aborted = False

    def __str__(self):
        return '{}[{}]'.format(self.__class__.__name__, self.uid())

    @property
    def cfg(self):
        """Configuration object."""
        return self._cfg

    @property
    def status(self):
        """Status object."""
        return self._status

    @property
    def aborted(self):
        """Returns if entity was aborted."""
        return self._aborted

    @property
    def active(self):
        """Entity not aborting/aborted."""
        return self._should_abort is False and self._aborted is False

    @property
    def runpath(self):
        """Path to be used for temp/output files by entity."""
        return self._runpath

    @property
    def scratch(self):
        """Path to be used for temp files by entity."""
        return self._scratch

    @property
    def parent(self):
        """
        Returns parent :py:class:`Entity <testplan.common.entity.base.Entity>`.
        """
        return self._parent

    @parent.setter
    def parent(self, value):
        """Reference to parent object."""
        self._parent = value

    @property
    def logger(self):
        """Entity logger object."""
        return get_logger()

    def pause(self):
        """Pause entity execution."""
        self.status.change(self.STATUS.PAUSING)
        self.pausing()

    def resume(self):
        """Resume entity execution."""
        self.status.change(self.STATUS.RESUMING)
        self.resuming()

    def abort(self):
        """
        Default abort policy. First abort all dependencies and then itself.
        """
        self._should_abort = True
        for dep in self.abort_dependencies():
            self._abort_entity(dep)
        self.aborting()
        self._aborted = True

    def abort_dependencies(self):
        """Default empty generator."""
        return
        yield

    def _abort_entity(self, entity, wait_timeout=None):
        """Method to abort an entity and log exceptions."""
        timeout = wait_timeout   or self.cfg.abort_wait_timeout
        try:
            self.logger.debug('Aborting {}'.format(entity))
            entity.abort()
            self.logger.debug('Aborted {}'.format(entity))
        except Exception as exc:
            self.logger.error(format_trace(inspect.trace(), exc))
            self.logger.error('Exception on aborting {} - {}'.format(
                self, exc))
        else:
            if wait(lambda: entity.aborted is True, timeout) is False:
                self.logger.error('Timeout on waiting to abort {}.'.format(
                    self))

    def aborting(self):
        """
        Aborting logic for self.
        """
        self.logger.debug('Abort logic not implemented for {}[{}]'.format(
            self.__class__.__name__, self.uid()))

    def pausing(self):
        raise NotImplementedError()

    def resuming(self):
        raise NotImplementedError()

    def wait(self, target_status, timeout=None):
        """Wait until objects status becomes target status."""
        timeout = timeout or self.cfg.status_wait_timeout
        if target_status in self._wait_handlers:
            self._wait_handlers[target_status](timeout=timeout)
        else:
            wait(lambda: self.status.tag == target_status, timeout=timeout)

    def uid(self):
        """Unique identifier of self."""
        if not self._uid:
            self._uid = uuid.uuid4()
        return self._uid

    def generate_runpath(self):
        """
        Returns runpath directory based on parent object and configuration.
        """
        if self.parent and self.parent.runpath:
            return os.path.join(self.parent.runpath, self.uid())

        runpath = self.cfg.runpath
        if runpath:
            return self.cfg.runpath(self) if callable(runpath) else runpath
        else:
            return default_runpath(self)

    def make_runpath_dirs(self):
        """
        Creates runpath related directories.
        """
        self._runpath = self.generate_runpath()
        self._scratch = os.path.join(self._runpath, 'scratch')
        self.logger.debug('{} has {} runpath and pid {}'.format(
            self.__class__.__name__, self.runpath, os.getpid()))
        if self.runpath is None:
            raise RuntimeError('{} runpath cannot be None'.format(
                self.__class__.__name__
            ))

        path_cleanup = self.cfg.path_cleanup
        if path_cleanup is False:
            makedirs(self._runpath)
            makedirs(self._scratch)
        else:
            makeemptydirs(self._runpath)
            makeemptydirs(self._scratch)


class RunnableConfig(EntityConfig):
    """
    Configuration object for
    :py:class:`~testplan.common.entity.base.Runnable` entity.
    """

    def configuration_schema(self):
        """
        Schema to validate
        :py:class:`Runnable <testplan.common.entity.base.Runnable>`
        object input configuration options.
        """

        overrides = {
            ConfigOption('interactive', default=False): bool,
        }
        return self.inherit_schema(overrides, super(RunnableConfig, self))


class RunnableStatus(EntityStatus):
    """
    Status of a
    :py:class:`Runnable <testplan.common.entity.base.Runnable>` entity.
    """

    EXECUTING = 'EXECUTING'
    RUNNING = 'RUNNING'
    FINISHED = 'FINISHED'
    PAUSING = 'PAUSING'
    PAUSED = 'PAUSED'

    def transitions(self):
        """"
        Defines the status transitions of a
        :py:class:`Runnable <testplan.common.entity.base.Runnable>` entity.
        """
        transitions = super(RunnableStatus, self).transitions()
        overrides = {self.NONE: set([self.RUNNING]),
                     self.RUNNING: set([self.FINISHED, self.EXECUTING,
                                        self.PAUSING]),
                     self.EXECUTING: set([self.RUNNING]),
                     self.PAUSING: set([self.PAUSED]),
                     self.PAUSED: set([self.RESUMING]),
                     self.RESUMING: set([self.RUNNING]),
                     self.FINISHED: set([self.RUNNING])}
        transitions.update(overrides)
        return transitions


class RunnableResult(object):
    """
    Result object of a
    :py:class:`~testplan.common.entity.base.Runnable` entity.
    """

    def __init__(self):
        self.step_results = OrderedDict()

    def __repr__(self):
        return '{}[{}]'.format(self.__class__.__name__, self.__dict__)


class Runnable(Entity):
    """
    An object that defines steps, a run method to execute the steps and
    provides results with the
    :py:class:`~testplan.common.entity.base.RunnableResult`
    object.

    It contains an
    :py:class:`~testplan.common.entity.base.Environment`
    object of
    :py:class:`~testplan.common.entity.base.Resource` objects
    that can be started/stopped and utilized by the steps defined.

    :param interactive: Enable interactive execution mode.
    :type interactive: ``bool``

    Also inherits all
    :py:class:`~testplan.common.entity.base.Entity` options.
    """
    CONFIG = RunnableConfig
    STATUS = RunnableStatus
    RESULT = RunnableResult

    def __init__(self, **options):
        super(Runnable, self).__init__(**options)
        self._environment = Environment(parent=self)
        self._result = self.__class__.RESULT()
        self._steps = deque()

    @property
    def result(self):
        """
        Returns a
        :py:class:`~testplan.common.entity.base.RunnableResult`
        """
        return self._result

    @property
    def resources(self):
        """
        Returns the
        :py:class:`Environment <testplan.common.entity.base.Environment>`
        of :py:class:`Resources <testplan.common.entity.base.Resource>`.
        """
        return self._environment

    def _add_step(self, step, *args, **kwargs):
        self._steps.append((step, args, kwargs))

    def pre_step_call(self, step):
        """Callable to be invoked before each step."""
        pass

    def skip_step(self, step):
        """Callable to determine if step should be skipped."""
        return False

    def post_step_call(self, step):
        """Callable to be invoked before each step."""
        pass

    def _run(self):
        self.status.change(RunnableStatus.RUNNING)
        while self.active:
            if self.status.tag == RunnableStatus.RUNNING:
                try:
                    func, args, kwargs = self._steps.popleft()
                    self.pre_step_call(func)
                    if self.skip_step(func) is False:
                        self.logger.debug('Executing step of {} - {}'.format(
                            self, func.__name__))
                        start_time = time.time()
                        self._execute_step(func, *args, **kwargs)
                        self.logger.debug(
                            'Finished step of {}, {} - {}s'.format(
                                self, func.__name__,
                                round(time.time() - start_time, 5)))
                    self.post_step_call(func)
                except IndexError:
                    self.status.change(RunnableStatus.FINISHED)
                    break
            time.sleep(self.cfg.active_loop_sleep)

    def _run_batch_steps(self):
        self.pre_resource_steps()

        self._add_step(self.resources.start)

        self.main_batch_steps()

        self._add_step(self.resources.stop, reversed=True)

        self.post_resource_steps()
        self._run()

    def _execute_step(self, step, *args, **kwargs):
        try:
            res = step(*args, **kwargs)
        except Exception as exc:
            print('Exception on {} {}, step {} - {}'.format(
                self.__class__.__name__, self.uid(), step.__name__, exc))
            self.logger.error(format_trace(inspect.trace(), exc))
            res = exc
        finally:
            self.result.step_results[step.__name__] = res
            self.status.update_metadata(**{str(step): res})

    def pre_resource_steps(self):
        """Steps to run before environment started."""
        pass

    def main_batch_steps(self):
        """Steps to run after environment started."""
        pass

    def post_resource_steps(self):
        """Steps to run after environment stopped."""
        pass

    def pausing(self):
        for resource in self.resources:
            resource.pause()
        self.status.change(RunnableStatus.PAUSED)

    def resuming(self):
        for resource in self.resources:
            resource.resume()
        self.status.change(RunnableStatus.RUNNING)

    def abort_dependencies(self):
        """
        Yield all dependencies to be aborted before self abort.
        """
        for resource in self.resources:
            yield resource

    def setup(self):
        """Setup step to be executed first."""
        pass

    def teardown(self):
        """Teardown step to be executed last."""
        pass

    def should_run(self):
        """Determines if current object should run."""
        return True

    def run(self):
        """Executes the defined steps and populates the result object."""
        try:
            self._add_step(self.setup)
            if self.cfg.interactive is True:
                raise
            else:
                self._run_batch_steps()
            self._add_step(self.teardown)
        except Exception as exc:
            self._result.run = exc
            self.logger.error(format_trace(inspect.trace(), exc))
        else:
            # TODO fix swallow exceptions in self._result.step_results.values()
            self._result.run = self.status.tag == RunnableStatus.FINISHED and\
                self.run_result() is True
        return self._result

    def run_result(self):
        """Returns if a run was successful."""
        return all(not isinstance(val, Exception) and val is not False
                   for val in self._result.step_results.values())


class FailedAction(object):
    """
    Simple Falsey container that can be used for
    returning results of certain failed async actions.

    The `error_msg` can later on be used for enriching the error messages.
    """

    def __init__(self, error_msg):
        self.error_msg = error_msg

    def __bool__(self):
        return False

    __nonzero__ = __bool__


class ResourceConfig(EntityConfig):
    """
    Configuration object for
    :py:class:`~testplan.common.entity.base.Resource` entity.
    """

    def configuration_schema(self):
        """
        Schema for options validation and assignment of default values.
        """
        overrides = {ConfigOption('async_start', default=True): bool}
        return self.inherit_schema(overrides, super(ResourceConfig, self))


class ResourceStatus(EntityStatus):
    """
    Status of a
    :py:class:`Resource <testplan.common.entity.base.Resource>` entity.
    """

    STARTING = 'STARTING'
    STARTED = 'STARTED'
    STOPPING = 'STOPPING'
    STOPPED = 'STOPPED'

    def transitions(self):
        """"
        Defines the status transitions of a
        :py:class:`Resource <testplan.common.entity.base.Resource>` entity.
        """
        transitions = super(ResourceStatus, self).transitions()
        overrides = {self.NONE: set([self.STARTING]),
                     self.STARTING: set([self.STARTED, self.STOPPING]),
                     self.STARTED: set([self.PAUSING, self.STOPPING]),
                     self.PAUSING: set([self.PAUSED]),
                     self.PAUSED: set([self.RESUMING, self.STOPPING]),
                     self.RESUMING: set([self.STARTED]),
                     self.STOPPING: set([self.STOPPED]),
                     self.STOPPED: set([self.STARTING])}
        transitions.update(overrides)
        return transitions


class Resource(Entity):
    """
    An object that can be started/stopped and expose its context
    object of key/value pair information.

    A Resource is usually part of an
    :py:class:`~testplan.common.entity.base.Environment`
    object of a
    :py:class:`~testplan.common.entity.base.Runnable` object.

    :param async_start: Resource can start asynchronously.
    :type async_start: ``bool``

    Also inherits all
    :py:class:`~testplan.common.entity.base.Entity` options.
    """
    CONFIG = ResourceConfig
    STATUS = ResourceStatus

    def __init__(self, **options):
        super(Resource, self).__init__(**options)
        self._context = None
        self._wait_handlers.update(
            {self.STATUS.STARTED: self._wait_started,
             self.STATUS.STOPPED: self._wait_stopped})

    @property
    def context(self):
        """Key/value pair information of a Resource."""
        return self._context

    @context.setter
    def context(self, context):
        """Set the Resource context."""
        self._context = context

    def start(self):
        """
        Triggers the start logic of a Resource by executing
        :py:meth:`Resource.starting <testplan.common.entity.base.Resource.starting>`
        method.
        """
        self.status.change(self.STATUS.STARTING)
        self.starting()

    def stop(self):
        """
        Triggers the stop logic of a Resource by executing
        :py:meth:`Resource.stopping <testplan.common.entity.base.Resource.stopping>`
        method.
        """
        self.status.change(self.STATUS.STOPPING)
        if self.active:
            self.stopping()

    def _wait_started(self, timeout=None):
        self.status.change(self.STATUS.STARTED)

    def _wait_stopped(self, timeout=None):
        self.status.change(self.STATUS.STOPPED)

    def starting(self):
        """
        Start logic for Resource that also sets the status to *STARTED*.
        """
        raise NotImplementedError()

    def stopping(self):
        """
        Stop logic for Resource that also sets the status to *STOPPED*.
        """
        raise NotImplementedError()

    def restart(self, timeout=None):
        """Stop and start the resource."""
        self.stop()
        self._wait_stopped(timeout=timeout)
        self.start()
        self._wait_started(timeout=timeout)

    def __enter__(self):
        self.start()
        self.wait(self.STATUS.STARTED)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        self.wait(self.STATUS.STOPPED)


class RunnableManagerStatus(EntityConfig):
    """
    Status of a
    :py:class:`RunnableManager <testplan.common.entity.base.RunnableManager>`
    entity.
    """


class RunnableManagerConfig(EntityConfig):
    """
    Configuration object for
    :py:class:`RunnableManager <testplan.common.entity.base.RunnableManager>`
    entity.
    """

    def configuration_schema(self):
        """
        Schema for options validation and assignment of default values.
        """
        overrides = {
            ConfigOption('parse_cmdline', default=True): bool,
            ConfigOption('port', default=None):
                Or(None,
                   And(Use(int),
                       lambda n: n > 0)),
            ConfigOption('abort_signals', default=[signal.SIGINT,
                                                   signal.SIGTERM]): [int]
        }
        return self.inherit_schema(overrides,
                                   super(RunnableManagerConfig, self))


class RunnableManager(Entity):
    """
    Executes a
    :py:class:`Runnable <testplan.common.entity.base.Runnable>` entity
    in a separate thread and handles the abort signals.

    :param parse_cmdline: Parse command lne arguments.
    :type parse_cmdline: ``bool``
    :param port: TODO port for interactive mode.
    :type port: ``bool``
    :param abort_signals: Signals to catch and trigger abort.
    :type abort_signals: ``list`` of signals

    Also inherits all
    :py:class:`~testplan.common.entity.base.Entity` options.
    """
    CONFIG = RunnableManagerConfig

    def __init__(self, **options):
        super(RunnableManager, self).__init__(**options)
        if self._cfg.parse_cmdline is True:
            options = self._enrich_options(options)
        self._runnable = self._initialize_runnable(**options)

    def _enrich_options(self, options):
        return options

    def __getattr__(self, item):
        try:
            return self.__getattribute__(item)
        except AttributeError:
            if '_runnable' in self.__dict__:
                return getattr(self._runnable, item)
            raise

    @property
    def runpath(self):
        """Expose the runnable runpath."""
        return self._runnable.runpath

    @property
    def cfg(self):
        """Expose the runnable configuration object."""
        return self._runnable.cfg

    @property
    def status(self):
        """Expose the runnable status."""
        return self._runnable.status

    def run(self):
        """
        Executes target runnable defined in configuration in a separate thread.

        :return: Runnable result object.
        :rtype: :py:class:`RunnableResult <testplan.common.entity.base.RunnableResult>`
        """
        for sig in self._cfg.abort_signals:
            signal.signal(sig,  self._handle_abort)
        execute_as_thread(self._runnable.run, daemon=True, join=True,
                          break_join=lambda: self.aborted is True)
        if isinstance(self._runnable.result, Exception):
            raise self._runnable.result
        return self._runnable.result

    def _initialize_runnable(self, **options):
        runnable_class = self._cfg.runnable
        runnable_config = dict(**options)
        return runnable_class(**runnable_config)

    def _handle_abort(self, signum, frame):
        for sig in self._cfg.abort_signals:
            signal.signal(sig,  signal.SIG_IGN)
        self.logger.debug('Signal handler called for signal {} from {}'.format(
            signum, threading.current_thread()))
        self.abort()

    def pausing(self):
        """Pause the runnable execution."""
        self._runnable.pause()

    def resuming(self):
        """Resume the runnable execution."""
        self._runnable.resume()

    def abort_dependencies(self):
        """Dependencies to be aborted first."""
        yield self._runnable

    def aborting(self):
        """Suppressing not implemented debug log by parent class."""
        pass

