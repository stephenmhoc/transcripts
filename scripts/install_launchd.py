"""Install only explicitly selected local jobs. Secrets stay in a 0600 env file."""
import argparse
from pathlib import Path
import os
import plistlib
import subprocess
import sys


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('service',choices=['worker','server'])
    parser.add_argument('--env',type=Path,required=True)
    parser.add_argument('--url',help='Worker control server HTTPS origin; loopback HTTP permitted locally')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    env=args.env.resolve()
    if not env.is_file() or env.stat().st_mode&0o077:
        raise SystemExit('Environment file must exist and be mode 0600')
    if args.service=='worker' and not args.url:
        raise SystemExit('--url is required for a worker')
    exe=root/'.venv'/'bin'/'transcripts'
    if not exe.is_file():raise SystemExit('Install this checkout into .venv first')
    label='com.merimerimeri.transcripts.'+args.service
    folder=Path.home()/'Library'/'LaunchAgents';folder.mkdir(parents=True,exist_ok=True)
    target=folder/(label+'.plist')
    if target.exists():raise SystemExit(f'{target} already exists; inspect it before replacing')
    logs=root/'var'/'logs';logs.mkdir(parents=True,exist_ok=True,mode=0o700)
    arguments=[str(exe),'--env',str(env),'serve' if args.service=='server' else 'worker']
    if args.service=='worker':arguments+=['--url',args.url,'--work-dir',str(root/'var'/'worker')]
    if args.service=='server':arguments+=['--host','127.0.0.1','--port','8765']
    values={'Label':label,'ProgramArguments':arguments,'WorkingDirectory':str(root),
        'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':30,'Umask':0o077,
        'EnvironmentVariables':{'PATH':'/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin','PYTHONUNBUFFERED':'1'},
        'StandardOutPath':str(logs/(args.service+'.log')),'StandardErrorPath':str(logs/(args.service+'.err.log'))}
    with target.open('wb') as file:plistlib.dump(values,file)
    target.chmod(0o600)
    subprocess.run(['launchctl','bootstrap',f'gui/{os.getuid()}',str(target)],check=True)
    print(f'Installed {label}. Inspect with launchctl print gui/{os.getuid()}/{label}')


if __name__=='__main__':main()
