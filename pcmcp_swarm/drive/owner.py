"""Owner approval by signature. The swarm only runs a plan Angie has SIGNED with her passphrase-protected
owner key (OpenSSH `ssh-keygen -Y sign`, built into macOS). A JSON line in a log file is never trusted.

Why: Instinct (or any process running as Angie's user) can write files, so a plain approval record could be
forged. It cannot produce a signature without the key's passphrase, which only Angie knows.

Limit (stated honestly): an agent with full shell access as the same user could still swap in its own
public key or edit this code. The owner key fingerprint is printed on every approval and run, and recorded
in the run evidence, so a swapped key is visible. Closing that fully needs a separate OS account.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import json
import re

from ..util import canonical_json, now_iso, read_json

NAMESPACE = "pcmcp-drive-approval"
IDENTITY = "pcmcp-owner"
# TRUST ANCHOR: a root-owned file at a FIXED path (not configurable). Angie writes it once with sudo.
# A process running as her user without her Mac password cannot change it, and --config cannot move it.
PIN_PATH = Path("/Library/Application Support/PCMCP/owner_key.pin")


def pin_location(dcfg: dict) -> tuple:
    """(path, must_be_root_owned). Production always uses the fixed root-owned pin; nothing in the
    environment or config can redirect it. (Only `drive/_testentry.py`, which refuses any config but
    the loopback fake Drive, replaces this function for the offline selftest.)"""
    return PIN_PATH, True


def read_pin(dcfg: dict) -> str:
    path, need_root = pin_location(dcfg)
    if not path.exists():
        raise FileNotFoundError(f"owner key pin missing at {path}; Angie runs the sudo command printed by `drive owner-setup`")
    if need_root:
        for target in (path, path.parent):
            st = target.stat()
            if st.st_uid != 0 or st.st_mode & 0o022:
                raise PermissionError(f"{target} must be owned by root and not group/world-writable; refusing to trust the pin")
    pin = path.read_text().strip()
    if not re.fullmatch(r"SHA256:[A-Za-z0-9+/=]+", pin):
        raise ValueError(f"owner key pin at {path} is not a SHA256 key fingerprint")
    return pin


def no_agent_env() -> dict:
    """SAFETY: without SSH_AUTH_SOCK, a key loaded in ssh-agent cannot sign without the passphrase prompt."""
    return {k: v for k, v in os.environ.items() if k not in ("SSH_AUTH_SOCK", "SSH_AGENT_PID")}


def paths(dcfg: dict) -> tuple:
    return Path(dcfg["owner_key"]).expanduser(), Path(dcfg["owner_allowed_signers"]).expanduser()


def _ssh(args, **kw):
    return subprocess.run(["ssh-keygen", *args], capture_output=True, text=True, **kw)


def fingerprint(dcfg: dict) -> str | None:
    key, _ = paths(dcfg)
    pub = Path(str(key) + ".pub")
    if not pub.exists():
        return None
    r = _ssh(["-l", "-f", str(pub)])
    return r.stdout.split()[1] if r.returncode == 0 and len(r.stdout.split()) > 1 else None


def write_allowed_signers(dcfg: dict) -> None:
    key, allowed = paths(dcfg)
    pub = Path(str(key) + ".pub").read_text().split()
    allowed.parent.mkdir(parents=True, exist_ok=True)
    allowed.write_text(f'{IDENTITY} namespaces="{NAMESPACE}" {pub[0]} {pub[1]}\n')


def has_passphrase(dcfg: dict) -> bool:
    key, _ = paths(dcfg)
    return _ssh(["-y", "-P", "", "-f", str(key)]).returncode != 0


def setup(dcfg: dict) -> str:
    """OWNER, once: create the passphrase-protected key (ssh-keygen asks Angie for the passphrase)."""
    key, _ = paths(dcfg)
    if key.exists():
        raise ValueError(f"owner key already exists at {key} (fingerprint {fingerprint(dcfg)})")
    key.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["ssh-keygen", "-t", "ed25519", "-C", IDENTITY, "-f", str(key)])
    if r.returncode != 0:
        raise RuntimeError("ssh-keygen failed; owner key not created")
    if not has_passphrase(dcfg):
        raise RuntimeError(f"the owner key at {key} has NO passphrase, so it protects nothing. Move it aside and run setup again with a passphrase.")
    write_allowed_signers(dcfg)
    fp = fingerprint(dcfg)
    d = PIN_PATH.parent
    print("\nNow pin this key so nothing running as your user can swap it (asks for your Mac password):\n"
          f'  sudo mkdir -p "{d}" && echo "{fp}" | sudo tee "{PIN_PATH}" >/dev/null && sudo chown -R root:wheel "{d}" && sudo chmod 755 "{d}" && sudo chmod 644 "{PIN_PATH}"\n'
          "Do NOT add this key to ssh-agent or the macOS keychain.")
    return fp


def sign_approval(dcfg: dict, run_dir: Path, plan_hash: str, command_id: str) -> dict:
    """Write the approval payload and sign it. ssh-keygen asks for the owner passphrase."""
    key, _ = paths(dcfg)
    if not key.exists():
        raise FileNotFoundError("no owner key yet: Angie runs `python3 -m pcmcp_swarm drive owner-setup` once")
    if not has_passphrase(dcfg):
        raise RuntimeError("owner key has no passphrase; refusing to use it for approvals")
    d = run_dir / "approvals"
    d.mkdir(exist_ok=True)
    payload = {"plan_hash": plan_hash, "run": run_dir.name, "approved_at": now_iso(), "command_id": command_id,
               "key_fingerprint": fingerprint(dcfg), "namespace": NAMESPACE}
    pf = d / f"{plan_hash[:16]}-{command_id}.json"
    pf.write_bytes(canonical_json(payload))
    r = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(pf)], env=no_agent_env())
    sig = Path(str(pf) + ".sig")
    if r.returncode != 0 or not sig.exists():
        os.replace(pf, str(pf) + ".unsigned")
        raise RuntimeError("signing failed (wrong passphrase?); plan NOT approved")
    return dict(payload, payload_file=pf.name, signature_file=sig.name)


def verify_approval(dcfg: dict, run_dir: Path, plan_hash: str) -> dict | None:
    """Return the approval for this exact plan + run, or None. Trusted only if ssh-keygen verifies the
    signature AND the fingerprint ssh-keygen reports equals the root-owned pin. The payload's own
    key_fingerprint field is never trusted."""
    _, allowed = paths(dcfg)
    pin = read_pin(dcfg)
    if not allowed.exists():
        return None
    for pf in sorted((run_dir / "approvals").glob(f"{plan_hash[:16]}-*.json")):
        sig = Path(str(pf) + ".sig")
        if not sig.exists():
            continue
        data = pf.read_bytes()            # read ONCE: the bytes verified are the bytes parsed
        r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", IDENTITY, "-n", NAMESPACE, "-s", str(sig)],
                           input=data, capture_output=True)
        if r.returncode != 0:
            continue
        m = re.search(r"key (SHA256:[A-Za-z0-9+/=]+)", (r.stdout + r.stderr).decode("utf-8", "replace"))
        if not m or m.group(1) != pin:
            continue
        try:
            p = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(p, dict) or canonical_json(p) != data:
            continue
        if p.get("plan_hash") == plan_hash and p.get("run") == run_dir.name and p.get("namespace") == NAMESPACE:
            return {"plan_hash": plan_hash, "run": run_dir.name, "approved_at": p.get("approved_at"),
                    "approve_command_id": p.get("command_id"), "verified_fingerprint": m.group(1),
                    "pinned_fingerprint": pin, "signature_file": sig.name}
    return None
