"""NOAA GOES-R GLM (Geostationary Lightning Mapper) flash reader: pure Python, no numpy/h5py.

GLM Level-2 "LCFA" files (one every 20 s per satellite) are public on AWS Open Data (NOAA NODD):
``s3://noaa-goes19/GLM-L2-LCFA/YYYY/DDD/HH/`` (GOES-East) and ``s3://noaa-goes18/...`` (GOES-West).
They are netCDF-4, i.e. HDF5. Pulling in h5py + numpy would add roughly 40 MB to every Vercel
function, so this module reads just what we need (``flash_lat`` / ``flash_lon``) with a
small HDF5 subset reader: superblock v2/v3, v2 object headers (+ continuations), dense link storage
(fractal heap), chunked datasets indexed by a v1 B-tree, shuffle + deflate filters, float32.
That is exactly how GLM files are written today. Anything else raises ``GLMFormatError`` and the
caller reports ``unknown``, never ``clear``.

Flash times: each file covers about 20 s. We take the file's *end* time (from the file name) as the flash
time, so the 30-minute all-clear is never earlier than the real flash (it can be up to about 20 s late).
"""
from __future__ import annotations

import re
import struct
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any

UNDEF = (1 << 64) - 1
BUCKETS = {"east": "noaa-goes19", "west": "noaa-goes18"}
SAT_IDS = {"east": "G19", "west": "G18"}
PRODUCT = "GLM-L2-LCFA"
# GOES-East (75.2 W) covers the central and eastern US best; GOES-West (137.2 W) covers the West.
EAST_WEST_SPLIT_LON = -106.0
_NAME_RE = re.compile(r"OR_GLM-L2-LCFA_G(\d+)_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})(\d)_e(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})(\d)_c")


class GLMFormatError(ValueError):
    pass


def satellite_for(lon: float) -> str:
    return "east" if lon >= EAST_WEST_SPLIT_LON else "west"


def file_times(key: str) -> tuple[datetime, datetime]:
    """(start, end) UTC from an LCFA object key."""
    m = _NAME_RE.search(key)
    if not m:
        raise GLMFormatError(f"not a GLM LCFA file name: {key}")
    g = m.groups()

    def mk(y, doy, hh, mm, ss, tenth):
        return (datetime(int(y), 1, 1, tzinfo=timezone.utc) + timedelta(days=int(doy) - 1, hours=int(hh), minutes=int(mm),
                                                                         seconds=int(ss), milliseconds=100 * int(tenth)))
    return mk(*g[1:7]), mk(*g[7:13])


def hour_prefix(when: datetime) -> str:
    when = when.astimezone(timezone.utc)
    return f"{PRODUCT}/{when:%Y}/{when.timetuple().tm_yday:03d}/{when:%H}/"


# --- minimal HDF5 reader ----------------------------------------------------------------------------

class _H5:
    def __init__(self, data: bytes):
        if data[:8] != b"\x89HDF\r\n\x1a\n":
            raise GLMFormatError("not an HDF5 file")
        self.b = data
        ver = data[8]
        if ver not in (2, 3):
            raise GLMFormatError(f"HDF5 superblock v{ver} not supported")
        if data[9] != 8 or data[10] != 8:
            raise GLMFormatError("only 8-byte offsets/lengths supported")
        base, _ext, _eof, root = struct.unpack_from("<4Q", data, 12)
        if base != 0:
            raise GLMFormatError("non-zero base address")
        self.root = root

    def u(self, off: int, n: int) -> int:
        if off < 0 or off + n > len(self.b):
            raise GLMFormatError("truncated file")
        return int.from_bytes(self.b[off:off + n], "little")

    def messages(self, addr: int) -> list[tuple[int, int, int]]:
        b = self.b
        if b[addr:addr + 4] != b"OHDR":
            raise GLMFormatError("only v2 object headers supported")
        flags = b[addr + 5]
        p = addr + 6
        if flags & 0x20:
            p += 16
        if flags & 0x10:
            p += 4
        sz = 1 << (flags & 3)
        n = self.u(p, sz)
        p += sz
        out, blocks, seen = [], [(p, p + n)], 0
        while blocks:
            s, e = blocks.pop(0)
            seen += 1
            if seen > 64:
                raise GLMFormatError("too many header continuation blocks")
            p = s
            while p + 4 <= e - 4:
                t, ln = b[p], self.u(p + 1, 2)
                p += 4 + (2 if flags & 4 else 0)
                if p + ln > e:
                    break
                if t == 0x10:
                    a, length = self.u(p, 8), self.u(p + 8, 8)
                    if b[a:a + 4] != b"OCHK":
                        raise GLMFormatError("bad continuation block")
                    blocks.append((a + 4, a + length - 4))
                out.append((t, p, ln))
                p += ln
        return out

    def links(self, group_addr: int) -> dict[str, int]:
        b = self.b
        res: dict[str, int] = {}
        for t, p, _ln in self.messages(group_addr):
            if t == 0x06:  # compact link message
                name, addr, _ = self._link(p)
                if name and addr is not None:
                    res[name] = addr
            elif t == 0x02:  # link info -> dense storage
                fl = b[p + 1]
                q = p + 2 + (8 if fl & 1 else 0)
                heap = self.u(q, 8)
                if heap != UNDEF:
                    res.update(self._heap_links(heap))
        return res

    def _link(self, q: int) -> tuple[str | None, int | None, int]:
        b = self.b
        if b[q] != 1:
            return None, None, q
        f = b[q + 1]
        q += 2
        lt = 0
        if f & 8:
            lt = b[q]
            q += 1
        if f & 4:
            q += 8
        if f & 0x10:
            q += 1
        nl = 1 << (f & 3)
        n = self.u(q, nl)
        q += nl
        name = b[q:q + n].decode("utf-8", "replace")
        q += n
        if lt == 0:
            return name, self.u(q, 8), q + 8
        length = self.u(q, 2)
        return name, None, q + 2 + length

    def _heap_links(self, heap: int) -> dict[str, int]:
        b = self.b
        if b[heap:heap + 4] != b"FRHP":
            raise GLMFormatError("bad fractal heap")
        p = heap + 5
        filtlen, fl = self.u(p + 2, 2), b[p + 4]
        p += 9 + 16 + 16 + 64
        width = self.u(p, 2)
        start = self.u(p + 2, 8)
        maxdirect = self.u(p + 10, 8)
        maxheap = self.u(p + 18, 2)
        root = self.u(p + 22, 8)
        currows = self.u(p + 30, 2)
        offb = (maxheap + 7) // 8
        blocks: list[tuple[int, int]] = []
        if currows == 0:
            blocks.append((root, start))
        else:
            if b[root:root + 4] != b"FHIB":
                raise GLMFormatError("bad indirect block")
            q = root + 5 + 8 + offb
            size = start
            for r in range(currows):
                if r >= 2:
                    size *= 2
                for _w in range(width):
                    a = self.u(q, 8)
                    q += 8
                    if size <= maxdirect:
                        if filtlen:
                            q += 12
                        if a != UNDEF:
                            blocks.append((a, size))
                    elif a != UNDEF:
                        raise GLMFormatError("nested indirect heap blocks not supported")
        res: dict[str, int] = {}
        for a, size in blocks:
            if b[a:a + 4] != b"FHDB":
                raise GLMFormatError("bad direct block")
            q = a + 5 + 8 + offb + (4 if fl & 2 else 0)
            end = a + size
            while q < end - 2 and b[q] == 1:
                name, addr, q = self._link(q)
                if name and addr is not None:
                    res[name] = addr
        return res

    def read_float32(self, ds_addr: int) -> list[float]:
        b = self.b
        dims = None
        dtype = None
        layout = None
        filters: list[int] = []
        for t, p, _ln in self.messages(ds_addr):
            if t == 0x01:
                ver, rank = b[p], b[p + 1]
                off = p + (4 if ver == 2 else 8)
                if ver == 2 and b[p + 3] == 2:  # null dataspace
                    dims = []
                else:
                    dims = [self.u(off + 8 * i, 8) for i in range(rank)]
            elif t == 0x03:
                cls = b[p] & 0x0F
                size = self.u(p + 4, 4)
                dtype = (cls, size, b[p + 1] & 1)
            elif t == 0x0B:
                ver, nf = b[p], b[p + 1]
                q = p + (8 if ver == 1 else 2)
                for _ in range(nf):
                    fid = self.u(q, 2)
                    if ver == 1 or fid >= 256:
                        namelen = self.u(q + 2, 2)
                        nvals = self.u(q + 6, 2)
                        q += 8 + ((namelen + 7) // 8 * 8 if ver == 1 else namelen)
                    else:
                        nvals = self.u(q + 4, 2)
                        q += 6
                    q += 4 * nvals
                    if ver == 1 and nvals % 2:
                        q += 4
                    filters.append(fid)
            elif t == 0x08:
                layout = p
        if dims is None or dtype is None or layout is None:
            raise GLMFormatError("incomplete dataset header")
        if dtype[0] != 1 or dtype[1] != 4 or dtype[2] != 0:
            raise GLMFormatError("expected little-endian float32")
        if len(dims) != 1:
            raise GLMFormatError("expected a 1-D dataset")
        n = dims[0]
        if n == 0:
            return []
        if set(filters) - {1, 2}:
            raise GLMFormatError(f"unsupported filters {filters}")
        ver, cls = b[layout], b[layout + 1]
        if ver != 3 or cls != 2:
            if ver == 3 and cls == 1:  # contiguous
                addr, size = self.u(layout + 2, 8), self.u(layout + 10, 8)
                return list(struct.unpack_from(f"<{n}f", b, addr)) if addr != UNDEF else []
            raise GLMFormatError(f"layout v{ver} class {cls} not supported")
        rank1 = b[layout + 2]
        btree = self.u(layout + 3, 8)
        chunk = self.u(layout + 11, 4)
        if rank1 != 2:
            raise GLMFormatError("unexpected chunk rank")
        out: list[float] = [0.0] * n
        for start, raw, mask in self._chunks(btree, rank1):
            data = raw
            for fid in reversed(filters):
                if mask & (1 << filters.index(fid)):
                    continue
                if fid == 1:
                    data = zlib.decompress(data)
                elif fid == 2:
                    m = len(data) // 4
                    data = bytes(data[j * m + i] for i in range(m) for j in range(4))
            vals = struct.unpack_from(f"<{len(data) // 4}f", data)
            for i, v in enumerate(vals[:chunk]):
                if start + i < n:
                    out[start + i] = v
        return out

    def _chunks(self, addr: int, rank1: int, depth: int = 0):
        b = self.b
        if addr == UNDEF:
            return
        if depth > 8 or b[addr:addr + 4] != b"TREE" or b[addr + 4] != 1:
            raise GLMFormatError("bad chunk B-tree")
        level, used = b[addr + 5], self.u(addr + 6, 2)
        q = addr + 24
        keysize = 8 + 8 * rank1
        for _ in range(used):
            size, mask = self.u(q, 4), self.u(q + 4, 4)
            start = self.u(q + 8, 8)
            child = self.u(q + keysize, 8)
            q += keysize + 8
            if level == 0:
                yield start, self.b[child:child + size], mask
            else:
                yield from self._chunks(child, rank1, depth + 1)


def read_flashes(data: bytes) -> list[tuple[float, float]]:
    """[(lat, lon), ...] for every flash in one LCFA file."""
    h5 = _H5(data)
    links = h5.links(h5.root)
    if "flash_lat" not in links or "flash_lon" not in links:
        raise GLMFormatError("flash_lat/flash_lon not found")
    lats = h5.read_float32(links["flash_lat"])
    lons = h5.read_float32(links["flash_lon"])
    if len(lats) != len(lons):
        raise GLMFormatError("lat/lon length mismatch")
    return [(la, lo) for la, lo in zip(lats, lons) if -90 <= la <= 90 and -180 <= lo <= 180]
