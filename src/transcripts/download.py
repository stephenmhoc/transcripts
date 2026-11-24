"""Bounded audio fetches with DNS-pinned public connections, including redirects."""
from pathlib import Path
from urllib.parse import urlsplit, urljoin
import hashlib
import http.client
import ipaddress
import shutil
import socket
import ssl
import time


class DownloadError(ValueError):
    pass


def public_target(url):
    try:
        if any(ord(c)<33 or ord(c)==127 for c in url) or "\\" in url:
            raise DownloadError("unsafe_audio_url")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password:
            raise DownloadError("unsafe_audio_url")
        port = parsed.port or (443 if parsed.scheme=="https" else 80)
        if port != (443 if parsed.scheme=="https" else 80):
            raise DownloadError("unsafe_audio_port")
        hostname = parsed.hostname.encode("idna").decode("ascii")
        addresses = socket.getaddrinfo(hostname,port,type=socket.SOCK_STREAM)
        if not addresses:
            raise DownloadError("audio_dns_failed")
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            # is_global excludes RFC1918, loopback, CGNAT/Tailscale, metadata, etc.
            if not ip.is_global or ip.is_multicast or (getattr(ip,"ipv4_mapped",None) is not None and not ip.ipv4_mapped.is_global):
                raise DownloadError("audio_internal_address_blocked")
        return parsed,hostname,port,addresses[0][4][0]
    except (ValueError, UnicodeError, socket.gaierror) as exc:
        if isinstance(exc,DownloadError):
            raise
        raise DownloadError("unsafe_audio_url") from exc


class PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, hostname, port, ip, tls):
        super().__init__(hostname,port,timeout=30)
        self.pinned_ip = ip
        self.tls = tls

    def connect(self):
        connection = socket.create_connection((self.pinned_ip,self.port),timeout=self.timeout)
        if self.tls:
            connection = ssl.create_default_context().wrap_socket(connection,server_hostname=self.host)
        peer = ipaddress.ip_address(connection.getpeername()[0])
        if peer != ipaddress.ip_address(self.pinned_ip):
            connection.close()
            raise DownloadError("audio_peer_mismatch")
        self.sock = connection


def download_audio(url, destination: Path, max_bytes=536870912, max_seconds=1800, max_redirects=10):
    started = time.monotonic()
    if shutil.disk_usage(destination.parent).free < max_bytes+2_147_483_648:
        raise DownloadError("insufficient_worker_disk")
    try:
        # Podcast enclosures commonly chain several analytics prefixes (Podtrac,
        # Podsights, Podscribe, ...) before the host's own CDN redirect; every hop
        # is revalidated by public_target.
        for _ in range(max_redirects+1):
            parsed,host,port,ip = public_target(url)
            connection = PinnedHTTP(host,port,ip,parsed.scheme=="https")
            try:
                path = parsed.path or "/"
                if parsed.query:
                    path += "?"+parsed.query
                host_header = f"[{host}]" if ":" in host else host
                connection.request("GET",path,headers={"Host":host_header,"User-Agent":"Meritranscripts/0.1","Accept":"audio/*, application/octet-stream","Accept-Encoding":"identity"})
                response = connection.getresponse()
                if response.status in {301,302,303,307,308}:
                    location = response.getheader("Location")
                    if not location:
                        raise DownloadError("audio_redirect_invalid")
                    url = urljoin(url,location)
                    continue
                if response.status != 200:
                    raise DownloadError("audio_http_failed")
                length = response.getheader("Content-Length")
                if length and (not length.isdigit() or int(length)>max_bytes):
                    raise DownloadError("audio_too_large")
                content_type = response.getheader("Content-Type","").lower().split(";")[0]
                if content_type and not (content_type.startswith("audio/") or content_type in {"application/octet-stream","binary/octet-stream","video/mp4","application/ogg"}):
                    raise DownloadError("audio_content_type_invalid")
                total, digest = 0,hashlib.sha256()
                with destination.open("wb") as output:
                    destination.chmod(0o600)
                    while chunk := response.read(65536):
                        total += len(chunk)
                        if total > max_bytes or time.monotonic()-started>max_seconds:
                            raise DownloadError("audio_limit_exceeded")
                        output.write(chunk)
                        digest.update(chunk)
                if not total or (length and total != int(length)):
                    raise DownloadError("audio_incomplete")
                return digest.hexdigest()
            finally:
                connection.close()
        raise DownloadError("audio_redirect_limit")
    except Exception as exc:
        destination.unlink(missing_ok=True)
        if isinstance(exc,DownloadError):
            raise
        # Avoid leaking signed URLs or provider responses into logs.
        raise DownloadError("audio_download_failed") from exc
