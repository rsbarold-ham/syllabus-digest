import os

from cryptography.fernet import Fernet, InvalidToken

_key = os.environ.get("APP_ENCRYPTION_KEY")
if not _key:
    raise RuntimeError(
        "APP_ENCRYPTION_KEY is not set. Generate one with "
        "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
        "and put it in .env."
    )
_fernet = Fernet(_key.encode())


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise ValueError("Could not decrypt stored value -- APP_ENCRYPTION_KEY may have changed.")
