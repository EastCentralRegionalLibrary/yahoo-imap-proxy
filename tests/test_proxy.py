import asyncio
import logging
import os
import ssl
import sys
import pytest
import tempfile
import subprocess
from unittest.mock import AsyncMock, Mock, call
from pathlib import Path
from typing import List, Tuple

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


@pytest.fixture(scope="session")
def logger():
    return setup_logging(debug=True)


@pytest.mark.parametrize("original, expected", BODYSTRUCTURE_TEST_CASES)
def test_patch_bodystructure_line(original: bytes, expected: bytes, logger):
    patched, _ = patch_bodystructure_line(original, 1, logger)
    assert patched == expected


# Helper to simulate asyncio.StreamReader
class DummyStreamReader(asyncio.StreamReader):
    def __init__(self, lines: List[bytes]):
        super().__init__()
        for line in lines:
            self.feed_data(line)
        self.feed_eof()


# Dummy writer for capturing writes
class DummyWriter:
    def __init__(self):
        self.written: List[bytes] = []

    def write(self, data: bytes):
        self.written.append(data)

    async def drain(self):
        pass


@pytest.fixture
def make_streams():
    """Factory to create reader/writer pair"""

    def _factory(lines: List[bytes]):
        return DummyStreamReader(lines), DummyWriter()

    return _factory


# Parameterized server->client tests
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "input_lines,expected_lines",
    [
        (
            [BODYSTRUCTURE_TEST_CASES[0][0], b"* OK done\r\n"],
            [BODYSTRUCTURE_TEST_CASES[0][1], b"* OK done\r\n"],
        ),
        (
            [b"* OK hi\r\n", b"* FLAGS (\\Seen)\r\n"],
            [b"* OK hi\r\n", b"* FLAGS (\\Seen)\r\n"],
        ),
    ],
)
async def test_pipe_server_to_client_behaviors(
    make_streams, logger, input_lines, expected_lines
):
    reader, writer = make_streams(input_lines)
    await pipe_server_to_client(reader, writer, logger)
    assert writer.written == expected_lines


# Parameterized client->server tests
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "input_lines",
    [
        ([b"A001 CMD1\r\n", b"A002 CMD2\r\n", b"A003 LOGOUT\r\n"]),
    ],
)
async def test_pipe_client_to_server_passthrough(make_streams, logger, input_lines):
    reader, writer = make_streams(input_lines)
    await pipe_client_to_server(reader, writer, logger)
    assert writer.written == input_lines


@pytest.mark.asyncio
async def test_mask_literal_payload(caplog):
    caplog.set_level(logging.DEBUG)
    # Simulate: marker, payload split over lines, and a normal line
    lines = [b"* 1 FETCH BODY[] {5}\r\n", b"abc", b"de\r\n", b"OK done\r\n"]
    reader = DummyStreamReader(lines)
    writer = DummyWriter()
    logger = logging.getLogger("test_imap")

    await pipe_server_to_client(reader, writer, logger)
    # Check that the mask log was emitted
    assert "b'<5 bytes of attachment data>'" in caplog.text
    # Ensure payload bytes were forwarded but not logged
    combined = b"".join(writer.written)
    assert b"abc" in combined and b"de" in combined
    assert "abc" not in caplog.text and "de" not in caplog.text
    # Ensure normal line is logged
    assert "b'OK done'" in caplog.text


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


def generate_self_signed_cert() -> Tuple[Path, Path, tempfile.TemporaryDirectory]:
    """
    Generate a self-signed certificate and private key in a temporary directory.
    Returns paths to (cert_path, key_path) and the TemporaryDirectory handle.
    """
    temp_dir = tempfile.TemporaryDirectory()
    key_path = Path(temp_dir.name) / "key.pem"
    cert_path = Path(temp_dir.name) / "cert.pem"

    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-subj",
            "/CN=localhost",
            "-days",
            "1",
        ],
        check=True,
        capture_output=True,
    )

    return cert_path, key_path, temp_dir


class DummySSLServer:
    """
    A simple SSL-enabled IMAP-like server for testing the proxy.
    """

    def __init__(self, port: int, cert_path: Path, key_path: Path) -> None:
        self.port: int = port
        self.cert_path: Path = cert_path
        self.key_path: Path = key_path
        self._server: asyncio.AbstractServer | None = None

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        # Send initial greeting
        writer.write(b"* OK Dummy SSL IMAP server\r\n")
        await writer.drain()

        # Echo simple + OK responses until closed
        while not reader.at_eof():
            line = await reader.readline()
            if not line:
                break
            writer.write(b"+ OK\r\n")
            await writer.drain()

    async def start(self) -> None:
        """
        Start the SSL server on localhost with provided certificate.
        """
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(
            certfile=str(self.cert_path), keyfile=str(self.key_path)
        )
        self._server = await asyncio.start_server(
            self.handle_client,
            host="127.0.0.1",
            port=self.port,
            ssl=context,
        )

    async def stop(self) -> None:
        """
        Stop the SSL server and close all connections.
        """
        if self._server:
            self._server.close()
            await self._server.wait_closed()


@pytest.mark.asyncio
async def test_imap_proxy_with_ssl(monkeypatch) -> None:
    """
    Exercise the proxy with an upstream SSL IMAP server.
    Verifies greeting and command passthrough over SSL.
    """
    # Generate temporary SSL credentials
    cert_path, key_path, temp_dir = generate_self_signed_cert()

    # Start dummy SSL server
    dummy_server = DummySSLServer(port=11993, cert_path=cert_path, key_path=key_path)
    await dummy_server.start()

    # Force proxy to trust our self-signed cert
    monkeypatch.setattr(ssl, "create_default_context", ssl._create_unverified_context)

    # Launch proxy configured for SSL upstream
    proxy = IMAPProxy(
        listen_port=10144,
        remote_host="127.0.0.1",
        remote_port=11993,
        use_ssl=True,
        debug=True,
    )
    proxy_task: asyncio.Task[None] = asyncio.create_task(proxy._serve())

    # Allow proxy to start listening
    await asyncio.sleep(0.2)

    # Connect through proxy with plaintext (proxy handles SSL upstream)
    reader, writer = await asyncio.open_connection(
        host="127.0.0.1",
        port=10144,
    )

    # Verify greeting
    greeting = await reader.readline()
    assert b"OK Dummy SSL IMAP server" in greeting

    # Send NOOP and expect + OK
    writer.write(b"A1 NOOP\r\n")
    await writer.drain()
    response = await reader.readline()
    assert b"+ OK" in response

    # Clean up: cancel proxy, stop server, close client
    proxy_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await proxy_task

    await dummy_server.stop()
    writer.close()
    await writer.wait_closed()
    temp_dir.cleanup()
