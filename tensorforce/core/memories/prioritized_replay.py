# Copyright 2017 reinforce.io. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""
Replay memory implementing prioritized experience replay.
"""

from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import random
from six.moves import xrange
import numpy as np
from collections import namedtuple

from tensorforce import util, TensorForceError
from tensorforce.core.memories import Memory

_SumRow = namedtuple('SumRow', ['item', 'priority'])


class SumTree(object):
    """
    Sum tree data structure where data is stored in leaves and each node on the
    tree contains a sum of the children.
            # For prioritized replay, 'sum-tree' data structure is used.
    Items and priorities are stored in leaf nodes, while internal nodes store
    the sum of priorities from all its descendants. Internally a single list
    stores the internal nodes followed by leaf nodes.

    See:
    - [Binary heap trees](https://en.wikipedia.org/wiki/Binary_heap)
    - [Section B.2.1 in the prioritized replay paper](https://arxiv.org/pdf/1511.05952.pdf)
    - [The CNTK implementation](https://github.com/Microsoft/CNTK/blob/258fbec7600fe525b50c3e12d4df0c971a42b96a/bindings/python/cntk/contrib/deeprl/agent/shared/replay_memory.py)

    Usage:
        tree = SumTree(100)
        tree.push('item1', priority=0.5)
        tree.push('item2', priority=0.6)
        item, priority = tree[0]
        batch = tree.sample_minibatch(2)
    """

    def __init__(self, capacity):
        self._capacity = capacity

        # initializes all internal nodes to have value 0.
        self._memory = [0] * (capacity - 1)
        self._position = 0
        self._actual_capacity = 2 * self._capacity - 1

    def put(self, item, priority=None):
        """
        Store a transition in replay memory.

        If the memory is full, the oldest one gets overwritten.
        """
        if not self._isfull():
            self._memory.append(None)
        position = self._next_position_then_increment()
        old_priority = 0 if self._memory[position] is None \
            else (self._memory[position].priority or 0)
        row = _SumRow(item, priority)
        self._memory[position] = row
        self._update_internal_nodes(
            position, (row.priority or 0) - old_priority)

    def move(self, external_index, new_priority):
        """Change the priority of a leaf node"""
        index = external_index + (self._capacity - 1)
        return self._move(index, new_priority)

    def _move(self, index, new_priority):
        """Change the priority of a leaf node"""
        item, old_priority = self._memory[index]
        old_priority = old_priority or 0
        self._memory[index] = _SumRow(item, new_priority)
        self._update_internal_nodes(index, new_priority - old_priority)

    def _update_internal_nodes(self, index, delta):
        """
        Update internal priority sums when leaf priority has been changed.
        Args:
            index: leaf node index
            delta: change in priority
        """
        # move up tree, increasing position, updating sum
        while index > 0:
            index = (index - 1) // 2
            self._memory[index] += delta

    def _isfull(self):
        return len(self) == self._capacity

    def _next_position_then_increment(self):
        """Similar to position++."""
        start = self._capacity - 1
        position = start + self._position
        self._position = (self._position + 1) % self._capacity
        return position

    def _sample_with_priority(self, p):
        """Sample random element with priority greater than p"""
        parent = 0
        while True:
            left = 2 * parent + 1
            if left >= len(self._memory):
                # parent points to a leaf node already.
                return parent

            left_p = self._memory[left] if left < self._capacity - 1 \
                else (self._memory[left].priority or 0)
            if p <= left_p:
                parent = left
            else:
                if left + 1 >= len(self._memory):
                    raise RuntimeError('Right child is expected to exist.')
                p -= left_p
                parent = left + 1

    def sample_minibatch(self, batch_size):
        """Sample minibatch of size batch_size."""
        pool_size = len(self)
        if pool_size == 0:
            return []

        delta_p = self._memory[0] / batch_size
        chosen_idx = []
        for i in xrange(batch_size):
            lower = max(i * delta_p, 0)
            upper = min((i + 1) * delta_p, self._memory[0])
            p = random.uniform(lower, upper)
            chosen_idx.append(self._sample_with_priority(p))
        return [(i, self._memory[i]) for i in chosen_idx]

    def __len__(self):
        """Return the current number of transitions."""
        return len(self._memory) - (self._capacity - 1)

    def __getitem__(self, index):
        return self._memory[self._capacity - 1:][index]

    def __getslice__(self, start, end):
        self.memory[self._capacity - 1:][start:end]


class PrioritizedReplay(Memory):
    def __init__(self, capacity, states_config, actions_config, prioritization_weight=1.0):
        super(PrioritizedReplay, self).__init__(
            capacity, states_config, actions_config)
        self.prioritization_weight = prioritization_weight
        self.internals_config = None
        self.batch_indices = None

        # stores (priority, observation) pairs
        self.observations = SumTree(capacity)

        # queue index where seen observations end and unseen ones begin
        self.none_priority_index = 0

        # stores last observation until next_state value is known
        self.last_observation = None

    def add_observation(self, state, action, reward, terminal, internal):
        if self.internals_config is None and internal is not None:
            self.internals_config = [(i.shape, i.dtype) for i in internal]

        if self.last_observation is not None:
            observation = self.last_observation + (state, internal)

            # we we are above capacity and have some seen observations
            if self.observations._isfull():
                if self.none_priority_index <= 0:
                    raise TensorForceError(
                        "Trying to replace unseen observations: Memory is at capacity and contains only unseen observations.")
                self.none_priority_index -= 1

            self.observations.put(observation, None)

        self.last_observation = (state, action, reward, terminal, internal)

    def get_batch(self, batch_size, next_states=False):
        """
        Samples a batch of the specified size according to priority.
        Args:
            batch_size: The batch size
            next_states: A boolean flag indicating whether 'next_states' values should be included
        Returns: A dict containing states, actions, rewards, terminals, internal states (and next states)
        """

        # init empty states etc
        states = {name: np.zeros((batch_size,) + tuple(state.shape), dtype=util.np_dtype(
            state.type)) for name, state in self.states_config.items()}
        actions = {name: np.zeros((batch_size,) + tuple(action.shape), dtype=util.np_dtype(
            'float' if action.continuous else 'int')) for name, action in self.actions_config.items()}
        rewards = np.zeros((batch_size,), dtype=util.np_dtype('float'))
        terminals = np.zeros((batch_size,), dtype=util.np_dtype('bool'))
        internals = [np.zeros((batch_size,) + shape, dtype)
                     for shape, dtype in self.internals_config]
        if next_states:
            next_states = {name: np.zeros((batch_size,) + tuple(state.shape), dtype=util.np_dtype(
                state.type)) for name, state in self.states_config.items()}
            next_internals = [np.zeros((batch_size,) + shape, dtype)
                              for shape, dtype in self.internals_config]

        # start with unseen observations
        unseen_indices = list(
            xrange(self.none_priority_index + self.observations._capacity - 1, len(self.observations) + self.observations._capacity - 1))
        self.batch_indices = unseen_indices[:batch_size]

        # get remaining observations using weighted sampling
        remaining = batch_size - len(self.batch_indices)
        if remaining:
            samples = self.observations.sample_minibatch(remaining)
            sample_indices = [i for i, o in samples]
            self.batch_indices += sample_indices

        # shuffle
        np.random.shuffle(self.batch_indices)

        # collect observations
        for n, index in enumerate(self.batch_indices):
            observation, _ = self.observations._memory[index]

            for name, state in states.items():
                state[n] = observation[0][name]
            for name, action in actions.items():
                action[n] = observation[1][name]
            rewards[n] = observation[2]
            terminals[n] = observation[3]
            for k, internal in enumerate(internals):
                internal[n] = observation[4][k]
            if next_states:
                for name, next_state in next_states.items():
                    next_state[n] = observation[5][name]
                for k, next_internal in enumerate(next_internals):
                    next_internal[n] = observation[6][k]

        if next_states:
            return dict(states=states, actions=actions, rewards=rewards, terminals=terminals, internals=internals, next_states=next_states, next_internals=next_internals)
        else:
            return dict(states=states, actions=actions, rewards=rewards, terminals=terminals, internals=internals)

    def update_batch(self, loss_per_instance):
        """
        Computes priorities according to loss.

        Args:
            loss_per_instance:
        Returns:
        """
        if self.batch_indices is None:
            raise TensorForceError(
                "Need to call get_batch before each update_batch call.")
        if len(loss_per_instance) != len(self.batch_indices):
            raise TensorForceError(
                "For all instances a loss value has to be provided.")

        for index, loss in zip(self.batch_indices, loss_per_instance):
            new_priority = loss ** self.prioritization_weight
            self.observations._move(index, new_priority)
            self.none_priority_index += 1
