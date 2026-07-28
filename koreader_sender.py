"""
Direct file transfer to KOReader via the Calibre wireless device protocol.

Architecture:
  - This module runs a TCP server (like Calibre's wireless server).
  - KOReader discovers it via UDP broadcast on port 54982, then connects via TCP.
  - The server sends INIT first, KOReader responds, then we push files.

Usage:
  - Call start_server() to bind ports and return a KOReaderServer instance.
  - Call server.wait_and_send(file_paths) to block until KOReader connects
    and files are transferred.
  - NOTE: Calibre's wireless device server must be stopped first (it uses the
    same ports). In Calibre: Preferences > Sharing > Sharing over the network
    > Stop server.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import uuid
from pathlib import Path

DEFAULT_TCP_PORT = 9090
DISCOVERY_UDP_PORT = 54982

# Opcodes
_OK = 0
_SET_DEVICE_INFO = 1
_GET_DEVICE_INFO = 3
_GET_BOOK_COUNT = 6
_SEND_BOOK = 8
_INIT = 9
_NOOP = 12


# ── Message framing ────────────────────────────────────────────────────────────

def _send(sock: socket.socket, opcode: int, data: dict) -> None:
    payload = json.dumps([opcode, data]).encode("utf-8")
    sock.sendall(str(len(payload)).encode("ascii") + payload)


def _recv(sock: socket.socket) -> tuple[int, dict]:
    buf = b""
    while b"[" not in buf:
        byte = sock.recv(1)
        if not byte:
            raise ConnectionError("KOReader closed the connection.")
        buf += byte
    idx = buf.index(b"[")
    length = int(buf[:idx].decode("ascii"))
    data = buf[idx:]
    while len(data) < length:
        chunk = sock.recv(min(4096, length - len(data)))
        if not chunk:
            raise ConnectionError("KOReader closed the connection mid-message.")
        data += chunk
    opcode, payload = json.loads(data.decode("utf-8"))
    return opcode, payload


# ── Protocol handler (runs after KOReader connects) ───────────────────────────

def _handle_connection(
    conn: socket.socket,
    file_paths: list[Path],
    password: str,
) -> None:
    conn.settimeout(30)

    challenge = str(uuid.uuid4())

    # 1. Send INIT — server always goes first
    _send(conn, _INIT, {
        "serverProtocolVersion": 1,
        "validExtensions": ["png", "jpg", "jpeg", "epub", "pdf", "mobi", "cbz"],
        "passwordChallenge": challenge,
        "currentLibraryName": "Calibre Library",
        "currentLibraryUUID": str(uuid.uuid4()),
        "calibre_version": [6, 0, 0],
        "canSupportUpdateBooks": True,
        "canSupportLpathChanges": True,
    })
    _, init_resp = _recv(conn)

    # 2. Verify password if KOReader sent a hash
    returned_hash = init_resp.get("passwordHash", "")
    if returned_hash:
        if not password:
            raise RuntimeError("KOReader requires a password but none was provided.")
        expected = hashlib.sha1(
            (password + challenge).encode("utf-8")
        ).hexdigest()
        if returned_hash != expected:
            raise RuntimeError("KOReader password is incorrect.")

    chunk_size = max(init_resp.get("maxBookContentPacketLen", 4096), 4096)

    # 3. Get device info
    _send(conn, _GET_DEVICE_INFO, {})
    _recv(conn)

    # 4. Set library info on device
    _send(conn, _SET_DEVICE_INFO, {
        "driveinfo": {
            "main": {
                "device_store_uuid": str(uuid.uuid4()),
                "location_code": "main",
            }
        }
    })
    _recv(conn)

    # 5. Get book count — must drain all metadata responses before sending books
    _send(conn, _GET_BOOK_COUNT, {
        "canStream": True,
        "canScan": True,
        "willUseCachedMetadata": False,
        "supportsSync": False,
        "canSupportBookFormatSync": True,
    })
    _, count_resp = _recv(conn)
    for _ in range(count_resp.get("count", 0)):
        _recv(conn)

    # 6. Push each file
    total = len(file_paths)
    for i, path in enumerate(file_paths):
        raw = path.read_bytes()
        lpath = path.name

        _send(conn, _SEND_BOOK, {
            "lpath": lpath,
            "length": len(raw),
            "metadata": {
                "title": path.stem,
                "authors": ["Unknown"],
                "uuid": str(uuid.uuid4()),
                "lpath": lpath,
            },
            "thisBook": i,
            "totalBooks": total,
            "willStreamBinary": True,
            "wantsSendOkToSendbook": True,
            "canSupportLpathChanges": True,
        })
        _recv(conn)  # KOReader ready

        for offset in range(0, len(raw), chunk_size):
            conn.sendall(raw[offset : offset + chunk_size])
        _recv(conn)  # KOReader confirmed receipt

    # 7. Disconnect
    _send(conn, _NOOP, {"ejecting": True})
    _recv(conn)


# ── Discovery broadcaster (UDP) ────────────────────────────────────────────────

def get_local_ip() -> str:
    """Return the LAN IP of this machine (the address KOReader should connect to)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "unknown"


def _run_discovery(stop_event: threading.Event, tcp_port: int, error_box: list) -> None:
    """
    Respond to KOReader's UDP discovery broadcasts on port 54982.
    KOReader broadcasts a query; we reply with our TCP port.
    error_box is a single-element list so the thread can report bind failures.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # SO_BROADCAST is needed to receive subnet broadcast packets on Windows
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1.0)
        sock.bind(("0.0.0.0", DISCOVERY_UDP_PORT))

        while not stop_event.is_set():
            try:
                data, addr = sock.recvfrom(4096)
                raw = data.decode("utf-8", errors="replace")
                # Strip optional leading length prefix before the JSON array
                start = next((i for i, c in enumerate(raw) if c == "["), None)
                if start is not None:
                    raw = raw[start:]
                # Reply with opcode 0 (OK/discovery ack) + port info — matches
                # what KOReader's Calibre plugin expects for UDP discovery.
                reply_data = {
                    "port": tcp_port,
                    "libraryCount": 0,
                    "willAskForPassword": False,
                    "serverProtocolVersion": 1,
                }
                for opcode in (_OK, _INIT):
                    reply = json.dumps([opcode, reply_data]).encode("utf-8")
                    framed = str(len(reply)).encode("ascii") + reply
                    sock.sendto(framed, addr)
            except socket.timeout:
                continue
    except OSError as e:
        error_box[0] = str(e)
    finally:
        try:
            sock.close()
        except Exception:
            pass


# ── Public API ─────────────────────────────────────────────────────────────────

def _port_in_use(port: int) -> bool:
    """Return True if something is already listening on the given TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


class KOReaderServer:
    def __init__(self, tcp_port: int, password: str):
        self._port = tcp_port
        self._password = password
        self._tcp_sock: socket.socket | None = None
        self._stop = threading.Event()
        self._discovery_thread: threading.Thread | None = None

    def start(self) -> None:
        """Bind the TCP server socket and start the UDP discovery thread."""
        self._tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._tcp_sock.bind(("0.0.0.0", self._port))
        except OSError as e:
            winerr = getattr(e, "winerror", None)
            # Check if the port is already occupied (connect test is cross-platform)
            port_in_use = _port_in_use(self._port)
            if winerr in (10048, 10013) and port_in_use:
                # 10013 (WSAEACCES) instead of 10048 (WSAEADDRINUSE) when the
                # holder used SO_EXCLUSIVEADDRUSE — that's exactly what Calibre does.
                raise RuntimeError(
                    f"Port {self._port} is already in use — Calibre's wireless device "
                    "server is probably still running.\n"
                    "Stop it first: Calibre > Preferences > Sharing > "
                    "Sharing over the network > Wireless device connection > Stop server."
                )
            if winerr == 10048 or "already in use" in str(e).lower():
                raise RuntimeError(
                    f"Port {self._port} is already in use. Stop whatever is using it "
                    "and try again."
                )
            if winerr == 10013 or "forbidden" in str(e).lower() or "permission" in str(e).lower():
                raise RuntimeError(
                    f"Windows blocked binding to port {self._port} (access denied).\n"
                    "Run this once in an Admin PowerShell to open the port:\n\n"
                    f"  netsh advfirewall firewall add rule name=\"KOReader\" "
                    f"dir=in action=allow protocol=TCP localport={self._port}\n\n"
                    "Or try a different port number in the KOReader settings."
                )
            raise
        self._tcp_sock.listen(1)
        self._tcp_sock.settimeout(1.0)

        self._udp_error: list = [None]
        self._discovery_thread = threading.Thread(
            target=_run_discovery,
            args=(self._stop, self._port, self._udp_error),
            daemon=True,
        )
        self._discovery_thread.start()

    def wait_and_send(
        self,
        file_paths: list[Path],
        timeout: int = 120,
    ) -> None:
        """
        Block until KOReader connects, then push files.
        Raises TimeoutError if no connection within `timeout` seconds.
        """
        if self._tcp_sock is None:
            raise RuntimeError("Server not started. Call start() first.")

        elapsed = 0
        conn = None
        while elapsed < timeout:
            try:
                conn, _ = self._tcp_sock.accept()
                break
            except socket.timeout:
                elapsed += 1

        if conn is None:
            raise TimeoutError(
                f"KOReader did not connect within {timeout} seconds. "
                "Make sure KOReader is on the same Wi-Fi network and tap "
                "Calibre > Connect."
            )

        try:
            _handle_connection(conn, file_paths, self._password)
        finally:
            conn.close()

    def stop(self) -> None:
        self._stop.set()
        if self._tcp_sock:
            try:
                self._tcp_sock.close()
            except Exception:
                pass


def send_files(
    file_paths: list[Path],
    tcp_port: int = DEFAULT_TCP_PORT,
    password: str = "",
    timeout: int = 120,
) -> None:
    """
    Convenience wrapper: start server, wait for KOReader, push files, stop.
    Blocks until done or timeout.
    """
    server = KOReaderServer(tcp_port=tcp_port, password=password)
    server.start()
    try:
        server.wait_and_send(file_paths, timeout=timeout)
    finally:
        server.stop()


def server_start_info(tcp_port: int = DEFAULT_TCP_PORT) -> dict:
    """
    Return {'local_ip': str, 'tcp_port': int, 'udp_ok': bool, 'udp_error': str|None}.
    Useful for building status messages before calling send_files.
    """
    return {
        "local_ip": get_local_ip(),
        "tcp_port": tcp_port,
    }
