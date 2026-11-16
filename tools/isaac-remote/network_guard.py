"""Inherited kernel network restriction for a trusted numerical replay worker.

This is not an arbitrary-code sandbox. Solver code stays in Actions Docker;
only registered simulator/verifier code executes here. No network sockets are
inherited. AF_UNIX IPC remains available for local graphics/driver processes.
"""
import ctypes
import errno
import os
import socket
import sys


class Comparison(ctypes.Structure):
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int),
                ('datum_a', ctypes.c_uint64), ('datum_b', ctypes.c_uint64)]


def restrict_network():
    lib = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                         ctypes.c_uint, ctypes.POINTER(Comparison)]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise RuntimeError('seccomp_init failed')
    deny = 0x00050000 | errno.EPERM
    try:
        for name in ('socket', 'socketpair', 'io_uring_setup', 'socketcall'):
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number < 0:
                if name in ('socket', 'socketpair'):
                    raise RuntimeError('Required network syscall unavailable')
                continue
            condition = Comparison(0, 1, socket.AF_UNIX, 0)  # SCMP_CMP_NE
            count = 1 if name in ('socket', 'socketpair') else 0
            rc = lib.seccomp_rule_add_array(ctx, deny, number, count,
                                            ctypes.byref(condition) if count else None)
            if rc:
                raise RuntimeError(f'Cannot restrict {name}: {rc}')
        if lib.seccomp_load(ctx):  # libseccomp also sets no_new_privs.
            raise RuntimeError('Cannot install replay network filter')
    finally:
        lib.seccomp_release(ctx)
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except PermissionError:
            continue
        else:
            sock.close()
            raise RuntimeError('Replay network filter did not deny a network socket')


if __name__ == '__main__':
    restrict_network()
    os.execvp(sys.argv[1], sys.argv[1:])
