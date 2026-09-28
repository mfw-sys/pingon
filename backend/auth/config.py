import os
import secrets
import logging
from pathlib import Path
from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BACKEND_DIR.parent

ENV_PATHS = [
    BACKEND_DIR / "data" / ".env",   # inside Docker volume → persists across rebuilds
    ROOT_DIR / ".env",
    BACKEND_DIR / ".env",
]

def _ensure_jwt_secret() -> str:
    """Ensure a cryptographically secure, unique JWT_SECRET_KEY exists in .env per installation."""
    target_env = None
    for p in ENV_PATHS:
        if p.exists():
            load_dotenv(p, override=True)
            target_env = p
            break

    if not target_env:
        target_env = ENV_PATHS[0] if ENV_PATHS else (ROOT_DIR / ".env")

    secret = os.getenv("JWT_SECRET_KEY", "").strip()

    if secret and secret != "default-insecure-secret-do-not-use" and target_env.exists():
        return secret

    # Generate a cryptographically secure 32-byte secret key (base64url encoded, 256 bits of entropy)
    generated_secret = secrets.token_urlsafe(32)

    existing_content = ""
    if target_env.exists():
        existing_content = target_env.read_text(encoding="utf-8")

    lines = existing_content.splitlines()
    key_found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("JWT_SECRET_KEY="):
            new_lines.append(f"JWT_SECRET_KEY={generated_secret}")
            key_found = True
        else:
            new_lines.append(line)

    if not key_found:
        if new_lines and new_lines[-1].strip() != "":
            new_lines.append("")
        new_lines.append(f"# Automatically generated unique secret for JWT tokens")
        new_lines.append(f"JWT_SECRET_KEY={generated_secret}")

    target_env.parent.mkdir(parents=True, exist_ok=True)
    target_env.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.environ["JWT_SECRET_KEY"] = generated_secret
    logging.info(f"Generated new unique JWT_SECRET_KEY and saved to {target_env}")
    return generated_secret

JWT_SECRET_KEY = _ensure_jwt_secret()
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

