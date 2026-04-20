import hashlib, hmac, secrets, base64

PBKDF2_ITERS = 200_000

def pbkdf2_hash(password: str, salt: bytes) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return base64.urlsafe_b64encode(dk).decode("ascii")

def make_password(password: str) -> tuple[str, str]:
    salt = secrets.token_bytes(16)
    return base64.urlsafe_b64encode(salt).decode("ascii"), pbkdf2_hash(password, salt)

def verify_password(password: str, salt_b64: str, hash_b64: str) -> bool:
    salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
    calc = pbkdf2_hash(password, salt)
    return hmac.compare_digest(calc, hash_b64)

def legacy_sha256(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()
