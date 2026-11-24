"""A single Mac worker pulls leased work; the homelab never executes shell input."""
from pathlib import Path
from threading import Thread, Event
from urllib.parse import urlsplit
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time

import httpx
from .config import origin_only
from .discovery import file_lock
from .download import download_audio
from .models import Result

log = logging.getLogger(__name__)
DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo-q4"
MAX_SPOOL_BYTES = 100_000_000


def atomic_json(path, content):
    temp = path.with_suffix(".tmp")
    fd = os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    os.fchmod(fd,0o600)
    with os.fdopen(fd,"w") as output:
        json.dump(content,output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp,path)


def submit_spool(client, spool):
    """Retry receipts before claiming more work. Stale attempts are quarantined."""
    for path in sorted(spool.glob("*.pending.json")):
        try:
            item = json.loads(path.read_text())
            item["job_id"],item["result"]
        except (ValueError,KeyError,TypeError):
            path.rename(path.with_name(path.name.replace(".pending.json",".rejected.json")))
            log.warning("An unreadable spooled result was preserved as rejected")
            continue
        response = client.post(f"/api/worker/jobs/{item['job_id']}/complete",json=item["result"])
        if response.is_success:
            receipt = response.json()
            if not receipt.get("receipt"):
                raise RuntimeError("Missing durable receipt")
            path.unlink()
            log.info("Transcript stored with a durable receipt")
        elif response.status_code == 409:
            path.rename(path.with_name(path.name.replace(".pending.json",".stale.json")))
            log.warning("An expired attempt was preserved as a stale result")
        elif response.is_client_error and response.status_code != 429:
            # The server will never accept this body; keep it but stop retrying
            # so one result can't block every later claim.
            path.rename(path.with_name(path.name.replace(".pending.json",".rejected.json")))
            log.warning("The server rejected a result (%s); preserved as rejected",response.status_code)
        else:
            response.raise_for_status()


def run_job(client, job, spool, model, inference_seconds=7200):
    job_id,token = job["id"],job["lease_token"]
    stop, lost = Event(),Event()
    interval = max(1,min(60,job["lease_seconds"]//4))
    last_good = [time.monotonic()]

    def heartbeat():
        while not stop.wait(interval):
            try:
                response = client.post(f"/api/worker/jobs/{job_id}/heartbeat",json={"lease_token":token})
                if response.status_code == 409:
                    lost.set()
                    return
                response.raise_for_status()
                last_good[0] = time.monotonic()
            except httpx.HTTPError:
                if time.monotonic()-last_good[0] > job["lease_seconds"]-interval:
                    lost.set()
                    return
    thread = Thread(target=heartbeat,daemon=True)
    thread.start()
    phase = "download_failed"
    try:
        with tempfile.TemporaryDirectory(prefix="audio-",dir=spool.parent) as temp:
            folder = Path(temp)
            audio,result_path = folder/"audio.bin",folder/"result.json"
            checksum = download_audio(job["audio_url"],audio)
            if lost.is_set():
                return
            phase = "inference_failed"
            # Model comes from local operator configuration, never from a job.
            process = subprocess.Popen([sys.executable,"-m","transcripts.inference",str(audio),str(result_path),model],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            started = time.monotonic()
            try:
                while process.poll() is None:
                    if lost.wait(1) or time.monotonic()-started>inference_seconds:
                        raise RuntimeError("Inference lease lost or timeout")
                if process.returncode != 0:
                    raise RuntimeError("Local inference failed")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            phase = "result_invalid"
            if not json.loads(result_path.read_text()).get("segments"):
                phase = "no_speech"
                raise RuntimeError("No speech detected")
            content = Result.model_validate({**json.loads(result_path.read_text()),"lease_token":token,"audio_sha256":checksum})
            if lost.is_set():
                return
            path = spool / f"{job_id}.pending.json"
            atomic_json(path,{"job_id":job_id,"result":content.model_dump()})
            # Retain audio until receipt where possible. The durable text spool is
            # sufficient to retry upload even if the network stays down.
            submit_spool(client,spool)
    except Exception:
        if not (spool/f"{job_id}.pending.json").exists() and not lost.is_set():
            with contextlib.suppress(httpx.HTTPError):
                client.post(f"/api/worker/jobs/{job_id}/fail",json={"lease_token":token,"code":phase}).raise_for_status()
        log.warning("Worker job interrupted (%s); recovery is durable",phase)
    finally:
        stop.set()
        thread.join(timeout=15)


def run_worker(url, token, work_dir: Path, once=False, model=DEFAULT_MODEL):
    origin_only(url)
    if len(token)<32:
        raise ValueError("WORKER_TOKEN must be a strong separate token")
    if not model or len(model)>256 or any(c in model for c in "\n\r"):
        raise ValueError("Invalid locally configured model")
    work_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    work_dir.chmod(0o700)
    spool = work_dir/"results"
    spool.mkdir(exist_ok=True,mode=0o700)
    with file_lock(work_dir/"worker.lock"), httpx.Client(base_url=url,headers={"Authorization":"Bearer "+token},timeout=60,follow_redirects=False,trust_env=False) as client:
        # A prior killed worker can leave audio. Only our isolated work directory
        # is cleaned, and only after obtaining the process-wide worker lock.
        for old in work_dir.glob("audio-*"):
            if old.is_dir():
                shutil.rmtree(old)
        while True:
            try:
                for stale in [*spool.glob("*.stale.json"),*spool.glob("*.rejected.json")]:
                    if time.time()-stale.stat().st_mtime>7*86400:
                        stale.unlink()
                total = sum(p.stat().st_size for p in spool.iterdir() if p.is_file())
                if total>MAX_SPOOL_BYTES:
                    raise RuntimeError("Worker result spool is full; inspect it before resuming")
                submit_spool(client,spool)
                response = client.post("/api/worker/claim")
                response.raise_for_status()
                job = response.json().get("job")
                if job:
                    log.info("Claimed one local transcription job")
                    run_job(client,job,spool,model)
                elif not once:
                    time.sleep(30)
            except httpx.HTTPError:
                log.warning("Transcript server unavailable; will retry")
                if not once:
                    time.sleep(30)
            except Exception:
                if once:
                    raise
                log.exception("Worker loop error; will retry")
                time.sleep(30)
            if once:
                return
