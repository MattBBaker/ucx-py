# Copyright (c) 2019-2020, NVIDIA CORPORATION. All rights reserved.
# See file LICENSE for terms.

import asyncio
import gc
import logging
import os
import struct
import weakref
from functools import partial
from os import close as close_fd
from random import randint
from functools import singledispatch
from io import RawIOBase

import psutil

from . import comm
from ._libs import ucx_api
from ._libs.utils import get_buffer_nbytes
from .continuous_ucx_progress import BlockingMode, NonBlockingMode
from .exceptions import UCXCanceled, UCXCloseError, UCXError
from .utils import hash64bits, nvtx_annotate

logger = logging.getLogger("ucx")

# The module should only instantiate one instance of the application context
# However, the init of CUDA must happen after all process forks thus we delay
# the instantiation of the application context to the first use of the API.
_ctx = None

#return codes for lower level APIs to remain compatible with UCX.
api_constants = ucx_api.get_constants()
OK = api_constants["OK"]
INPROGRESS = api_constants["INPROGRESS"]


def _get_ctx():
    global _ctx
    if _ctx is None:
        _ctx = ApplicationContext()
    return _ctx


async def exchange_peer_info(
    endpoint, msg_tag, ctrl_tag, guarantee_msg_order, port, listener
):
    """Help function that exchange endpoint information"""

    # Pack peer information incl. a checksum
    fmt = "QQ?QQ"
    my_info = struct.pack(
        fmt,
        msg_tag,
        ctrl_tag,
        guarantee_msg_order,
        port,
        hash64bits(msg_tag, ctrl_tag, guarantee_msg_order, port),
    )
    peer_info = bytearray(len(my_info))

    # Send/recv peer information. Notice, we force an `await` between the two
    # streaming calls (see <https://github.com/rapidsai/ucx-py/pull/509>)
    if listener is True:
        await comm.stream_send(endpoint, my_info, len(my_info))
        await comm.stream_recv(endpoint, peer_info, len(peer_info))
    else:
        await comm.stream_recv(endpoint, peer_info, len(peer_info))
        await comm.stream_send(endpoint, my_info, len(my_info))

    # Unpacking and sanity check of the peer information
    ret = {}
    (
        ret["msg_tag"],
        ret["ctrl_tag"],
        ret["guarantee_msg_order"],
        ret["port"],
        ret["checksum"],
    ) = struct.unpack(fmt, peer_info)

    expected_checksum = hash64bits(
        ret["msg_tag"], ret["ctrl_tag"], ret["guarantee_msg_order"], ret["port"]
    )

    if expected_checksum != ret["checksum"]:
        raise RuntimeError(
            f'Checksum invalid! {hex(expected_checksum)} != {hex(ret["checksum"])}'
        )

    if port != ret["port"]:
        raise RuntimeError(f'Port mismatch! {port} != {ret["port"]}')

    if ret["guarantee_msg_order"] != guarantee_msg_order:
        raise ValueError("Both peers must set guarantee_msg_order identically")

    return ret


class CtrlMsg:
    """Implementation of control messages

    For now we have one opcode `1` which means shutdown.
    The opcode takes `close_after_n_recv`, which is the number of
    messages to receive before the worker should close.
    """

    fmt = "QQ"
    nbytes = struct.calcsize(fmt)

    @staticmethod
    def serialize(opcode, close_after_n_recv):
        return struct.pack(CtrlMsg.fmt, int(opcode), int(close_after_n_recv))

    @staticmethod
    def deserialize(serialized_bytes):
        return struct.unpack(CtrlMsg.fmt, serialized_bytes)

    @staticmethod
    def handle_ctrl_msg(ep_weakref, log, msg, future):
        """Function that is called when receiving the control message"""
        try:
            future.result()
        except UCXCanceled:
            return  # The ctrl signal was canceled
        logger.debug(log)
        ep = ep_weakref()
        if ep is None or ep.closed():
            return  # The endpoint is closed

        opcode, close_after_n_recv = CtrlMsg.deserialize(msg)
        if opcode == 1:
            ep.close_after_n_recv(close_after_n_recv, count_from_ep_creation=True)
        else:
            raise UCXError("Received unknown control opcode: %s" % opcode)

    @staticmethod
    def setup_ctrl_recv(ep):
        """Help function to setup the receive of the control message"""
        log = "[Recv shutdown] ep: %s, tag: %s" % (hex(ep.uid), hex(ep._ctrl_tag_recv),)
        msg = bytearray(CtrlMsg.nbytes)
        shutdown_fut = comm.tag_recv(
            ep._ep, msg, len(msg), ep._ctrl_tag_recv, name=log,
        )

        shutdown_fut.add_done_callback(
            partial(CtrlMsg.handle_ctrl_msg, weakref.ref(ep), log, msg)
        )


async def _listener_handler_coroutine(
    conn_request, ctx, func, port, guarantee_msg_order, endpoint_error_handling
):
    # We create the Endpoint in four steps:
    #  1) Generate unique IDs to use as tags
    #  2) Exchange endpoint info such as tags
    #  3) Use the info to create the an endpoint
    seed = os.urandom(16)
    msg_tag = hash64bits("msg_tag", seed, port)
    ctrl_tag = hash64bits("ctrl_tag", seed, port)

    endpoint = ctx.worker.ep_create_from_conn_request(
        conn_request, endpoint_error_handling
    )
    peer_info = await exchange_peer_info(
        endpoint=endpoint,
        msg_tag=msg_tag,
        ctrl_tag=ctrl_tag,
        guarantee_msg_order=guarantee_msg_order,
        listener=True,
        port=port,
    )
    ep = Endpoint(
        endpoint=endpoint,
        ctx=ctx,
        msg_tag_send=peer_info["msg_tag"],
        msg_tag_recv=msg_tag,
        ctrl_tag_send=peer_info["ctrl_tag"],
        ctrl_tag_recv=ctrl_tag,
        guarantee_msg_order=guarantee_msg_order,
    )

    logger.debug(
        "_listener_handler() server: %s, msg-tag-send: %s, "
        "msg-tag-recv: %s, ctrl-tag-send: %s, ctrl-tag-recv: %s"
        % (
            hex(endpoint.handle),
            hex(ep._msg_tag_send),
            hex(ep._msg_tag_recv),
            hex(ep._ctrl_tag_send),
            hex(ep._ctrl_tag_recv),
        )
    )

    # Setup the control receive
    CtrlMsg.setup_ctrl_recv(ep)

    # Removing references here to avoid delayed clean up
    del ctx

    # Finally, we call `func`
    if asyncio.iscoroutinefunction(func):
        await func(ep)
    else:
        func(ep)


def _listener_handler(
    conn_request, callback_func, port, ctx, guarantee_msg_order, endpoint_error_handling
):
    asyncio.ensure_future(
        _listener_handler_coroutine(
            conn_request,
            ctx,
            callback_func,
            port,
            guarantee_msg_order,
            endpoint_error_handling,
        )
    )


def _epoll_fd_finalizer(epoll_fd, progress_tasks):
    assert epoll_fd >= 0
    # Notice, progress_tasks must be cleared before we close
    # epoll_fd
    progress_tasks.clear()
    close_fd(epoll_fd)


class ApplicationContext:
    """
    The context of the Asyncio interface of UCX.
    """

    def __init__(self, config_dict={}, blocking_progress_mode=None):
        self.progress_tasks = []

        # For now, a application context only has one worker
        self.context = ucx_api.UCXContext(config_dict)
        self.worker = ucx_api.UCXWorker(self.context)

        if blocking_progress_mode is not None:
            self.blocking_progress_mode = blocking_progress_mode
        elif "UCXPY_NON_BLOCKING_MODE" in os.environ:
            self.blocking_progress_mode = False
        else:
            self.blocking_progress_mode = True

        if self.blocking_progress_mode:
            self.epoll_fd = self.worker.init_blocking_progress_mode()
            weakref.finalize(
                self, _epoll_fd_finalizer, self.epoll_fd, self.progress_tasks
            )

    def create_listener(
        self, callback_func, port, guarantee_msg_order, endpoint_error_handling=False
    ):

        """Create and start a listener to accept incoming connections

        callback_func is the function or coroutine that takes one
        argument -- the Endpoint connected to the client.

        In order to call ucp.reset() inside callback_func remember to
        close the Endpoint given as an argument. It is not enough to

        Also notice, the listening is closed when the returned Listener
        goes out of scope thus remember to keep a reference to the object.

        Parameters
        ----------
        callback_func: function or coroutine
            A callback function that gets invoked when an incoming
            connection is accepted
        port: int, optional
            An unused port number for listening
        guarantee_msg_order: boolean, optional
            Whether to guarantee message order or not. Remember, both peers
            of the endpoint must set guarantee_msg_order to the same value.
        endpoint_error_handling: boolean, optional
            Enable endpoint error handling raising exceptions when an error
            occurs, may incur in performance penalties but prevents a process
            from terminating unexpectedly that may happen when disabled.

        Returns
        -------
        Listener
            The new listener. When this object is deleted, the listening stops
        """
        self.continuous_ucx_progress()
        if port in (None, 0):
            # Get a random port number and check if it's not used yet. Doing this
            # without relying on `socket` allows preventing UCX errors such as
            # "none of the available transports can listen for connections", due
            # to the socket still being in TIME_WAIT state.
            try:
                with open("/proc/sys/net/ipv4/ip_local_port_range") as f:
                    start_port, end_port = [int(i) for i in next(f).split()]
            except FileNotFoundError:
                start_port, end_port = (32768, 60000)

            used_ports = set(conn.laddr[1] for conn in psutil.net_connections())
            while True:
                port = randint(start_port, end_port)

                if port not in used_ports:
                    break

        logger.info("create_listener() - Start listening on port %d" % port)
        ret = Listener(
            ucx_api.UCXListener(
                worker=self.worker,
                port=port,
                cb_func=_listener_handler,
                cb_args=(
                    callback_func,
                    port,
                    self,
                    guarantee_msg_order,
                    endpoint_error_handling,
                ),
            )
        )
        return ret

    async def create_endpoint(
        self, ip_address, port, guarantee_msg_order, endpoint_error_handling=False
    ):
        """Create a new endpoint to a server

        Parameters
        ----------
        ip_address: str
            IP address of the server the endpoint should connect to
        port: int
            IP address of the server the endpoint should connect to
        guarantee_msg_order: boolean, optional
            Whether to guarantee message order or not. Remember, both peers
            of the endpoint must set guarantee_msg_order to the same value.
        endpoint_error_handling: boolean, optional
            Enable endpoint error handling raising exceptions when an error
            occurs, may incur in performance penalties but prevents a process
            from terminating unexpectedly that may happen when disabled.

        Returns
        -------
        Endpoint
            The new endpoint
        """
        self.continuous_ucx_progress()
        ucx_ep = self.worker.ep_create(ip_address, port, endpoint_error_handling)
        self.worker.progress()
        print("Post progress")
        # We create the Endpoint in four steps:
        #  1) Generate unique IDs to use as tags
        #  2) Exchange endpoint info such as tags
        #  3) Use the info to create an endpoint
        seed = os.urandom(16)
        msg_tag = hash64bits("msg_tag", seed, port)
        ctrl_tag = hash64bits("ctrl_tag", seed, port)
        print("Exchanging peer info")
        peer_info = await exchange_peer_info(
            endpoint=ucx_ep,
            msg_tag=msg_tag,
            ctrl_tag=ctrl_tag,
            guarantee_msg_order=guarantee_msg_order,
            listener=False,
            port=port,
        )
        ep = Endpoint(
            endpoint=ucx_ep,
            ctx=self,
            msg_tag_send=peer_info["msg_tag"],
            msg_tag_recv=msg_tag,
            ctrl_tag_send=peer_info["ctrl_tag"],
            ctrl_tag_recv=ctrl_tag,
            guarantee_msg_order=guarantee_msg_order,
        )
        print("EP class made")
        logger.debug(
            "create_endpoint() client: %s, msg-tag-send: %s, "
            "msg-tag-recv: %s, ctrl-tag-send: %s, ctrl-tag-recv: %s"
            % (
                hex(ep._ep.handle),
                hex(ep._msg_tag_send),
                hex(ep._msg_tag_recv),
                hex(ep._ctrl_tag_send),
                hex(ep._ctrl_tag_recv),
            )
        )

        # Setup the control receive
        CtrlMsg.setup_ctrl_recv(ep)
        print("About to return")
        return ep

    def create_ucp_endpoint(self, buffer, guarantee_msg_order):
        """Create a new endpoint to a peer

        Parameters
        ----------
        buffer: buffer
            Buffer object that points to a ucp_address_t pointer.
        guarantee_msg_order: boolean, optional
            Whether to guarantee message order or not. Remember, both peers
            of the endpoint must set guarantee_msg_order to the same value.
        Returns
        -------
        Endpoint
            The new endpoint
        """
        ucx_ep = self.worker.ucp_ep_create(buffer)
        print("Calling into create code")
        ep = Endpoint(
            endpoint=ucx_ep,
            ctx=self,
            msg_tag_send=None,
            msg_tag_recv=None,
            ctrl_tag_send=None,
            ctrl_tag_recv=None,
            guarantee_msg_order=guarantee_msg_order,
        )
        return ep

    def continuous_ucx_progress(self, event_loop=None):
        """Guarantees continuous UCX progress

        Use this function to associate UCX progress with an event loop.
        Notice, multiple event loops can be associate with UCX progress.

        This function is automatically called when calling
        `create_listener()` or `create_endpoint()`.

        Parameters
        ----------
        event_loop: asyncio.event_loop, optional
            The event loop to evoke UCX progress. If None,
            `asyncio.get_event_loop()` is used.
        """
        loop = event_loop if event_loop is not None else asyncio.get_event_loop()
        if loop in self.progress_tasks:
            return  # Progress has already been guaranteed for the current event loop

        if self.blocking_progress_mode:
            task = BlockingMode(self.worker, loop, self.epoll_fd)
        else:
            task = NonBlockingMode(self.worker, loop)
        self.progress_tasks.append(task)

    def get_ucp_worker(self):
        """Returns the underlying UCP worker handle (ucp_worker_h)
        as a Python integer.
        """
        return self.worker.handle

    def get_config(self):
        """Returns all UCX configuration options as a dict.

        Returns
        -------
        dict
            The current UCX configuration options
        """
        return self.context.get_config()

    def get_address(self):
        return ucx_api.UCXAddress(self.get_ucp_worker())

    def mem_map(self, mem, alloc=False, fixed=False):
        return self.context.mem_map(mem, alloc, fixed)

    def flush_worker(self):
        return self.worker.flush()

    def fence_worker(self):
        return self.worker.fence()



class Listener:
    """A handle to the listening service started by `create_listener()`

    The listening continues as long as this object exist or `.close()` is called.
    Please use `create_listener()` to create an Listener.
    """

    def __init__(self, backend):
        assert backend.initialized
        self._b = backend

    def closed(self):
        """Is the listener closed?"""
        return not self._b.initialized

    @property
    def port(self):
        """The network point listening on"""
        return self._b.port

    def close(self):
        """Closing the listener"""
        self._b.close()


class Endpoint:
    """An endpoint represents a connection to a peer

    Please use `create_listener()` and `create_endpoint()`
    to create an Endpoint.
    """

    def __init__(
        self,
        endpoint,
        ctx,
        msg_tag_send,
        msg_tag_recv,
        ctrl_tag_send,
        ctrl_tag_recv,
        guarantee_msg_order,
    ):
        self._ep = endpoint
        self._ctx = ctx
        self._msg_tag_send = msg_tag_send
        self._msg_tag_recv = msg_tag_recv
        self._ctrl_tag_send = ctrl_tag_send
        self._ctrl_tag_recv = ctrl_tag_recv
        self._guarantee_msg_order = guarantee_msg_order
        self._send_count = 0  # Number of calls to self.send()
        self._recv_count = 0  # Number of calls to self.recv()
        self._finished_recv_count = 0  # Number of returned (finished) self.recv() calls
        self._shutting_down_peer = False  # Told peer to shutdown
        # UCX supports CUDA if "cuda" is part of the TLS or TLS is "all"
        tls = ctx.get_config()["TLS"]
        self._cuda_support = "cuda" in tls or tls == "all"
        self._close_after_n_recv = None

    @property
    def uid(self):
        """The unique ID of the underlying UCX endpoint"""
        return self._ep.handle

    def closed(self):
        """Is this endpoint closed?"""
        return self._ep is None or not self._ep.initialized

    def abort(self):
        """Close the communication immediately and abruptly.
        Useful in destructors or generators' ``finally`` blocks.

        Notice, this functions doesn't signal the connected peer to close.
        To do that, use `Endpoint.close()`
        """
        if self.closed():
            return
        logger.debug("Endpoint.abort(): %s" % hex(self.uid))
        self._ep.close()
        self._ep = None
        self._ctx = None

    async def close(self):
        """Close the endpoint cleanly.
        This will attempt to flush outgoing buffers before actually
        closing the underlying UCX endpoint.
        """
        if self.closed():
            return
        try:
            # Making sure we only tell peer to shutdown once
            if self._shutting_down_peer:
                return
            self._shutting_down_peer = True

            # Send a shutdown message to the peer
            msg = CtrlMsg.serialize(opcode=1, close_after_n_recv=self._send_count)
            log = "[Send shutdown] ep: %s, tag: %s, close_after_n_recv: %d" % (
                hex(self.uid),
                hex(self._ctrl_tag_send),
                self._send_count,
            )
            logger.debug(log)
            try:
                await comm.tag_send(
                    self._ep, msg, len(msg), self._ctrl_tag_send, name=log,
                )
            # The peer might already be shutting down thus we can ignore any send errors
            except UCXError as e:
                logging.warning(
                    "UCX failed closing worker %s (probably already closed): %s"
                    % (hex(self.uid), repr(e))
                )
        finally:
            if not self.closed():
                # Give all current outstanding send() calls a chance to return
                self._ctx.worker.progress()
                await asyncio.sleep(0)
                self.abort()

    @nvtx_annotate("UCXPY_SEND", color="green", domain="ucxpy")
    async def send(self, buffer, nbytes=None, tag=None):
        """Send `buffer` to connected peer.

        Parameters
        ----------
        buffer: exposing the buffer protocol or array/cuda interface
            The buffer to send. Raise ValueError if buffer is smaller
            than nbytes.
        nbytes: int, optional
            Number of bytes to send. Default is the whole buffer.
        tag: hashable, optional
            Set a tag that the receiver must match.
        """
        if self.closed():
            raise UCXCloseError("Endpoint closed")
        nbytes = get_buffer_nbytes(
            buffer, check_min_size=nbytes, cuda_support=self._cuda_support
        )
        log = "[Send #%03d] ep: %s, tag: %s, nbytes: %d, type: %s" % (
            self._send_count,
            hex(self.uid),
            hex(self._msg_tag_send),
            nbytes,
            type(buffer),
        )
        logger.debug(log)
        self._send_count += 1
        if tag is None:
            tag = self._msg_tag_send
        else:
            tag = hash64bits(self._msg_tag_send, hash(tag))
        if self._guarantee_msg_order:
            tag += self._send_count
        return await comm.tag_send(self._ep, buffer, nbytes, tag, name=log)

    @nvtx_annotate("UCXPY_RECV", color="red", domain="ucxpy")
    async def recv(self, buffer, nbytes=None, tag=None):
        """Receive from connected peer into `buffer`.

        Parameters
        ----------
        buffer: exposing the buffer protocol or array/cuda interface
            The buffer to receive into. Raise ValueError if buffer
            is smaller than nbytes or read-only.
        nbytes: int, optional
            Number of bytes to receive. Default is the whole buffer.
        tag: hashable, optional
            Set a tag that must match the received message. Notice, currently
            UCX-Py doesn't support a "any tag" thus `tag=None` only matches a
            send that also sets `tag=None`.
        """
        if self.closed():
            raise UCXCloseError("Endpoint closed")
        nbytes = get_buffer_nbytes(
            buffer, check_min_size=nbytes, cuda_support=self._cuda_support
        )
        log = "[Recv #%03d] ep: %s, tag: %s, nbytes: %d, type: %s" % (
            self._recv_count,
            hex(self.uid),
            hex(self._msg_tag_recv),
            nbytes,
            type(buffer),
        )
        logger.debug(log)
        self._recv_count += 1
        if tag is None:
            tag = self._msg_tag_recv
        else:
            tag = hash64bits(self._msg_tag_recv, hash(tag))
        if self._guarantee_msg_order:
            tag += self._recv_count
        ret = await comm.tag_recv(self._ep, buffer, nbytes, tag, name=log)
        self._finished_recv_count += 1
        if (
            self._close_after_n_recv is not None
            and self._finished_recv_count >= self._close_after_n_recv
        ):
            self.abort()
        return ret

    def cuda_support(self):
        """Return whether UCX is configured with CUDA support or not"""
        return self._cuda_support

    def get_ucp_worker(self):
        """Returns the underlying UCP worker handle (ucp_worker_h)
        as a Python integer.
        """
        return self._ctx.worker.handle

    def get_ucp_endpoint(self):
        """Returns the underlying UCP endpoint handle (ucp_ep_h)
        as a Python integer.
        """
        return self._ep.handle

    def ucx_info(self):
        """Return low-level UCX info about this endpoint as a string"""
        return self._ep.info()

    def close_after_n_recv(self, n, count_from_ep_creation=False):
        """Close the endpoint after `n` received messages.

        Parameters
        ----------
        n: int
            Number of messages to received before closing the endpoint.
        count_from_ep_creation: bool, optional
            Whether to count `n` from this function call (default) or
            from the creation of the endpoint.
        """
        if not count_from_ep_creation:
            n += self._finished_recv_count  # Make `n` absolute
        if self._close_after_n_recv is not None:
            raise UCXError(
                "close_after_n_recv has already been set to: %d (abs)"
                % self._close_after_n_recv
            )
        if n == self._finished_recv_count:
            self.abort()
        elif n > self._finished_recv_count:
            self._close_after_n_recv = n
        else:
            raise UCXError(
                "`n` cannot be less than current recv_count: %d (abs) < %d (abs)"
                % (n, self._finished_recv_count)
            )
    def unpack_rkey(self, rkey):
        """Unpack an rkey on this Endpoint. Returns a RemoteMem object that can
           be use for RMA/AMO operations            
        """
        _rkey = self._ep.unpack_rkey(rkey)
        return RemoteMemory(_rkey, self)


# The following functions initialize and use a single ApplicationContext instance


def init(options={}, env_takes_precedence=False, blocking_progress_mode=None):
    """Initiate UCX.

    Usually this is done automatically at the first API call
    but this function makes it possible to set UCX options programmable.
    Alternatively, UCX options can be specified through environment variables.

    Parameters
    ----------
    options: dict, optional
        UCX options send to the underlying UCX library
    env_takes_precedence: bool, optional
        Whether environment variables takes precedence over the `options`
        specified here.
    blocking_progress_mode: bool, optional
        If None, blocking UCX progress mode is used unless the environment variable
        `UCXPY_NON_BLOCKING_MODE` is defined.
        Otherwise, if True blocking mode is used and if False non-blocking mode is used.
    """
    global _ctx
    if _ctx is not None:
        raise RuntimeError(
            "UCX is already initiated. Call reset() and init() "
            "in order to re-initate UCX with new options."
        )
    if env_takes_precedence:
        for k in os.environ.keys():
            if k in options:
                del options[k]

    _ctx = ApplicationContext(options, blocking_progress_mode=blocking_progress_mode)


def reset():
    """Resets the UCX library by shutting down all of UCX.

    The library is initiated at next API call.
    """
    global _ctx
    if _ctx is not None:
        weakref_ctx = weakref.ref(_ctx)
        _ctx = None
        gc.collect()
        if weakref_ctx() is not None:
            msg = (
                "Trying to reset UCX but not all Endpoints and/or Listeners "
                "are closed(). The following objects are still referencing "
                "ApplicationContext: "
            )
            for o in gc.get_referrers(weakref_ctx()):
                msg += "\n  %s" % str(o)
            raise UCXError(msg)


def get_ucx_version():
    """Return the version of the underlying UCX installation

    Notice, this function doesn't initialize UCX.

    Returns
    -------
    tuple
        The version as a tuple e.g. (1, 7, 0)
    """
    return ucx_api.get_ucx_version()


def progress():
    """Try to progress the communication layer

    Returns
    -------
    bool
        Returns True if progress was made
    """
    return _get_ctx().worker.progress()


def get_config():
    """Returns all UCX configuration options as a dict.

    If UCX is uninitialized, the options returned are the
    options used if UCX were to be initialized now.
    Notice, this function doesn't initialize UCX.

    Returns
    -------
    dict
        The current UCX configuration options
    """

    if _ctx is None:
        return ucx_api.get_current_options()
    else:
        return _get_ctx().get_config()


def create_listener(
    callback_func, port=None, guarantee_msg_order=False, endpoint_error_handling=False
):
    return _get_ctx().create_listener(
        callback_func,
        port,
        guarantee_msg_order,
        endpoint_error_handling=endpoint_error_handling,
    )


async def create_endpoint(
    ip_address, port, guarantee_msg_order=False, endpoint_error_handling=False
):
    return await _get_ctx().create_endpoint(
        ip_address,
        port,
        guarantee_msg_order,
        endpoint_error_handling=endpoint_error_handling,
    )

@singledispatch
def mem_map(memory):
    """Map memory and register memory for use in RMA and AMO operations on this context.
       This function may either recieve an object with backing memory and register that,
       or it may allocate memory and return a new handle. After this remote hardware may
       directly access this memory without intervention of the local CPU.
       Returns a MemoryHandle object that maybe used in other UCX APIs
    ----------
    memory: int or object that implments buffer protocol
        If memory is an int, then a region of memory will be allocated and registered at a minimum of that size.
        If memory is a buffer, then the memory region containing that buffer object will be registered
    """
    return _get_ctx().mem_map(memory, alloc=False)

@mem_map.register
def _(memory: int):
    return _get_ctx().mem_map(memory, alloc=True)

def continuous_ucx_progress(event_loop=None):
    _get_ctx().continuous_ucx_progress(event_loop=event_loop)


def get_ucp_worker():
    return _get_ctx().get_ucp_worker()


def create_ucp_endpoint(buffer, guarantee_msg_order):
    return _get_ctx().create_ucp_endpoint(buffer, guarantee_msg_order)


def get_ucp_worker_address():
    """Returns a WorkerAddress object that wraps a
    ucp_worker_address_h handle in a python object. This object
    can be passed to `create_endpoint()` to create an endpoint
    to this worker"""
    return _get_ctx().get_address()


def flush():
    """Flushes outstanding AMO and RMA operations. This ensures that the
       operations issued on this worker have completed both locally and remotely.
       This function does not guarentee ordering. If more operations are issued
       after flush is called and before the future has completed then those ops may
       complete before outstanding operations in the flush have finished.
    """
    if _ctx is not None:
        return _get_ctx().flush_worker()
    return OK

    
def fence():
    """Ensures ordering of non-blocking communication operations on the UCP worker.
       This function returns nothing, but will raise an error if it cannot make
       this guarantee. This function does not ensure any operations have completed.

       NOTE: Some transports cannot guarentee ordering and will always raise
       an error if there are outstanding operations. This means that sane
       useage of fence should not use retry loops. See `flush` instead.

       Example:
       # Code that does RMA ops that need to complete first
       # Now to ensure order
       try:
           ucp.fence()
       except UCXError:
           await ucp.flush()
       # Continue with more RMA operations
    """
    if _ctx is not None:
        if not _get_ctx().fence_worker():
            raise exceptions.UCXError("Could not fence.")
    return OK

class MemoryHandle:
    """This class represents a memory handle registered to UCX. This memory is registered
       with a NIC (eg, a Mellanox IB card) for high speed RMA/AMO operations from remote nodes
       without interrupting the host CPU.
    """
    def __init__(self, memh):
        self._memh = memh
    def pack_rkey(self):
        """Pack a remote key (rkey). This rkey will have all the information a remote
           node will need to do RMA/AMO operations on the memory of the local MemHandle.
           This key will pack in a buffer for distribution with either an in band mechanism,
           such as tag_send()/tag_recv() or an out of band machanism such as PMI.
        """
        return self._memh.pack_rkey()

    @property
    def address(self):
        return self._memh.address()

    @property
    def length(self):
        return self._memh.length()

class RemoteMemory:
    """This class represents an unpacked rkey and associated meta data to do RMA/AMO
       operations with the memory represented by the packed rkey
    """
    def __init__(self, rkey, ep, explicit=False):
        self.ep = ep
        self._rkey = rkey
        self._base = 0
        if explicit:
            self.put = self.put_nb
            self.get = self.get_nb
        else:
            self.put = self.put_nbi
            self.get = self.get_nbi

    def put_nb(self, memory, size=None, dest=None):
        """RMA put operation. Takes the memory specified in the buffer object and writes it.
        If the first parameter is a UcpBuffer then it is the only parmeter needed.
        If the first parameter is is any other buffer object, then a second start
        parameter is needed to specify where in remote memory the object should be written.

        Returns
        -------
        UCXRequest
            request object that holds metadata about the underlaying driver's progress
        """
        if dest is None:
            dest = self._base
        if size is None:
            size = get_buffer_nbytes(memory, None, self.ep.cuda_support())
        return ucx_api.put_nb(memory, size, dest, self._rkey)

    def get_nb(self, memory, size=None, dest=None):
        """RMA get operation. Reads remote memory into a local buffer
        If the first parameter is a UcpBuffer then it is the only parmeter needed.
        If the first parameter is is any other buffer object, then a second start
        parameter is needed to specify where in remote memory the object should be read.

        Returns
        -------
        UCXRequest
            request object that holds metadata about the underlaying driver's progress
        """
        if dest is None:
            dest = self._base
        if size is None:
            size = get_buffer_nbytes(memory, None, self.ep.cuda_support())
        return ucx_api.get_nb(memory, size, dest, self._rkey)

    def put_nbi(self, memory, size=None, dest=None):
        """RMA put operation. Takes the memory specified in the buffer object and writes it.
        If the first parameter is a UcpBuffer then it is the only parmeter needed.
        If the first parameter is is any other buffer object, then a second start
        parameter is needed to specify where in remote memory the object should be written.

        Returns
        -------
        UCS_OK
            Buffer maybe reused immediately
        UCS_INPROGRESS
            Buffer is in use by the underlying driver and not safe for reuse until the worker is flushed
        """
        if dest is None:
            dest = self._base
        if size is None:
            size = get_buffer_nbytes(memory, None, self.ep.cuda_support())
        return ucx_api.put_nbi(memory, size, dest, self._rkey)

    def get_nbi(self, memory, size=None, dest=None):
        """RMA get operation. Reads remote memory into a local buffer
        If the first parameter is a UcpBuffer then it is the only parmeter needed.
        If the first parameter is is any other buffer object, then a second start
        parameter is needed to specify where in remote memory the object should be read.

        Returns
        -------
        UCS_OK
            Buffer is ready for use immediately
        UCS_INPROGRESS
            Buffer is in use by the underlying driver and not safe for use until the worker is flushed
        """
        if dest is None:
            dest = 0
        if size is None:
            get_buffer_nbytes(memory, None, self.ep.cuda_support())
        return ucx_api.get_nbi(memory, size, dest, self._rkey)


class UcxIO(RawIOBase):
    """A class to simulate python streams backed by UCX RMA operations"""
    def __init__(self, ep, dest, length, rkey):
        self.pos = 0
        self.rkey = rkey
        self.remote_addr = dest
        self.length = length
        self.rmem = ep.unpack_rkey(self.rkey)
        self.ep = ep

    def block_on_request(self, req):
        if req == OK:
            return
        status = req.check_status()
        while status != OK:
            progress()
            status = req.check_status()

    def readinto(self, buff):
        size = get_buffer_nbytes(buff, None, self.ep.cuda_support())
        if self.pos + size > self.length:
            size = self.length - self.pos
        req = self.rmem.get_nb(buff, size, self.remote_addr + self.pos)
        self.block_on_request(req)
        self.pos += size
        return size

    def flush(self):
        req = flush()
        self.block_on_request(req)

    def seek(self, pos, whence=0):
        if whence == 1:
            pos += self.pos
        if whence == 2:
            pos = self.length - pos
        self.pos = pos

    def write(self, buff):
        print("pos: {} addr: {}".format(self.pos, self.remote_addr))
        size = get_buffer_nbytes(buff, None, self.ep.cuda_support())
        if self.pos + size > self.length:
            size = self.length - self.pos
        req = self.rmem.put_nb(buff, size, self.remote_addr + self.pos)
        self.block_on_request(req)
        self.pos += size
        return size

    def seekable(self):
        return True

    def writable(self):
        return True

    def readable(self):
        return True

# Setting the __doc__
create_listener.__doc__ = ApplicationContext.create_listener.__doc__
create_endpoint.__doc__ = ApplicationContext.create_endpoint.__doc__
continuous_ucx_progress.__doc__ = ApplicationContext.continuous_ucx_progress.__doc__
get_ucp_worker.__doc__ = ApplicationContext.get_ucp_worker.__doc__
