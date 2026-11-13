from cryptography.fernet import Fernet, InvalidToken
import hmac, hashlib


def _f(key: str) -> Fernet:
    return Fernet(key.encode())


def enc(key: str, value: str) -> str:
    return _f(key).encrypt(value.encode()).decode()


def dec(key: str, token: str) -> str:
    try:
        return _f(key).decrypt(token.encode()).decode()
    except InvalidToken:
        raise RuntimeError("No se pudo descifrar el dato (clave incorrecta)")


def blind_index(key: str, value: str) -> str:
    """Índice ciego para buscar/forzar unicidad sin guardar el dato en claro."""
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()
