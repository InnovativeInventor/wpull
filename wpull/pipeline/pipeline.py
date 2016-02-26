import abc
import asyncio
import enum
import gettext
import logging

import time
from typing import Optional, Sequence, TypeVar, Generic
from wpull.backport.logging import BraceMessage as __

_logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)

POISON_PILL = object()
ITEM_PRIORITY = 1
POISON_PRIORITY = 0


WorkItemT = TypeVar('WorkItemT')


class ItemTask(Generic[WorkItemT], metaclass=abc.ABCMeta):
    @abc.abstractmethod
    @asyncio.coroutine
    def process(self, work_item: WorkItemT):
        pass


class ItemSource(Generic[WorkItemT], metaclass=abc.ABCMeta):
    @abc.abstractmethod
    @asyncio.coroutine
    def get_item(self) -> Optional[WorkItemT]:
        pass


class ItemQueue(Generic[WorkItemT]):
    def __init__(self):
        self._queue = asyncio.PriorityQueue()
        self._unfinished_items = 0
        self._worker_ready_condition = asyncio.Condition()
        self._entry_count = 0

    @asyncio.coroutine
    def put_item(self, item: WorkItemT):
        while self._queue.qsize() > 0:
            yield from self._worker_ready_condition.acquire()
            yield from self._worker_ready_condition.wait()
            self._worker_ready_condition.release()

        self._unfinished_items += 1
        self._queue.put_nowait((ITEM_PRIORITY, self._entry_count, item))
        self._entry_count += 1

    def put_poison_nowait(self):
        self._queue.put_nowait((POISON_PRIORITY, self._entry_count, POISON_PILL))
        self._entry_count += 1

    @asyncio.coroutine
    def get(self) -> WorkItemT:
        priority, entry_count, item = yield from self._queue.get()

        return item

    @asyncio.coroutine
    def item_done(self):
        self._unfinished_items -= 1
        assert self._unfinished_items >= 0

        yield from self._worker_ready_condition.acquire()
        self._worker_ready_condition.notify_all()
        self._worker_ready_condition.release()

    @property
    def unfinished_items(self) -> int:
        return self._unfinished_items

    @asyncio.coroutine
    def wait_for_worker(self):
        yield from self._worker_ready_condition.acquire()
        yield from self._worker_ready_condition.wait()
        self._worker_ready_condition.release()


class Worker(object):
    def __init__(self, item_queue: ItemQueue, tasks: Sequence[ItemTask]):
        self._item_queue = item_queue
        self._tasks = tasks

    @asyncio.coroutine
    def process_one(self):
        item = yield from self._item_queue.get()

        if item == POISON_PILL:
            return item

        _logger.debug(__('Processing item {}', item))

        for task in self._tasks:
            yield from task.process(item)

        yield from self._item_queue.item_done()

        return item

    @asyncio.coroutine
    def process(self):
        while True:
            item = yield from self.process_one()

            if item == POISON_PILL:
                _logger.debug('Worker quitting.')
                break


class Producer(object):
    def __init__(self, item_source: ItemSource, item_queue: ItemQueue):
        self._item_source = item_source
        self._item_queue = item_queue
        self._running = False

    @asyncio.coroutine
    def process_one(self):
        _logger.debug('Get item from source')
        item = yield from self._item_source.get_item()

        if item:
            yield from self._item_queue.put_item(item)
            return item

    @asyncio.coroutine
    def process(self):
        self._running = True

        while self._running:
            item = yield from self.process_one()

            if not item and self._item_queue.unfinished_items == 0:
                self.stop()
                break
            elif not item:
                yield from self._item_queue.wait_for_worker()

    def stop(self):
        if self._running:
            _logger.debug('Producer stopping.')
            self._running = False


class PipelineState(enum.Enum):
    stopped = 'stopped'
    running = 'running'
    stopping = 'stopping'


class Pipeline(object):
    def __init__(self, item_source: ItemSource, tasks: Sequence[ItemTask],
                 item_queue: Optional[ItemQueue]=None):
        self._item_queue = item_queue or ItemQueue()
        self._producer = Producer(item_source, self._item_queue)
        self._worker = Worker(self._item_queue, tasks)

        self._state = PipelineState.stopped
        self._concurrency = 1
        self._producer_task = None
        self._worker_tasks = set()
        self._unpaused_event = asyncio.Event()

    @asyncio.coroutine
    def process(self):
        if self._state == PipelineState.stopped:
            self._state = PipelineState.running
            self._producer_task = asyncio.ensure_future(self._run_producer_wrapper())
            self._unpaused_event.set()

        while self._state == PipelineState.running:
            yield from self._process_one_worker()

        yield from self._shutdown_processing()

    @asyncio.coroutine
    def _process_one_worker(self):
        assert self._state == PipelineState.running, self._state

        while len(self._worker_tasks) < self._concurrency:
            _logger.debug('Creating worker')
            worker_task = asyncio.ensure_future(self._worker.process())
            self._worker_tasks.add(worker_task)

        if self._worker_tasks:
            wait_coroutine = asyncio.wait(
                self._worker_tasks, return_when=asyncio.FIRST_COMPLETED)
            done_tasks = (yield from wait_coroutine)[0]

            for task in done_tasks:
                task.result()
                self._worker_tasks.remove(task)
        else:
            yield from self._unpaused_event.wait()

    @asyncio.coroutine
    def _shutdown_processing(self):
        assert self._state == PipelineState.stopping

        _logger.debug('Exited workers loop.')

        if self._worker_tasks:
            _logger.debug('Waiting for workers to stop.')
            yield from asyncio.wait(self._worker_tasks)

        _logger.debug('Waiting for producer to stop.')

        self._worker_tasks.clear()

        yield from self._producer_task

        self._state = PipelineState.stopped

    def stop(self):
        if self._state == PipelineState.running:
            self._state = PipelineState.stopping
            self._producer.stop()
            self._kill_workers()

    @asyncio.coroutine
    def _run_producer_wrapper(self):
        '''Run the producer, if exception, stop engine.'''
        try:
            yield from self._producer.process()
        except Exception as error:
            if not isinstance(error, StopIteration):
                # Stop the workers so the producer exception will be handled
                # when we finally yield from this coroutine
                _logger.debug('Producer died.', exc_info=True)
                self.stop()
            raise
        else:
            self.stop()

    def _kill_workers(self):
        for dummy in range(len(self._worker_tasks)):
            _logger.debug('Put poison pill.')
            self._item_queue.put_poison_nowait()

    @property
    def concurrency(self) -> int:
        return self._concurrency

    @concurrency.setter
    def concurrency(self, new_concurrency: int):
        if new_concurrency < 0:
            raise ValueError('Concurrency cannot be negative')

        change = new_concurrency - self._concurrency
        self._concurrency = new_concurrency

        if self._state != PipelineState.running:
            return

        if change < 0:
            for dummy in range(abs(change)):
                _logger.debug('Put poison pill for less workers.')
                self._item_queue.put_poison_nowait()
        elif change > 0:
            _logger.debug('Put 1 poison pill to trigger more workers.')
            self._item_queue.put_poison_nowait()

        if self._concurrency:
            self._unpaused_event.set()
        else:
            self._unpaused_event.clear()

    def _warn_discarded_items(self):
        _logger.warning(__(
            gettext.ngettext(
                'Discarding {num} unprocessed item.',
                'Discarding {num} unprocessed items.',
                self._item_queue.unfinished_items
            ),
            num=self._item_queue.unfinished_items
        ))
