"""Invoked in a killable child process. No network transcription API is used."""
import importlib.metadata
import json
from pathlib import Path
import sys


# Benchmarked on an M4 (10 min of podcast audio): large-v3-turbo-q4 with context
# took 24s with full punctuation; without context turbo drops punctuation and
# casing after the first window. Word timestamps cost ~60% more for no gain here.
# Whisper's compression-ratio and temperature fallbacks guard against context loops.
OPTIONS = {"condition_on_previous_text":True,"word_timestamps":False}


def main():
    import mlx_whisper
    source, output, model = sys.argv[1:]
    result = mlx_whisper.transcribe(source,path_or_hf_repo=model,verbose=False,**OPTIONS)
    payload = {"model":model,"language":result["language"],
        "software":"meritranscripts/0.1.0 mlx-whisper/"+importlib.metadata.version("mlx-whisper"),
        "options":OPTIONS,
        "segments":[{"start":s["start"],"end":s["end"],"text":s["text"].strip()} for s in result["segments"] if s["text"].strip()]}
    Path(output).write_text(json.dumps(payload))
    Path(output).chmod(0o600)


if __name__ == "__main__":
    main()
