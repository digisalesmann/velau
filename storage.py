"""
storage.py — avatar image uploads to Supabase Storage.

Requires a public storage bucket named "avatars" to exist in the Supabase
project (create it once via the dashboard: Storage → New bucket → name
"avatars" → Public bucket). Uploads use the service_role key so they bypass
bucket RLS policies — never expose that key to the client.

Env vars required on Render:
  SUPABASE_URL              e.g. https://xxxx.supabase.co
  SUPABASE_SERVICE_ROLE_KEY the service_role secret key (Project Settings → API)
"""
import os
import logging
import requests

logger = logging.getLogger("Storage")

_BUCKET = "avatars"


def upload_avatar(username: str, image_bytes: bytes, content_type: str = "image/jpeg") -> str:
    """Uploads (or overwrites) the avatar for a user and returns its public URL."""
    supabase_url = os.getenv("SUPABASE_URL", "")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not service_key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set — avatar uploads disabled"
        )

    ext = "png" if content_type == "image/png" else "jpg"
    path = f"{username}.{ext}"

    resp = requests.post(
        f"{supabase_url}/storage/v1/object/{_BUCKET}/{path}",
        headers={
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
            "Content-Type": content_type,
            "x-upsert": "true",  # overwrite any existing avatar at this path
        },
        data=image_bytes,
        timeout=20,
    )
    if resp.status_code not in (200, 201):
        logger.warning(f"Avatar upload failed for {username}: {resp.status_code} {resp.text}")
        raise RuntimeError(f"Avatar upload failed: {resp.status_code}")

    return f"{supabase_url}/storage/v1/object/public/{_BUCKET}/{path}"


# ── Payment proof uploads ────────────────────────────────────────────────────
# Separate PRIVATE bucket, unlike avatars — proof screenshots can contain bank
# account numbers, so there is no public URL for these. Create the bucket once
# via the Supabase dashboard: Storage → New bucket → name "payment-proofs" →
# Public bucket UNCHECKED. Bytes are only ever served back through the
# authenticated /subscription/proof_image/{payment_id} proxy route in main.py.

_PROOF_BUCKET = "payment-proofs"


def upload_payment_proof(payment_id: str, image_bytes: bytes, content_type: str = "image/jpeg") -> str:
    """Uploads a proof-of-payment screenshot and returns its storage PATH
    (not a public URL, since the bucket is private)."""
    supabase_url = os.getenv("SUPABASE_URL", "")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not service_key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set — proof uploads disabled"
        )

    ext = "png" if content_type == "image/png" else "jpg"
    path = f"{payment_id}.{ext}"

    resp = requests.post(
        f"{supabase_url}/storage/v1/object/{_PROOF_BUCKET}/{path}",
        headers={
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
            "Content-Type": content_type,
            "x-upsert": "true",
        },
        data=image_bytes,
        timeout=20,
    )
    if resp.status_code not in (200, 201):
        logger.warning(f"Payment proof upload failed for {payment_id}: {resp.status_code} {resp.text}")
        raise RuntimeError(f"Proof upload failed: {resp.status_code}")

    return path


def get_payment_proof_bytes(path: str) -> tuple[bytes, str]:
    """Fetches proof bytes via the service key, for the authorized proxy route."""
    supabase_url = os.getenv("SUPABASE_URL", "")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not service_key:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set — proof retrieval disabled"
        )

    resp = requests.get(
        f"{supabase_url}/storage/v1/object/{_PROOF_BUCKET}/{path}",
        headers={"Authorization": f"Bearer {service_key}", "apikey": service_key},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "image/jpeg")
