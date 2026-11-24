from collections import namedtuple
import socket
import pytest
import transcripts.download as module
from transcripts.download import public_target,DownloadError,download_audio


@pytest.mark.parametrize('address',['127.0.0.1','10.1.2.3','169.254.169.254','100.100.100.100','100.113.106.26','::1','fd7a:115c:a1e0::1','224.0.0.1','::ffff:127.0.0.1'])
def test_internal_addresses_blocked(monkeypatch,address):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(address,443))])
    with pytest.raises(DownloadError):public_target('https://untrusted.example/audio.mp3')


@pytest.mark.parametrize('url',['file:///etc/passwd','https://user:secret@example.org/a','http://example.org:22/a','https://example.org/a\r\nInjected:yes','https://example.org\\@127.0.0.1/a'])
def test_untrusted_schemes_ports_and_userinfo_blocked(url):
    with pytest.raises(DownloadError):public_target(url)


def test_redirect_to_internal_is_revalidated(monkeypatch,tmp_path):
    def lookup(host,*a,**kw):return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1' if host=='localhost' else '8.8.8.8',443))]
    monkeypatch.setattr(socket,'getaddrinfo',lookup)
    monkeypatch.setattr(module.shutil,'disk_usage',lambda p:namedtuple('Disk','total used free')(10**12,0,10**12))
    connections=[]
    class Response:
        status=302
        def getheader(self,name,*args):return 'https://localhost/private'
    class Connection:
        def __init__(self,*args):connections.append(args)
        def request(self,*a,**k):pass
        def getresponse(self):return Response()
        def close(self):pass
    monkeypatch.setattr(module,'PinnedHTTP',Connection)
    with pytest.raises(DownloadError):download_audio('https://example.org/audio',tmp_path/'audio')
    assert len(connections)==1 and connections[0][2]=='8.8.8.8'


def test_analytics_redirect_chains_are_followed_but_bounded(monkeypatch,tmp_path):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))])
    monkeypatch.setattr(module.shutil,'disk_usage',lambda p:namedtuple('Disk','total used free')(10**12,0,10**12))
    def serve(redirects):
        hops=[]
        class Response:
            sent=False
            def __init__(self):self.status=302 if len(hops)<=redirects else 200
            def getheader(self,name,default=None):
                return f'https://hop{len(hops)}.example/a' if name=='Location' else 'audio/mpeg' if name=='Content-Type' else None
            def read(self,size):
                if self.sent:return b''
                self.sent=True;return b'a'*20
        class Connection:
            def __init__(self,*args):pass
            def request(self,*a,**kw):hops.append(a[1])
            def getresponse(self):return Response()
            def close(self):pass
        monkeypatch.setattr(module,'PinnedHTTP',Connection)
        return hops
    # Six hops: tracking prefixes, then the host's CDN redirect, as real feeds do.
    hops=serve(6)
    assert download_audio('https://dts.example/a',tmp_path/'audio') and len(hops)==7
    serve(11)
    with pytest.raises(DownloadError,match='audio_redirect_limit'):download_audio('https://dts.example/a',tmp_path/'audio')
    assert not (tmp_path/'audio').exists()


def test_signed_request_preserved_and_streamed_limit(monkeypatch,tmp_path):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))])
    monkeypatch.setattr(module.shutil,'disk_usage',lambda p:namedtuple('Disk','total used free')(10**12,0,10**12))
    requests=[]
    class Response:
        status=200
        sent=False
        def getheader(self,name,default=None):return 'audio/mpeg' if name=='Content-Type' else None
        def read(self,size):
            if self.sent:return b''
            self.sent=True;return b'a'*20
    class Connection:
        def __init__(self,*args):pass
        def request(self,*a,**kw):requests.append((a,kw))
        def getresponse(self):return Response()
        def close(self):pass
    monkeypatch.setattr(module,'PinnedHTTP',Connection)
    with pytest.raises(DownloadError):download_audio('https://example.org/a?token=a%2Bb&part=1',tmp_path/'audio',max_bytes=10)
    assert requests[0][0][1]=='/a?token=a%2Bb&part=1'
    assert 'Authorization' not in requests[0][1]['headers'] and 'Cookie' not in requests[0][1]['headers']
    assert not (tmp_path/'audio').exists()
