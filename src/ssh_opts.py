"""The one SSH host-key policy for every ssh call Aurix makes to its own GPU / helper boxes.

Was StrictHostKeyChecking set to "no" in nine places: ssh then connects to ANY machine answering at that address, even if its
host key changed, so someone on the network path could impersonate a GPU box and receive the commands (and any HF
tokens in them). `accept-new` still records a box's key the first time without a prompt (so adding a server stays
zero-click), but refuses a box whose key CHANGED.

If a box was reinstalled, remove its old key once (`ssh-keygen -R <host>`). The policy can be changed with
AURIX_SSH_HOSTKEY_POLICY=accept-new|yes|no (anything else falls back to accept-new).
"""

import os

_ALLOWED = ("accept-new", "yes", "no")


def hostkey_policy() -> str:
    v = (os.getenv("AURIX_SSH_HOSTKEY_POLICY") or "accept-new").strip().lower()
    return v if v in _ALLOWED else "accept-new"


def hostkey_option() -> str:
    """The value for `ssh -o`, e.g. "StrictHostKeyChecking=accept-new"."""
    return f"StrictHostKeyChecking={hostkey_policy()}"
