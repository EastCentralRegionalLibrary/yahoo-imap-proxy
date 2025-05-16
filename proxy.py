"""
A minimal IMAP proxy that sits between an IMAP client and server,
patching BODYSTRUCTURE responses to ensure all attachment parts include a NAME parameter.
It also masks raw attachment data in logs to prevent binary spam,
and provides structured logging at various levels for monitoring and debugging.
"""

import sys
import asyncio
import ssl
import logging
import re
from typing import Optional, Set, Tuple


def setup_logging(debug: bool = False) -> logging.Logger:
    """
    Configure root logger. If debug is True, set level to DEBUG, else INFO.
    Returns a named logger for the proxy.
    """
    level: int = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        stream=sys.stdout,
    )
    return logging.getLogger("MinimalIMAPProxy")


# Regex to find attachment parts lacking a NAME parameter in BODYSTRUCTURE
BODYSTRUCTURE_WITHOUT_NAME_RE = re.compile(
    rb'\(\s*"?(APPLICATION|IMAGE|AUDIO|VIDEO|TEXT)"?\s+"?([^"\)\s]+)"?(?:\s+[^\)]*?)?"BASE64"',
    re.IGNORECASE,
)

# Regex to match an entire line that *is* exactly a literal‑size marker
LITERAL_SIZE_RE = re.compile(rb"^\* \d+ FETCH .* \{(\d+)\}\r\n?$")


def patch_bodystructure_line(
    line: bytes,
    attachment_counter: int,
    logger: logging.Logger,
) -> Tuple[bytes, int]:
    """
    Parses and modifies the BODYSTRUCTURE line to assign unique
    identifiers to attachments. Returns the modified line and the
    updated attachment counter.

    This ensures attachments are numbered sequentially when
    rewriting server BODYSTRUCTURE responses.
    """
    matches = list(BODYSTRUCTURE_WITHOUT_NAME_RE.finditer(line))
    if not matches:
        return line, attachment_counter

    # Report at debug level which parts will be patched
    logger.debug(
        f"Found {len(matches)} attachment(s) with missing NAME in BODYSTRUCTURE"
    )
    new_line: bytes = line
    offset: int = 0

    for m in matches:
        start, end = m.span()
        structure_part: bytes = line[start:end]

        # Skip any parts that somehow already include a NAME
        if b'"NAME"' in structure_part.upper():
            logger.debug("Skipping patch: NAME already present")
            continue

        # Generate a filename based on media subtype and counter
        major_type: str = m.group(1).decode()
        minor_type: str = m.group(2).decode()
        synthesized: str = f"attachment_{attachment_counter}.{minor_type.lower()}"
        attachment_counter += 1

        # Replace the NIL placeholder for NAME with our synthesized parameter
        modified_structure: bytes = re.sub(
            rb'\(\s*"?' + m.group(1) + rb'"?\s+"?' + m.group(2) + rb'"?\s+NIL',
            f'("{major_type}" "{minor_type}" ("NAME" "{synthesized}")'.encode(),
            structure_part,
            count=1,
        )

        logger.debug(
            f"Patching BODYSTRUCTURE: {structure_part.decode(errors='ignore')} -> {modified_structure.decode()}"
        )

        # Reconstruct the full line with the patched segment
        new_line = (
            new_line[: start + offset] + modified_structure + new_line[end + offset :]
        )
        offset += len(modified_structure) - (end - start)

    # Summarize patching at info level
    logger.info(
        f"Patched {len(matches)} BODYSTRUCTURE part(s) with synthesized NAME fields"
    )
    return new_line, attachment_counter


async def pipe_server_to_client(
    srv_reader: asyncio.StreamReader,
    cli_writer: asyncio.StreamWriter,
    logger: logging.Logger,
) -> None:
    """
    Forwards data from the IMAP server to the client,
    masks literal attachment payloads in logs, and
    patches BODYSTRUCTURE lines as needed.
    """
    logger.debug("Starting server-to-client pipe")

    # Maintains logical numbering of attachment order in message starting at 1
    attachment_counter: int = 1

    # Tracks remaining bytes of a current literal payload to mask
    attachment_chars: int = 0

    while True:
        line: bytes = await srv_reader.readline()
        if not line:
            # No more data: close the pipe
            logger.info("EOF from server, closing server-to-client pipe")
            break

        # If we're in the middle of masking a literal payload,
        # consume without logging until it's fully handled.
        if attachment_chars > 0:
            # Decrease remaining counter by the chunk length
            attachment_chars -= len(line)
            # Forward raw bytes to client, but never log contents
            cli_writer.write(line)
            await cli_writer.drain()
            continue

        # Not currently masking: check for BODYSTRUCTURE lines first
        if b"BODYSTRUCTURE" in line:
            logger.debug(f"S->P: {line.strip()!r}")
            # Patch BODYSTRUCTURE (e.g., renumber attachment IDs)
            line, attachment_counter = patch_bodystructure_line(
                line, attachment_counter, logger
            )

        # Detect the start of a literal payload
        elif m := LITERAL_SIZE_RE.search(line):
            # Extract the byte count to mask
            size = int(m.group(1))
            # Log immediately: we know exactly how many bytes are coming
            logger.debug(f"S->P: b'<{size} bytes of attachment data>'")
            # Initialize mask counter for the upcoming payload
            attachment_chars = size

        else:
            # All other lines: safe to log directly
            logger.debug(f"S->P: {line.strip()!r}")

        # Always forward the current line to the client
        cli_writer.write(line)
        await cli_writer.drain()


async def pipe_client_to_server(
    cli_reader: asyncio.StreamReader,
    srv_writer: asyncio.StreamWriter,
    logger: logging.Logger,
) -> None:
    """
    Forwards client commands to the IMAP server, logging them at DEBUG level.
    """
    logger.debug("Starting client-to-server pipe")
    while True:
        line: bytes = await cli_reader.readline()
        if not line:
            logger.info("EOF from client, closing client-to-server pipe")
            break

        logger.debug(f"C->S: {line.strip()!r}")
        srv_writer.write(line)
        await srv_writer.drain()


class IMAPProxy:
    """
    Core proxy class: listens for client connections, establishes an upstream
    connection to the IMAP server, and shuttles data bi-directionally.
    """

    def __init__(
        self,
        listen_port: int,
        remote_host: str,
        remote_port: int,
        use_ssl: bool,
        debug: bool,
    ) -> None:
        self.listen_port: int = listen_port
        self.remote_host: str = remote_host
        self.remote_port: int = remote_port
        self.use_ssl: bool = use_ssl
        self.logger: logging.Logger = setup_logging(debug)
        self.active_tasks: Set[asyncio.Task[None]] = set()

    async def handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """
        Handle a new client connection: connect to upstream IMAP server,
        then start the request and response pipes.
        """
        addr = writer.get_extra_info("peername")
        self.logger.info(f"Client connected: {addr}")

        try:
            ssl_ctx: Optional[ssl.SSLContext] = (
                ssl.create_default_context() if self.use_ssl else None
            )
            srv_reader, srv_writer = await asyncio.open_connection(
                self.remote_host, self.remote_port, ssl=ssl_ctx
            )
            self.logger.debug(
                f"Connected to IMAP server {self.remote_host}:{self.remote_port}"
            )
        except Exception as e:
            self.logger.error(
                f"Failed to connect to upstream IMAP server {self.remote_host}:{self.remote_port}: {e}"
            )
            writer.close()
            await writer.wait_closed()
            return

        try:
            await asyncio.gather(
                pipe_client_to_server(reader, srv_writer, self.logger),
                pipe_server_to_client(srv_reader, writer, self.logger),
            )
        except (asyncio.CancelledError, ConnectionResetError) as e:
            self.logger.warning(f"Connection with {addr} closed unexpectedly: {e}")
        finally:
            # Ensure writer is closed and log disconnection
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            self.logger.info(f"Client disconnected: {addr}")

    async def _serve(self) -> None:
        """
        Start listening on the configured port and serve forever.
        """

        async def track_client(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            task = asyncio.create_task(self.handle(reader, writer))
            self.active_tasks.add(task)
            task.add_done_callback(self.active_tasks.discard)

        server = await asyncio.start_server(track_client, "0.0.0.0", self.listen_port)
        self.logger.info(f"IMAP Proxy listening on 0.0.0.0:{self.listen_port}")
        try:
            async with server:
                await server.serve_forever()
        except asyncio.CancelledError:
            self.logger.info("Shutting down server")
            server.close()
            await server.wait_closed()

            # Cancel all active client handler tasks
            for task in self.active_tasks:
                task.cancel()
            await asyncio.gather(*self.active_tasks, return_exceptions=True)

            # now let client handlers see EOF/cancellation
            raise

    def run(self) -> None:
        """
        Run the proxy until interrupted (e.g. Ctrl+C).
        """
        asyncio.run(self._serve())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Minimal IMAP proxy with enhanced logging and BODYSTRUCTURE patching"
    )
    parser.add_argument("--port", type=int, default=10143, help="Local listen port")
    parser.add_argument(
        "--host", type=str, default="127.0.0.1", help="Remote IMAP server host"
    )
    parser.add_argument(
        "--remote-port", type=int, default=1143, help="Remote IMAP server port"
    )
    parser.add_argument(
        "--ssl", action="store_true", help="Use SSL for upstream connection"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    proxy = IMAPProxy(args.port, args.host, args.remote_port, args.ssl, args.debug)
    try:
        proxy.run()
    except KeyboardInterrupt:
        proxy.logger.info("IMAP Proxy shutting down on user request (Ctrl+C)")
