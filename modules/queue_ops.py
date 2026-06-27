"""Queue recovery — restart container, clear queue.

These are heavy operations. They run only when get_queue_info().all_error is True,
indicating the queue has been deadlocked (cookies expired, source blocked, etc).
"""

import ssl
import urllib.request

from .config import ctx as _ssl_ctx
from .suwayomi import graphql_query


def restart_suwayomi_container():
    """Restart Suwayomi container via Portainer API to clear cookies/state.
    
    This clears downloader cookies so MangaFire connections are fresh,
    allowing downloads to resume. Returns True on success.
    """
    try:
        with open('/config/.hermes_2/secrets/portainer_entdocker.txt') as f:
            token = f.read().strip()
        container_id = "4b541cb8c91a"  # suwayomi-server-preview-suwayomi-1
        url = f"https://entdocker.lan:9443/api/endpoints/3/docker/containers/{container_id}/restart?t=30"
        req = urllib.request.Request(
            url,
            headers={"X-API-Key": token},
            method="POST"
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        resp = urllib.request.urlopen(req, timeout=15, context=ctx)
        return resp.status == 204
    except Exception:
        return False


def clear_download_queue():
    """Clear Suwayomi download queue via GraphQL mutation. Returns True on success."""
    q = '''mutation {
        clearDownloader(input: {clientMutationId: "auto-search"}) {
            clientMutationId
        }
    }'''
    result = graphql_query(q, timeout=10)
    if not result or "errors" in result:
        return False
    return True