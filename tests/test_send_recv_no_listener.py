import os
import asyncio
import pytest
import sys
import ucp

np = pytest.importorskip("numpy")

msg_sizes = [2 ** i for i in range(0, 25, 4)]
dtypes = ["|u1", "<i8", "f8"]


def handle_exception(loop, context):
    msg = context.get("exception", context["message"])
    print(msg)
    sys.exit(-1)

def progress_and_reschedule():
    ucp.progress()
    asyncio.get_event_loop().call_soon(progress_and_reschedule)

@pytest.mark.asyncio
@pytest.mark.parametrize("size", msg_sizes)
async def test_send_recv_bytes(size):
    asyncio.get_event_loop().set_exception_handler(handle_exception)
    asyncio.get_event_loop().call_soon(progress_and_reschedule)

    msg = b"message in bytes"
    reply = np.empty_like(msg)

    client = await ucp.create_endpoint(ucp.get_worker_address())

    ping = client.send(msg)
    pong = client.recv(reply)
    await asyncio.gather(ping, pong)

    np.testing.assert_array_equal(reply, msg)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", msg_sizes)
@pytest.mark.parametrize("dtype", dtypes)
async def test_send_recv_numpy(size, dtype):
    asyncio.get_event_loop().set_exception_handler(handle_exception)
    asyncio.get_event_loop().call_soon(progress_and_reschedule)

    msg = np.arange(size, dtype=dtype)
    reply = np.empty_like(msg)

    client = await ucp.create_endpoint(ucp.get_worker_address())

    ping = client.send(msg)
    pong = client.recv(reply)
    await asyncio.gather(ping, pong)

    np.testing.assert_array_equal(reply, msg)
