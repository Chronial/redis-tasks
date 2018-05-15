import sys
from contextlib import contextmanager

import pytest

from redis_tasks.task import Task
from redis_tasks.exceptions import WorkerShutdown, TaskAborted
from tests.utils import stub, Something, mock_func_proxy


def test_successful_execute(mocker):
    task = Task(mock_func_proxy, ["foo"], {"foo": "bar"})
    func = mocker.patch('tests.utils.mock_func_target')
    outcome = task.execute()
    assert func.called_once_with("foo", foo="bar")
    assert outcome.outcome == 'success'

    task = Task(mock_func_proxy)
    func.reset_mock()
    outcome = task.execute()
    assert func.called_once_with("foo", foo="bar")
    assert outcome.outcome == 'success'


def test_failed_execute(mocker):
    func = mocker.patch('tests.utils.mock_func_target', side_effect=ValueError("TestException"))
    task = Task(mock_func_proxy)
    outcome = task.execute()
    assert func.called_once_with()
    assert outcome.outcome == 'failure'
    assert outcome.message.splitlines()[-1] == 'ValueError: TestException'


def test_aborted_execute(mocker):
    func = mocker.patch('tests.utils.mock_func_target', side_effect=WorkerShutdown())
    task = Task(mock_func_proxy)
    outcome = task.execute()
    assert func.called_once_with()
    assert outcome.outcome == 'aborted'
    assert outcome.message == 'Worker shutdown'


def test_broken_task():
    task = Task(mock_func_proxy)
    task.func_name = "nonimportable.function"
    outcome = task.execute()
    assert outcome.outcome == 'failure'
    assert outcome.message.splitlines()[-1].startswith(
        'RuntimeError: Failed to import task function')


def test_shutdown_cm(mocker):
    @contextmanager
    def entry_shutdown_cm():
        raise WorkerShutdown()
        yield

    func = mocker.patch('tests.utils.mock_func_target')
    task = Task(mock_func_proxy)
    outcome = task.execute(shutdown_cm=entry_shutdown_cm())
    assert not func.called
    assert outcome.outcome == 'aborted'
    assert outcome.message == 'Worker shutdown'

    @contextmanager
    def exit_shutdown_cm():
        yield
        raise WorkerShutdown()

    func.reset_mock()
    outcome = task.execute(shutdown_cm=exit_shutdown_cm())
    assert func.called_once_with()
    assert outcome.outcome == 'aborted'
    assert outcome.message == 'Worker shutdown'

    in_cm = False

    @contextmanager
    def reporting_cm():
        nonlocal in_cm
        in_cm = True
        yield
        in_cm = False

    def checking_func():
        assert in_cm

    func = mocker.patch('tests.utils.mock_func_target', new=checking_func)
    outcome = task.execute(shutdown_cm=reporting_cm())
    assert outcome.outcome == 'success'


def test_generate_outcome():
    task = Task(stub)
    assert task._generate_outcome(None, None, None).outcome == 'success'

    try:
        raise TypeError('mytest')
    except TypeError as e:
        exc_info = sys.exc_info()
    outcome = task._generate_outcome(*exc_info)
    assert outcome.outcome == 'failure'
    assert 'mytest' in outcome.message

    outcome = task._generate_outcome(TaskAborted, TaskAborted("a message"), None)
    assert outcome.outcome == 'aborted'
    assert outcome.message == 'a message'


class CMCheckMiddleware:
    """Check that no middleware functions are run inside the shutdown_cm"""
    def __init__(self):
        self.in_cm = False
        self.failed = False

    def __enter__(self):
        self.in_cm = True

    def __exit__(self, *args):
        self.in_cm = False

    def run_task(self, task, run, args, kwargs):
        self.failed |= self.in_cm
        try:
            run(*args, **kwargs)
        finally:
            self.failed |= self.in_cm

    def process_outcome(self, task, *exc_info):
        self.failed |= self.in_cm

    def __call__(self):
        return self


class _SpyMiddleware:
    history = []

    @classmethod
    def reset(cls):
        cls.history = []

    def run_task(self, task, run, args, kwargs):
        self.history.append((self, 'before', (task, *args)))
        if getattr(self, 'raise_before', None):
            raise self.raise_before
        try:
            run(*args)
        finally:
            self.history.append((self, 'after', (task, *args)))
            if getattr(self, 'raise_after', None):
                raise self.raise_after

    def process_outcome(self, *args):
        self.history.append((self, 'process_outcome', args))
        if getattr(self, 'outcome', None):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            else:
                return self.outcome

    def __call__(self):
        return self


@pytest.fixture()
def SpyMiddleware():
    yield _SpyMiddleware
    _SpyMiddleware.reset()


def test_middleware_order(mocker, SpyMiddleware):
    task = Task(stub)
    spies = [SpyMiddleware(), SpyMiddleware()]
    cmcheck = CMCheckMiddleware()
    mocker.patch('redis_tasks.task.task_middlewares', new=[cmcheck, *spies])
    outcome = task.execute(shutdown_cm=cmcheck)
    assert outcome.outcome == "success"
    assert SpyMiddleware.history == [
        (spies[0], 'before', (task, )),
        (spies[1], 'before', (task, )),
        (spies[1], 'after', (task, )),
        (spies[0], 'after', (task, )),
        (spies[1], 'process_outcome', (task, None, None, None)),
        (spies[0], 'process_outcome', (task, None, None, None))]
    assert not cmcheck.failed


def test_middleware_raise_before(mocker, SpyMiddleware):
    task = Task(stub)
    spies = [SpyMiddleware(), SpyMiddleware(), SpyMiddleware()]
    mocker.patch('redis_tasks.task.task_middlewares', new=spies)
    spies[1].raise_before = ArithmeticError()
    outcome = task.execute()
    assert outcome.outcome == "failure"
    assert 'ArithmeticError' in outcome.message
    assert SpyMiddleware.history == [
        (spies[0], 'before', (task, )),
        (spies[1], 'before', (task, )),
        (spies[0], 'after', (task, )),
        (spies[2], 'process_outcome', (task, ArithmeticError, Something, Something)),
        (spies[1], 'process_outcome', (task, ArithmeticError, Something, Something)),
        (spies[0], 'process_outcome', (task, ArithmeticError, Something, Something))]


def test_middleware_raise_after(mocker, SpyMiddleware):
    task = Task(stub)
    spies = [SpyMiddleware(), SpyMiddleware()]
    mocker.patch('redis_tasks.task.task_middlewares', new=spies)
    spies[1].raise_after = ArithmeticError()
    outcome = task.execute()
    assert outcome.outcome == "failure"
    assert 'ArithmeticError' in outcome.message
    assert SpyMiddleware.history == [
        (spies[0], 'before', (task, )),
        (spies[1], 'before', (task, )),
        (spies[1], 'after', (task, )),
        (spies[0], 'after', (task, )),
        (spies[1], 'process_outcome', (task, ArithmeticError, Something, Something)),
        (spies[0], 'process_outcome', (task, ArithmeticError, Something, Something))]


def test_outcome_middlewares(mocker, SpyMiddleware):
    task = Task(stub)
    spies = [SpyMiddleware() for i in range(2)]
    mocker.patch('redis_tasks.task.task_middlewares', new=spies)
    assert task._generate_outcome(None, None, None).outcome == 'success'
    assert SpyMiddleware.history == [
        (spies[1], 'process_outcome', (task, None, None, None)),
        (spies[0], 'process_outcome', (task, None, None, None))]

    SpyMiddleware.reset()
    spies = [SpyMiddleware() for i in range(3)]
    mocker.patch('redis_tasks.task.task_middlewares', new=spies)
    spies[2].outcome = True
    spies[1].outcome = ArithmeticError()
    spies[0].outcome = TaskAborted("fake abort")
    sentinel = mocker.sentinel.error
    outcome = task._generate_outcome(None, sentinel, None)
    assert SpyMiddleware.history == [
        (spies[2], 'process_outcome', (task, None, sentinel, None)),
        (spies[1], 'process_outcome', (task, None, None, None)),
        (spies[0], 'process_outcome', (task, ArithmeticError, spies[1].outcome, Something))]
    assert outcome.outcome == 'aborted'
    assert outcome.message == 'fake abort'


def test_middleware_constructor_exception(SpyMiddleware, mocker):
    task = Task(stub)
    spies = [SpyMiddleware() for i in range(2)]
    mws = [spies[0], "nope", spies[1]]
    mocker.patch('redis_tasks.task.task_middlewares', new=mws)
    assert task.execute().outcome == 'failure'
    assert SpyMiddleware.history == [
        (spies[0], 'before', (task, )),
        (spies[0], 'after', (task, )),
        (spies[1], 'process_outcome', (task, TypeError, Something, Something)),
        (spies[0], 'process_outcome', (task, TypeError, Something, Something))]
    SpyMiddleware.reset()
