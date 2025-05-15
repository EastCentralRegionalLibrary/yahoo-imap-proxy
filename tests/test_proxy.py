import asyncio
import os
import sys
import pytest
from unittest.mock import AsyncMock, Mock, call

# Ensure import path is correct
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.join(SCRIPT_DIR, "..")
sys.path.insert(0, PARENT_DIR)

from proxy import (
    IMAPProxy,
    patch_bodystructure_line,
    pipe_client_to_server,
    pipe_server_to_client,
    setup_logging,
)

BODYSTRUCTURE_TEST_CASES = [
    (
        b'* 1 FETCH (BODYSTRUCTURE ("APPLICATION" "PDF" NIL NIL NIL "BASE64"))\r\n',
        b'* 1 FETCH (BODYSTRUCTURE ("APPLICATION" "PDF" ("NAME" "attachment_1.pdf") NIL NIL "BASE64"))\r\n',
    ),
    (
        b'* 2 FETCH (BODYSTRUCTURE ("IMAGE" "JPEG" NIL NIL NIL "BASE64"))\r\n',
        b'* 2 FETCH (BODYSTRUCTURE ("IMAGE" "JPEG" ("NAME" "attachment_1.jpeg") NIL NIL "BASE64"))\r\n',
    ),
    (
        b'* 3 FETCH (BODYSTRUCTURE ("TEXT" "PLAIN" ("NAME" "note.txt") NIL NIL "BASE64"))\r\n',
        b'* 3 FETCH (BODYSTRUCTURE ("TEXT" "PLAIN" ("NAME" "note.txt") NIL NIL "BASE64"))\r\n',
    ),
    (
        b'* 4 FETCH (BODYSTRUCTURE ("VIDEO" "MP4" NIL NIL NIL "BASE64"))\r\n',
        b'* 4 FETCH (BODYSTRUCTURE ("VIDEO" "MP4" ("NAME" "attachment_1.mp4") NIL NIL "BASE64"))\r\n',
    ),
    (
        b"* malformed nonsense BODYSTRUCTURE NIL\r\n",
        b"* malformed nonsense BODYSTRUCTURE NIL\r\n",
    ),
]


@pytest.fixture
def logger():
    return setup_logging(debug=True)


@pytest.mark.parametrize("original, expected", BODYSTRUCTURE_TEST_CASES)
def test_patch_bodystructure_line_behavior(original, expected, logger):
    patched, _ = patch_bodystructure_line(original, 1, logger)
    assert patched == expected


@pytest.fixture
def make_mock_stream():
    class MockStream:
        def __init__(self, lines):
            self._lines = asyncio.Queue()
            for line in lines:
                self._lines.put_nowait(line)
            self._lines.put_nowait(b"")  # EOF

        async def readline(self):
            return await self._lines.get()

        async def readexactly(self, n):
            return b"x" * n

    return MockStream


@pytest.mark.asyncio
async def test_server_to_client_patches_bodystructure(make_mock_stream, logger):
    input_lines = [
        BODYSTRUCTURE_TEST_CASES[0][0],
        b"* OK Completed\r\n",
    ]
    expected = [
        BODYSTRUCTURE_TEST_CASES[0][1],
        b"* OK Completed\r\n",
    ]

    reader = make_mock_stream(input_lines)
    writer = Mock(write=Mock(), drain=AsyncMock())

    await pipe_server_to_client(reader, writer, logger)

    writer.write.assert_has_calls([call(expected[0]), call(expected[1])])
    assert writer.drain.call_count == 2


@pytest.mark.asyncio
async def test_server_to_client_passthrough(make_mock_stream, logger):
    input_lines = [
        b"* OK IMAP4rev1 Service Ready\r\n",
        b"* FLAGS (\\Seen \\Deleted)\r\n",
        b"* BYE Logging out\r\n",
    ]

    reader = make_mock_stream(input_lines)
    writer = Mock(write=Mock(), drain=AsyncMock())

    await pipe_server_to_client(reader, writer, logger)

    writer.write.assert_has_calls([call(line) for line in input_lines])
    assert writer.drain.call_count == len(input_lines)


@pytest.mark.asyncio
async def test_client_to_server_passthrough(make_mock_stream, logger):
    input_lines = [
        b"A001 LOGIN user pass\r\n",
        b"A002 SELECT INBOX\r\n",
        b"A003 LOGOUT\r\n",
    ]

    reader = make_mock_stream(input_lines)
    writer = Mock(write=Mock(), drain=AsyncMock())

    await pipe_client_to_server(reader, writer, logger)

    writer.write.assert_has_calls([call(line) for line in input_lines])
    assert writer.drain.call_count == len(input_lines)


class MockIMAPServer:
    def __init__(self, port):
        self.port = port
        self._server = None
        self._connections = []

    async def handle_client(self, reader, writer):
        self._connections.append((reader, writer))
        writer.write(b"* OK Mock IMAP server ready\r\n")
        await writer.drain()
        while not reader.at_eof():
            data = await reader.readline()
            if not data:
                break
            writer.write(b"+ OK\r\n")
            await writer.drain()

    async def start(self):
        self._server = await asyncio.start_server(
            self.handle_client, "127.0.0.1", self.port
        )

    async def stop(self):
        for _, writer in self._connections:
            writer.close()
            await writer.wait_closed()
        self._server.close()
        await self._server.wait_closed()


@pytest.mark.asyncio
async def test_imap_proxy_integration_with_ctrl_c():
    # Setup mock IMAP server
    mock_server = MockIMAPServer(1143)
    await mock_server.start()

    # Setup and run proxy
    proxy = IMAPProxy(
        listen_port=10143,
        remote_host="127.0.0.1",
        remote_port=1143,
        use_ssl=False,
        debug=True,
    )

    proxy_task = asyncio.create_task(proxy._serve())

    # Give the server a moment to start
    await asyncio.sleep(0.5)

    # Connect a client to the proxy
    reader, writer = await asyncio.open_connection("127.0.0.1", 10143)
    greeting = await reader.readline()
    assert b"OK Mock IMAP server ready" in greeting

    writer.write(b"A1 CAPABILITY\r\n")
    await writer.drain()
    response = await reader.readline()
    assert b"+ OK" in response

    # Simulate Ctrl+C / KeyboardInterrupt by cancelling the server
    proxy_task.cancel()
    try:
        await proxy_task
    except asyncio.CancelledError:
        pass

    await mock_server.stop()
    writer.close()
    await writer.wait_closed()

    # Sanity check
    assert proxy_task.done()
