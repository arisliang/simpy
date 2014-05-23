"""
This *events* module contains the various event type used by the SimPy core.

The base class for all events is :class:`Event`. Though it can be directly
used, there are several specialized subclasses of it:

- :class:`Timeout`: is scheduled with a certain delay and lets processes hold
  their state for a certain amount of time.

- :class:`Initialize`: Initializes a new :class:`Process`.

- :class:`Process`: Processes are also modeled as an event so other processes
  can wait until another one finishes.

- :class:`Condition`: Events can be concatenated with ``|`` an ``&`` to either
  wait until one or both of the events are triggered.

- :class:`AllOf`: Special case of :class:`Condition`; wait until a list of
  events has been triggered.

- :class:`AnyOf`: Special case of :class:`Condition`; wait until one of a list
  of events has been triggered.

This module also defines the :exc:`Interrupt` exception.

"""
from inspect import isgenerator
from collections import OrderedDict

from simpy._compat import PY2

if PY2:
    import sys


PENDING = object()
"""Unique object to identify pending values of events."""

URGENT = 0
"""Priority of interrupts and process initialization events."""
NORMAL = 1
"""Default priority used by events."""


class Event(object):
    """Base class for all events.

    Every event is bound to an environment *env* (see
    :class:`~simpy.core.BaseEnvironment`) and has an optional *value*.

    An event has a list of :attr:`callbacks`. A callback can be any callable
    that accepts a single argument which is the event instances the callback
    belongs to. This list is not exclusively for SimPy internals---you can also
    append custom callbacks. All callbacks are executed in the order that they
    were added when the event is processed.

    This class also implements ``__and__()`` (``&``) and ``__or__()`` (``|``).
    If you concatenate two events using one of these operators,
    a :class:`Condition` event is generated that lets you wait for both or one
    of them.

    """
    def __init__(self, env):
        self.env = env
        """The :class:`~simpy.core.Environment` the event lives in."""
        self.callbacks = []
        """List of functions that are called when the event is processed."""
        self._value = PENDING

    def __repr__(self):
        """Return the description of the event (see :meth:`_desc`) with the id
        of the event."""
        return '<%s object at 0x%x>' % (self._desc(), id(self))

    def _desc(self):
        """Return a string *Event()*."""
        return '%s()' % self.__class__.__name__

    @property
    def triggered(self):
        """Becomes ``True`` if the event has been triggered and its callbacks
        are about to be invoked."""
        return self._value is not PENDING

    @property
    def processed(self):
        """Becomes ``True`` if the event has been processed (e.g., its
        callbacks have been invoked)."""
        return self.callbacks is None

    @property
    def value(self):
        """The value of the event if it is available.

        The value is available when the event has been triggered.

        Raise a :exc:`AttributeError` if the value is not yet available.

        """
        if self._value is PENDING:
            raise AttributeError('Value of %s is not yet available' % self)
        return self._value

    def trigger(self, event):
        """Triggers the event with the state and value of the provided *event*.

        This method can be used directly as a callback function.

        """
        self.ok = event.ok
        self._value = event._value
        self.env.schedule(self)

    def succeed(self, value=None):
        """Schedule the event and mark it as successful. Return the event
        instance.

        You can optionally pass an arbitrary ``value`` that will be sent into
        processes waiting for that event.

        Raise a :exc:`RuntimeError` if this event has already been scheduled.

        """
        if self._value is not PENDING:
            raise RuntimeError('%s has already been triggered' % self)

        self.ok = True
        self._value = value
        self.env.schedule(self)
        return self

    def fail(self, exception):
        """Schedule the event and mark it as failed. Return the event instance.

        The ``exception`` will be thrown into processes waiting for that event.

        Raise a :exc:`ValueError` if ``exception`` is not an :exc:`Exception`.

        Raise a :exc:`RuntimeError` if this event has already been scheduled.

        """
        if self._value is not PENDING:
            raise RuntimeError('%s has already been triggered' % self)
        if not isinstance(exception, BaseException):
            raise ValueError('%s is not an exception.' % exception)
        self.ok = False
        self._value = exception
        self.env.schedule(self)
        return self

    def __and__(self, other):
        """Return ``True`` if this event and *other* are triggered."""
        return Condition(self.env, Condition.all_events, [self, other])

    def __or__(self, other):
        """Return ``True`` if this event or *other is triggered, or both."""
        return Condition(self.env, Condition.any_events, [self, other])


class Timeout(Event):
    """An :class:`Event` that is scheduled with a certain *delay* after its
    creation.

    This event can be used by processes to wait (or hold their state) for
    *delay* time steps. It is immediately scheduled at ``env.now + delay`` and
    has thus (in contrast to :class:`Event`) no *success()* or *fail()* method.

    """
    def __init__(self, env, delay, value=None):
        if delay < 0:
            raise ValueError('Negative delay %s' % delay)
        # NOTE: The following initialization code is inlined from
        # Event.__init__() for performance reasons.
        self.env = env
        self.callbacks = []
        self._value = value
        self._delay = delay
        self.ok = True
        env.schedule(self, NORMAL, delay)

    def _desc(self):
        """Return a string *Timeout(delay[, value=value])*."""
        return '%s(%s%s)' % (self.__class__.__name__, self._delay,
                             '' if self._value is None else
                             (', value=%s' % self._value))


class Initialize(Event):
    """Initializes a process. Only used internally by :class:`Process`."""
    def __init__(self, env, process):
        # NOTE: The following initialization code is inlined from
        # Event.__init__() for performance reasons.
        self.env = env
        self.callbacks = [process._resume]
        self._value = None

        # The initialization events needs to be scheduled as urgent so that it
        # will be handled before interrupts. Otherwise a process whose
        # generator has not yet been started could be interrupted.
        self.ok = True
        env.schedule(self, URGENT)


class Interruption(Event):
    """Interrupts a process while waiting for another event."""
    def __init__(self, process, cause):
        # NOTE: The following initialization code is inlined from
        # Event.__init__() for performance reasons.
        self.env = process.env
        self.callbacks = [self._interrupt]
        self._value = Interrupt(cause)
        self.ok = False
        self.defused = True

        if process._value is not PENDING:
            raise RuntimeError('%s has terminated and cannot be interrupted.' %
                               process)

        if process is self.env.active_process:
            raise RuntimeError('A process is not allowed to interrupt itself.')

        self.process = process
        self.env.schedule(self, URGENT)

    def _interrupt(self, event):
        # Ignore dead processes. Multiple concurrently scheduled interrupts
        # cause this situation. If the process dies while handling the first
        # one, the remaining interrupts must be ignored.
        if self.process._value is not PENDING:
            return

        # A process never expects an interrupt and is always waiting for a
        # target event. Remove the process from the callbacks of the target.
        self.process._target.callbacks.remove(self.process._resume)

        self.process._resume(self)


class Process(Event):
    """A *Process* is a wrapper for the process *generator* (that is returned
    by a *process function*) during its execution.

    It also contains internal and external status information and is used for
    process interaction, e.g., for interrupts.

    ``Process`` inherits :class:`Event`. You can thus wait for the termination
    of a process by simply yielding it from your process function.

    An instance of this class is returned by
    :meth:`simpy.core.Environment.process()`.

    """
    def __init__(self, env, generator):
        if not isgenerator(generator):
            raise ValueError('%s is not a generator.' % generator)

        # NOTE: The following initialization code is inlined from
        # Event.__init__() for performance reasons.
        self.env = env
        self.callbacks = []
        self._value = PENDING

        self._generator = generator

        # Schedule the start of the execution of the process.
        self._target = Initialize(env, self)

    def _desc(self):
        """Return a string *Process(process_func_name)*."""
        return '%s(%s)' % (self.__class__.__name__, self._generator.__name__)

    @property
    def target(self):
        """The event that the process is currently waiting for.

        Returns ``None`` if the process is dead."""

        return self._target

    @property
    def is_alive(self):
        """``True`` until the process generator exits."""
        return self._value is PENDING

    def interrupt(self, cause=None):
        """Interupt this process optionally providing a *cause*.

        A process cannot be interrupted if it already terminated. A process can
        also not interrupt itself. Raise a :exc:`RuntimeError` in these
        cases."""
        Interruption(self, cause)

    def _resume(self, event):
        """Resume the execution of the process.

        Send the result of the event the process was waiting for into the
        process generator and retrieve a new event from it. Register this
        method as callback for that event.

        If the process generator exits or raises an exception, terminate this
        process. Also schedule this process to notify all registered callbacks,
        that the process terminated.

        """
        # Mark the current process as active.
        self.env._active_proc = self

        while True:
            # Get next event from process
            try:
                if event.ok:
                    event = self._generator.send(event._value)
                else:
                    # The process has no choice but to handle the failed event
                    # (or fail itself).
                    event.defused = True
                    event = self._generator.throw(event._value)
            except StopIteration as e:
                # Process has terminated.
                event = None
                self.ok = True
                self._value = e.args[0] if len(e.args) else None
                self.env.schedule(self)
                break
            except BaseException as e:
                # Process has failed.
                event = None
                self.ok = False
                self._value = type(e)(*e.args)
                self._value.__cause__ = e
                if PY2:
                    self._value.__traceback__ = sys.exc_info()[2]
                self.env.schedule(self)
                break

            # Process returned another event to wait upon.
            try:
                # Be optimistic and blindly access the callbacks attribute.
                if event.callbacks is not None:
                    # The event has not yet been triggered. Register callback
                    # to resume the process if that happens.
                    event.callbacks.append(self._resume)
                    break
            except AttributeError:
                # Our optimism didn't work out, figure out what went wrong and
                # inform the user.
                if not hasattr(event, 'callbacks'):
                    msg = 'Invalid yield value "%s"' % event

                descr = _describe_frame(self._generator.gi_frame)
                error = RuntimeError('\n%s%s' % (descr, msg))
                # Drop the AttributeError as the cause for this exception.
                error.__cause__ = None
                raise error

        self._target = event
        self.env._active_proc = None


class Condition(Event):
    """A *Condition* :class:`Event` groups several *events* and is triggered if
    a given condition (implemented by the *evaluate* function) becomes true.

    The value of the condition is a dictionary that maps the input events to
    their respective values. It only contains entries for those events that
    occurred until the condition was met.

    If one of the events fails, the condition also fails and forwards the
    exception of the failing event.

    The ``evaluate`` function receives the list of target events and the
    number of processed events in this list. If it returns ``True``, the
    condition is scheduled. The :func:`Condition.all_events()` and
    :func:`Condition.any_events()` functions are used to implement *and*
    (``&``) and *or* (``|``) for events.

    Conditions events can be nested.

    """
    def __init__(self, env, evaluate, events):
        super(Condition, self).__init__(env)
        self._evaluate = evaluate
        self._events = events
        self._count = 0

        # Check if events belong to the same environment.
        for event in events:
            if self.env != event.env:
                raise ValueError('It is not allowed to mix events from '
                        'different environments')

        # Check if the condition is met for each processed event. Attach
        # _check() as a callback otherwise.
        for event in events:
            if event.callbacks is None:
                self._check(event)
            else:
                event.callbacks.append(self._check)

        # Register a callback which will update the value of this
        # condition once it is being processed.
        self.callbacks.append(self._collect_values)

    def _desc(self):
        """Return a string *Condition(and_or_or, [events])*."""
        return '%s(%s, %s)' % (self.__class__.__name__,
                               self._evaluate.__name__, self._events)

    def _get_values(self):
        """Recursively collect the current values of all nested conditions into
        a flat dictionary."""
        values = OrderedDict()

        for event in self._events:
            if isinstance(event, Condition):
                values.update(event._get_values())
            elif event.callbacks is None:
                values[event] = event._value

        return values

    def _collect_values(self, event):
        """Update the final value of this condition."""
        if event.ok:
            self._value = OrderedDict()
            self._value.update(self._get_values())

    def _check(self, event):
        """Check if the condition was already met and schedule the *event* if
        so."""
        if self._value is not PENDING:
            return

        self._count += 1

        if not event.ok:
            # Abort if the event has failed.
            event.defused = True
            self.fail(event._value)
        elif self._evaluate(self._events, self._count):
            # The condition has been met. The _collect_values callback will
            # populate set the value once this condition gets processed.
            self.succeed()

    @staticmethod
    def all_events(events, count):
        """A condition function that returns ``True`` if all *events* have
        been triggered."""
        return len(events) == count

    @staticmethod
    def any_events(events, count):
        """A condition function that returns ``True`` if at least one of
        *events* has been triggered."""
        return count > 0 or len(events) == 0


class AllOf(Condition):
    """A :class:`Condition` event that waits for all *events*."""
    def __init__(self, env, events):
        super(AllOf, self).__init__(env, Condition.all_events, events)


class AnyOf(Condition):
    """A :class:`Condition` event that waits until the first of *events* is
    triggered."""
    def __init__(self, env, events):
        super(AnyOf, self).__init__(env, Condition.any_events, events)


class Interrupt(Exception):
    """This exceptions is sent into a process if it is interrupted by another
    process (see :func:`Process.interrupt()`).

    *cause* may be none if no cause was explicitly passed to
    :func:`Process.interrupt()`.

    An interrupt has a higher priority as a normal event. Thus, if a process
    has a normal event and an interrupt scheduled at the same time, the
    interrupt will always be thrown into the process first.

    If a process is interrupted multiple times at the same time, all interrupts
    will be thrown into the process in the same order as they occurred."""

    def __str__(self):
        return '%s(%r)' % (self.__class__.__name__, self.cause)

    @property
    def cause(self):
        """The cause of the interrupt or ``None`` if no cause was provided."""
        return self.args[0]


def _describe_frame(frame):
    """Print filename, line number and function name of a stack frame."""
    filename, name = frame.f_code.co_filename, frame.f_code.co_name
    lineno = frame.f_lineno

    with open(filename) as f:
        for no, line in enumerate(f):
            if no + 1 == lineno:
                break

    return '  File "%s", line %d, in %s\n    %s\n' % (filename, lineno, name,
                                                      line.strip())
