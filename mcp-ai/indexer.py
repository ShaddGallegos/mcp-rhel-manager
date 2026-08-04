#!/usr/bin/env python3
"""MCP AI system indexer: discover logs, packages, services, binaries and libraries/plugins.
Writes a single JSONL record to the training outdir for ingestion.
"""
import os, sys, json, argparse, socket, datetime, hashlib, subprocess, shutil, pwd, grp, stat, platform

def safe_run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        return p.stdout.strip()
    except Exception:
        return ""

def get_hostname():
    try:
        return socket.gethostname()
    except Exception:
        return ""

def read_os_release():
    info = {}
    try:
        with open('/etc/os-release') as f:
            for line in f:
                line=line.strip()
                if '=' in line:
                    k,v=line.split('=',1)
                    info[k]=v.strip().strip('"')
    except Exception:
        pass
    return info

def find_logs(paths, max_files=2000, max_depth=4):
    logs=[]
    seen=0
    for root in paths:
        if not os.path.exists(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath[len(root):].count(os.sep) if len(dirpath) > len(root) else 0
            if depth>max_depth:
                dirnames[:] = []
            for fn in filenames:
                if seen>=max_files:
                    break
                lower=fn.lower()
                if lower.endswith('.log') or any(k in lower for k in ('messages','secure','syslog','dmesg','boot')) or 'journal' in dirpath:
                    path=os.path.join(dirpath,fn)
                    try:
                        st=os.stat(path)
                        logs.append({'path':path,'size':st.st_size,'mtime':int(st.st_mtime),'uid':st.st_uid})
                        seen+=1
                    except Exception:
                        continue
            if seen>=max_files:
                break
    return logs

def list_rpm():
    if shutil.which('rpm'):
        out = safe_run(['rpm','-qa','--qf','%{NAME} %{VERSION}-%{RELEASE}\\n'])
        pkgs=[]
        for line in out.splitlines():
            parts=line.strip().split()
            if not parts: continue
            name=parts[0]
            version=' '.join(parts[1:]) if len(parts)>1 else ''
            pkgs.append({'name':name,'version':version})
        return pkgs
    return []

def list_pip():
    out = safe_run([sys.executable,'-m','pip','list','--format','json'])
    try:
        arr=json.loads(out)
        return [{'name':p.get('name'),'version':p.get('version')} for p in arr]
    except Exception:
        return []

def list_services():
    services=[]
    if shutil.which('systemctl'):
        out = safe_run(['systemctl','list-unit-files','--type=service','--no-pager','--no-legend'])
        for line in out.splitlines():
            line=line.strip()
            if not line: continue
            parts=line.split()
            name=parts[0]
            state=parts[1] if len(parts)>1 else ''
            services.append({'name':name,'state':state})
    return services

def detect_binaries(names):
    found=[]
    for name in names:
        p = shutil.which(name)
        if not p: continue
        ver=None
        for flag in ['--version','-v','-V','version']:
            out = safe_run([p,flag], timeout=5)
            if out:
                ver = out.splitlines()[0].strip()
                break
        found.append({'name':name,'path':p,'version':ver})
    return found

def find_libraries(root_paths, max_results=500):
    libs=[]
    for root in root_paths:
        if not os.path.exists(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            base=os.path.basename(dirpath).lower()
            if base in ('plugins','modules','extensions','lib','libs','lib64','site-packages'):
                try:
                    entries = len(os.listdir(dirpath))
                except Exception:
                    entries = None
                libs.append({'path':dirpath,'entries':entries})
            if len(libs)>=max_results:
                break
        if len(libs)>=max_results:
            break
    return libs

def compute_sha256(obj):
    s=json.dumps(obj,sort_keys=True,separators=(',',':')).encode('utf-8')
    return hashlib.sha256(s).hexdigest()

def platform_uname():
    try:
        u = platform.uname()
        return {'system':u.system,'node':u.node,'release':u.release,'version':u.version,'machine':u.machine,'processor':u.processor}
    except Exception:
        return {}

def main():
    parser=argparse.ArgumentParser(description="MCP AI system indexer")
    parser.add_argument('--outdir',default='/var/lib/mcp/training')
    parser.add_argument('--prefix',default='system-index')
    parser.add_argument('--limit-logs',type=int,default=2000)
    parser.add_argument('--max-depth',type=int,default=4)
    args=parser.parse_args()

    outdir = os.path.abspath(args.outdir)
    os.makedirs(outdir, exist_ok=True)

    record = {
        'type':'system_index',
        'meta': {
            'hostname': get_hostname(),
            'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat()+'Z',
            'os_release': read_os_release(),
            'uname': platform_uname()
        },
        'data': {}
    }

    # Logs and filesystem artifacts (lightweight)
    record['data']['logs'] = find_logs(['/var/log','/var/lib/mcp','/opt','/usr','/home'], max_files=args.limit_logs, max_depth=args.max_depth)

    # Packages and language-specific libs
    record['data']['packages_rpm'] = list_rpm()
    record['data']['pip_for_python'] = list_pip()

    # Services and binaries
    record['data']['services'] = list_services()
    record['data']['binaries'] = detect_binaries(['python3','java','node','npm','podman','docker','ansible','sshd'])

    # Libraries and plugin folders
    record['data']['libraries'] = find_libraries(['/usr/lib','/usr/lib64','/opt','/var/lib/mcp','/usr/local/lib'], max_results=300)

    # compute a stable sha for dedupe
    record['_sha256'] = compute_sha256(record)

    fname = os.path.join(outdir, f"{args.prefix}-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl")
    tmp = fname + '.tmp'
    try:
        with open(tmp,'w') as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        os.replace(tmp, fname)
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        print('ERROR: could not write index file:', e, file=sys.stderr)
        sys.exit(2)

    # attempt to set ownership to mcp-ai if available
    try:
        uid = pwd.getpwnam('mcp-ai').pw_uid
        gid = grp.getgrnam('mcp-ai').gr_gid
        os.chown(fname, uid, gid)
    except Exception:
        pass

    print('Wrote system index to:', fname)
    print('counts: logs=%d packages=%d pip=%d services=%d binaries=%d libraries=%d' % (
        len(record['data'].get('logs',[])),
        len(record['data'].get('packages_rpm',[])),
        len(record['data'].get('pip_for_python',[])),
        len(record['data'].get('services',[])),
        len(record['data'].get('binaries',[])),
        len(record['data'].get('libraries',[])),
    ))

if __name__ == '__main__':
    main()
