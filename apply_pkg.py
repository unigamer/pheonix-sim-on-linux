#!/usr/bin/env python3
r"""
apply_pkg.py – Extract and apply a PhoenixRC .pkg update file.

The .pkg format:
  Header (18 bytes):
    [01]  magic
    [05]  length of version string (N)
    [00]  padding
    [N bytes]  version string (e.g. "6.0.i")
    [2 bytes]  file count? (unused here)
    [4 bytes LE]  compressed size of first chunk
    [4 bytes LE]  decompressed size of first chunk (always 1 024 000)

  Followed by concatenated chunks.  The *first* chunk starts immediately
  after the header (no size prefix – sizes come from the header).
  Every *subsequent* chunk is prefixed with:
    [4 bytes LE]  compressed size
    [4 bytes LE]  decompressed size
  then the zlib data.

  All chunk decompressed payloads are concatenated to form the data stream.

Data-stream layout (all integers little-endian):
  [4]  num_files (int32)
  For each file:
    [1]  entry_type  (03 = program-file, 01 = data-file …)
    [2]  filename_len (int16)
    [filename_len]  filename (no null)
    [2]  srcpath_len (int16)
    [srcpath_len]  source path (no null) – used only for display
    If entry_type == 0x03:
      [4]  ver_len (int32)
      [ver_len]  new version string (no null)
    [4]  file_size (int32 / uint32)
    [file_size]  raw file bytes

Usage:
  python3 apply_pkg.py <pkg_file> <install_root>

  install_root is the Windows "C:\Program Files\PhoenixRC\" directory
  mapped to a Linux path, e.g.:
    /wineprefix/drive_c/Program\ Files/PhoenixRC
"""

import sys
import os
import zlib
import struct
import io

# --------------------------------------------------------------------------- #

def iter_chunks(data: bytes, first_comp_size: int, first_decomp_size: int):
    """Yield decompressed payloads of every chunk in the pkg byte-string."""
    offset = 0
    # First chunk – sizes already known from header
    comp_size = first_comp_size
    decomp_size = first_decomp_size
    while offset < len(data):
        payload = zlib.decompress(data[offset:offset + comp_size])
        assert len(payload) == decomp_size, \
            f"Chunk size mismatch: got {len(payload)}, expected {decomp_size}"
        yield payload
        offset += comp_size
        if offset >= len(data):
            break
        # Subsequent chunks have an 8-byte prefix
        comp_size, decomp_size = struct.unpack_from("<II", data, offset)
        offset += 8


def read_u16(stream: io.BytesIO) -> int:
    return struct.unpack("<H", stream.read(2))[0]


def read_u32(stream: io.BytesIO) -> int:
    return struct.unpack("<I", stream.read(4))[0]


def read_bytes(stream: io.BytesIO, n: int) -> bytes:
    return stream.read(n)


def read_pkg(pkg_path: str):
    """Return (version, [(name, data)]) for the file entries of a .pkg.

    Version-tag entries (type 0x03) carry no payload and are left out.
    """
    with open(pkg_path, "rb") as f:
        raw = f.read()
    if len(raw) < 20 or raw[0] != 0x01 or raw[2] != 0x00:
        raise ValueError("not a PhoenixRC .pkg")
    ver_len = raw[1]
    version = raw[3:3 + ver_len].decode("ascii")
    first_comp, first_decomp = struct.unpack_from("<II", raw, 3 + ver_len + 2)
    stream = io.BytesIO(b"".join(iter_chunks(raw[3 + ver_len + 10:], first_comp, first_decomp)))
    files = []
    for _ in range(read_u32(stream)):
        entry_type = stream.read(1)[0]
        read_u16(stream)
        name = read_bytes(stream, read_u16(stream)).decode("ascii", errors="replace")
        read_bytes(stream, read_u16(stream))
        if entry_type == 0x03:
            read_bytes(stream, read_u32(stream))
        else:
            files.append((name, read_bytes(stream, read_u32(stream))))
    return version, files


def parse_and_apply(pkg_path: str, install_root: str, dry_run: bool = False):
    print(f"[apply_pkg] Reading {pkg_path}")
    with open(pkg_path, "rb") as f:
        raw = f.read()

    # --- Parse header ---
    assert raw[0] == 0x01 and raw[2] == 0x00, "Unexpected magic bytes"
    ver_len = raw[1]
    version = raw[3:3 + ver_len].decode("ascii")
    print(f"[apply_pkg] Package version: {version}")

    header_end = 3 + ver_len   # points to the 2 unknown bytes
    # skip 2 unknown bytes
    first_comp  = struct.unpack_from("<I", raw, header_end + 2)[0]
    first_decomp = struct.unpack_from("<I", raw, header_end + 6)[0]
    chunk_data_start = header_end + 10   # = 18 for a 5-char version

    print(f"[apply_pkg] First chunk: {first_comp} compressed → {first_decomp} decompressed")

    # --- Concatenate all decompressed chunks ---
    print("[apply_pkg] Decompressing all chunks …", end="", flush=True)
    parts = []
    n = 0
    for chunk in iter_chunks(raw[chunk_data_start:], first_comp, first_decomp):
        parts.append(chunk)
        n += 1
        if n % 10 == 0:
            print(f" {n}", end="", flush=True)
    print(f" done ({n} chunks)")

    stream = io.BytesIO(b"".join(parts))
    total_bytes = sum(len(p) for p in parts)
    print(f"[apply_pkg] Total decompressed: {total_bytes:,} bytes")

    # --- Parse file index ---
    num_files = read_u32(stream)
    print(f"[apply_pkg] Files to install: {num_files}")

    for i in range(num_files):
        entry_type = stream.read(1)[0]
        _flags     = read_u16(stream)          # 2 unknown/flags bytes – skip
        fname_len  = read_u16(stream)
        fname      = read_bytes(stream, fname_len).decode("ascii", errors="replace")
        src_len    = read_u16(stream)
        src_path   = read_bytes(stream, src_len).decode("ascii", errors="replace")

        new_ver   = None
        file_data = None
        if entry_type == 0x03:
            # Version-tag entry: contains new version string but no file payload.
            # The installer writes the version string into PhoenixRC.ver itself.
            nv_len  = read_u32(stream)
            new_ver = read_bytes(stream, nv_len).decode("ascii", errors="replace")
        else:
            # Regular file entry: 4-byte size + raw payload follows immediately.
            file_size = read_u32(stream)
            file_data = read_bytes(stream, file_size)

        dest = os.path.join(install_root, fname)
        tag  = f"(v{new_ver})" if new_ver else ""
        sz   = len(file_data) if file_data is not None else 0
        print(f"[apply_pkg]  [{i+1}/{num_files}] {fname} {tag}  ({sz:,} bytes)")
        print(f"             src: {src_path}")
        print(f"             dst: {dest}")

        if not dry_run and file_data is not None:
            # If dest is an existing directory (e.g. a packed-resource archive
            # whose name collides with a legacy directory), write alongside it
            # with a .pak extension so neither is lost.
            actual_dest = dest
            if os.path.isdir(dest):
                actual_dest = dest + ".pak"
                print(f"             NOTE: {dest} is a directory – writing to {actual_dest}")
            os.makedirs(os.path.dirname(actual_dest) or install_root, exist_ok=True)
            with open(actual_dest, "wb") as out:
                out.write(file_data)
            print("             written")

    # Always update PhoenixRC.ver to the new version string.
    ver_file = os.path.join(install_root, "PhoenixRC.ver")
    if not dry_run:
        with open(ver_file, "w") as vf:
            vf.write(version + "\n")
        print(f"[apply_pkg] PhoenixRC.ver updated to {version}")

    print("[apply_pkg] Done.")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <pkg_file> <install_root> [--dry-run]")
        sys.exit(1)
    pkg  = sys.argv[1]
    root = sys.argv[2]
    dry  = "--dry-run" in sys.argv
    parse_and_apply(pkg, root, dry_run=dry)
