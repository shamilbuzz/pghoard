"""
pghoard - content encryption

Copyright (c) 2015 Ohmu Ltd
See LICENSE for details
"""

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.hashes import SHA1, SHA256
from cryptography.hazmat.primitives.hmac import HMAC
from cryptography.hazmat.primitives import serialization
import io
import os
import struct

FILEMAGIC = b"pghoa1"
IO_BLOCK_SIZE = 2 ** 20


class EncryptorError(Exception):
    """ EncryptorError """


class Encryptor(object):
    def __init__(self, rsa_public_key_pem):
        self.rsa_public_key = serialization.load_pem_public_key(rsa_public_key_pem, backend=default_backend())
        self.cipher = None
        self.authenticator = None

    def update(self, data):
        ret = b""
        if self.cipher is None:
            key = os.urandom(16)
            nonce = os.urandom(16)
            auth_key = os.urandom(32)
            self.cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), backend=default_backend()).encryptor()
            self.authenticator = HMAC(auth_key, SHA256(), backend=default_backend())
            pad = padding.OAEP(mgf=padding.MGF1(algorithm=SHA1()),
                               algorithm=SHA1(),
                               label=None)
            cipherkey = self.rsa_public_key.encrypt(key + nonce + auth_key, pad)
            ret = FILEMAGIC + struct.pack(">H", len(cipherkey)) + cipherkey
        cur = self.cipher.update(data)
        self.authenticator.update(cur)
        ret += cur
        return ret

    def finalize(self):
        if self.cipher is None:
            raise EncryptorError("Invalid state")
        ret = self.cipher.finalize()
        self.authenticator.update(ret)
        ret += self.authenticator.finalize()
        self.cipher = None
        self.authenticator = None
        return ret


class Decryptor(object):
    def __init__(self, rsa_private_key_pem):
        self.rsa_private_key = serialization.load_pem_private_key(rsa_private_key_pem, password=None, backend=default_backend())
        self.cipher = None
        self.authenticator = None
        self.buf = b""

    def update(self, data):
        self.buf += data
        if self.cipher is None:
            if len(self.buf) < 8:
                return b""
            if self.buf[0:6] != FILEMAGIC:
                raise EncryptorError("Invalid magic bytes")
            cipherkeylen = struct.unpack(">H", self.buf[6:8])[0]
            if len(self.buf) < 8 + cipherkeylen:
                return b""
            pad = padding.OAEP(mgf=padding.MGF1(algorithm=SHA1()),
                               algorithm=SHA1(),
                               label=None)
            try:
                plainkey = self.rsa_private_key.decrypt(self.buf[8:8 + cipherkeylen], pad)
            except AssertionError:
                raise EncryptorError("Decrypting key data failed")
            if len(plainkey) != 64:
                raise EncryptorError("Integrity check failed")
            key = plainkey[0:16]
            nonce = plainkey[16:32]
            auth_key = plainkey[32:64]

            self.cipher = Cipher(algorithms.AES(key), modes.CTR(nonce), backend=default_backend()).decryptor()
            self.authenticator = HMAC(auth_key, SHA256(), backend=default_backend())
            self.buf = self.buf[8 + cipherkeylen:]

        if len(self.buf) < 32:
            return b""

        self.authenticator.update(self.buf[:-32])
        result = self.cipher.update(self.buf[:-32])
        self.buf = self.buf[-32:]

        return result

    def finalize(self):
        if self.cipher is None:
            raise EncryptorError("Invalid state")
        if self.buf != self.authenticator.finalize():
            raise EncryptorError("Integrity check failed")
        result = self.cipher.finalize()
        self.buf = b""
        self.cipher = None
        self.authenticator = None
        return result


class DecryptorFile(io.BufferedIOBase):
    def __init__(self, source_fp, rsa_private_key_pem):
        super(DecryptorFile, self).__init__()
        self.buffer = b""
        self.buffer_offset = 0
        self.decryptor = Decryptor(rsa_private_key_pem)
        self.state = "OPEN"
        self.source_fp = source_fp

    def _check_not_closed(self):
        if self.state == "CLOSED":
            raise ValueError("I/O operation on closed file")

    def _read_all(self):
        blocks = []
        if self.buffer_offset > 0:
            blocks.append(self.buffer)
            self.buffer = b""
            self.buffer_offset = 0
        while True:
            data = self.source_fp.read(IO_BLOCK_SIZE)
            if not data:
                self.state = "EOF"
                break
            data = self.decryptor.update(data)
            if data:
                blocks.append(data)
        data = self.decryptor.finalize()
        if data:
            blocks.append(data)
        return b"".join(blocks)

    def _read_block(self, size):
        readylen = len(self.buffer) - self.buffer_offset
        if size <= readylen:
            retval = self.buffer[self.buffer_offset:self.buffer_offset + size]
            self.buffer_offset += size
            return retval
        blocks = []
        if self.buffer_offset:
            blocks = [self.buffer[self.buffer_offset:]]
        else:
            blocks = [self.buffer]
        while readylen < size:
            data = self.source_fp.read(IO_BLOCK_SIZE)
            if not data:
                self.state = "EOF"
                data = self.decryptor.finalize()
                if data:
                    blocks.append(data)
                    readylen += len(data)
                break
            data = self.decryptor.update(data)
            if data:
                blocks.append(data)
                readylen += len(data)
        self.buffer = b"".join(blocks)
        self.buffer_offset = 0
        if size < readylen:
            retval = self.buffer[:size]
            self.buffer_offset = size
        else:
            retval = self.buffer
            self.buffer = b""
            self.buffer_offset = 0
        return retval

    def close(self):
        """Close stream"""
        if self.state == "CLOSED":
            return
        self.decryptor = None
        self.source_fp = None
        self.state = "CLOSED"

    @property
    def closed(self):
        """True if this stream is closed"""
        return self.state == "CLOSED"

    def fileno(self):
        self._check_not_closed()
        return self.source_fp.fileno()

    def flush(self):
        self._check_not_closed()

    def peek(self, size=-1):  # pylint: disable=unused-argument
        self._check_not_closed()
        # XXX
        return b""

    def read(self, size=-1):
        """Read up to size decrypted bytes"""
        self._check_not_closed()
        if self.state == "EOF" or size == 0:
            return b""
        elif size < 0:
            return self._read_all()
        else:
            return self._read_block(size)

    def read1(self, size=-1):
        return self.read(size)

    def readable(self):
        """True if this stream supports reading"""
        self._check_not_closed()
        return self.state in ["OPEN", "EOF"]

    def seek(self, offset, whence=0):  # pylint: disable=unused-argument
        self._check_not_closed()
        raise OSError("Seek on a stream that is not seekable")

    def seekable(self):
        """True if this stream supports random access"""
        self._check_not_closed()
        return False

    def tell(self):
        self._check_not_closed()
        raise OSError("Tell on a stream that is not seekable")

    def truncate(self):
        self._check_not_closed()
        raise OSError("Truncate on a stream that is not seekable")

    def writable(self):
        """True if this stream supports writing"""
        self._check_not_closed()
        return False