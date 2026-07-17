# This file is part of the pychess-variants fairyfishnet client.
# Copyright (C) 2016-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
# Copyright (C) 2019 Bajusz Tamás <gbtami@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Portable CPU capability detection helpers."""

import contextlib
import ctypes
import os
import platform


@contextlib.contextmanager
def make_cpuid():
    # Loosely based on cpuid.py by Anders Høst, licensed MIT:
    # https://github.com/flababah/cpuid.py

    # Prepare system information
    is_windows = os.name == "nt"
    is_64bit = ctypes.sizeof(ctypes.c_void_p) == 8
    if platform.machine().lower() not in ["amd64", "x86_64", "x86", "i686"]:
        raise OSError("Got no CPUID opcodes for %s" % platform.machine())

    # Struct for return value
    class CPUID_struct(ctypes.Structure):
        _fields_ = [
            ("eax", ctypes.c_uint32),
            ("ebx", ctypes.c_uint32),
            ("ecx", ctypes.c_uint32),
            ("edx", ctypes.c_uint32),
        ]

    # Select kernel32 or libc
    if is_windows:
        libc = getattr(ctypes, "windll").kernel32
    else:
        libc = ctypes.CDLL(None)  # pyright: ignore[reportArgumentType]

    # Select opcodes
    if is_64bit:
        if is_windows:
            # Windows x86_64
            # Three first call registers : RCX, RDX, R8
            # Volatile registers         : RAX, RCX, RDX, R8-11
            opc = [
                0x53,  # push   %rbx
                0x89,
                0xD0,  # mov    %edx,%eax
                0x49,
                0x89,
                0xC9,  # mov    %rcx,%r9
                0x44,
                0x89,
                0xC1,  # mov    %r8d,%ecx
                0x0F,
                0xA2,  # cpuid
                0x41,
                0x89,
                0x01,  # mov    %eax,(%r9)
                0x41,
                0x89,
                0x59,
                0x04,  # mov    %ebx,0x4(%r9)
                0x41,
                0x89,
                0x49,
                0x08,  # mov    %ecx,0x8(%r9)
                0x41,
                0x89,
                0x51,
                0x0C,  # mov    %edx,0xc(%r9)
                0x5B,  # pop    %rbx
                0xC3,  # retq
            ]
        else:
            # Posix x86_64
            # Three first call registers : RDI, RSI, RDX
            # Volatile registers         : RAX, RCX, RDX, RSI, RDI, R8-11
            opc = [
                0x53,  # push   %rbx
                0x89,
                0xF0,  # mov    %esi,%eax
                0x89,
                0xD1,  # mov    %edx,%ecx
                0x0F,
                0xA2,  # cpuid
                0x89,
                0x07,  # mov    %eax,(%rdi)
                0x89,
                0x5F,
                0x04,  # mov    %ebx,0x4(%rdi)
                0x89,
                0x4F,
                0x08,  # mov    %ecx,0x8(%rdi)
                0x89,
                0x57,
                0x0C,  # mov    %edx,0xc(%rdi)
                0x5B,  # pop    %rbx
                0xC3,  # retq
            ]
    else:
        # CDECL 32 bit
        # Three first call registers : Stack (%esp)
        # Volatile registers         : EAX, ECX, EDX
        opc = [
            0x53,  # push   %ebx
            0x57,  # push   %edi
            0x8B,
            0x7C,
            0x24,
            0x0C,  # mov    0xc(%esp),%edi
            0x8B,
            0x44,
            0x24,
            0x10,  # mov    0x10(%esp),%eax
            0x8B,
            0x4C,
            0x24,
            0x14,  # mov    0x14(%esp),%ecx
            0x0F,
            0xA2,  # cpuid
            0x89,
            0x07,  # mov    %eax,(%edi)
            0x89,
            0x5F,
            0x04,  # mov    %ebx,0x4(%edi)
            0x89,
            0x4F,
            0x08,  # mov    %ecx,0x8(%edi)
            0x89,
            0x57,
            0x0C,  # mov    %edx,0xc(%edi)
            0x5F,  # pop    %edi
            0x5B,  # pop    %ebx
            0xC3,  # ret
        ]

    code_size = len(opc)
    code = (ctypes.c_ubyte * code_size)(*opc)

    if is_windows:
        # Allocate executable memory
        libc.VirtualAlloc.restype = ctypes.c_void_p
        libc.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong, ctypes.c_ulong]
        addr = libc.VirtualAlloc(None, code_size, 0x1000, 0x40)
        if not addr:
            raise MemoryError("Could not VirtualAlloc RWX memory")
    else:
        # Allocate memory
        libc.valloc.restype = ctypes.c_void_p
        libc.valloc.argtypes = [ctypes.c_size_t]
        addr = libc.valloc(code_size)
        if not addr:
            raise MemoryError("Could not valloc memory")

        # Make executable
        libc.mprotect.restype = ctypes.c_int
        libc.mprotect.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        if 0 != libc.mprotect(addr, code_size, 1 | 2 | 4):
            raise OSError("Failed to set RWX using mprotect")

    # Copy code to allocated executable memory. No need to flush instruction
    # cache for CPUID.
    ctypes.memmove(addr, code, code_size)

    # Create and yield callable
    result = CPUID_struct()
    func_type = ctypes.CFUNCTYPE(None, ctypes.POINTER(CPUID_struct), ctypes.c_uint32, ctypes.c_uint32)
    func_ptr = func_type(addr)

    def cpuid(eax, ecx=0):
        func_ptr(result, eax, ecx)
        return result.eax, result.ebx, result.ecx, result.edx

    yield cpuid

    # Free
    if is_windows:
        libc.VirtualFree.restype = ctypes.c_long
        libc.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong]
        libc.VirtualFree(addr, 0, 0x8000)
    else:
        libc.free.restype = None
        libc.free.argtypes = [ctypes.c_void_p]
        libc.free(addr)


def cmd_cpuid(argv):
    with make_cpuid() as cpuid:
        headers = ["CPUID", "EAX", "EBX", "ECX", "EDX"]
        print(" ".join(header.ljust(8) for header in headers).rstrip())

        for eax in [0x0, 0x80000000]:
            highest, _, _, _ = cpuid(eax)
            for eax in range(eax, highest + 1):
                a, b, c, d = cpuid(eax)
                print("%08x %08x %08x %08x %08x" % (eax, a, b, c, d))
