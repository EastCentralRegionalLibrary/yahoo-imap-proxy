# IMAP BODYSTRUCTURE Proxy

A minimal, asynchronous IMAP proxy designed to sit between your mail client and an IMAP server. Its primary goal is to ensure compatibility with email processing tools that rely on specific IMAP response formats, particularly for attachments.

---

## Why This Proxy?

Some IMAP servers (e.g., Yahoo!) may omit the `NAME` parameter for attachment parts in their `BODYSTRUCTURE` responses. This omission can cause issues for downstream applications (such as email-to-print utilities or automated archiving scripts) that rely on the `NAME` parameter to derive filenames. This proxy intercepts and patches these `BODYSTRUCTURE` responses so that every attachment part includes a valid `NAME` value.

---

## Key Features

- **Automatic BODYSTRUCTURE Patching**

  - Detects attachment parts missing the `NAME` parameter.
  - Synthesizes filenames (e.g., `attachment_1.pdf`, `attachment_2.png`) based on MIME subtype and a counter.
  - Ensures all attachments report a `NAME`, improving compatibility with various clients and tools.

- **Log Masking for Attachments**

  - Prevents raw base64-encoded attachment data from flooding logs.
  - Summarizes binary payloads with a placeholder (e.g., `<10240 bytes of attachment data>`).

- **Structured Logging**

  - Uses standard Python logging levels: `DEBUG`, `INFO`, `WARNING`, and `ERROR`.
  - Timestamps and context for monitoring proxy activity, client connections, and errors.
  - `--debug` flag enables verbose logging for troubleshooting.

- **Asynchronous & Efficient**

  - Built with `asyncio` for non-blocking I/O and concurrent client handling with minimal overhead.

- **SSL/TLS Support**

  - Connects to the upstream IMAP server using SSL/TLS when the `--ssl` flag is provided.

- **Graceful Shutdown**

  - Cleanly handles `KeyboardInterrupt` (Ctrl+C), cancelling active connections and tasks.

---

## Repository Structure

```
proxy-bodystructure/      # root directory
├── proxy.py              # Main proxy implementation
├── sample.eml            # Example email for manual testing
├── tests/                # Unit and integration tests
│   └── test_proxy.py     # Core functionality tests
├── requirements-dev.txt  # Development dependencies (pytest, pyinstaller)
└── README.md             # This documentation
```

---

## Requirements

- **Python**: 3.8 or newer
- **Development & Packaging (optional)**

  - `pytest` — For running the test suite
  - `pyinstaller` — For building a standalone executable

### Install Dependencies

```bash
pip install -r requirements-dev.txt
```

> The core proxy uses only standard library modules.

---

## Usage

### 1. Run Tests

```bash
pytest
```

### 2. Run the Proxy

```bash
python proxy.py [--port PORT] [--host HOST] [--remote-port REMOTE_PORT] [--ssl] [--debug]
```

**Options:**

| Flag            | Type    | Default     | Description                         |
| --------------- | ------- | ----------- | ----------------------------------- |
| `--port`        | int     | `10143`     | Local TCP port to listen on         |
| `--host`        | string  | `127.0.0.1` | Upstream IMAP server host           |
| `--remote-port` | int     | `1143`      | Upstream IMAP server port           |
| `--ssl`         | boolean | `false`     | Use SSL/TLS for upstream connection |
| `--debug`       | boolean | `false`     | Enable verbose debug logging        |

### 3. Build Standalone Executable

```bash
pyinstaller --onefile proxy.py
```

- The executable will be placed in `dist/` (e.g., `dist/proxy` or `dist/proxy.exe`).

---

## Examples

Run the proxy without SSL, listening on all interfaces, and forwarding to `mail.example.com:993`:

```bash
python proxy.py --host mail.example.com --remote-port 993 --debug
```

Then configure your IMAP client to connect to `localhost:10143` instead of `mail.example.com:993`.

---

## Testing Details

- **Unit tests**:

  - `patch_bodystructure_line`: Validates NAME parameter synthesis and insertion.
  - `pipe_server_to_client` / `pipe_client_to_server`: Ensures correct data tunneling and patch application.

- **Integration test**:

  - Simulates a mock IMAP server and client, including cancellation (Ctrl+C) to verify graceful shutdown.

Run all tests with:

```bash
pytest --maxfail=1 --disable-warnings -q
```

---

## Contributing

1. Fork the repo
2. Create a new branch (`git checkout -b feature/your-feature`)
3. Make your changes and commit (`git commit -m 'Describe your changes'`)
4. Push to your branch (`git push origin feature/your-feature`)
5. Open a Pull Request

---

## License

This project is licensed under the MIT License. See the `LICENSE` file for details.
