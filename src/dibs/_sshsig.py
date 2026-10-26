# SPDX-FileCopyrightText: Simon A. F. Lund <os@safl.dk>
#
# SPDX-License-Identifier: BSD-3-Clause

"""SSH public keys and ``ssh-keygen -Y`` signatures.

Signature checking is left to OpenSSH, the reference implementation of
the format, rather than reimplemented here. Signatures are made and
checked in the ``dibs`` namespace, so one made for dibs cannot be
passed off as, say, a git commit signature, or the other way round.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

NAMESPACE = "dibs"

_KEY_TYPES = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
)
_SIGNATURE_MAX = 8192


def parse_pubkey(raw: str) -> tuple[str, str]:
    """``(key, fingerprint)`` for one OpenSSH public key line. ``key`` is
    the ``<type> <base64>`` pair with any comment dropped; raises
    :class:`ValueError` on anything ``ssh-keygen`` does not accept."""
    parts = (raw or "").strip().split()
    if len(parts) < 2 or "\n" in raw.strip() or parts[0] not in _KEY_TYPES:
        raise ValueError(
            "pubkey must be one OpenSSH public key line, e.g. the contents of id_ed25519.pub"
        )
    key = f"{parts[0]} {parts[1]}"
    out = subprocess.run(
        ["ssh-keygen", "-l", "-E", "sha256", "-f", "-"],
        input=key + "\n",
        capture_output=True,
        text=True,
        timeout=10,
    )
    if out.returncode != 0:
        raise ValueError("pubkey is not a valid OpenSSH public key")
    return key, out.stdout.split()[1]


def verify(*, principal: str, key: str, message: str, signature: str) -> bool:
    """True when ``signature`` is a ``dibs``-namespace signature of
    ``message`` by ``key``. ``principal`` must already be validated: it
    is written into the ``allowed_signers`` line."""
    if len(signature) > _SIGNATURE_MAX or "BEGIN SSH SIGNATURE" not in signature:
        return False
    with tempfile.TemporaryDirectory(prefix="dibs-sshsig-") as tmp:
        allowed = Path(tmp) / "allowed_signers"
        allowed.write_text(f'{principal} namespaces="{NAMESPACE}" {key}\n')
        sig = Path(tmp) / "message.sig"
        sig.write_text(signature)
        out = subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "verify",
                "-f",
                str(allowed),
                "-I",
                principal,
                "-n",
                NAMESPACE,
                "-s",
                str(sig),
            ],
            input=message,
            capture_output=True,
            text=True,
            timeout=10,
        )
    return out.returncode == 0
