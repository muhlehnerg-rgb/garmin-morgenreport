"""Schluesselverwaltung und hybride Verschluesselung fuer Cockpit-Backups."""

from __future__ import annotations

import argparse
import os
import struct
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


MAGIC = b"COCKPIT-BACKUP-V1\n"


def erzeuge_schluesselpaar(privater_pfad, oeffentlicher_pfad):
    privater_schluessel = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    privater_pfad.parent.mkdir(parents=True, exist_ok=True)
    oeffentlicher_pfad.parent.mkdir(parents=True, exist_ok=True)
    privater_pfad.write_bytes(
        privater_schluessel.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    try:
        os.chmod(privater_pfad, 0o600)
    except OSError:
        pass
    oeffentlicher_pfad.write_bytes(
        privater_schluessel.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def verschluesseln(eingabe, ausgabe, oeffentlicher_schluessel_pfad):
    oeffentlicher_schluessel = serialization.load_pem_public_key(
        oeffentlicher_schluessel_pfad.read_bytes()
    )
    daten_schluessel = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    chiffre = AESGCM(daten_schluessel).encrypt(nonce, eingabe.read_bytes(), MAGIC)
    verschluesselter_schluessel = oeffentlicher_schluessel.encrypt(
        daten_schluessel,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    ausgabe.parent.mkdir(parents=True, exist_ok=True)
    ausgabe.write_bytes(
        MAGIC
        + struct.pack(">I", len(verschluesselter_schluessel))
        + verschluesselter_schluessel
        + nonce
        + chiffre
    )


def entschluesseln(eingabe, ausgabe, privater_schluessel_pfad):
    blob = eingabe.read_bytes()
    if not blob.startswith(MAGIC):
        raise ValueError("Unbekanntes Backup-Format")
    position = len(MAGIC)
    schluessellaenge = struct.unpack(">I", blob[position:position + 4])[0]
    position += 4
    verschluesselter_schluessel = blob[position:position + schluessellaenge]
    position += schluessellaenge
    nonce = blob[position:position + 12]
    chiffre = blob[position + 12:]

    privater_schluessel = serialization.load_pem_private_key(
        privater_schluessel_pfad.read_bytes(), password=None
    )
    daten_schluessel = privater_schluessel.decrypt(
        verschluesselter_schluessel,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    ausgabe.parent.mkdir(parents=True, exist_ok=True)
    ausgabe.write_bytes(AESGCM(daten_schluessel).decrypt(nonce, chiffre, MAGIC))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Cockpit-Backup verschluesseln")
    unterbefehle = parser.add_subparsers(dest="befehl", required=True)

    keygen = unterbefehle.add_parser("keygen")
    keygen.add_argument("--private-key", required=True, type=Path)
    keygen.add_argument("--public-key", required=True, type=Path)

    encrypt = unterbefehle.add_parser("encrypt")
    encrypt.add_argument("--input", required=True, type=Path)
    encrypt.add_argument("--output", required=True, type=Path)
    encrypt.add_argument("--public-key", required=True, type=Path)

    decrypt = unterbefehle.add_parser("decrypt")
    decrypt.add_argument("--input", required=True, type=Path)
    decrypt.add_argument("--output", required=True, type=Path)
    decrypt.add_argument("--private-key", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.befehl == "keygen":
        erzeuge_schluesselpaar(args.private_key, args.public_key)
        print("Backup-Schluesselpaar erstellt.")
    elif args.befehl == "encrypt":
        verschluesseln(args.input, args.output, args.public_key)
        print("Backup verschluesselt.")
    else:
        entschluesseln(args.input, args.output, args.private_key)
        print("Backup entschluesselt.")


if __name__ == "__main__":
    main()
