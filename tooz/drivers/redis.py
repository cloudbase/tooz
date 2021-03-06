# -*- coding: utf-8 -*-

#    Copyright (C) 2014 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from __future__ import absolute_import

import contextlib
import logging

from concurrent import futures
import msgpack
from oslo.utils import strutils
import redis
from redis import exceptions
import six

import tooz
from tooz import coordination
from tooz import locking
from tooz import utils

LOG = logging.getLogger(__name__)


@contextlib.contextmanager
def _translate_failures():
    """Translates common redis exceptions into tooz exceptions."""
    try:
        yield
    except (exceptions.ConnectionError, exceptions.TimeoutError) as e:
        raise coordination.ToozConnectionError(utils.exception_message(e))
    except exceptions.RedisError as e:
        raise coordination.ToozError(utils.exception_message(e))


class RedisLock(locking.Lock):
    def __init__(self, coord, client, name, timeout):
        self._name = "%s_%s_lock" % (coord.namespace, six.text_type(name))
        self._lock = client.lock(self._name, timeout=timeout)
        self._coord = coord
        self._acquired = False

    @property
    def name(self):
        return self._name

    def acquire(self, blocking=True):
        if blocking is True or blocking is False:
            blocking_timeout = None
        else:
            blocking_timeout = float(blocking)
            blocking = True
        with _translate_failures():
            self._acquired = self._lock.acquire(
                blocking=blocking, blocking_timeout=blocking_timeout)
            if self._acquired:
                self._coord._acquired_locks.add(self)
            return self._acquired

    def release(self):
        if not self._acquired:
            return False
        with _translate_failures():
            try:
                self._lock.release()
            except exceptions.LockError:
                return False
            self._coord._acquired_locks.discard(self)
            self._acquired = False
            return True

    def heartbeat(self):
        if self._acquired:
            with _translate_failures():
                self._lock.extend(self._lock.timeout)


class RedisDriver(coordination.CoordinationDriver):
    """Redis provides a few nice benefits that act as a poormans zookeeper.

    - Durability (when setup with AOF mode).
    - Consistent, note that this is still restricted to only
      one redis server, without the recently released redis (alpha)
      clustering > 1 server will not be consistent when partitions
      or failures occur (even redis clustering docs state it is
      not a fully AP or CP solution, which means even with it there
      will still be *potential* inconsistencies).
    - Master/slave failover (when setup with redis sentinel), giving
      some notion of HA (values *can* be lost when a failover transition
      occurs).

    Further resources/links:

    - http://redis.io/
    - http://redis.io/topics/sentinel
    - http://redis.io/topics/cluster-spec
    """

    # Redis deletes dictionaries that have no keys in them, which means the
    # key will disappear which means we can't tell the difference between
    # a group not existing and a group being empty without this key being
    # saved...
    _GROUP_EXISTS = '__created__'
    _NAMESPACE_SEP = ':'

    # These are used when extracting options from to make a client.
    #
    # See: http://redis-py.readthedocs.org/en/latest/ for how to use these
    # options to configure the underlying redis client...
    _CLIENT_ARGS = frozenset([
        'db',
        'encoding',
        'retry_on_timeout',
        'socket_keepalive',
        'socket_timeout',
        'ssl',
        'ssl_certfile',
        'ssl_keyfile',
    ])
    _CLIENT_BOOL_ARGS = frozenset([
        'retry_on_timeout',
        'ssl',
    ])
    _CLIENT_INT_ARGS = frozenset([
         'db',
         'socket_keepalive',
         'socket_timeout',
    ])
    _CLIENT_DEFAULT_SOCKET_TO = 30

    def __init__(self, member_id, parsed_url, options):
        super(RedisDriver, self).__init__()
        self._parsed_url = parsed_url
        self._options = options
        timeout = options.get('timeout', [self._CLIENT_DEFAULT_SOCKET_TO])
        self.timeout = int(timeout[-1])
        lock_timeout = options.get('lock_timeout', [self.timeout])
        self.lock_timeout = int(lock_timeout[-1])
        self._namespace = options.get('namespace', '_tooz')
        self._group_prefix = "%s_group" % (self._namespace)
        self._leader_prefix = "%s_leader" % (self._namespace)
        self._groups = "%s_groups" % (self._namespace)
        self._client = None
        self._member_id = member_id
        self._acquired_locks = set()
        self._joined_groups = set()
        self._executor = None
        self._started = False

    @property
    def namespace(self):
        return self._namespace

    @property
    def running(self):
        return self._started

    def get_lock(self, name):
        return RedisLock(self, self._client, name, self.lock_timeout)

    @staticmethod
    def _dumps(data):
        try:
            return msgpack.dumps(data)
        except (msgpack.PackException, ValueError) as e:
            raise coordination.ToozError(utils.exception_message(e))

    @staticmethod
    def _loads(blob):
        try:
            return msgpack.loads(blob)
        except (msgpack.UnpackException, ValueError) as e:
            raise coordination.ToozError(utils.exception_message(e))

    @classmethod
    def _make_client(cls, parsed_url, options, default_socket_timeout):
        kwargs = {}
        if parsed_url.hostname:
            kwargs['host'] = parsed_url.hostname
            if parsed_url.port:
                kwargs['port'] = parsed_url.port
        else:
            if not parsed_url.path:
                raise ValueError("Expected socket path in parsed urls path")
            kwargs['unix_socket_path'] = parsed_url.path
        if parsed_url.password:
            kwargs['password'] = parsed_url.password
        for a in cls._CLIENT_ARGS:
            if a not in options:
                continue
            # The reason the last index is used is that when multiple options
            # of the same name are given via a url the values will be
            # accumulated in a list (and not just be a single value)...
            #
            # For ex: the following is a valid url which will have 2 values
            # for the 'timeout' argument:
            #
            # redis://localhost:6379?timeout=5&timeout=2
            if a in cls._CLIENT_BOOL_ARGS:
                v = strutils.bool_from_string(options[a][-1])
            elif a in cls._CLIENT_INT_ARGS:
                v = int(options[a][-1])
            else:
                v = options[a][-1]
            kwargs[a] = v
        if 'socket_timeout' not in kwargs:
            kwargs['socket_timeout'] = default_socket_timeout
        return redis.StrictRedis(**kwargs)

    def _start(self):
        self._executor = futures.ThreadPoolExecutor(max_workers=1)
        try:
            self._client = self._make_client(self._parsed_url, self._options,
                                             self.timeout)
        except exceptions.RedisError as e:
            raise coordination.ToozConnectionError(utils.exception_message(e))
        else:
            # Ensure that the server is alive and not dead, this does not
            # ensure the server will always be alive, but does insure that it
            # at least is alive once...
            self.heartbeat()
            self._started = True

    @classmethod
    def _encode_member_id(cls, member_id):
        if member_id == cls._GROUP_EXISTS:
            raise ValueError("Not allowed to use private keys as a member id")
        return six.text_type(member_id)

    @staticmethod
    def _decode_member_id(member_id):
        return member_id

    def _encode_group_id(self, group_id):
        return self._NAMESPACE_SEP.join([self._group_prefix,
                                         six.text_type(group_id)])

    def _encode_group_leader(self, group_id):
        return self._NAMESPACE_SEP.join([self._leader_prefix,
                                         six.text_type(group_id)])

    def heartbeat(self):
        with _translate_failures():
            self._client.ping()
        for lock in self._acquired_locks:
            try:
                lock.heartbeat()
            except coordination.ToozError:
                LOG.warning("Unable to heartbeat lock '%s'", lock,
                            exc_info=True)

    def _stop(self):
        while self._acquired_locks:
            lock = self._acquired_locks.pop()
            try:
                lock.release()
            except coordination.ToozError:
                LOG.warning("Unable to release lock '%s'", lock, exc_info=True)
        while self._joined_groups:
            group_id = self._joined_groups.pop()
            try:
                self.leave_group(group_id).get()
            except coordination.ToozError:
                LOG.warning("Unable to leave group '%s'", group_id,
                            exc_info=True)
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None
        if self._client is not None:
            self._client = None
        self._started = False

    def _submit(self, cb, *args, **kwargs):
        if not self._started:
            raise coordination.ToozError("Redis driver has not been started")
        try:
            return self._executor.submit(cb, *args, **kwargs)
        except RuntimeError:
            raise coordination.ToozError("Redis driver asynchronous executor"
                                         " has been shutdown")

    def create_group(self, group_id):
        encoded_group = self._encode_group_id(group_id)

        def _create_group(p):
            if p.exists(encoded_group):
                raise coordination.GroupAlreadyExist(group_id)
            p.sadd(self._groups, group_id)
            # Add our special key to avoid redis from deleting the dictionary
            # when it becomes empty (which is not what we currently want)...
            p.hset(encoded_group, self._GROUP_EXISTS, '1')

        return RedisFutureResult(self._submit(self._client.transaction,
                                              _create_group, encoded_group,
                                              self._groups,
                                              value_from_callable=True))

    def update_capabilities(self, group_id, capabilities):
        encoded_group = self._encode_group_id(group_id)
        encoded_member_id = self._encode_member_id(self._member_id)

        def _update_capabilities(p):
            if not p.exists(encoded_group):
                raise coordination.GroupNotCreated(group_id)
            if not p.hexists(encoded_group, encoded_member_id):
                raise coordination.MemberNotJoined(group_id, self._member_id)
            else:
                p.hset(encoded_group, encoded_member_id,
                       self._dumps(capabilities))

        return RedisFutureResult(self._submit(self._client.transaction,
                                              _update_capabilities,
                                              encoded_group,
                                              value_from_callable=True))

    def leave_group(self, group_id):
        encoded_group = self._encode_group_id(group_id)
        encoded_member_id = self._encode_member_id(self._member_id)

        def _leave_group(p):
            if not p.exists(encoded_group):
                raise coordination.GroupNotCreated(group_id)
            c = p.hdel(encoded_group, encoded_member_id)
            if c == 0:
                raise coordination.MemberNotJoined(group_id, self._member_id)
            else:
                self._joined_groups.discard(group_id)

        return RedisFutureResult(self._submit(self._client.transaction,
                                              _leave_group, encoded_group,
                                              value_from_callable=True))

    def get_members(self, group_id):
        encoded_group = self._encode_group_id(group_id)

        def _get_members(p):
            if not p.exists(encoded_group):
                raise coordination.GroupNotCreated(group_id)
            members = []
            for m in p.hkeys(encoded_group):
                if m != self._GROUP_EXISTS:
                    members.append(self._decode_member_id(m))
            return members

        return RedisFutureResult(self._submit(self._client.transaction,
                                              _get_members, encoded_group,
                                              value_from_callable=True))

    def get_member_capabilities(self, group_id, member_id):
        encoded_group = self._encode_group_id(group_id)
        encoded_member_id = self._encode_member_id(member_id)

        def _get_member_capabilities(p):
            if not p.exists(encoded_group):
                raise coordination.GroupNotCreated(group_id)
            capabilities = p.hget(encoded_group, encoded_member_id)
            if capabilities is None:
                raise coordination.MemberNotJoined(group_id, member_id)
            return self._loads(capabilities)

        return RedisFutureResult(self._submit(self._client.transaction,
                                              _get_member_capabilities,
                                              encoded_group,
                                              value_from_callable=True))

    def join_group(self, group_id, capabilities=b""):
        encoded_group = self._encode_group_id(group_id)
        encoded_member_id = self._encode_member_id(self._member_id)

        def _join_group(p):
            if not p.exists(encoded_group):
                raise coordination.GroupNotCreated(group_id)
            c = p.hset(encoded_group, encoded_member_id,
                       self._dumps(capabilities))
            if c == 0:
                # Field already exists...
                raise coordination.MemberAlreadyExist(group_id,
                                                      self._member_id)
            else:
                self._joined_groups.add(group_id)

        return RedisFutureResult(self._submit(self._client.transaction,
                                              _join_group,
                                              encoded_group,
                                              value_from_callable=True))

    def get_groups(self):

        def _get_groups():
            results = []
            for g in self._client.smembers(self._groups):
                results.append(g)
            return results

        return RedisFutureResult(self._submit(_get_groups))

    def _init_watch_group(self, group_id):
        members = self.get_members(group_id)
        self._group_members[group_id].update(members.get(timeout=None))

    def watch_join_group(self, group_id, callback):
        self._init_watch_group(group_id)
        return super(RedisDriver, self).watch_join_group(group_id, callback)

    def unwatch_join_group(self, group_id, callback):
        return super(RedisDriver, self).unwatch_join_group(group_id, callback)

    def watch_leave_group(self, group_id, callback):
        self._init_watch_group(group_id)
        return super(RedisDriver, self).watch_leave_group(group_id, callback)

    def unwatch_leave_group(self, group_id, callback):
        return super(RedisDriver, self).unwatch_leave_group(group_id, callback)

    @staticmethod
    def watch_elected_as_leader(group_id, callback):
        raise tooz.NotImplemented

    @staticmethod
    def unwatch_elected_as_leader(group_id, callback):
        raise tooz.NotImplemented

    def run_watchers(self, timeout=None):
        result = []
        for group_id in self.get_groups().get(timeout=timeout):
            group_members = set(self.get_members(group_id)
                                .get(timeout=timeout))
            old_group_members = self._group_members.get(group_id, set())
            for member_id in (group_members - old_group_members):
                result.extend(
                    self._hooks_join_group[group_id].run(
                        coordination.MemberJoinedGroup(group_id,
                                                       member_id)))
            for member_id in (old_group_members - group_members):
                result.extend(
                    self._hooks_leave_group[group_id].run(
                        coordination.MemberLeftGroup(group_id,
                                                     member_id)))
            self._group_members[group_id] = group_members
        return result


class RedisFutureResult(coordination.CoordAsyncResult):
    """Redis asynchronous result that references a future."""

    def __init__(self, fut):
        self._fut = fut

    def get(self, timeout=10):
        try:
            # Late translate the common failures since the redis client
            # may throw things that we can not catch in the callbacks where
            # it is used (especially one that uses the transaction
            # method).
            with _translate_failures():
                return self._fut.result(timeout=timeout)
        except futures.TimeoutError as e:
            raise coordination.OperationTimedOut(utils.exception_message(e))

    def done(self):
        return self._fut.done()
