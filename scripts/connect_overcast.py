"""Local setup helper: open its QR image and approve on your own iPhone."""
from pathlib import Path
import argparse
import base64
import time
import httpx
from dotenv import load_dotenv
from transcripts.config import Settings


def main():
    p=argparse.ArgumentParser();p.add_argument('--env',default='.env');args=p.parse_args()
    load_dotenv(args.env,override=True)
    settings=Settings.from_env()
    with httpx.Client(base_url=settings.origin,headers={'Authorization':'Bearer '+settings.owner_token},timeout=30,follow_redirects=False) as client:
        for attempt in range(15):
            try:
                response=client.post('/actions/overcast/qr')
                if response.status_code!=200:
                    raise SystemExit('Cannot start Overcast sign-in: '+str(response.json().get('detail','unavailable')))
                break
            except httpx.ConnectError:
                if attempt==14:raise SystemExit('Start the private server before connecting Overcast.')
                time.sleep(1)
        flow=response.json()
        target=Path('var/overcast-sign-in.png');target.parent.mkdir(exist_ok=True,mode=0o700)
        target.write_bytes(base64.b64decode(flow['image'].split(',',1)[1]));target.chmod(0o600)
        print('Waiting for your iPhone approval. Open var/overcast-sign-in.png and scan with the iPhone camera.',flush=True)
        try:
            for _ in range(120):
                time.sleep(5)
                try:
                    check=client.post('/actions/overcast/qr/'+flow['flow_id'])
                except httpx.TransportError:
                    continue
                if check.status_code!=200:
                    print('QR session expired or unavailable; start again.',flush=True)
                    return
                if check.json().get('connected'):
                    print('Overcast connected. Automatic discovery will start within a minute.',flush=True)
                    return
            print('QR expired. Run the helper again for a new code.',flush=True)
        finally:
            target.unlink(missing_ok=True)


if __name__=='__main__':main()
