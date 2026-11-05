"""Generate, check, and store the Temporal TLS certificates.

`generate` issues a self-signed CA with a frontend and an internode certificate into a
directory outside the repository. `put` checks a directory, generated or supplied by the
organization's PKI under the same file names, and writes it to the Secrets Manager secrets the
`temporal-control-plane` module creates. With `--import-to-acm` it also imports the internode
certificate into ACM, re-importing into the tracked ARN on rotation, so ACM reports its expiry.
ACM never returns the key of an imported certificate, so the server always reads the
certificates from Secrets Manager. Certificate material never passes through Terraform.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services import process

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSITION_ROOT = REPO_ROOT / "infra" / "terraform" / "examples" / "private-bedrock"
OPENSSL_TIMEOUT_SECONDS = process.SHORT_COMMAND_TIMEOUT_SECONDS

CA = "ca.pem"
CA_KEY = "ca-key.pem"
FRONTEND = "frontend.pem"
FRONTEND_KEY = "frontend-key.pem"
INTERNODE = "internode.pem"
INTERNODE_KEY = "internode-key.pem"
UI = "ui.pem"
UI_KEY = "ui-key.pem"

SERVER_AUTH = "TLS Web Server Authentication"
CLIENT_AUTH = "TLS Web Client Authentication"

_EXTENSIONS = """\
[req]
distinguished_name = dn
prompt = no

[dn]
CN = Temporal

[ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash

[frontend]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = {frontend_sans}
authorityKeyIdentifier = keyid
subjectKeyIdentifier = hash

[ui]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = {ui_sans}
authorityKeyIdentifier = keyid
subjectKeyIdentifier = hash

[internode]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = serverAuth,clientAuth
subjectAltName = {internode_sans}
authorityKeyIdentifier = keyid
subjectKeyIdentifier = hash
"""


class CertificateError(Exception):
    """A certificate directory or an operation on it cannot be used."""


def _openssl(*args: str) -> str:
    result = process.run(
        ["openssl", *args], timeout=OPENSSL_TIMEOUT_SECONDS, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise CertificateError(f"openssl {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def _sans(names: list[str]) -> str:
    return ",".join(f"DNS:{name}" for name in names)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def generate(
    out: Path,
    *,
    frontend_names: list[str],
    internode_names: list[str],
    days: int = 397,
    ca_days: int = 3650,
    frontend: bool = True,
    ui_names: list[str] | None = None,
    force: bool = False,
    repo_root: Path = REPO_ROOT,
) -> list[Path]:
    """Issue a CA and the leaf certificates into `out`, refusing to overwrite without `force`."""
    out = out.expanduser().resolve()
    if _inside(out, repo_root.resolve()):
        raise CertificateError(
            f"{out} is inside the repository. Write the certificates to a directory outside it, "
            "such as ~/.agentic-platform/temporal-tls, so no key can be committed."
        )
    leaves = [(INTERNODE, INTERNODE_KEY, "internode", internode_names)]
    if frontend:
        leaves.append((FRONTEND, FRONTEND_KEY, "frontend", frontend_names))
    if ui_names:
        leaves.append((UI, UI_KEY, "ui", ui_names))
    targets = [out / CA, out / CA_KEY] + [out / name for leaf in leaves for name in leaf[:2]]
    existing = [path.name for path in targets if path.exists()]
    if existing and not force:
        raise CertificateError(
            f"{', '.join(existing)} already exist in {out}. Pass --force to replace them."
        )

    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    previous_umask = os.umask(0o077)
    try:
        _issue(
            out,
            leaves,
            frontend_names,
            internode_names,
            ui_names or ["ui.temporal.internal"],
            days,
            ca_days,
        )
    finally:
        os.umask(previous_umask)
    _openssl("verify", "-CAfile", str(out / CA), *(str(out / leaf[0]) for leaf in leaves))
    return targets


def _issue(
    out: Path,
    leaves: list[tuple[str, str, str, list[str]]],
    frontend_names: list[str],
    internode_names: list[str],
    ui_names: list[str],
    days: int,
    ca_days: int,
) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        config = Path(scratch) / "extensions.cnf"
        config.write_text(
            _EXTENSIONS.format(
                frontend_sans=_sans(frontend_names),
                internode_sans=_sans(internode_names),
                ui_sans=_sans(ui_names),
            ),
            encoding="utf-8",
        )
        _new_key(out / CA_KEY)
        _openssl(
            "req", "-x509", "-new", "-config", str(config), "-extensions", "ca",
            "-key", str(out / CA_KEY), "-sha256", "-days", str(ca_days),
            "-subj", "/CN=Temporal CA", "-out", str(out / CA),
        )  # fmt: skip
        for cert, key, section, names in leaves:
            _new_key(out / key)
            request = Path(scratch) / f"{section}.csr"
            _openssl(
                "req", "-new", "-config", str(config), "-key", str(out / key),
                "-subj", f"/CN={names[0]}", "-out", str(request),
            )  # fmt: skip
            _openssl(
                "x509", "-req", "-in", str(request), "-CA", str(out / CA),
                "-CAkey", str(out / CA_KEY), "-set_serial", f"0x{secrets.token_hex(16)}",
                "-days", str(days), "-sha256", "-extfile", str(config),
                "-extensions", section, "-out", str(out / cert),
            )  # fmt: skip


def _new_key(path: Path) -> None:
    _openssl(
        "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(path)
    )


@dataclass(frozen=True)
class Material:
    ca: str
    internode_certificate: str
    internode_key: str
    frontend_certificate: str = ""
    frontend_key: str = ""


def _certificate_text(path: Path) -> str:
    return _openssl("x509", "-in", str(path), "-noout", "-text")


def _check_leaf(
    directory: Path, cert: str, key: str, usages: tuple[str, ...], name: str = ""
) -> None:
    cert_path, key_path = directory / cert, directory / key
    _openssl("verify", "-CAfile", str(directory / CA), "-untrusted", str(cert_path), str(cert_path))
    certificate_key = _openssl("x509", "-in", str(cert_path), "-noout", "-pubkey")
    private_key = _openssl("pkey", "-in", str(key_path), "-pubout")
    if certificate_key.strip() != private_key.strip():
        raise CertificateError(f"{key} is not the private key of {cert}.")
    text = _certificate_text(cert_path)
    missing = [usage for usage in usages if usage not in text]
    if missing:
        raise CertificateError(f"{cert} lacks the extended key usage {', '.join(missing)}.")
    if name:
        named = process.run(
            ["openssl", "x509", "-in", str(cert_path), "-noout", "-checkhost", name],
            timeout=OPENSSL_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
        )
        # OpenSSL 3.0 exits 0 from -checkhost on a mismatch, so the verdict is read from stdout.
        if named.returncode != 0 or "does match certificate" not in named.stdout:
            raise CertificateError(
                f"{cert} does not name {name}, the server name private-bedrock configures. Issue "
                "it for that name, or set the matching temporal_*_server_name and apply first."
            )
    result = process.run(
        ["openssl", "x509", "-in", str(cert_path), "-noout", "-checkend", "0"],
        timeout=OPENSSL_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CertificateError(f"{cert} has expired.")


def load(
    directory: Path, *, frontend: bool, frontend_name: str = "", internode_name: str = ""
) -> Material:
    """Read and check a certificate directory, refusing a file the frontend source excludes.

    A non-empty name must be on the matching certificate.
    """
    directory = directory.expanduser().resolve()
    present = {name for name in (FRONTEND, FRONTEND_KEY) if (directory / name).exists()}
    if not frontend and present:
        raise CertificateError(
            f"{', '.join(sorted(present))} found in {directory}, but the NLB presents the ACM "
            "certificate in temporal_frontend_acm_certificate_arn. Remove the frontend files, or "
            "clear that variable to serve them from Secrets Manager."
        )
    required = [CA, INTERNODE, INTERNODE_KEY] + ([FRONTEND, FRONTEND_KEY] if frontend else [])
    missing = [name for name in required if not (directory / name).exists()]
    if missing:
        raise CertificateError(f"{directory} is missing {', '.join(missing)}.")

    _check_leaf(directory, INTERNODE, INTERNODE_KEY, (SERVER_AUTH, CLIENT_AUTH), internode_name)
    if frontend:
        _check_leaf(directory, FRONTEND, FRONTEND_KEY, (SERVER_AUTH,), frontend_name)

    def read(name: str) -> str:
        return (directory / name).read_text(encoding="utf-8")

    return Material(
        ca=read(CA),
        internode_certificate=read(INTERNODE),
        internode_key=read(INTERNODE_KEY),
        frontend_certificate=read(FRONTEND) if frontend else "",
        frontend_key=read(FRONTEND_KEY) if frontend else "",
    )


def fingerprint(pem: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as handle:
        handle.write(pem)
    try:
        output = _openssl("x509", "-in", handle.name, "-noout", "-fingerprint", "-sha256")
    finally:
        os.unlink(handle.name)
    return output.strip().partition("=")[2].replace(":", "").lower()


def put(
    directory: Path,
    tls: dict[str, Any],
    secretsmanager: Any,
    acm: Any,
    *,
    import_to_acm: bool = False,
) -> list[str]:
    """Check the directory, sync ACM, then write the secrets with the CA bundle last.

    A failed check writes nothing. A failed write leaves the CA bundle as it was, and the error
    names the secrets already written.
    """
    frontend = tls.get("frontend_certificate_source") == "secrets"
    required = ["ca_secret_arn", "internode_secret_arn"] + (
        ["frontend_secret_arn"] if frontend else []
    )
    missing = [name for name in required if not tls.get(name)]
    if missing:
        raise CertificateError(
            f"The temporal_tls output has no {', '.join(missing)}. Set "
            "create_temporal_control_plane = true and apply private-bedrock first."
        )
    material = load(
        directory,
        frontend=frontend,
        frontend_name=tls.get("frontend_server_name") or "",
        internode_name=tls.get("internode_server_name") or "",
    )
    actions: list[str] = []

    tracked = tls.get("internode_acm_certificate_arn") or ""
    if import_to_acm:
        request: dict[str, Any] = {
            "Certificate": material.internode_certificate.encode(),
            "PrivateKey": material.internode_key.encode(),
            "CertificateChain": material.ca.encode(),
        }
        if tracked:
            request["CertificateArn"] = tracked
        imported = acm.import_certificate(**request)["CertificateArn"]
        if tracked:
            actions.append(f"re-imported the internode certificate into {imported}")
        else:
            actions.append(
                f"imported the internode certificate as {imported}. Set "
                f'temporal_internode_acm_certificate_arn = "{imported}" and apply to track it'
            )
    elif tracked:
        in_acm = acm.get_certificate(CertificateArn=tracked)["Certificate"]
        if fingerprint(in_acm) != fingerprint(material.internode_certificate):
            raise CertificateError(
                f"{INTERNODE} is not the certificate ACM tracks in {tracked}. Pass "
                "--import-to-acm to re-import it there, or put the certificate ACM holds."
            )

    writes = [
        (
            "the internode certificate and key",
            tls["internode_secret_arn"],
            {"certificate": material.internode_certificate, "private_key": material.internode_key},
        ),
    ]
    if frontend:
        writes.append(
            (
                "the frontend certificate and key",
                tls["frontend_secret_arn"],
                {
                    "certificate": material.frontend_certificate,
                    "private_key": material.frontend_key,
                },
            )
        )
    written: list[str] = []
    for label, secret_arn, value in [*writes, ("the CA bundle", tls["ca_secret_arn"], material.ca)]:
        try:
            secretsmanager.put_secret_value(
                SecretId=secret_arn,
                SecretString=value if isinstance(value, str) else json.dumps(value),
            )
        except Exception as exc:
            exc.add_note(
                f"temporal-certs put wrote {', '.join(written) or 'nothing'} before the failure, "
                "and the CA bundle is unchanged. Run put again before inv temporal-deploy."
            )
            raise
        written.append(label)
        actions.append(f"wrote {label}")
    return actions


def _terraform_tls(env: dict[str, str]) -> dict[str, Any]:
    result = process.run(
        ["terraform", f"-chdir={COMPOSITION_ROOT}", "output", "-json", "temporal_tls"],
        timeout=process.MEDIUM_COMMAND_TIMEOUT_SECONDS,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise CertificateError(f"terraform output failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _names(values: list[str] | None, default: str) -> list[str]:
    return values or [default]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    generate_parser = commands.add_parser(
        "generate", help="Issue a self-signed CA and certificates."
    )
    generate_parser.add_argument("--out", required=True, type=Path)
    generate_parser.add_argument(
        "--frontend-name", action="append", help="Frontend SAN, repeatable. The first is the CN."
    )
    generate_parser.add_argument(
        "--internode-name", action="append", help="Internode SAN, repeatable. The first is the CN."
    )
    generate_parser.add_argument(
        "--ui-name",
        action="append",
        help="Also issue a UI load balancer certificate with this SAN, repeatable.",
    )
    generate_parser.add_argument("--days", type=int, default=397)
    generate_parser.add_argument("--no-frontend", action="store_true")
    generate_parser.add_argument("--force", action="store_true")

    put_parser = commands.add_parser("put", help="Check a directory and write it to the secrets.")
    put_parser.add_argument("--dir", required=True, type=Path)
    put_parser.add_argument("--import-to-acm", action="store_true")
    put_parser.add_argument("--region", default="")

    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            written = generate(
                args.out,
                frontend_names=_names(args.frontend_name, "frontend.temporal.internal"),
                internode_names=_names(args.internode_name, "internode.temporal.internal"),
                days=args.days,
                frontend=not args.no_frontend,
                ui_names=args.ui_name,
                force=args.force,
            )
            names = ", ".join(path.name for path in written)
            print(f"[temporal-certs] wrote {names} to {written[0].parent}")
            return 0

        from scripts.aws_clients import configure_aws_fips
        from scripts.config import get_settings

        settings = get_settings()
        configure_aws_fips(settings)
        import boto3

        session = boto3.Session(region_name=args.region or settings.aws_region)
        tls = _terraform_tls(dict(os.environ))
        for action in put(
            args.dir,
            tls,
            session.client("secretsmanager"),
            session.client("acm"),
            import_to_acm=args.import_to_acm,
        ):
            print(f"[temporal-certs] {action}")
        print("[temporal-certs] run inv temporal-deploy to load the new certificates")
        return 0
    except CertificateError as exc:
        print(f"[temporal-certs] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
