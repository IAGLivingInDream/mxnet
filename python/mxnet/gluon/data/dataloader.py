# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

# coding: utf-8
# pylint: disable=
"""Dataset generator."""
__all__ = ['DataLoader']

import numpy as np
import multiprocessing
import multiprocessing.queues
from multiprocessing.reduction import ForkingPickler
import pickle

from . import sampler as _sampler
from ... import nd, context


def rebuild_ndarray(*args):
    """Rebuild ndarray from pickled shared memory"""
    return nd.NDArray(nd.ndarray._new_from_shared_mem(*args))


def reduce_ndarray(data):
    """Reduce ndarray to shared memory handle"""
    return rebuild_ndarray, data._to_shared_mem()


ForkingPickler.register(nd.NDArray, reduce_ndarray)


class ConnectionWrapper(object):
    """Connection wrapper for multiprocessing that supports sending
    NDArray via shared memory."""

    def __init__(self, conn):
        self.conn = conn

    def send(self, obj):
        """Send object"""
        buf = io.BytesIO()
        ForkingPickler(buf, pickle.HIGHEST_PROTOCOL).dump(obj)
        self.send_bytes(buf.getvalue())

    def recv(self):
        """Receive object"""
        buf = self.recv_bytes()
        return pickle.loads(buf)

    def __getattr__(self, name):
        """Emmulate conn"""
        return getattr(self.conn, name)


class SimpleQueue(multiprocessing.queues.Queue):
    def __init__(self, *args, **kwargs):
        super(SimpleQueue, self).__init__(*args, ctx=multiprocessing.get_context(), **kwargs)

    def _make_methods(self):
        if not isinstance(self._reader, ConnectionWrapper):
            self._reader = ConnectionWrapper(self._reader)
            self._writer = ConnectionWrapper(self._writer)
        super(SimpleQueue, self)._make_methods()


def default_batchify_fn(data):
    """Collate data into batch."""
    if isinstance(data[0], nd.NDArray):
        return nd.stack(*data)
    elif isinstance(data[0], tuple):
        data = zip(*data)
        return [default_batchify_fn(i) for i in data]
    else:
        data = np.asarray(data)
        return nd.array(data, dtype=data.dtype)


def default_mp_batchify_fn(data):
    """Collate data into batch. Use shared memory for stacking."""
    if isinstance(data[0], nd.NDArray):
        out = nd.empty((len(data),) + data[0].shape, dtype=data[0].dtype,
                       ctx=context.Context('cpu_shared', 0))
        return nd.stack(*data, out=out)
    elif isinstance(data[0], tuple):
        data = zip(*data)
        return [default_mp_batchify_fn(i) for i in data]
    else:
        data = np.asarray(data)
        return nd.array(data, dtype=data.dtype,
                        ctx=context.Context('cpu_shared', 0))


def worker_loop(dataset, key_queue, data_queue, batchify_fn):
    while True:
        idx, samples = key_queue.get()
        if idx is None:
            break
        batch = batchify_fn([dataset[i] for i in samples])
        data_queue.put((idx, batch))


class DataLoader(object):
    """Loads data from a dataset and returns mini-batches of data.

    Parameters
    ----------
    dataset : Dataset
        Source dataset. Note that numpy and mxnet arrays can be directly used
        as a Dataset.
    batch_size : int
        Size of mini-batch.
    shuffle : bool
        Whether to shuffle the samples.
    sampler : Sampler
        The sampler to use. Either specify sampler or shuffle, not both.
    last_batch : {'keep', 'discard', 'rollover'}
        How to handle the last batch if batch_size does not evenly divide
        `len(dataset)`.

        keep - A batch with less samples than previous batches is returned.
        discard - The last batch is discarded if its incomplete.
        rollover - The remaining samples are rolled over to the next epoch.
    batch_sampler : Sampler
        A sampler that returns mini-batches. Do not specify batch_size,
        shuffle, sampler, and last_batch if batch_sampler is specified.
    """

    def __init__(self, dataset, batch_size=None, shuffle=False, sampler=None,
                 last_batch=None, batch_sampler=None, batchify_fn=None,
                 num_workers=0):
        self._dataset = dataset

        if batch_sampler is None:
            if batch_size is None:
                raise ValueError("batch_size must be specified unless "
                                 "batch_sampler is specified")
            if sampler is None:
                if shuffle:
                    sampler = _sampler.RandomSampler(len(dataset))
                else:
                    sampler = _sampler.SequentialSampler(len(dataset))
            elif shuffle:
                raise ValueError("shuffle must not be specified if sampler is specified")

            batch_sampler = _sampler.BatchSampler(
                sampler, batch_size, last_batch if last_batch else 'keep')
        elif batch_size is not None or shuffle or sampler is not None or \
                        last_batch is not None:
            raise ValueError("batch_size, shuffle, sampler and last_batch must "
                             "not be specified if batch_sampler is specified.")

        self._batch_sampler = batch_sampler
        self._num_workers = num_workers
        if batchify_fn is None:
            if num_workers > 0:
                self._batchify_fn = default_mp_batchify_fn
            else:
                self._batchify_fn = default_batchify_fn
        else:
            self._batchify_fn = batchify_fn

    def __iter__(self):
        if self._num_workers == 0:
            for batch in self._batch_sampler:
                yield self._batchify_fn([self._dataset[idx] for idx in batch])
            return

        key_queue = SimpleQueue(maxsize=65535)
        data_queue = SimpleQueue(maxsize=65535)

        workers = []
        for _ in range(self._num_workers):
            worker = multiprocessing.Process(
                target=worker_loop,
                args=(self._dataset, key_queue, data_queue, self._batchify_fn))
            worker.daemon = True
            worker.start()
            workers.append(worker)

        for idx, batch in enumerate(self._batch_sampler):
            key_queue.put((idx, batch))


        data_buffer = {}
        curr_idx = 0
        for _ in range(len(self._batch_sampler)):
            idx, batch = data_queue.get()
            data_buffer[idx] = batch
            while curr_idx in data_buffer:
                yield data_buffer.pop(curr_idx)
                curr_idx += 1
        
        for i in range(self._num_workers):
            key_queue.put((None, None))

        for worker in workers:
            worker.join()

    def __len__(self):
        return len(self._batch_sampler)
