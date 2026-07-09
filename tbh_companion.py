"""
tbh-companion — stage tracker para o TBH Tracker Vue.
Lê dados ao vivo do TaskBarHero.exe via IL2CPP + ReadProcessMemory.
Faz upload para Firebase RTDB em users/{uid}/stage a cada 2s.

Uso: python tbh_companion.py --uid <firebase_uid> [--hz 0.5]
"""

COMPANION_VERSION = "1.2.0"

import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import struct
import sys
import time
import argparse
import threading
import queue as _queue
import winreg
from collections import deque

# ── File logging (patch no builtins.print — não toca sys.stdout) ─────────────
import builtins as _builtins
_orig_print = _builtins.print
_log_file   = None

# stdout/stderr robustos: em console cp1252 (PT-BR) caracteres como '→' quebrariam
# o print e derrubariam o loop. errors='replace' garante que log nunca crashe.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    _log_path = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "tbh_companion.log")
    _log_file = open(_log_path, "w", encoding="utf-8", buffering=1)
except Exception:
    pass

def _patched_print(*args, sep=" ", end="\n", file=None, flush=False):
    try:
        _orig_print(*args, sep=sep, end=end, file=file, flush=flush)
    except Exception:
        pass
    if file is None and _log_file:
        try:
            msg = sep.join(str(a) for a in args)
            _log_file.write(time.strftime("%H:%M:%S") + " " + msg + end)
            if flush:
                _log_file.flush()
        except Exception:
            pass

_builtins.print = _patched_print
try:
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives.hashes import SHA1
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False

# ── Win32 constants ──────────────────────────────────────────────────────────
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ           = 0x0010
TH32CS_SNAPPROCESS        = 0x00000002
TH32CS_SNAPMODULE         = 0x00000008
MAX_PATH                  = 260

PROCESS_NAME = "TaskBarHero.exe"
MODULE_NAME  = "GameAssembly.dll"

# ── IL2CPP Class offsets (validados via offsets.pyc do tbh-reader) ──────────
CLASS_NAME        = 0x10   # char* name
CLASS_ELEMENT     = 0x40   # self-ref para tipos normais
CLASS_CAST        = 0x48   # self-ref para tipos normais
CLASS_PARENT      = 0x58   # Il2CppClass* parent
CLASS_STATIC_FLDS = 0xB8   # void* static_fields

SINGLETON_INSTANCE = 0x00  # instância em static_fields + 0

# List<T> / Array layout
LIST_ITEMS = 0x10
LIST_SIZE  = 0x18
ARRAY_DATA = 0x20

# ── Offsets extraídos de offsets.pyc (tbh-reader v0.33.1) ───────────────────
# MonsterSpawnManager
MSM_MONSTER_LIST      = 0x28  # List<Monster> vivos
MSM_DEAD_MONSTER_LIST = 0x30  # List<Monster> mortos — para mobs_killed
MSM_SUMMONED_LIST     = 0x38  # List<Monster> invocados

# Unit / Monster
UNIT_HEALTH_CONTROLLER = 0xB0  # ptr → UnitHealthController
HC_HP_CURRENT          = 0x40  # float hp atual
HC_HP_MAX              = 0x4C  # float hp máximo
MONSTER_STAGE_KEY      = 0x3D4 # int stageKey (980) — confirmado no offsets.pyc do tbh-meter

# StageManager
SM_HERO_LIST = 0x30  # List<HeroRuntime> heróis em campo

# HeroRuntime
HR_INFO       = 0x30  # ptr → HeroInfoData
HR_LEVEL_FAKE = 0xD8  # ObscuredInt: nível fake (plaintext em +0)
HR_EXP_FAKE   = 0x118 # ObscuredInt: xp fake (plaintext em +0)
ACTK_FAKE     = 12    # offset dentro do ObscuredInt pro fakeValue

# HeroInfoData
HID_HERO_KEY  = 0x30  # int heroKey

# LogManager
LM_LOG_BY_TYPE = 0x20  # Dict<ELogType, List<ILog>> — offsets.pyc campo 0 do LogManager

# Log entry offsets (base obj has 64 bytes of IL2CPP overhead)
SCL_ACT        = 0x40  # int — act (StageClearLog)
SCL_STAGE      = 0x44  # int — stage no
SCL_CLEAR_TIME = 0x48  # float — tempo oficial de clear (segundos)
SCL_IS_BOSS    = 0x4C  # int — boss stage?

SFL_ACT        = 0x40  # int (StageFailedLog)
SFL_STAGE      = 0x44  # int

# ELogType enum values
ELOGTYPE_STAGECLEAR  = 1
ELOGTYPE_GETBOX      = 3
ELOGTYPE_STAGEFAILED = 7

# GetBoxLog
GBL_BOX_KEY      = 0x40  # int boxKey
GBL_MONSTER_TYPE = 0x50  # int: 0=mob, 1=boss, 2=actboss

# EMonsterLogType
BOX_MOB     = 0
BOX_BOSS    = 1
BOX_ACTBOSS = 2

# AggregateManager
AM_AGGREGATES = 0x20  # Dict<EAggregateType, Dict> externo

# Dict8B (stride 24: hash@0, next@4, key@8, value@16)
DICT_ENTRIES  = 0x18  # ptr → backing array
DICT_COUNT    = 0x20  # número de entradas (incluindo tombstones)
DICT8B_STRIDE = 24
DICT8B_KEY    = 8
DICT8B_VALUE  = 16

# EAggregateType enum
EAGGR_GOLDEARN = 2

# TypeInfoTable
TYPEINFO_TABLE_ENTRY_SIZE = 8

def _resource_path(rel: str) -> str:
    """Resolve caminho tanto em dev quanto dentro do bundle PyInstaller."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)

CALIB_SEED_PATH = _resource_path("calib_seed.json")

SAVE_PATH = os.path.expandvars(
    r"%USERPROFILE%\AppData\LocalLow\TesseractStudio\TaskbarHero\SaveFile_Live.es3"
)
ES3_PASSWORD  = b'emuMqG3bLYJ938ZDCfieWJ'
HERO_CLS_NAME = {101:'Knight', 201:'Ranger', 301:'Sorcerer',
                 401:'Priest', 501:'Abalist', 601:'Slayer'}


_LOG_PATH = os.path.join(os.path.expanduser("~"), "TBHTracker_startup.log")

def _log(msg: str):
    print(msg)
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _read_save_heroes() -> list[str]:
    """Lê arrangedHeroKey do save ES3 e retorna lista de class names."""
    if not _CRYPTO_OK or not os.path.exists(SAVE_PATH):
        return []
    try:
        with open(SAVE_PATH, 'rb') as f:
            data = f.read()
        iv         = data[:16]
        ciphertext = data[16:]
        kdf = PBKDF2HMAC(algorithm=SHA1(), length=16, salt=iv,
                         iterations=100, backend=default_backend())
        key = kdf.derive(ES3_PASSWORD)
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv),
                        backend=default_backend())
        dec = cipher.decryptor()
        plain = dec.update(ciphertext) + dec.finalize()
        pad = plain[-1]
        plain = plain[:-pad]
        raw = json.loads(plain.decode('utf-8'))
        psd_raw = raw.get('PlayerSaveData', {}).get('value', '{}')
        psd = json.loads(psd_raw) if isinstance(psd_raw, str) else psd_raw
        cs  = psd.get('commonSaveData', {})
        keys = [k for k in (cs.get('arrangedHeroKey') or []) if k and k != -1]
        return [HERO_CLS_NAME.get(k, str(k)) for k in keys]
    except Exception as e:
        print(f"[heroes] erro lendo save: {e}")
        return []

DIFFICULTY = {0: "Normal", 1: "Nightmare", 2: "Hell", 3: "Torment"}

# Mapa de diff_code do prefixo do stageKey → diff_idx
_SK_DIFF_CODE = {1: 0, 2: 1, 3: 2, 9: 3}

def _decode_stage_key(sk: int):
    """Decodifica stageKey em (act, stage_no, diff_idx) sem precisar de stage_info.
    Formato v1.00.11: (diff+1)*1000 + act*100 + stage_no  (chaves < 10000)
    Formato v1.00.20+: D*100000 + act*10000 + X*1000 + stage_no*100 + YY  (chaves >= 100000)
    """
    if not sk or sk <= 0:
        return None, None, None
    if sk < 10_000:
        diff = (sk // 1000) - 1
        act  = (sk // 100) % 10
        sno  = sk % 100
        if 0 <= diff <= 3 and 1 <= act <= 3 and 1 <= sno <= 99:
            return act, sno, diff
    elif sk >= 100_000:
        diff_code = sk // 100_000
        act  = (sk // 10_000) % 10
        sno  = (sk // 100) % 100
        diff = _SK_DIFF_CODE.get(diff_code)
        if diff is not None and 1 <= act <= 9 and 1 <= sno <= 99:
            return act, sno, diff
    return None, None, None

# ── Win32 structs ────────────────────────────────────────────────────────────
class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize",              wt.DWORD),
        ("cntUsage",            wt.DWORD),
        ("th32ProcessID",       wt.DWORD),
        ("th32DefaultHeapID",   ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID",        wt.DWORD),
        ("cntThreads",          wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase",      wt.LONG),
        ("dwFlags",             wt.DWORD),
        ("szExeFile",           ctypes.c_char * MAX_PATH),
    ]

class MODULEENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize",       wt.DWORD),
        ("th32ModuleID", wt.DWORD),
        ("th32ProcessID",wt.DWORD),
        ("GlblcntUsage", wt.DWORD),
        ("ProccntUsage", wt.DWORD),
        ("modBaseAddr",  ctypes.POINTER(wt.BYTE)),
        ("modBaseSize",  wt.DWORD),
        ("hModule",      wt.HMODULE),
        ("szModule",     ctypes.c_char * 256),
        ("szExePath",    ctypes.c_char * MAX_PATH),
    ]

_k32 = None
def k32():
    global _k32
    if _k32 is None:
        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return _k32

# ── Process helpers ──────────────────────────────────────────────────────────
def find_pid(name: str) -> int | None:
    snap = k32().CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wt.HANDLE(-1).value:
        return None
    entry = PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        if not k32().Process32First(snap, ctypes.byref(entry)):
            return None
        while True:
            if entry.szExeFile.decode("utf-8", "replace").lower() == name.lower():
                return entry.th32ProcessID
            if not k32().Process32Next(snap, ctypes.byref(entry)):
                return None
    finally:
        k32().CloseHandle(snap)

def open_process(pid: int):
    return k32().OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)

def module_base(pid: int, mod_name: str) -> int | None:
    snap = k32().CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, pid)
    if snap == wt.HANDLE(-1).value:
        time.sleep(0.05)
        snap = k32().CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, pid)
        if snap == wt.HANDLE(-1).value:
            return None
    entry = MODULEENTRY32()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        if not k32().Module32First(snap, ctypes.byref(entry)):
            return None
        while True:
            if entry.szModule.decode("utf-8", "replace").lower() == mod_name.lower():
                return ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value
            if not k32().Module32Next(snap, ctypes.byref(entry)):
                return None
    finally:
        k32().CloseHandle(snap)

# ── Memory reader ────────────────────────────────────────────────────────────
class Reader:
    def __init__(self, handle):
        self._h = handle

    def read(self, addr: int, size: int) -> bytes | None:
        if not addr:
            return None
        buf  = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t(0)
        ok   = k32().ReadProcessMemory(self._h, ctypes.c_void_p(addr), buf, size, ctypes.byref(read))
        if not ok or read.value != size:
            return None
        return buf.raw

    def rptr(self, addr: int) -> int | None:
        b = self.read(addr, 8)
        if b is None:
            return None
        v = struct.unpack_from("<Q", b)[0]
        return v if v else None

    def ri32(self, addr: int) -> int | None:
        b = self.read(addr, 4)
        return struct.unpack_from("<i", b)[0] if b else None

    def ru32(self, addr: int) -> int | None:
        b = self.read(addr, 4)
        return struct.unpack_from("<I", b)[0] if b else None

    def ri64(self, addr: int) -> int | None:
        b = self.read(addr, 8)
        return struct.unpack_from("<q", b)[0] if b else None

    def rf32(self, addr: int) -> float | None:
        b = self.read(addr, 4)
        if b is None:
            return None
        v = struct.unpack_from("<f", b)[0]
        return v if v == v else None  # NaN check

    def read_cstr(self, addr: int) -> str | None:
        b = self.read(addr, 64)
        if not b:
            return None
        end = b.find(b'\x00')
        raw = b[:end] if end >= 0 else b
        s   = raw.decode("ascii", "replace")
        if not all(32 <= ord(c) < 127 for c in s):
            return None
        return s

# ── IL2CPP helpers ────────────────────────────────────────────────────────────
def load_calib_seed() -> dict:
    path = CALIB_SEED_PATH
    if not os.path.exists(path):
        alt = os.path.expanduser(r"~\AppData\Local\Programs\tbh-meter\resources\reader\calib_seed.json")
        if os.path.exists(alt):
            path = alt
        else:
            return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def detect_game_version(handle) -> str | None:
    buf  = ctypes.create_string_buffer(MAX_PATH * 2)
    size = wt.DWORD(MAX_PATH * 2)
    if k32().QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
        exe_path = buf.raw[:size.value * 2].decode("utf-16-le", "replace").rstrip("\x00")
        ver_path = os.path.join(os.path.dirname(exe_path), "Version.txt")
        try:
            with open(ver_path, encoding="utf-8-sig") as f:
                return f.read(40).strip()
        except Exception:
            pass
    return None

def find_calib_entry(seed: dict, version: str | None) -> dict | None:
    """Retorna a entrada do calib para a versão exata do jogo, ou None se não encontrada."""
    if not version:
        return None
    calib = seed.get("calib", {})
    for key, entry in calib.items():
        if key.startswith(version):
            return entry
    return None

def typeinfo_table_base(mem: Reader, ga_base: int, anchor_rva: int) -> int | None:
    return mem.rptr(ga_base + anchor_rva)

def class_by_index(mem: Reader, table_base: int, idx: int) -> int | None:
    return mem.rptr(table_base + idx * TYPEINFO_TABLE_ENTRY_SIZE)

def class_name_ok(mem: Reader, klass: int, expected: str) -> bool:
    name_ptr = mem.rptr(klass + CLASS_NAME)
    if not name_ptr:
        return False
    return mem.read_cstr(name_ptr) == expected

def singleton_instance(mem: Reader, klass: int) -> int | None:
    parent      = mem.rptr(klass + CLASS_PARENT)
    if not parent:
        return None
    static_flds = mem.rptr(parent + CLASS_STATIC_FLDS)
    if not static_flds:
        return None
    return mem.rptr(static_flds + SINGLETON_INSTANCE)

# ── Auto-detect anchor_rva ────────────────────────────────────────────────────
_APP_DIR      = os.path.join(os.path.expandvars("%LOCALAPPDATA%"), "TBHTracker")
_ANCHOR_CACHE = os.path.join(_APP_DIR, "anchor_cache.json")
_LOCK_FILE    = os.path.join(_APP_DIR, "tbhtracker.lock")


def _kill_previous_instance():
    """Se outro TBHTracker estiver rodando, encerra antes de continuar."""
    try:
        if os.path.exists(_LOCK_FILE):
            with open(_LOCK_FILE) as f:
                pid = int(f.read().strip())
            if pid != os.getpid():
                os.system(f"taskkill /F /PID {pid} >nul 2>&1")
                time.sleep(0.8)   # aguarda o processo encerrar
    except Exception:
        pass
    # Registra o próprio PID
    os.makedirs(_APP_DIR, exist_ok=True)
    with open(_LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    import atexit
    atexit.register(_release_lock)


def _release_lock():
    try:
        os.remove(_LOCK_FILE)
    except Exception:
        pass

def _load_anchor_cache(version: str) -> tuple[int, int] | tuple[None, None]:
    """Retorna (anchor_rva, msm_idx) do cache ou (None, None)."""
    try:
        with open(_ANCHOR_CACHE, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") == version:
            return data["anchor_rva"], data.get("msm_idx", 2931)
    except Exception:
        pass
    return None, None

def _save_anchor_cache(version: str, anchor_rva: int, msm_idx: int):
    try:
        os.makedirs(_APP_DIR, exist_ok=True)
        with open(_ANCHOR_CACHE, "w", encoding="utf-8") as f:
            json.dump({"version": version, "anchor_rva": anchor_rva, "msm_idx": msm_idx}, f)
    except Exception:
        pass

# ── Cache de calibração por versão (todas as referências reencontradas) ─────
# Guarda o que foi mapeado por nome/comportamento para cada versão do jogo,
# para não re-varrer a memória toda vez (só na 1ª vez de cada versão).
_CALIB_CACHE = os.path.join(_APP_DIR, "calib_cache.json")

def _load_calib_cache(version: str) -> dict:
    try:
        with open(_CALIB_CACHE, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") == version:
            return data
    except Exception:
        pass
    return {}

def _save_calib_cache(version: str, **fields):
    data = _load_calib_cache(version)
    data.update(fields)
    data["version"] = version
    try:
        os.makedirs(_APP_DIR, exist_ok=True)
        with open(_CALIB_CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass

def _writable_sections(dll_path: str) -> list[tuple[int, int]]:
    """Lê o PE header do DLL e retorna (rva, size) das seções graváveis (.data/.bss)."""
    result = []
    try:
        with open(dll_path, 'rb') as f:
            f.seek(0x3C)
            pe_off = struct.unpack('<I', f.read(4))[0]
            f.seek(pe_off)
            if f.read(4) != b'PE\x00\x00':
                return result
            f.seek(pe_off + 4)
            coff = f.read(20)
            num_sections = struct.unpack_from('<H', coff, 2)[0]
            opt_size     = struct.unpack_from('<H', coff, 16)[0]
            f.seek(pe_off + 4 + 20 + opt_size)
            for _ in range(num_sections):
                shdr = f.read(40)
                if len(shdr) < 40:
                    break
                vsize = struct.unpack_from('<I', shdr, 8)[0]
                rva   = struct.unpack_from('<I', shdr, 12)[0]
                chars = struct.unpack_from('<I', shdr, 36)[0]
                MEM_WRITE = 0x80000000
                MEM_EXEC  = 0x20000000
                if (chars & MEM_WRITE) and not (chars & MEM_EXEC) and vsize > 0:
                    result.append((rva, vsize))
    except Exception as e:
        _log(f"[scan] PE parse erro: {e}")
    return result


def _ga_dll_path(pid: int) -> str | None:
    snap = k32().CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, pid)
    if snap == wt.HANDLE(-1).value:
        return None
    entry = MODULEENTRY32()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        if not k32().Module32First(snap, ctypes.byref(entry)):
            return None
        while True:
            if entry.szModule.decode("utf-8", "replace").lower() == MODULE_NAME.lower():
                return entry.szExePath.decode("utf-8", "replace")
            if not k32().Module32Next(snap, ctypes.byref(entry)):
                return None
    finally:
        k32().CloseHandle(snap)


def _scan_anchor_rva(mem: Reader, ga_base: int, indices: dict, hint_rva: int,
                     pid: int | None = None) -> tuple[int, int] | tuple[None, None]:
    """
    Escaneia as seções graváveis do GameAssembly.dll (lidas do PE header) em
    busca do ponteiro para a IL2CPP type table. Testa o índice exato e ±50 para
    tolerar pequeno drift entre versões do jogo.
    """
    msm_idx_hint = indices.get("MonsterSpawnManager", 2931)
    CHUNK = 0x10000  # 64KB por leitura

    # Obtém as seções graváveis do PE header para limitar o scan
    sections: list[tuple[int, int]] = []
    if pid:
        dll_path = _ga_dll_path(pid)
        if dll_path:
            sections = _writable_sections(dll_path)
            _log(f"[scan] seções PE: {[(hex(r), hex(s)) for r,s in sections]}")

    if not sections:
        # Fallback: ±4MB ao redor do hint conhecido
        sections = [(max(0, hint_rva - 0x400000), 0x800000)]
        _log(f"[scan] fallback window hint=0x{hint_rva:x}")

    seen_tables: set[int] = set()
    chunks_read = 0
    survivors   = 0
    name_checks = 0
    deadline    = time.time() + 150.0
    dll_end_approx = ga_base + 0x10000000  # 256 MB — pointers inside DLL are not heap

    # Pre-filtro: 8 entradas consecutivas centradas no hint; todas devem ser
    # ponteiros de heap válidos E DISTINTOS. Reduz survivors de ~33K para <500.
    PRE_N    = 8
    PRE_OFF  = (msm_idx_hint - PRE_N // 2) * 8
    PRE_SIZE = PRE_N * 8
    HEAP_LO  = 0x10000000          # heap real começa bem acima de 16 MB
    HEAP_HI  = 0x800000000000

    DRIFT    = 500                  # ±500 em vez de ±200 — cobre drift maior

    for sec_rva, sec_size in sections:
        for chunk_off in range(0, sec_size, CHUNK):
            if time.time() > deadline:
                _log(f"[scan] timeout — chunks={chunks_read} survivors={survivors} name_checks={name_checks}")
                return None, None

            rva   = sec_rva + chunk_off
            size  = min(CHUNK, sec_size - chunk_off)
            chunk = mem.read(ga_base + rva, size)
            if not chunk:
                continue
            chunks_read += 1
            if chunks_read % 10 == 0:
                _log(f"[scan] chunk={chunks_read} survivors={survivors}")

            for i in range(0, len(chunk) - 8, 8):
                table = struct.unpack_from("<Q", chunk, i)[0]
                if not (HEAP_LO <= table < HEAP_HI):
                    continue
                if ga_base <= table < dll_end_approx:
                    continue
                if table in seen_tables:
                    continue
                seen_tables.add(table)

                # Pre-filtro: 8 ponteiros heap consecutivos, todos únicos
                blk = mem.read(table + PRE_OFF, PRE_SIZE)
                if not blk or len(blk) < PRE_SIZE:
                    continue
                ptrs = [struct.unpack_from("<Q", blk, j * 8)[0] for j in range(PRE_N)]
                if not all(HEAP_LO <= p < HEAP_HI for p in ptrs):
                    continue
                if len(set(ptrs)) < PRE_N - 1:   # admite 1 repetição ocasional
                    continue

                survivors += 1
                # Lê 1001 entradas (±500) de uma vez e busca MonsterSpawnManager
                scan_base = max(0, msm_idx_hint - DRIFT)
                n_entries = DRIFT * 2 + 1
                blk2 = mem.read(table + scan_base * 8, n_entries * 8)
                if not blk2:
                    continue
                for j in range(min(n_entries, len(blk2) // 8)):
                    klass = struct.unpack_from("<Q", blk2, j * 8)[0]
                    if not (HEAP_LO <= klass < HEAP_HI):
                        continue
                    name_checks += 1
                    if class_name_ok(mem, klass, "MonsterSpawnManager"):
                        found_idx = scan_base + j
                        _log(f"[scan] anchor=0x{rva+i:x} idx={found_idx} (delta={found_idx-msm_idx_hint:+d}) survivors={survivors} name_checks={name_checks}")
                        return rva + i, found_idx

    _log(f"[scan] não encontrado — chunks={chunks_read} survivors={survivors} name_checks={name_checks}")
    return None, None

def iter_dict8b(mem: Reader, dict_ptr: int):
    """Itera pares (key: int32, value: int64/ptr) de um Dict8B."""
    entries_arr = mem.rptr(dict_ptr + DICT_ENTRIES)
    count       = mem.ri32(dict_ptr + DICT_COUNT)
    if not entries_arr or not count or count <= 0 or count > 100_000:
        return
    base = entries_arr + ARRAY_DATA
    for i in range(count):
        entry     = base + i * DICT8B_STRIDE
        hash_code = mem.ri32(entry)
        if hash_code is None or hash_code < 0:  # tombstone
            continue
        key = mem.ri32(entry + DICT8B_KEY)
        val = mem.ri64(entry + DICT8B_VALUE)
        if key is not None and val is not None:
            yield key, val

# ── DPS tracker (HP-delta rolling window) ────────────────────────────────────
class DpsTracker:
    """Calcula DPS pela queda de HP dos monstros entre ticks."""
    WINDOW = 5.0  # segundos

    def __init__(self):
        self._last_hp:    dict[int, float] = {}  # addr -> hp
        self._window:     deque             = deque()  # (ts, damage)
        self.total_damage: float            = 0.0
        self.peak_dps:    float             = 0.0

    def reset(self):
        self._last_hp.clear()
        self._window.clear()
        self.total_damage = 0.0
        self.peak_dps     = 0.0

    def update(self, monsters: list[tuple[int, float]], ts: float) -> float:
        """monsters = [(addr, hp_cur), ...]. Retorna DPS atual."""
        current = {}
        damage  = 0.0

        for addr, hp in monsters:
            if hp is None or hp <= 0:
                continue
            current[addr] = hp
            prev = self._last_hp.get(addr)
            if prev is not None and hp < prev:
                damage += prev - hp

        # Monstros que desapareceram (mortos): conta HP restante como dano
        for addr, prev_hp in self._last_hp.items():
            if addr not in current and prev_hp > 0:
                damage += prev_hp

        self._last_hp = current

        if damage > 0:
            self._window.append((ts, damage))
            self.total_damage += damage

        # Remove entradas antigas da janela
        cutoff = ts - self.WINDOW
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

        if not self._window:
            return 0.0
        window_damage = sum(d for _, d in self._window)
        elapsed = ts - self._window[0][0]
        cur_dps = window_damage / max(elapsed, 0.5)
        if cur_dps > self.peak_dps:
            self.peak_dps = cur_dps
        return cur_dps

# ── Stage reader ──────────────────────────────────────────────────────────────
class StageReader:
    def __init__(self):
        self._pid       = None
        self._handle    = None
        self._mem: Reader | None = None
        self._ga_base   = None
        self._seed      = load_calib_seed()
        self._calib     = None
        self._table     = None
        self._singletons:   dict[str, int] = {}
        self._dps           = DpsTracker()
        self._gold_start:   int | None = None
        self._last_key:     int | None = None
        self._dead_baseline: int       = 0
        self._log_clear_sz: int        = 0
        self._log_fail_sz:  int        = 0
        self._log_box_sz:   int        = 0
        # _version das List<ILog> — cresce a cada Add/Remove. Detecção por versão
        # funciona mesmo quando a lista está no cap (size travado, mas version sobe).
        self._log_clear_ver: int       = -1
        self._log_fail_ver:  int       = -1
        self._log_box_ver:   int       = -1
        # Ponteiro da última entrada (sinal robusto de clear/fail — imune a abrir baú)
        self._last_clear_tail: int     = 0
        self._last_fail_tail:  int     = 0
        self._stage_boxes:  list[int]  = []  # EMonsterLogType dos baús deste stage
        self._lm_dict_off:  int        = LM_LOG_BY_TYPE  # offset do Dict<ELogType,List> (varia por versão)
        self._agg_dict_off: int        = AM_AGGREGATES   # offset do dict de agregados (ouro)
        self._scl_base:     int        = SCL_ACT         # base dos campos do StageClearLog (act,+4 stage,+8 time,+12 boss)
        self._ver:          str | None = None
        self._has_log:      bool       = False  # LogManager resolvido → clears vêm do log oficial
        # Retry lazy do LogManager: no attach (menu/sem clears) a lista de logs está
        # vazia e a detecção do dict_off falha. Retentamos durante o loop até os
        # primeiros logs aparecerem, sem depender do usuário reabrir o companion.
        self._log_retry_next: float    = 0.0    # próximo timestamp p/ retentar
        self._lm_idx_hint:  int | None = None    # idx da classe LogManager já achada por nome
                                                 # (evita re-scan lento nos retries)
        self._lm_scanned:   bool       = False   # scan por nome já rodou (só 1x por sessão)
        self._status_cb                = None   # callback p/ mostrar progresso na splash

    def set_status_cb(self, cb):
        self._status_cb = cb

    def _report(self, msg: str):
        if self._status_cb:
            try: self._status_cb(msg)
            except Exception: pass

    def _attach(self) -> bool:
        pid = find_pid(PROCESS_NAME)
        if not pid:
            print(f"[attach] processo '{PROCESS_NAME}' não encontrado")
            return False
        print(f"[attach] pid={pid} encontrado")
        handle = open_process(pid)
        if not handle:
            print(f"[attach] open_process falhou (sem permissão? tente como admin)")
            return False
        ga_base = module_base(pid, MODULE_NAME)
        if not ga_base:
            k32().CloseHandle(handle)
            print(f"[attach] módulo '{MODULE_NAME}' não encontrado no processo")
            return False
        print(f"[attach] ga_base=0x{ga_base:x}")

        self._pid        = pid
        self._handle     = handle
        self._mem        = Reader(handle)
        self._ga_base    = ga_base
        self._singletons = {}
        self._calib      = None
        self._table      = None

        ver   = detect_game_version(handle)
        entry = find_calib_entry(self._seed, ver)

        if entry:
            self._calib = entry
            self._table = typeinfo_table_base(self._mem, ga_base, entry["anchor_rva"])
            _log(f"[attach] pid={pid} ver={ver} calib=OK table={'0x'+hex(self._table)[2:] if self._table else 'None'}")
        else:
            # Versão não está no calib_seed — tenta auto-detectar anchor_rva
            seed_calib = self._seed.get("calib", {})
            anchor_rva = None
            if seed_calib:
                fallback  = list(seed_calib.values())[-1]
                indices   = fallback["indices"]
                hint_rva  = fallback["anchor_rva"]

                cached_rva, cached_idx = _load_anchor_cache(ver) if ver else (None, None)
                if cached_rva:
                    anchor_rva = cached_rva
                    msm_idx    = cached_idx
                    _log(f"[attach] pid={pid} ver={ver} anchor_rva cache=0x{anchor_rva:x} msm_idx={msm_idx}")
                else:
                    _log(f"[attach] pid={pid} ver={ver} não no calib — varrendo anchor_rva...")
                    anchor_rva, msm_idx = _scan_anchor_rva(self._mem, ga_base, indices, hint_rva, pid=pid)
                    if anchor_rva:
                        _save_anchor_cache(ver, anchor_rva, msm_idx)
                        _log(f"[attach] anchor_rva=0x{anchor_rva:x} msm_idx={msm_idx}")
                    else:
                        _log("[attach] anchor_rva não encontrado — sem leitura de stage")

                if anchor_rva:
                    # Usa o índice correto encontrado pelo scan (pode diferir do calib base)
                    patched_indices = {**indices, "MonsterSpawnManager": msm_idx}
                    # Inclui stage_info do fallback para que totalMobs seja acessível
                    self._calib = {
                        "anchor_rva":  anchor_rva,
                        "indices":     patched_indices,
                        "stage_info":  fallback.get("stage_info", {}),
                    }
                    self._table = typeinfo_table_base(self._mem, ga_base, anchor_rva)
            else:
                _log("[attach] calib_seed vazio — não pode iniciar leitura de memória")

        # Corrige o LogManager para a versão atual (índice + offset do dict).
        # Sem isso, versões fora do calib_seed liam o dict de logs errado (0x1).
        self._ver = ver
        self._fix_logmanager(ver)
        # Reencontra a AggregateManager (ouro) — classe ofuscada, por comportamento.
        self._fix_aggregate(ver)
        # Valida/detecta os offsets dos campos do StageClearLog (act/stage/tempo).
        self._fix_scl_offsets(ver)
        # Self-check diagnóstico dos offsets de struct restantes.
        self._selfcheck()

        # Captura baselines dos logs AGORA, pra não falso-positivo em clears
        # que já existem em memória de antes de anexarmos.
        self.capture_log_baselines()
        if self._log_clear_sz >= 0:
            _log(f"[attach] LogManager OK — clears={self._log_clear_sz} fails={self._log_fail_sz}")
        else:
            _log("[attach] LogManager não encontrado — usando detecção por key/mobs")
        return True

    def _detach(self):
        if self._handle:
            k32().CloseHandle(self._handle)
        self._pid = self._handle = self._mem = self._ga_base = None
        self._singletons = {}
        self._dps.reset()
        self._gold_start = None
        # Estado do LogManager é por-processo (table base muda ao reabrir o jogo):
        # zera pra re-resolver do zero no próximo attach.
        self._has_log       = False
        self._lm_idx_hint   = None
        self._lm_scanned    = False
        self._log_retry_next = 0.0
        # _last_key preservado: o loop principal usa para fechar o histórico

    def _singleton(self, name: str) -> int | None:
        if not self._calib or not self._table:
            return None
        cached = self._singletons.get(name)
        if cached:
            return cached
        idx = self._calib["indices"].get(name)
        if idx is None:
            return None
        klass = class_by_index(self._mem, self._table, idx)
        name_ok = class_name_ok(self._mem, klass, name) if klass else False
        if not klass or not name_ok:
            return None
        inst = singleton_instance(self._mem, klass)
        if inst:
            self._singletons[name] = inst
        return inst

    # ── Correção do LogManager por versão (índice + offset do dict) ────────────
    def _scan_class_idx_by_name(self, name: str, hi: int = 60000) -> int | None:
        """Varre a type table procurando uma classe pelo nome exato. Lento; usar com cache."""
        if not self._table:
            return None
        for idx in range(0, hi):
            klass = class_by_index(self._mem, self._table, idx)
            if not klass:
                continue
            np = self._mem.rptr(klass + CLASS_NAME)
            if not np:
                continue
            if self._mem.read_cstr(np) == name:
                return idx
        return None

    def _detect_log_dict_off(self, lm: int) -> int | None:
        """Acha o offset do Dict<ELogType,List> nos campos-ponteiro do LogManager,
        validando pelas chaves ELogType conhecidas (StageClear=1, GetBox=3, StageFailed=7)."""
        KNOWN = {ELOGTYPE_STAGECLEAR, ELOGTYPE_GETBOX, ELOGTYPE_STAGEFAILED}
        for off in range(0x10, 0xA0, 8):
            p = self._mem.rptr(lm + off)
            if not p:
                continue
            cnt = self._mem.ri32(p + DICT_COUNT)
            if not cnt or cnt <= 0 or cnt > 200:
                continue
            keys = {k for k, _ in iter_dict8b(self._mem, p)}
            if KNOWN & keys and len(keys) >= 3:
                return off
        return None

    def _fix_logmanager(self, ver: str | None):
        """Garante que LogManager resolva corretamente na versão atual do jogo.
        Corrige o índice da classe (por nome, com cache) e o offset do dict de logs."""
        self._has_log = False
        if not self._calib or not self._table:
            return
        # 1) O índice do calib já resolve um LogManager válido com dict de logs?
        def _lm_ok(idx):
            if idx is None:
                return None
            klass = class_by_index(self._mem, self._table, idx)
            if not klass or not class_name_ok(self._mem, klass, "LogManager"):
                return None
            inst = singleton_instance(self._mem, klass)
            if not inst:
                return None
            off = self._detect_log_dict_off(inst)
            return (inst, off) if off is not None else None

        res = _lm_ok(self._calib["indices"].get("LogManager"))
        if res:
            self._lm_dict_off = res[1]
            self._has_log = True
            _log(f"[attach] LogManager idx do calib OK, dict_off=0x{self._lm_dict_off:x}")
            return

        # 2) Índice do calib não serve — usa cache ou varre por nome
        cache = _load_calib_cache(ver) if ver else {}
        lm_idx, lm_off = cache.get("lm_idx"), cache.get("lm_dict_off")
        if lm_idx is not None and _lm_ok(lm_idx):
            self._calib["indices"]["LogManager"] = lm_idx
            self._lm_dict_off = lm_off
            self._has_log = True
            self._singletons.pop("LogManager", None)
            _log(f"[attach] LogManager cache idx={lm_idx} dict_off=0x{lm_off:x}")
            return

        # 3) Já achamos a classe por nome numa tentativa anterior? Só falta o dict
        # ter entradas — revalida direto, sem re-scan lento (retry no menu vazio).
        if self._lm_idx_hint is not None:
            res = _lm_ok(self._lm_idx_hint)
            if res:
                idx = self._lm_idx_hint
                self._calib["indices"]["LogManager"] = idx
                self._lm_dict_off = res[1]
                self._has_log = True
                self._singletons.pop("LogManager", None)
                if ver:
                    _save_calib_cache(ver, lm_idx=idx, lm_dict_off=res[1])
                _log(f"[attach] LogManager idx={idx} dict_off=0x{self._lm_dict_off:x} (salvo no cache)")
            return  # classe já localizada; sem res é só logs ainda vazios → próximo retry

        # 4) Scan por nome — só uma vez por sessão. A classe existe na type table
        # independente de haver logs, então se não achar aqui, re-scanear não ajuda.
        if self._lm_scanned:
            return
        self._lm_scanned = True
        _log("[attach] LogManager: varrendo índice por nome (uma vez por versão)...")
        self._report("Primeira vez nesta versão — mapeando a memória do jogo. Só desta vez.")
        idx = self._scan_class_idx_by_name("LogManager")
        if idx is not None:
            self._lm_idx_hint = idx  # lembra p/ os retries não re-scanearem
        res = _lm_ok(idx) if idx is not None else None
        if idx is not None and res:
            self._calib["indices"]["LogManager"] = idx
            self._lm_dict_off = res[1]
            self._has_log = True
            self._singletons.pop("LogManager", None)
            if ver:
                _save_calib_cache(ver, lm_idx=idx, lm_dict_off=res[1])
            _log(f"[attach] LogManager idx={idx} dict_off=0x{self._lm_dict_off:x} (salvo no cache)")
        else:
            _log("[attach] LogManager não localizado — detecção oficial de clear indisponível")

    def _retry_logmanager_if_needed(self):
        """Retenta resolver o LogManager durante o loop enquanto _has_log for False.

        No attach (menu/sem clears) o Dict<ELogType,List> está vazio, então
        _detect_log_dict_off não consegue validar as chaves e a detecção falha.
        Assim que o primeiro combate gera logs, este retry (a cada ~5s) resolve o
        LogManager sozinho — sem o usuário precisar reabrir o companion. Também
        re-valida os offsets do StageClearLog e recaptura os baselines dos logs."""
        if self._has_log or not self._calib or not self._table:
            return
        now = time.time()
        if now < self._log_retry_next:
            return
        self._log_retry_next = now + 5.0
        # Limpa o singleton em cache: uma tentativa anterior pode ter fixado uma
        # instância com dict_off errado.
        self._singletons.pop("LogManager", None)
        self._fix_logmanager(self._ver)
        if self._has_log:
            self._fix_scl_offsets(self._ver)
            self.capture_log_baselines()
            _log("[retry] LogManager resolvido durante o loop — detecção oficial de clear ativa")

    # ── AggregateManager (ouro): classe OFUSCADA — achada por comportamento ─────
    def _agg_dict_ok(self, inst: int, off: int) -> bool:
        """Assinatura única da AggregateManager: dict de EAggregateType com
        GOLDEARN(2)→subkey com valor grande E o type 0 com muitas entradas (kills/tipo)."""
        d = self._mem.rptr(inst + off)
        if not d:
            return False
        cnt = self._mem.ri32(d + DICT_COUNT)
        if not cnt or cnt <= 0 or cnt > 100:
            return False
        gold_ok = False
        type0_big = False
        seen = 0
        for k, v in iter_dict8b(self._mem, d):
            seen += 1
            if seen > 60:
                break
            if k == EAGGR_GOLDEARN and v:
                for sk, sv in iter_dict8b(self._mem, v):
                    if sv and sv > 1000:
                        gold_ok = True
                        break
            elif k == 0 and v:
                inner_cnt = self._mem.ri32(v + DICT_COUNT)
                if inner_cnt and inner_cnt > 20:
                    type0_big = True
        return gold_ok and type0_big

    def _find_aggregate_idx(self):
        """Varre singletons procurando a AggregateManager pela assinatura dos dados.
        Retorna (idx, dict_off) ou (None, None)."""
        if not self._table:
            return None, None
        deadline = time.time() + 180.0
        for idx in range(0, 60000):
            if time.time() > deadline:
                break
            klass = class_by_index(self._mem, self._table, idx)
            if not klass:
                continue
            inst = singleton_instance(self._mem, klass)
            if not inst:
                continue
            for off in range(0x18, 0x40, 8):
                if self._agg_dict_ok(inst, off):
                    return idx, off
        return None, None

    def _fix_aggregate(self, ver: str | None):
        """Garante que o ouro (AggregateManager) resolva na versão atual.
        Classe ofuscada → localizada por comportamento, com cache por versão."""
        if not self._calib or not self._table:
            return

        def _agg_ok(idx, off):
            if idx is None or off is None:
                return False
            klass = class_by_index(self._mem, self._table, idx)
            inst = singleton_instance(self._mem, klass) if klass else None
            return bool(inst and self._agg_dict_ok(inst, off))

        # 1) cache da versão
        cache = _load_calib_cache(ver) if ver else {}
        a_idx, a_off = cache.get("agg_idx"), cache.get("agg_dict_off")
        if _agg_ok(a_idx, a_off):
            self._calib["idx_ut"] = a_idx
            self._agg_dict_off = a_off
            _log(f"[attach] AggregateManager cache idx={a_idx} dict_off=0x{a_off:x}")
            return

        # 2) varredura comportamental (uma vez por versão)
        _log("[attach] AggregateManager (ouro): varrendo por comportamento...")
        self._report("Mapeando referências do jogo (ouro)… só desta vez nesta versão.")
        a_idx, a_off = self._find_aggregate_idx()
        if _agg_ok(a_idx, a_off):
            self._calib["idx_ut"] = a_idx
            self._agg_dict_off = a_off
            if ver:
                _save_calib_cache(ver, agg_idx=a_idx, agg_dict_off=a_off)
            _log(f"[attach] AggregateManager idx={a_idx} dict_off=0x{a_off:x} (salvo no cache)")
        else:
            _log("[attach] AggregateManager não localizada — ouro indisponível")

    # ── StageClearLog: offsets dos campos (act/stage/clearTime/isBoss) ──────────
    def _scl_entry_ptrs(self, n: int = 10) -> list[int]:
        """Ponteiros das últimas n entradas de StageClearLog."""
        lm = self._singleton("LogManager")
        if not lm:
            return []
        dict_ptr = self._mem.rptr(lm + self._lm_dict_off)
        if not dict_ptr:
            return []
        for k, v in iter_dict8b(self._mem, dict_ptr):
            if k != ELOGTYPE_STAGECLEAR or not v:
                continue
            count = self._mem.ri32(v + LIST_SIZE)
            arr   = self._mem.rptr(v + LIST_ITEMS)
            if not count or count <= 0 or not arr:
                return []
            out = []
            for i in range(max(0, count - n), count):
                ep = self._mem.rptr(arr + ARRAY_DATA + i * 8)
                if ep:
                    out.append(ep)
            return out
        return []

    def _scl_ok(self, base: int, entries: list[int]):
        """Valida a base de offsets do StageClearLog: 4 int consecutivos
        act(1-99), stage(1-99), clearTime(0-100000s), isBoss(0/1).
        Retorna True/False, ou None se não há entradas p/ validar."""
        if not entries:
            return None
        saw_time = False
        for ep in entries:
            a  = self._mem.ri32(ep + base)
            s  = self._mem.ri32(ep + base + 4)
            ct = self._mem.ri32(ep + base + 8)
            b  = self._mem.ri32(ep + base + 12)
            if a is None or s is None or ct is None or b is None:
                return False
            if not (1 <= a <= 99 and 1 <= s <= 99 and 0 <= ct <= 100000 and b in (0, 1)):
                return False
            if ct > 0:
                saw_time = True
        return saw_time

    def _fix_scl_offsets(self, ver: str | None):
        """Valida/auto-detecta os offsets dos campos do StageClearLog (crítico
        pro ranking: act, stage e clearTime). Sem entradas p/ validar, mantém padrão."""
        self._scl_base = SCL_ACT
        if not self._has_log:
            return
        entries = self._scl_entry_ptrs(10)
        cache = _load_calib_cache(ver) if ver else {}

        # 1) offset do cache
        c_off = cache.get("scl_base")
        if c_off is not None and self._scl_ok(c_off, entries):
            self._scl_base = c_off
            return
        # 2) padrão atual valida (ou não há dados → mantém padrão)
        chk = self._scl_ok(SCL_ACT, entries)
        if chk or chk is None:
            self._scl_base = SCL_ACT
            if chk and ver:
                _save_calib_cache(ver, scl_base=SCL_ACT)
            return
        # 3) detecta varrendo offsets consecutivos
        for off in range(0x30, 0x64, 4):
            if self._scl_ok(off, entries):
                self._scl_base = off
                if ver:
                    _save_calib_cache(ver, scl_base=off)
                _log(f"[attach] StageClearLog offsets detectados em 0x{off:x} (padrão 0x{SCL_ACT:x} não batia)")
                return
        _log("[attach] StageClearLog: offsets não validados — usando padrão")

    # ── Self-check: valida offsets de struct restantes e loga se driftarem ──────
    def _selfcheck(self):
        """Valida offsets hardcoded lendo valores conhecidos; loga aviso se algo
        driftou (num update grande), pra diagnosticar rápido em vez de dado errado."""
        msm = self._singleton("MonsterSpawnManager")
        if msm:
            for off, name in ((MSM_MONSTER_LIST, 'alive'), (MSM_DEAD_MONSTER_LIST, 'dead')):
                lp = self._mem.rptr(msm + off)
                sz = self._mem.ri32(lp + LIST_SIZE) if lp else None
                if lp is None or sz is None or not (0 <= sz <= 100000):
                    _log(f"[selfcheck] AVISO: MSM.{name} (0x{off:x}) suspeito (sz={sz}) — pode ter driftado")
        entries = self._scl_entry_ptrs(3)
        if entries and self._scl_ok(self._scl_base, entries) is False:
            _log(f"[selfcheck] AVISO: StageClearLog base 0x{self._scl_base:x} não valida")

    # ── Monster list helpers ──────────────────────────────────────────────────
    def _read_monster_list(self, list_ptr: int) -> list[int]:
        """Retorna lista de ponteiros de monstros de um List<Monster>."""
        if not list_ptr:
            return []
        arr   = self._mem.rptr(list_ptr + LIST_ITEMS)
        count = self._mem.ri32(list_ptr + LIST_SIZE)
        if not arr or not count or count <= 0 or count > 5000:
            return []
        units = []
        for i in range(min(count, 2000)):
            u = self._mem.rptr(arr + ARRAY_DATA + i * 8)
            if u:
                units.append(u)
        return units

    def _detect_monster_offsets(self, arr: int, count: int) -> bool:
        """Detecta offsets de stageKey, act, stageNo e difficulty no struct Monster."""
        SCAN_START = 0x200
        SCAN_END   = 0x500
        monsters_raw = []
        for i in range(min(6, count)):
            u = self._mem.rptr(arr + ARRAY_DATA + i * 8)
            if not u:
                continue
            blk = self._mem.read(u + SCAN_START, SCAN_END - SCAN_START)
            if blk:
                monsters_raw.append(blk)
        if len(monsters_raw) < 2:
            return False

        # Mapa de offset → valor consistente entre todos os monstros
        consistent: dict[int, int] = {}
        for off in range(0, len(monsters_raw[0]) - 3, 4):
            vals = [struct.unpack_from('<i', m, off)[0]
                    for m in monsters_raw if len(m) > off + 3]
            if len(vals) >= 2 and len(set(vals)) == 1:
                consistent[SCAN_START + off] = vals[0]

        # stageKey: valor plausível (>1000) mais próximo do hint histórico
        sk_candidates = {o: v for o, v in consistent.items() if 100 < v < 9_999_999}
        if not sk_candidates:
            _log("[attach] detect: sem candidatos para stageKey")
            return False
        hint = MONSTER_STAGE_KEY
        self._stagekey_off = min(sk_candidates, key=lambda o: abs(o - hint))
        stage_key_val = sk_candidates[self._stagekey_off]
        _log(f"[attach] stagekey_off=0x{self._stagekey_off:x} val={stage_key_val}")

        # act: valor consistente em 1-3
        act_candidates = {o: v for o, v in consistent.items()
                          if 1 <= v <= 3 and o != self._stagekey_off}
        # stageNo: valor consistente em 1-50
        sno_candidates = {o: v for o, v in consistent.items()
                          if 1 <= v <= 50 and o not in act_candidates and o != self._stagekey_off}
        # difficulty: valor consistente em 0-3
        diff_candidates = {o: v for o, v in consistent.items()
                           if 0 <= v <= 3 and o not in act_candidates
                           and o not in sno_candidates and o != self._stagekey_off}

        # Escolhe o candidato mais plausível para cada campo (menor offset após stageKey)
        def _pick(candidates: dict) -> tuple[int, int] | tuple[None, None]:
            if not candidates:
                return None, None
            o = min(candidates, key=lambda x: abs(x - self._stagekey_off))
            return o, candidates[o]

        self._act_off,   act_val  = _pick(act_candidates)
        self._stageno_off, sno_val = _pick(sno_candidates)
        self._diff_off,  diff_val = _pick(diff_candidates)
        def _fmt(o, v): return f"0x{o:x}={v}" if o is not None else "n/a"
        _log(f"[attach] act={_fmt(self._act_off, act_val)} sno={_fmt(self._stageno_off, sno_val)} diff={_fmt(self._diff_off, diff_val)}")

        # Dump diagnóstico: int32s consistentes próximos de act_off
        if self._act_off is not None:
            near = {o: v for o, v in consistent.items()
                    if abs(o - self._act_off) <= 0x40}
            _log(f"[diag] campos próximos de act(0x{self._act_off:x}): {dict(sorted(near.items()))}")

        return True

    # ── Stage key ─────────────────────────────────────────────────────────────
    def _stage_key(self) -> int | None:
        msm = self._singleton("MonsterSpawnManager")
        if not msm:
            return None
        list_ptr = self._mem.rptr(msm + MSM_MONSTER_LIST)
        if not list_ptr:
            self._singletons.pop("MonsterSpawnManager", None)
            return None
        arr   = self._mem.rptr(list_ptr + LIST_ITEMS)
        count = self._mem.ri32(list_ptr + LIST_SIZE)
        if not arr or not count or count <= 0 or count > 2000:
            return None

        # Auto-detecta offsets na primeira vez que temos monstros
        if not hasattr(self, '_stagekey_off'):
            self._stagekey_off = None
        if self._stagekey_off is None:
            if not self._detect_monster_offsets(arr, count):
                return None

        keys, acts, snos, diffs = [], [], [], []
        for i in range(min(10, count)):
            u = self._mem.rptr(arr + ARRAY_DATA + i * 8)
            if not u:
                continue
            k = self._mem.ri32(u + self._stagekey_off)
            if k and 0 < k < 9_999_999:
                keys.append(k)
            if self._act_off:
                a = self._mem.ri32(u + self._act_off)
                if a and 1 <= a <= 3:
                    acts.append(a)
            if self._stageno_off:
                s = self._mem.ri32(u + self._stageno_off)
                if s and 1 <= s <= 50:
                    snos.append(s)
            if self._diff_off:
                d = self._mem.ri32(u + self._diff_off)
                if d is not None and 0 <= d <= 3:
                    diffs.append(d)

        if not keys:
            return None
        stage_key = max(set(keys), key=keys.count)
        self._last_act    = max(set(acts),  key=acts.count)  if acts  else None
        self._last_stageno = max(set(snos), key=snos.count) if snos  else None
        self._last_diff   = max(set(diffs), key=diffs.count) if diffs else None
        return stage_key

    # ── Mobs count ────────────────────────────────────────────────────────────
    def _list_size(self, msm: int, off: int) -> int:
        lp = self._mem.rptr(msm + off)
        if not lp:
            return 0
        return max(0, self._mem.ri32(lp + LIST_SIZE) or 0)

    def _mobs_counts(self) -> tuple[int, int]:
        """Retorna (alive, killed_neste_stage) do MonsterSpawnManager."""
        msm = self._singleton("MonsterSpawnManager")
        if not msm:
            return 0, 0
        alive    = self._list_size(msm, MSM_MONSTER_LIST)
        dead_raw = self._list_size(msm, MSM_DEAD_MONSTER_LIST)
        return alive, max(0, dead_raw - self._dead_baseline)

    def _raw_dead_count(self) -> int:
        msm = self._singleton("MonsterSpawnManager")
        if not msm:
            return 0
        return self._list_size(msm, MSM_DEAD_MONSTER_LIST)

    def dump_msm_fields(self):
        """Dumpa todos os campos int32/float do MSM para descobrir campos de progresso do HUD."""
        msm = self._singleton("MonsterSpawnManager")
        if not msm:
            print("[msm_dump] MSM não encontrado")
            return
        alive = self._list_size(msm, MSM_MONSTER_LIST)
        dead  = self._list_size(msm, MSM_DEAD_MONSTER_LIST)
        fields = {}
        for off in range(0x10, 0x180, 4):
            v = self._mem.ri32(msm + off)
            if v is not None and v != 0:
                fields[hex(off)] = v
        floats = {}
        for off in range(0x10, 0x180, 4):
            v = self._mem.rf32(msm + off)
            if v is not None and 0.0 < v < 100_000_000 and v != int(v):
                floats[hex(off)] = round(v, 3)
        print(f"[msm_dump] alive={alive} dead={dead}")
        print(f"[msm_dump] int32s: {fields}")
        print(f"[msm_dump] floats: {floats}")

    # ── DPS via HP delta ──────────────────────────────────────────────────────
    def _read_dps(self) -> float:
        msm = self._singleton("MonsterSpawnManager")
        if not msm:
            return 0.0
        list_ptr = self._mem.rptr(msm + MSM_MONSTER_LIST)
        if not list_ptr:
            return 0.0
        units    = self._read_monster_list(list_ptr)
        monsters = []
        for u in units:
            hc = self._mem.rptr(u + UNIT_HEALTH_CONTROLLER)
            if not hc:
                continue
            hp = self._mem.rf32(hc + HC_HP_CURRENT)
            if hp is not None:
                monsters.append((u, hp))
        return self._dps.update(monsters, time.time())

    # ── Gold (AggregateManager) ───────────────────────────────────────────────
    def _read_combat_gold(self) -> int | None:
        if not self._calib or not self._table:
            return None
        idx_ut = self._calib.get("idx_ut")
        if idx_ut is None:
            return None
        klass = class_by_index(self._mem, self._table, idx_ut)
        if not klass:
            return None
        inst = singleton_instance(self._mem, klass)
        if not inst:
            return None
        outer_dict = self._mem.rptr(inst + self._agg_dict_off)
        if not outer_dict:
            return None
        for k, v in iter_dict8b(self._mem, outer_dict):
            if k != EAGGR_GOLDEARN:
                continue
            inner_dict = v
            if not inner_dict:
                break
            for sk, sv in iter_dict8b(self._mem, inner_dict):
                if sk == 1:
                    return sv
            break
        return None

    # ── Log events (StageClearLog / StageFailedLog) ───────────────────────────
    def _log_list_size(self, elog_type: int) -> int:
        """Retorna o tamanho da List<ILog> do tipo dado no LogManager."""
        lm = self._singleton("LogManager")
        if not lm:
            return -1  # -1 = LogManager não disponível
        dict_ptr = self._mem.rptr(lm + self._lm_dict_off)
        if not dict_ptr:
            return -1
        for k, v in iter_dict8b(self._mem, dict_ptr):
            if k == elog_type and v:
                count = self._mem.ri32(v + LIST_SIZE)
                return count if count is not None and count >= 0 else 0
        return 0

    def _log_list_version(self, elog_type: int) -> int | None:
        """Retorna o _version da List<ILog> (offset LIST_SIZE+4). Cresce a cada
        Add/Remove — detecta clears mesmo com a lista no cap (size travado)."""
        lm = self._singleton("LogManager")
        if not lm:
            return None
        dict_ptr = self._mem.rptr(lm + self._lm_dict_off)
        if not dict_ptr:
            return None
        for k, v in iter_dict8b(self._mem, dict_ptr):
            if k == elog_type and v:
                return self._mem.ri32(v + LIST_SIZE + 4)
        return None

    def _log_tail_ptr(self, elog_type: int) -> int | None:
        """Ponteiro da ÚLTIMA entrada da List<ILog> do tipo dado. Muda só quando um
        item NOVO é adicionado (clear/fail real) — imune a mexidas de version que
        abrir baú provoca. None se o LogManager não resolve; 0 se lista vazia."""
        lm = self._singleton("LogManager")
        if not lm:
            return None
        dict_ptr = self._mem.rptr(lm + self._lm_dict_off)
        if not dict_ptr:
            return None
        for k, v in iter_dict8b(self._mem, dict_ptr):
            if k == elog_type and v:
                count = self._mem.ri32(v + LIST_SIZE)
                arr   = self._mem.rptr(v + LIST_ITEMS)
                if not count or count <= 0 or not arr:
                    return 0
                return self._mem.rptr(arr + ARRAY_DATA + (count - 1) * 8) or 0
        return 0

    def dump_logmanager(self):
        """Dumpa todas as entradas do Dict<ELogType, List<ILog>> para diagnóstico."""
        cached = self._singletons.get("LogManager")
        print(f"[lm_dump] cached=0x{cached:x}" if cached else "[lm_dump] cached=None")

        # Tenta lookup fresco ignorando cache
        if self._calib and self._table:
            idx = self._calib["indices"].get("LogManager")
            klass = class_by_index(self._mem, self._table, idx) if idx is not None else None
            fresh = singleton_instance(self._mem, klass) if klass else None
            print(f"[lm_dump] fresh=0x{fresh:x}" if fresh else f"[lm_dump] fresh=None (idx={idx} klass={hex(klass) if klass else None})")

            # Tenta via static_fields do próprio klass (sem ir para parent)
            if klass:
                own_sf  = self._mem.rptr(klass + CLASS_STATIC_FLDS)
                parent  = self._mem.rptr(klass + CLASS_PARENT)
                par_sf  = self._mem.rptr(parent + CLASS_STATIC_FLDS) if parent else None
                print(f"[lm_dump] klass=0x{klass:x} own_sf=0x{own_sf:x}" if own_sf else f"[lm_dump] klass=0x{klass:x} own_sf=None")
                print(f"[lm_dump] parent=0x{parent:x} par_sf=0x{par_sf:x}" if par_sf else f"[lm_dump] parent={hex(parent) if parent else None} par_sf=None")
                # Testa instância nos static_fields do próprio klass
                if own_sf:
                    for off in [0x00, 0x08, 0x10, 0x18]:
                        v = self._mem.rptr(own_sf + off)
                        if v and v > 0x10000:
                            print(f"[lm_dump] own_sf+0x{off:x} → 0x{v:x} (parece ponteiro válido)")
                        elif v:
                            print(f"[lm_dump] own_sf+0x{off:x} → 0x{v:x}")
        else:
            print("[lm_dump] sem calib/table")
            return

        lm = cached or fresh
        if not lm:
            print("[lm_dump] LogManager não encontrado")
            return

        dict_ptr = self._mem.rptr(lm + self._lm_dict_off)
        if not dict_ptr:
            print(f"[lm_dump] dict_ptr=None (lm=0x{lm:x} off=0x{self._lm_dict_off:x})")
            # Tenta outros offsets
            for off in [0x18, 0x20, 0x28, 0x30]:
                p = self._mem.rptr(lm + off)
                print(f"[lm_dump]   off=0x{off:x} → 0x{p:x}" if p else f"[lm_dump]   off=0x{off:x} → None")
            return
        entries = {}
        for k, v in iter_dict8b(self._mem, dict_ptr):
            sz = self._mem.ri32(v + LIST_SIZE) if v else None
            entries[k] = sz
        print(f"[lm_dump] lm=0x{lm:x} dict=0x{dict_ptr:x} entries={entries}")
        print(f"[lm_dump] baseline: clear={self._log_clear_sz} fail={self._log_fail_sz} box={self._log_box_sz}")

    def check_events(self) -> str | None:
        """Detecta StageClearLog/StageFailedLog. Retorna 'success', 'fail' ou None.

        Usa o PONTEIRO da última entrada como sinal: ele só muda quando um item novo
        é adicionado (clear/fail real), funcionando mesmo com a lista no cap (o jogo
        remove o mais antigo + adiciona o novo → o último aponta pra outro objeto).
        Imune a mudanças de _version que abrir baú provoca (que geravam fails falsos)."""
        # ── StageClear ──────────────────────────────────────────────────────────
        clear_tail = self._log_tail_ptr(ELOGTYPE_STAGECLEAR)
        if clear_tail is not None and clear_tail != 0:
            if self._last_clear_tail == 0:
                self._last_clear_tail = clear_tail          # baseline lazy
            elif clear_tail != self._last_clear_tail:
                self._last_clear_tail = clear_tail
                _log("[clear] StageClear detectado (nova entrada)")
                return 'success'

        # ── StageFailed ─────────────────────────────────────────────────────────
        fail_tail = self._log_tail_ptr(ELOGTYPE_STAGEFAILED)
        if fail_tail is not None and fail_tail != 0:
            if self._last_fail_tail == 0:
                self._last_fail_tail = fail_tail
            elif fail_tail != self._last_fail_tail:
                self._last_fail_tail = fail_tail
                _log("[fail] StageFailed detectado (nova entrada)")
                return 'fail'

        return None

    def read_last_clear_info(self) -> dict | None:
        """Lê act+stage do último StageClearLog — fallback quando stage_info não tem a key."""
        lm = self._singleton("LogManager")
        if not lm:
            return None
        dict_ptr = self._mem.rptr(lm + self._lm_dict_off)
        if not dict_ptr:
            return None
        for k, v in iter_dict8b(self._mem, dict_ptr):
            if k != ELOGTYPE_STAGECLEAR or not v:
                continue
            count = self._mem.ri32(v + LIST_SIZE)
            if not count or count <= 0:
                return None
            items_ptr = self._mem.rptr(v + LIST_ITEMS)
            if not items_ptr:
                return None
            entry_ptr = self._mem.rptr(items_ptr + ARRAY_DATA + (count - 1) * 8)
            if not entry_ptr:
                return None
            # Offsets auto-validados/detectados por versão (self._scl_base).
            act      = self._mem.ri32(entry_ptr + self._scl_base)
            stage_no = self._mem.ri32(entry_ptr + self._scl_base + 4)
            # clear_time é o tempo OFICIAL do jogo em segundos (int32, não float)
            # — é exatamente o que aparece no HUD ao limpar o stage.
            clear_time = self._mem.ri32(entry_ptr + self._scl_base + 8)
            is_boss    = self._mem.ri32(entry_ptr + self._scl_base + 12)
            if act is not None and stage_no is not None and 1 <= act <= 99 and 1 <= stage_no <= 99:
                info = {"act": act, "stageNo": stage_no}
                if clear_time is not None and 0 < clear_time < 100000:
                    info["clearTime"] = clear_time   # segundos
                if is_boss is not None:
                    info["isBoss"] = is_boss
                return info
        return None

    def capture_log_baselines(self):
        """Captura os tamanhos atuais dos logs como novo baseline."""
        for elog_type, attr in [
            (ELOGTYPE_STAGECLEAR,  '_log_clear_sz'),
            (ELOGTYPE_STAGEFAILED, '_log_fail_sz'),
            (ELOGTYPE_GETBOX,      '_log_box_sz'),
        ]:
            sz = self._log_list_size(elog_type)
            if sz >= 0:
                setattr(self, attr, sz)
        # Baseline do ponteiro da última entrada (detecção robusta de clear/fail)
        ct = self._log_tail_ptr(ELOGTYPE_STAGECLEAR)
        if ct is not None:
            self._last_clear_tail = ct
        ft = self._log_tail_ptr(ELOGTYPE_STAGEFAILED)
        if ft is not None:
            self._last_fail_tail = ft
        bv = self._log_list_version(ELOGTYPE_GETBOX)
        if bv is not None:
            self._log_box_ver = bv

    def _read_box_entries(self, from_idx: int) -> list[int]:
        """Lê GetBoxLog a partir de from_idx. Não altera estado."""
        lm = self._singleton("LogManager")
        if not lm:
            return []
        dict_ptr = self._mem.rptr(lm + self._lm_dict_off)
        if not dict_ptr:
            return []
        for k, v in iter_dict8b(self._mem, dict_ptr):
            if k != ELOGTYPE_GETBOX or not v:
                continue
            count = self._mem.ri32(v + LIST_SIZE)
            if count is None or count <= from_idx:
                return []
            arr = self._mem.rptr(v + LIST_ITEMS)
            if not arr:
                return []
            result = []
            for i in range(from_idx, min(count, from_idx + 50)):
                ep = self._mem.rptr(arr + ARRAY_DATA + i * 8)
                if ep:
                    mt = self._mem.ri32(ep + GBL_MONSTER_TYPE)
                    if mt is not None and BOX_MOB <= mt <= BOX_ACTBOSS:
                        result.append(mt)
            return result
        return []

    def collect_boxes(self):
        """Coleta novos GetBoxLog para o stage atual (acumula em _stage_boxes).

        Cap-safe: usa o _version da lista como gatilho (igual clear/fail). Quando a
        lista ainda cresce, lê os novos por índice; quando está no cap (size travado,
        o jogo remove o antigo + adiciona o novo), lê a última entrada (tail)."""
        ver = self._log_list_version(ELOGTYPE_GETBOX)
        if ver is None:
            # Fallback (sem version): comportamento antigo por size.
            new = self._read_box_entries(self._log_box_sz)
            if new:
                self._stage_boxes.extend(new)
                sz = self._log_list_size(ELOGTYPE_GETBOX)
                if sz >= 0:
                    self._log_box_sz = sz
            return

        if self._log_box_ver < 0:                      # baseline lazy
            self._log_box_ver = ver
            sz = self._log_list_size(ELOGTYPE_GETBOX)
            if sz >= 0:
                self._log_box_sz = sz
            return
        if ver <= self._log_box_ver:
            return                                     # nada novo

        sz = self._log_list_size(ELOGTYPE_GETBOX)
        if sz > self._log_box_sz:
            new = self._read_box_entries(self._log_box_sz)     # cresceu: novos por índice
        else:
            new = self._read_box_entries(max(0, sz - 1))       # no cap: lê o tail (novo)
        self._log_box_ver = ver
        if sz >= 0:
            self._log_box_sz = sz
        if new:
            self._stage_boxes.extend(new)

    # ── Main tick ─────────────────────────────────────────────────────────────
    def tick(self) -> dict | None:
        if not self._handle:
            if not self._attach():
                return None

        if not find_pid(PROCESS_NAME):
            self._detach()
            return None

        # LogManager pode não ter resolvido no attach (logs ainda vazios no menu).
        # Retenta durante o loop até os primeiros logs aparecerem.
        self._retry_logmanager_if_needed()

        stage_key = self._stage_key()
        if stage_key is None:
            # Entre ondas: lista temporariamente vazia — usa o último key conhecido
            stage_key = self._last_key
            if stage_key is None:
                print("[tick] stage_key=None e _last_key=None — sem monstros vivos detectados")
                return None  # ainda não entrou em combate
            key_from_fallback = True
        else:
            self._last_key = stage_key
            key_from_fallback = False

        now  = int(time.time() * 1000)
        ts   = time.time()

        # Mobs
        alive, dead = self._mobs_counts()

        # Baús: acumula os GetBoxLog do stage a cada tick (cap-safe, via _version)
        self.collect_boxes()

        # DPS
        dps       = self._read_dps()
        total_dmg = self._dps.total_damage

        # Gold
        gold_now   = self._read_combat_gold()
        gold_delta = None
        if gold_now is not None:
            if self._gold_start is None:
                self._gold_start = gold_now
            gold_delta = max(0, gold_now - self._gold_start)

        result = {
            "stageKey":        stage_key,
            "ts":              now,
            "mobsAlive":       alive,
            "mobsKilled":      dead,
            "dps":             round(dps),
            "totalDmg":        round(total_dmg),
            "keyFromFallback": key_from_fallback,
        }

        if gold_delta is not None:
            result["goldTotal"] = gold_delta

        # Enriquece com stage_info do calib
        if self._calib:
            info = self._calib.get("stage_info", {}).get(str(stage_key))
            if info and len(info) >= 4:
                act, stage_no, total_mobs, diff_idx = info
                diff_name = DIFFICULTY.get(diff_idx, str(diff_idx))
                result.update({
                    "act":        act,
                    "stageNo":    stage_no,
                    "difficulty": diff_idx,
                    "diffName":   diff_name,
                    "totalMobs":  total_mobs,
                    "label":      f"{diff_name} {act}-{stage_no}",
                })
            else:
                # Fallback: decodifica o próprio stageKey (cobre Torment e versões futuras)
                act, sno, diff = _decode_stage_key(stage_key)
                if act and sno:
                    diff_name = DIFFICULTY.get(diff, '') if diff is not None else ''
                    result.update({
                        "act":     act,
                        "stageNo": sno,
                        **({"difficulty": diff, "diffName": diff_name} if diff is not None else {}),
                        "label":   f"{diff_name} {act}-{sno}".strip(),
                    })

        return result

    def reset_stage(self, dead_baseline: int = 0):
        self._dps.reset()
        self._gold_start    = None
        self._dead_baseline = dead_baseline
        # _last_key NÃO é zerado aqui — o caller define o valor correto após chamar reset_stage
        # Invalida o cache do MSM para forçar re-descoberta do novo singleton
        # O jogo cria um novo MonsterSpawnManager a cada stage — sem isso
        # lemos a dead list acumulada do stage anterior.
        self._singletons.pop("MonsterSpawnManager", None)
        self.capture_log_baselines()
        self._stage_boxes = []   # baús acumulados são por-stage

    def mark_stage_complete(self):
        """Limpa _last_key após fechar via log event, forçando re-detecção limpa."""
        self._last_key = None

    @property
    def is_attached(self) -> bool:
        return self._handle is not None


# ── Backend do site (pareamento por código + escrita via API) ────────────────
# Substitui o login anônimo direto no Firebase: o companion nunca guarda
# credencial nenhuma do Firebase, só um token opaco emitido pelo backend após
# o usuário confirmar o código de vinculação logado no site (mesma conta da web).
import urllib.request as _urlreq
import webbrowser

BACKEND_BASE  = "https://www.tbhtracker.online"  # produção
# Protection Bypass for Automation — só é preciso se o backend estiver atrás
# de Deployment Protection da Vercel (ex.: testando contra um preview). Vazio
# = nenhum header extra é enviado. Nunca commitar um valor real aqui (esse
# secret dá acesso a qualquer preview protegido do projeto).
VERCEL_BYPASS_SECRET = ""
_SESSION_FILE = os.path.join(_APP_DIR, "companion_session.json")


def _copy_to_clipboard(text: str):
    """Copia texto pra área de transferência via Win32 puro (sem lib nova;
    tkinter exigiria rodar na mesma thread do mainloop, que já está ocupada
    pela splash)."""
    try:
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE  = 0x0002
        data = text.encode("utf-16-le") + b"\x00\x00"

        k32_, u32_ = ctypes.windll.kernel32, ctypes.windll.user32
        h_mem = k32_.GlobalAlloc(GMEM_MOVEABLE, len(data))
        p_mem = k32_.GlobalLock(h_mem)
        ctypes.memmove(p_mem, data, len(data))
        k32_.GlobalUnlock(h_mem)

        u32_.OpenClipboard(0)
        u32_.EmptyClipboard()
        u32_.SetClipboardData(CF_UNICODETEXT, h_mem)  # Windows assume o handle daqui pra frente
        u32_.CloseClipboard()
    except Exception as e:
        _log(f"[pairing] falha ao copiar código: {e}")


def _bypass_headers() -> dict:
    return {"x-vercel-protection-bypass": VERCEL_BYPASS_SECRET} if VERCEL_BYPASS_SECRET else {}


def _http_post_json(url: str, payload: dict, timeout: int = 10) -> dict:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", **_bypass_headers()}
    req  = _urlreq.Request(url, data=body, method="POST", headers=headers)
    with _urlreq.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _http_get_json(url: str, timeout: int = 10) -> dict:
    req = _urlreq.Request(url, headers=_bypass_headers())
    with _urlreq.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _load_session() -> dict | None:
    try:
        with open(_SESSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("token"):
            return data
    except Exception:
        pass
    return None


def _save_session(token: str, email: str | None = None):
    try:
        os.makedirs(_APP_DIR, exist_ok=True)
        with open(_SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump({"token": token, "email": email}, f)
    except Exception as e:
        _log(f"[pairing] erro ao salvar sessão: {e}")


def _clear_session():
    try:
        os.remove(_SESSION_FILE)
    except Exception:
        pass


def _pair(status_cb) -> dict | None:
    """Mostra um código de 6 chars (via status_cb) e espera o usuário confirmar
    em {BACKEND_BASE}/companion-link. Devolve {'token': ...} ou None se expirar."""
    try:
        info = _http_post_json(f"{BACKEND_BASE}/api/companion/pair/start", {})
    except Exception as e:
        _log(f"[pairing] falha ao iniciar pareamento: {e}")
        status_cb("Erro ao contatar o site. Verifique sua conexão.")
        return None

    code       = info["code"]
    expires_at = time.time() + info.get("expiresIn", 600)
    _log(f"[pairing] código gerado: {code}")

    _copy_to_clipboard(code)
    link_url = f"{BACKEND_BASE}/companion-link?code={code}"
    try:
        webbrowser.open(link_url)
        status_cb(f"Abrindo o navegador...\ncódigo copiado: {code}")
    except Exception as e:
        _log(f"[pairing] falha ao abrir navegador: {e}")
        status_cb(f"Acesse {BACKEND_BASE}/companion-link\ne cole o código: {code}")

    while time.time() < expires_at:
        time.sleep(3)
        try:
            res = _http_get_json(f"{BACKEND_BASE}/api/companion/pair/status?code={code}")
        except Exception as e:
            _log(f"[pairing] erro no polling: {e}")
            continue
        if res.get("status") == "confirmed":
            _log("[pairing] vinculado!")
            status_cb("Vinculado! Conectando ao jogo...")
            return {"token": res["token"], "email": res.get("email")}
        if res.get("status") == "expired":
            break

    _log("[pairing] código expirou sem confirmação")
    status_cb("Código expirado — reinicie o companion pra gerar outro.")
    return None


class BackendClient:
    """Escreve no Firebase indiretamente via API do site. O token opaco já
    resolve pro uid certo do lado do servidor — o companion não guarda (nem
    nunca viu) nenhuma credencial de auth do Firebase."""

    def __init__(self, token: str):
        self._token = token

    def _post(self, kind: str, data: dict):
        _http_post_json(f"{BACKEND_BASE}/api/companion/write",
                         {"token": self._token, "kind": kind, "data": data})

    def set_current(self, data: dict):
        try:
            self._post("current", data)
            if not getattr(self, '_wrote_once', False):
                self._wrote_once = True
                _log(f"[backend] escrevendo — stageKey={data.get('stageKey')}")
        except Exception as e:
            _log(f"[backend] set_current error: {e}")

    def push_history(self, record: dict):
        try:
            self._post("history", record)
            _log(f"[backend] push_history OK — key={record['startTs']} outcome={record.get('outcome')}")
        except Exception as e:
            _log(f"[backend] push_history error: {e}")

    def set_box_drop(self, stage_label: str, dropped_at: int):
        try:
            self._post("box_drop", {"stageLabel": stage_label, "droppedAt": dropped_at})
        except Exception as e:
            _log(f"[backend] set_box_drop error: {e}")


class ConsoleClient:
    def set_current(self, data: dict):
        print("[stage]", json.dumps(data, ensure_ascii=False))

    def push_history(self, record: dict):
        print("[history]", json.dumps(record, ensure_ascii=False))

    def set_box_drop(self, stage_label: str, dropped_at: int):
        print(f"[box_drop] {stage_label} @ {dropped_at}")


# ── Main loop ─────────────────────────────────────────────────────────────────
def run_loop(client, hz: float = 0.5, stop_event: threading.Event | None = None,
             status_q: "_queue.Queue | None" = None):
    interval = 1.0 / hz
    reader      = StageReader()
    last_key    = None
    start_ts    = None
    prev_data      = None   # data completo do tick anterior (mesmo stageKey)
    kills_tracked  = 0      # kills acumulados por queda no alive (funciona com spawn em ondas)
    prev_alive_kt  = None   # alive do tick anterior para rastrear kills
    fallback_ticks = 0      # ticks consecutivos com stageKey vindo do fallback (sem mobs vivos)
    gap_start_ts   = None   # ts do primeiro tick de fallback (= momento real em que último mob morreu)
    # Pending close: após detectar outcome, aguardamos 1 tick extra para capturar
    # o GetBoxLog do boss (aparece ~0.6s depois do StageClearLog)
    pending: dict | None = None   # {'key', 'start_ts', 'end_ts', 'outcome', 'box_baseline'}
    _first_stage_done = False      # primeira run após iniciar sempre é inválida (companion abriu mid-stage)
    _msm_dump_ticks   = 0          # quantas vezes já dumpamos o MSM (para diagnóstico)
    lap_totals: dict[int, int] = {}  # stageKey -> kills no último clear (total real p/ a barra)
    lap_peak_kills = 0             # pico de mobsKilled no lap atual (base do total adaptativo)

    def _status(msg: str):
        if status_q:
            try: status_q.put_nowait(msg)
            except Exception: pass
        print(msg)

    def _do_close(key, s_ts, e_ts, outcome, **kwargs):
        nonlocal _first_stage_done
        was_first = not _first_stage_done
        if not _first_stage_done:
            # Companion abriu com o game já rodando — dados parciais, sempre inválido
            outcome = 'invalid'
            print(f"[companion] primeira run → forçando invalid (dados incompletos)")
            _first_stage_done = True
        # Total real do stage = kills no clear (auto-calibra o calib errado). NÃO grava
        # na 1ª run (baseline da dead-list ainda não setado → contagem inflada) e
        # aplica teto de sanidade contra valores absurdos. Usa MÁXIMO (não sobrescreve)
        # para convergir pro total real e não encolher a barra numa lap contada parcial.
        if not was_first and key and outcome == 'success' and 0 < lap_peak_kills < 2000:
            lap_totals[key] = max(lap_totals.get(key, 0), lap_peak_kills)
        _close_stage(client, reader, key, s_ts, e_ts, outcome=outcome, **kwargs)

    def _calc_outcome(key, kills, alive, duration_ms) -> str:
        calib_info = (reader._calib or {}).get("stage_info", {}).get(str(key))
        total_mobs = calib_info[2] if calib_info and len(calib_info) >= 3 and calib_info[2] > 0 else 0
        if total_mobs > 0:
            clear = kills >= total_mobs * 0.80 and alive < 10 and duration_ms >= 15_000
        else:
            clear = kills > 50 and alive < 10 and duration_ms >= 15_000
        return 'success' if clear else 'abandoned'

    reader.set_status_cb(_status)   # progresso do mapeamento aparece na splash

    _status(f"Aguardando {PROCESS_NAME}...")
    _attached     = False
    _splash_open  = True   # sinalizado False pela splash ao fechar
    _splash_start = time.time()
    SPLASH_MIN_S  = 5.0    # tempo mínimo de exibição da splash

    while not (stop_event and stop_event.is_set()):
        try:
            data = reader.tick()

            if data is None:
                if reader.is_attached:
                    # Jogo encontrado mas sem stage ativo (hub/menu/loading)
                    if not _attached:
                        _attached = True
                        elapsed = time.time() - _splash_start
                        remaining = max(0.0, SPLASH_MIN_S - elapsed)
                        if remaining > 0:
                            _status(f"Conectado! Fechando em {int(remaining)+1}s...")
                            time.sleep(remaining)
                        _status("__CLOSE__")
                    else:
                        _status("Aguardando stage...")
                else:
                    if _attached:
                        _status("Aguardando jogo...")
                        _attached = False
                if pending is not None:
                    boxes  = (list(reader._stage_boxes) or reader._read_box_entries(pending['box_baseline']))
                    heroes = _read_save_heroes()
                    _do_close(pending['key'], pending['start_ts'],
                              pending['end_ts'], outcome=pending['outcome'],
                              boxes=boxes, heroes=heroes, log_info=pending.get('log_info'))
                    pending = None
                elif last_key is not None:
                    heroes = _read_save_heroes()
                    now_ms = int(time.time() * 1000)
                    pd_alive = prev_data.get('mobsAlive', 999) if prev_data else 999
                    oc = _calc_outcome(last_key, kills_tracked, pd_alive, now_ms - (start_ts or now_ms))
                    _do_close(last_key, start_ts, now_ms, outcome=oc, heroes=heroes)
                    print("[companion] jogo encerrado")
                    last_key  = None
                    start_ts  = None
            else:
                if not _attached:
                    _attached = True
                    elapsed = time.time() - _splash_start
                    remaining = max(0.0, SPLASH_MIN_S - elapsed)
                    if remaining > 0:
                        _status(f"Conectado! Fechando em {int(remaining)+1}s...")
                        time.sleep(remaining)
                    _status("__CLOSE__")
                key = data["stageKey"]
                now = data["ts"]

                # Rastreia ticks de fallback para detectar retry do mesmo stage.
                # Wave gaps (~0–1s) têm 1–2 ticks de fallback; loading screens têm ≥3.
                force_stage_reset = False
                if data.get("keyFromFallback"):
                    if fallback_ticks == 0:
                        gap_start_ts = now  # primeiro tick sem mobs = momento real do fim do stage
                        print(f"[gap] início do gap — kills={kills_tracked} key={key}")
                    fallback_ticks += 1
                    print(f"[gap] tick={fallback_ticks} mobsAlive={data.get('mobsAlive',0)}")
                else:
                    # Key veio de monsters vivos.
                    # Com o log oficial disponível, o encerramento de lap/retry vem SEMPRE
                    # do StageClearLog (check_events) — a heurística de gap fica desligada
                    # para não fechar o stage cedo com tempo de relógio de parede.
                    if not reader._has_log:
                        if fallback_ticks >= 5 and key == last_key:
                            # Gap longo = loading screen / retry manual
                            print(f"[companion] retry detectado (fallback={fallback_ticks}): {key}")
                            force_stage_reset = True
                        elif fallback_ticks >= 1 and key == last_key and last_key is not None and start_ts is not None:
                            # Gap curto (wave/lap): só detecta lap end em stages com poucos mobs (loop/infinito)
                            # Stages one-shot (Torment, Hell, etc.) têm totalMobs alto e encerram via key change
                            calib_info = (reader._calib or {}).get("stage_info", {}).get(str(last_key))
                            _total = calib_info[2] if calib_info and len(calib_info) >= 3 and calib_info[2] > 0 else 0
                            _dur   = now - start_ts
                            _is_loop = 0 < _total <= 200  # stages loop têm mob count pequeno por lap
                            if _is_loop and kills_tracked >= _total * 0.80 and _dur >= 15_000:
                                print(f"[companion] lap end detectado (kills={kills_tracked}/{_total} gap={fallback_ticks}): {key}")
                                force_stage_reset = True
                            else:
                                print(f"[gap] encerrado com {fallback_ticks} tick(s) — wave ({kills_tracked}/{_total or '?'} kills {_dur//1000}s)")
                    fallback_ticks = 0
                    if not force_stage_reset:
                        gap_start_ts = None

                # Resolve pending close antes de processar o tick actual
                if pending is not None:
                    boxes  = (list(reader._stage_boxes) or reader._read_box_entries(pending['box_baseline']))
                    heroes = _read_save_heroes()
                    _do_close(pending['key'], pending['start_ts'],
                              pending['end_ts'], outcome=pending['outcome'],
                              boxes=boxes, heroes=heroes, log_info=pending.get('log_info'))
                    reader.mark_stage_complete()
                    dead_now = reader._raw_dead_count()
                    reader.reset_stage(dead_baseline=dead_now)
                    last_key      = None
                    start_ts      = None
                    pending       = None
                    kills_tracked = 0
                    prev_alive_kt = None
                    fallback_ticks = 0

                outcome = reader.check_events()  # 'success'/'fail'/None

                # ── Detecção de outcome via log (quando funcionar) ────────────
                if outcome and last_key is not None:
                    box_baseline = reader._log_box_sz
                    log_info = reader.read_last_clear_info() if outcome == 'success' else None
                    pending = {
                        'key':          last_key,
                        'start_ts':     start_ts,
                        'end_ts':       now,
                        'outcome':      outcome,
                        'box_baseline': box_baseline,
                        'log_info':     log_info,
                    }
                    print(f"[companion] stage {outcome} (log): {data.get('label', key)}")

                elif pending is None:
                    stage_reset = (key != last_key) or force_stage_reset
                    if stage_reset:
                        if last_key is not None:
                            # Para retry (force_stage_reset), o fim real do stage é o primeiro
                            # tick sem mobs (gap_start_ts), não o tick atual de detecção do retry.
                            real_end_ts = (gap_start_ts if (gap_start_ts and force_stage_reset) else now)
                            duration_ms = real_end_ts - (start_ts or real_end_ts)
                            # Heurística de conclusão: usa o último estado conhecido do
                            # stage anterior. O stageKey muda no mesmo tick que o último
                            # mob morre — prev_data captura o estado pré-transição.
                            pd_alive = prev_data.get('mobsAlive', 999) if prev_data else 999
                            effective_outcome = _calc_outcome(last_key, kills_tracked, pd_alive, duration_ms)
                            heroes = _read_save_heroes()
                            _do_close(last_key, start_ts, real_end_ts,
                                      outcome=effective_outcome, heroes=heroes)
                            print(
                                f"[companion] stage {effective_outcome} "
                                f"(kills={kills_tracked} alive={pd_alive} dur={duration_ms//1000}s): "
                                f"{data.get('label', last_key)}"
                            )
                        dead_now = reader._raw_dead_count()
                        reader.reset_stage(dead_baseline=dead_now)
                        reader._last_key = key  # garante fallback correto em wave gaps do novo stage
                        last_key      = key
                        start_ts      = now
                        prev_data     = None
                        kills_tracked = 0
                        prev_alive_kt = None
                        fallback_ticks = 0
                        gap_start_ts  = None
                        lap_peak_kills = 0   # novo lap: zera o pico p/ o total adaptativo

                # Acumula kills pela queda no alive (fallback) E rastreia o pico de
                # mobsKilled (dead-list = contagem real, imune à taxa de polling).
                if key == last_key:
                    cur_alive = data.get('mobsAlive', 0)
                    if prev_alive_kt is not None and cur_alive < prev_alive_kt:
                        kills_tracked += prev_alive_kt - cur_alive
                    prev_alive_kt = cur_alive
                    prev_data = data
                    lap_peak_kills = max(lap_peak_kills, data.get('mobsKilled', 0) or 0)

                if start_ts is not None and pending is None:
                    elapsed_s = max(1, (now - start_ts) / 1000)
                    payload   = {**data, "startTs": start_ts, "companionVersion": COMPANION_VERSION,
                                 "mobsKilledTracked": kills_tracked}
                    # Total adaptativo: kills no último clear deste stage (auto-calibra
                    # o totalMobs do calib, que estava alto → barra parava na metade).
                    _lt = lap_totals.get(key)
                    if _lt and _lt > 0:
                        payload["totalMobs"] = _lt
                    # Timer congelado no gap final: durante os ticks sem mobs (fallback)
                    # não deixa o relógio passar do tempo real — congela no início do gap.
                    if data.get("keyFromFallback") and fallback_ticks >= 2 and gap_start_ts:
                        payload["stageElapsedMs"] = max(0, gap_start_ts - start_ts)
                    else:
                        payload["stageElapsedMs"] = max(0, now - start_ts)
                    if "goldTotal" in data:
                        payload["goldPerSec"] = round(data["goldTotal"] / elapsed_s)
                    client.set_current(payload)

            time.sleep(interval)

        except KeyboardInterrupt:
            print("\n[companion] encerrado.")
            heroes = _read_save_heroes()
            if pending is not None:
                boxes = (list(reader._stage_boxes) or reader._read_box_entries(pending['box_baseline']))
                _do_close(pending['key'], pending['start_ts'],
                          pending['end_ts'], outcome=pending['outcome'],
                          boxes=boxes, heroes=heroes)
            elif last_key is not None:
                now_ms = int(time.time() * 1000)
                pd_alive = prev_data.get('mobsAlive', 999) if prev_data else 999
                oc = _calc_outcome(last_key, kills_tracked, pd_alive, now_ms - (start_ts or now_ms))
                _do_close(last_key, start_ts, now_ms, outcome=oc, heroes=heroes)
            break
        except Exception as e:
            import traceback
            _log(f"[companion] EXCEPTION: {e}\n{traceback.format_exc()}")
            time.sleep(interval)


_BOX_LABEL = {BOX_MOB: 'mob', BOX_BOSS: 'boss', BOX_ACTBOSS: 'actboss'}


def _close_stage(client, reader: "StageReader", stage_key, start_ts, end_ts,
                 outcome: str = 'unknown', boxes: list[int] | None = None,
                 heroes: list[int] | None = None, log_info: dict | None = None):
    if not start_ts:
        print(f"[close] ignorado — sem start_ts")
        return

    # Tempo OFICIAL do jogo (StageClearLog.clearTime, em segundos) tem prioridade
    # sobre o relógio de parede — é exatamente o que o HUD mostra ao limpar o stage.
    game_secs = (log_info or {}).get("clearTime")
    wall_ms   = max(0, end_ts - start_ts)
    if game_secs and game_secs > 0:
        duration    = game_secs * 1000
        time_source = "game"
    else:
        duration    = wall_ms
        time_source = "wall"

    # Filtro de ruído: NUNCA descarta um evento confirmado pelo jogo — clears
    # oficiais (fonte=game, qualquer duração: bosses de 2s, stages iniciais) e
    # mortes (fail via StageFailedLog) são sempre gravados. O piso de 15s vale
    # SÓ para fechamentos incertos por relógio de parede (heurística: abandono/
    # abertura no meio), que podem ser falsos-positivos de gaps entre ondas.
    confirmed = (time_source == "game") or (outcome in ("success", "fail", "invalid"))
    if not confirmed and duration < 15_000:
        print(f"[close] ignorado — duração {duration//1000}s < 15s incerto "
              f"(fonte={time_source} outcome={outcome} stage_key={stage_key})")
        return

    total_dmg = round(reader._dps.total_damage)
    dur_secs  = max(1, duration / 1000)
    avg_dps   = round(total_dmg / dur_secs)
    max_dps   = round(reader._dps.peak_dps)
    calib     = reader._calib or {}
    info      = calib.get("stage_info", {}).get(str(stage_key))

    boxes = boxes or []
    box_types = [_BOX_LABEL.get(b, str(b)) for b in boxes]

    record = {
        "stageKey":   stage_key,
        "startTs":    start_ts,
        "endTs":      end_ts,
        "duration":   duration,
        "timeSource": time_source,
        "totalDmg":   total_dmg,
        "avgDps":     avg_dps,
        "maxDps":     max_dps,
        "outcome":    outcome,
        "boxes":      box_types,
        "heroes":     heroes or [],
    }
    if game_secs:
        record["clearTimeSec"] = game_secs
    if info and len(info) >= 4:
        act, stage_no, total_mobs, diff_idx = info
        record.update({
            "act":        act,
            "stageNo":    stage_no,
            "difficulty": diff_idx,
            "diffName":   DIFFICULTY.get(diff_idx, str(diff_idx)),
            "totalMobs":  total_mobs,
            "label":      f"{DIFFICULTY.get(diff_idx, '?')} {act}-{stage_no}",
        })
    else:
        # Fallback 1: decodifica stageKey diretamente
        act, sno, diff = _decode_stage_key(stage_key)
        # Fallback 2: StageClearLog (só em success, pode ter act/sno mais preciso)
        if log_info:
            act = log_info["act"]
            sno = log_info["stageNo"]
        if act and sno:
            diff_name = DIFFICULTY.get(diff, '') if diff is not None else ''
            record.update({
                "act":     act,
                "stageNo": sno,
                **({"difficulty": diff, "diffName": diff_name} if diff is not None else {}),
                "label":   f"{diff_name} {act}-{sno}".strip(),
            })
    label = record.get('label')
    print(f"[close] {label or stage_key} outcome={outcome} dur={duration//1000}s "
          f"(fonte={time_source}) dmg={total_dmg}")
    client.push_history(record)

    if label and any(b in ('boss', 'actboss') for b in box_types):
        client.set_box_drop(label, end_ts)


def _make_app_icon(size: int = 256) -> "Image.Image":
    """Gera ícone TBH TRCK com Pillow — fundo escuro, TBH cinza, TRCK verde."""
    from PIL import Image, ImageDraw, ImageFont
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    r    = size // 6
    bg   = (26, 31, 46, 255)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=bg)

    # tenta carregar fonte bold, cai para padrão se não tiver
    def _font(sz):
        for name in ["arialbd.ttf", "calibrib.ttf", "verdanab.ttf", "DejaVuSans-Bold.ttf"]:
            try:
                return ImageFont.truetype(name, sz)
            except Exception:
                pass
        return ImageFont.load_default()

    fsz   = size // 3
    fnt   = _font(fsz)
    pad   = size // 16
    # "TBH" — cinza claro, acima
    draw.text((pad, pad), "TBH",  font=fnt, fill=(226, 232, 240, 255))
    # "TRCK" — verde primário, abaixo
    draw.text((pad, pad + fsz + size // 20), "TRCK", font=fnt, fill=(79, 229, 181, 255))
    return img


def _save_ico(path: str):
    """Gera e salva icon.ico em múltiplos tamanhos."""
    img = _make_app_icon(256)
    img.save(path, format="ICO", sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])


# ── Iniciar com o Windows (registro, sem instalador) ──────────────────────────
_RUN_KEY_PATH   = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE_NAME = "TBHTracker"


def _startup_command() -> str:
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def _is_run_on_startup() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, _RUN_VALUE_NAME)
            return value == _startup_command()
    except (FileNotFoundError, OSError):
        return False


def _set_run_on_startup(enabled: bool):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _RUN_VALUE_NAME, 0, winreg.REG_SZ, _startup_command())
            else:
                try:
                    winreg.DeleteValue(key, _RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
    except Exception as e:
        _log(f"[settings] erro ao configurar início automático: {e}")


# ── Janela de Configurações (tkinter) ─────────────────────────────────────────
class SettingsWindow:
    """Janela acessível pelo ícone da bandeja: conta vinculada e iniciar com
    o Windows."""

    BG      = "#1a1f2e"
    SURFACE = "#141926"
    FG      = "#e2e8f0"
    FG_DIM  = "#64748b"
    GREEN   = "#4fe5b5"
    RED     = "#f87171"
    WIDTH   = 480
    HEIGHT  = 260

    def __init__(self, email: str | None, on_unlink):
        import tkinter as tk
        self._tk       = tk
        self._on_unlink = on_unlink

        root = tk.Tk()
        self._root = root
        root.title("TBH Tracker — Configurações")
        root.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        root.configure(bg=self.BG)
        root.resizable(False, False)
        _set_window_icon(root)

        pad = {"padx": 20}

        tk.Label(root, text=f"TBH Tracker  v{COMPANION_VERSION}",
                 font=("Arial", 13, "bold"), bg=self.BG, fg=self.FG).pack(anchor="w", pady=(16, 12), **pad)

        # ── Conta ──
        acc = tk.Frame(root, bg=self.SURFACE)
        acc.pack(fill="x", **pad)
        tk.Label(acc, text="CONTA VINCULADA", font=("Consolas", 8, "bold"),
                 bg=self.SURFACE, fg=self.FG_DIM).pack(anchor="w", padx=12, pady=(10, 2))
        tk.Label(acc, text=email or "(e-mail não disponível)", font=("Consolas", 10),
                 bg=self.SURFACE, fg=self.GREEN).pack(anchor="w", padx=12, pady=(0, 10))
        tk.Button(acc, text="Desvincular conta", command=self._unlink,
                  bg=self.SURFACE, fg=self.RED, activebackground=self.SURFACE,
                  activeforeground=self.RED, relief="flat", bd=0,
                  font=("Consolas", 9), cursor="hand2").pack(anchor="w", padx=12, pady=(0, 10))

        # ── Inicialização ──
        startup = tk.Frame(root, bg=self.SURFACE)
        startup.pack(fill="x", pady=(12, 0), **pad)
        self._startup_var = tk.BooleanVar(value=_is_run_on_startup())
        tk.Checkbutton(
            startup, text="Iniciar automaticamente com o Windows",
            variable=self._startup_var, command=self._toggle_startup,
            bg=self.SURFACE, fg=self.FG, selectcolor=self.SURFACE,
            activebackground=self.SURFACE, activeforeground=self.FG,
            font=("Consolas", 9), cursor="hand2",
        ).pack(anchor="w", padx=12, pady=10)

    def _toggle_startup(self):
        _set_run_on_startup(self._startup_var.get())

    def _unlink(self):
        _clear_session()
        self._on_unlink()
        self._root.destroy()

    def run(self):
        self._root.mainloop()


_settings_open = threading.Lock()


def _open_settings_window(email: str | None):
    """Chamado pelo menu da bandeja. Sem lock: cliques repetidos no menu
    simplesmente não abrem uma segunda janela enquanto a primeira existir."""
    if not _settings_open.acquire(blocking=False):
        return
    try:
        def _on_unlink():
            _log("[settings] conta desvinculada — reinicie o TBH Tracker pra vincular outra.")
        win = SettingsWindow(email, _on_unlink)
        win.run()
    except Exception as e:
        _log(f"[settings] erro ao abrir janela: {e}")
    finally:
        _settings_open.release()


def _set_window_icon(root):
    """Aplica o ícone TBH TRCK na janela (título + barra de tarefas). Gera o
    .ico na hora se não existir (ex.: rodando o .py direto sem ter buildado
    ainda), pra nunca cair no ícone genérico do tkinter."""
    try:
        ico_path = _resource_path("icon.ico")
        if not os.path.exists(ico_path):
            ico_path = os.path.join(_APP_DIR, "icon.ico")
            if not os.path.exists(ico_path):
                os.makedirs(_APP_DIR, exist_ok=True)
                _save_ico(ico_path)
        root.iconbitmap(ico_path)
    except Exception as e:
        _log(f"[ui] falha ao aplicar ícone: {e}")


# ── Splash screen (tkinter) ────────────────────────────────────────────────────
class SplashScreen:
    """Janela de loading que roda na thread principal enquanto o loop inicia."""

    BG      = "#1a1f2e"
    FG      = "#e2e8f0"
    FG_DIM  = "#64748b"
    GREEN   = "#4fe5b5"
    WIDTH   = 360
    HEIGHT  = 220

    def __init__(self, status_q: "_queue.Queue"):
        import tkinter as tk
        self._q  = status_q
        self._tk = tk
        root = tk.Tk()
        self._root = root
        root.title("TBH Tracker")
        root.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        root.resizable(False, False)
        root.configure(bg=self.BG)
        _set_window_icon(root)
        root.overrideredirect(True)          # sem barra de título
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.97)

        # centraliza
        root.update_idletasks()
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x  = (sw - self.WIDTH)  // 2
        y  = (sh - self.HEIGHT) // 2
        root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

        # ── conteúdo ──
        frame = tk.Frame(root, bg=self.BG, padx=28, pady=20)
        frame.pack(fill="both", expand=True)

        tk.Label(frame, text="TBH",  font=("Arial", 28, "bold"), bg=self.BG, fg=self.FG).pack(anchor="w")
        tk.Label(frame, text="TRCK", font=("Arial", 28, "bold"), bg=self.BG, fg=self.GREEN).pack(anchor="w")

        tk.Label(frame, text="", bg=self.BG).pack()   # espaço

        self._status_var = tk.StringVar(value="Iniciando...")
        tk.Label(frame, textvariable=self._status_var,
                 font=("Consolas", 9), bg=self.BG, fg=self.FG_DIM,
                 wraplength=self.WIDTH - 56, justify="left").pack(anchor="w")

        self._ver_var = tk.StringVar(value=f"v{COMPANION_VERSION}")
        tk.Label(frame, textvariable=self._ver_var,
                 font=("Consolas", 8), bg=self.BG, fg="#334155").pack(anchor="w", pady=(6, 0))

        self._closed = False

    def _poll(self):
        try:
            while True:
                msg = self._q.get_nowait()
                if msg == "__CLOSE__":
                    self._root.destroy()
                    self._closed = True
                    return
                self._status_var.set(msg)
        except _queue.Empty:
            pass
        if not self._closed:
            self._root.after(200, self._poll)

    def run(self):
        self._root.after(200, self._poll)
        self._root.mainloop()


def main():
    _kill_previous_instance()

    ap = argparse.ArgumentParser(description="TBH Tracker")
    ap.add_argument("--hz",      type=float, default=2.0)
    ap.add_argument("--console", action="store_true")
    args = ap.parse_args()

    session = _load_session()

    # Startup log — útil para diagnosticar o exe bundled
    log_path = os.path.join(os.path.expanduser("~"), "TBHTracker_startup.log")
    with open(log_path, "w") as f:
        f.write(f"exe: {sys.executable}\n")
        f.write(f"frozen: {getattr(sys, 'frozen', False)}\n")
        f.write(f"backend: {BACKEND_BASE}\n")
        f.write(f"sessao salva: {'sim' if session else 'nao'}\n")

    if args.console:
        client = ConsoleClient()
        run_loop(client, args.hz)
        return

    stop_evt  = threading.Event()
    status_q  = _queue.Queue()

    def _status_cb(msg: str):
        try:
            status_q.put_nowait(msg)
        except Exception:
            pass

    _shared = {"email": session.get('email') if session else None}

    def _loop():
        client_session = session
        if not client_session:
            paired = _pair(_status_cb)
            if not paired:
                time.sleep(4)
                _status_cb("__CLOSE__")
                return
            _save_session(paired['token'], paired.get('email'))
            client_session = paired
        _shared["email"] = client_session.get('email')
        client = BackendClient(client_session['token'])
        run_loop(client, args.hz, stop_event=stop_evt, status_q=status_q)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()

    # ── Splash: roda na thread principal, fecha quando jogo é encontrado ──
    try:
        splash = SplashScreen(status_q)
        splash.run()   # bloqueia até receber __CLOSE__ ou janela ser fechada
    except Exception:
        pass   # se tkinter não disponível, ignora

    # ── Tray icon: toma conta da thread principal após splash ─────────────
    try:
        import pystray
        icon_img = _make_app_icon(64)

        def on_quit(icon, item):
            stop_evt.set()
            icon.stop()

        def on_settings(icon, item):
            threading.Thread(target=_open_settings_window, args=(_shared["email"],), daemon=True).start()

        menu = pystray.Menu(
            pystray.MenuItem(f"TBH Tracker  v{COMPANION_VERSION}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Configurações", on_settings),
            pystray.MenuItem("Encerrar", on_quit),
        )
        tray = pystray.Icon("TBHTracker", icon_img, f"TBH Tracker v{COMPANION_VERSION}", menu)
        tray.run()
    except ImportError:
        thread.join()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log_path = os.path.join(os.path.expanduser("~"), "TBHTracker_error.log")
        with open(log_path, "w") as f:
            f.write(traceback.format_exc())
        raise
