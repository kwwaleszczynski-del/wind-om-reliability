"""Download only the event logs of the open datasets from Zenodo (byte ranges of the yearly archives).

* **Penmanshiel** (14 x Senvion MM82, CC BY 4.0, record 16807304): the ``Status_*.csv`` members of every
  yearly SCADA archive, 2016-2024 - about 8 MB instead of 7 GB.
* **Hill of Towie** (21 x Siemens SWT-2.3-VS-82, CC BY 4.0, record 22662930, version 2.1.0): the monthly
  ``tblAlarmLog_*.csv`` members of every yearly archive, 2016-2026 (about 32 MB instead of 15 GB), plus the
  turbine metadata and the alarm description table.
* **Kelmarsh** (6 x Senvion MM92, CC BY 4.0) is read from the data folder of
  github.com/kwwaleszczynski-del/wind-power-curve-kelmarsh (``KELMARSH_DATA_DIR``): its yearly archives already
  hold the status logs, 2016-2021.

The remote file reader and the Zenodo helpers come from the ``wind_downtime`` package
(github.com/kwwaleszczynski-del/wind-downtime-availability, the ``scada`` extra). Each member is fetched in one
range request of exactly its size (a large read-ahead would download far more than these small members), the
received bytes are counted and the download stops above ``BUDGET_MB``. Every member's CRC-32 is checked.

Usage:
    python -m wind_om fetch --data-dir data
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

PENMANSHIEL_RECORD = "16807304"
HILL_OF_TOWIE_RECORD = "22662930"
HOT_STATIC = ("Hill_of_Towie_turbine_metadata.csv", "Hill_of_Towie_alarms_description.csv",
              "Hill_of_Towie_turbine_fields_description.csv")
PENMANSHIEL_STATIC = ("Penmanshiel_WT_static.csv",)
USER_AGENT = "wind-om-reliability (open-source analysis)"


def _session():
    import requests

    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


BUFFER = 64 << 10  # read-ahead for the central directory; members are fetched with exact ranges
BUDGET_MB = 60.0
PAUSE_S = 1.0


class Meter:
    """Counts the bytes received through range requests and stops above a budget."""

    def __init__(self, budget_mb: float = BUDGET_MB):
        self.bytes, self.budget = 0, budget_mb * 1e6

    def add(self, n: int) -> None:
        self.bytes += n
        if self.bytes > self.budget:
            raise RuntimeError(f"download budget of {self.budget / 1e6:.0f} MB reached; stopped")


METER = Meter()


def _get(session, url: str, headers: dict, tries: int = 4):
    """GET with a pause between requests and a wait on HTTP 429 (Retry-After, else 60 s); no tight loop."""
    import time

    for k in range(tries):
        time.sleep(PAUSE_S)
        r = session.get(url, headers=headers, timeout=300)
        if r.status_code != 429:
            r.raise_for_status()
            return r
        wait = float(r.headers.get("Retry-After", 60))
        log.warning("HTTP 429 from Zenodo, waiting %.0f s (%d/%d)", wait, k + 1, tries)
        time.sleep(wait)
    r.raise_for_status()
    return r


def _read_member(session, url: str, m) -> bytes:
    """One member of a remote zip in a single range request (local header + data), CRC-checked."""
    import struct
    import zlib

    n = 30 + len(m.filename.encode()) + 1024 + m.compress_size  # header + generous room for the extra field
    r = _get(session, url, {"Range": f"bytes={m.header_offset}-{m.header_offset + n - 1}"})
    METER.add(len(r.content))
    buf = r.content
    if buf[:4] != b"PK\x03\x04":
        raise OSError(f"{m.filename}: no local header at offset {m.header_offset}")
    name_len, extra_len = struct.unpack("<HH", buf[26:30])
    data = buf[30 + name_len + extra_len:30 + name_len + extra_len + m.compress_size]
    if m.compress_type == 8:
        out = zlib.decompressobj(-15).decompress(data)
    elif m.compress_type == 0:
        out = data
    else:
        raise OSError(f"{m.filename}: unsupported compression {m.compress_type}")
    if zlib.crc32(out) != m.CRC or len(out) != m.file_size:
        raise OSError(f"{m.filename}: CRC or size mismatch")
    return out


def extract_members(session, info: dict, pattern: re.Pattern, dest: Path, buffer: int = BUFFER) -> Path:
    """Copy the members of a remote zip whose names match ``pattern`` into the local zip ``dest``."""
    import io
    import zipfile

    from wind_downtime.fetch import RemoteFile

    if dest.exists():
        log.info("cached %s", dest.name)
        return dest

    class Counted(RemoteFile):
        def readinto(self, b):
            n = super().readinto(b)
            METER.add(n)
            return n

    # the central directory sits at the end of the archive: a few small reads
    remote = zipfile.ZipFile(io.BufferedReader(Counted(session, info["url"], info["size"]), buffer))
    members = [m for m in remote.infolist() if pattern.search(m.filename)]
    log.info("%s: %d of %d members, %.2f MB compressed", dest.name, len(members), len(remote.infolist()),
             sum(m.compress_size for m in members) / 1e6)
    tmp = dest.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for m in members:
            out.writestr(m.filename, _read_member(session, info["url"], m))
    tmp.replace(dest)
    log.info("  received so far: %.1f MB", METER.bytes / 1e6)
    return dest


def fetch_penmanshiel(data_dir: Path, session=None) -> list[Path]:
    from wind_downtime.fetch import record_files
    from wind_power_curve.zenodo import download_file

    session = session or _session()
    raw = Path(data_dir) / "raw" / "penmanshiel"
    raw.mkdir(parents=True, exist_ok=True)
    files = record_files(session, PENMANSHIEL_RECORD)
    out = [download_file(session, files[n], raw / n) for n in PENMANSHIEL_STATIC]
    pattern = re.compile(r"^Status_Penmanshiel_\d+_.*\.csv$")
    for name in sorted(n for n in files if n.startswith("Penmanshiel_SCADA_") and n.endswith(".zip")):
        dest = raw / ("status_" + re.sub(r"[^A-Za-z0-9_.-]", "_", name))
        out.append(extract_members(session, files[name], pattern, dest))
    return out


def fetch_hill_of_towie(data_dir: Path, session=None) -> list[Path]:
    from wind_downtime.fetch import record_files
    from wind_power_curve.zenodo import download_file

    session = session or _session()
    raw = Path(data_dir) / "raw" / "hill_of_towie"
    raw.mkdir(parents=True, exist_ok=True)
    files = record_files(session, HILL_OF_TOWIE_RECORD)
    out = [download_file(session, files[n], raw / n) for n in HOT_STATIC]
    pattern = re.compile(r"^tblAlarmLog_\d{4}_\d{2}\.csv$")
    for name in sorted(n for n in files if re.fullmatch(r"\d{4}\.zip", n)):
        out.append(extract_members(session, files[name], pattern, raw / f"alarms_{name}"))
    return out


def fetch(data_dir: Path) -> list[Path]:
    s = _session()
    return fetch_penmanshiel(data_dir, s) + fetch_hill_of_towie(data_dir, s)
